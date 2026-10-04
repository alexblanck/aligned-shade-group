"""The aligned cover group entity."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button.const import SERVICE_PRESS
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.components.cover import (
    DOMAIN as COVER_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_TYPE,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .alignment import (
    AlignmentGroup,
    Direction,
    Move,
    Plan,
    ShadeConfig,
    matched_roll_group,
)
from .const import (
    CONF_CONTROLS,
    CONF_PICO_CLOSE,
    CONF_PICO_OPEN,
    CONF_PICO_STOP,
    CONF_SHADES,
    CONTROL_PICO,
    DOMAIN,
)
from .pico import PicoButtons

_LOGGER = logging.getLogger(__name__)

ATTR_HEMLINE_HEIGHTS = "hemline_heights"


@dataclass(frozen=True)
class _RunningMove:
    """A move being carried out, used to estimate where its shade is."""

    move: Move
    start: datetime
    # The Pico's endpoint when a Pico press starts the shade.
    pico_endpoint_pct: int | None = None

    def expected_reports(self) -> set[int]:
        """Positions the shade may report while carrying out this move."""
        expected = {self.move.from_pct, self.move.target_pct}
        if self.pico_endpoint_pct is not None:
            expected.add(self.pico_endpoint_pct)
        return expected

    def estimate_position(self, now: datetime) -> int:
        move = self.move
        elapsed_s = max(0.0, (now - self.start).total_seconds())
        moved_pct = elapsed_s / move.shade.travel_time_s * 100
        if move.target_pct > move.from_pct:
            return round(min(move.target_pct, move.from_pct + moved_pct))
        return round(max(move.target_pct, move.from_pct - moved_pct))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the aligned cover group entity."""
    options = entry.options
    group = matched_roll_group(ShadeConfig(**shade) for shade in options[CONF_SHADES])
    # Only a Pico paired to every shade is used so far.
    entity_ids = {shade.entity_id for shade in group.shades}
    pico = next(
        (
            PicoButtons(
                open=control[CONF_PICO_OPEN],
                stop=control[CONF_PICO_STOP],
                close=control[CONF_PICO_CLOSE],
            )
            for control in options[CONF_CONTROLS]
            if control[CONF_TYPE] == CONTROL_PICO
            and set(control[CONF_SHADES]) == entity_ids
        ),
        None,
    )
    entity = AlignedCoverGroup(entry=entry, group=group, pico=pico)
    entry.runtime_data = entity  # for diagnostics
    async_add_entities([entity])


class AlignedCoverGroup(CoverEntity):
    """A cover group that keeps its shades' hemlines aligned."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = None  # the group's device name is the entity's name
    _attr_device_class = CoverDeviceClass.SHADE
    # Derived from the shades' own recorded states; no need to store them too.
    _unrecorded_attributes = frozenset({ATTR_ENTITY_ID, ATTR_HEMLINE_HEIGHTS})
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        entry: ConfigEntry,
        group: AlignmentGroup,
        pico: PicoButtons | None,
    ) -> None:
        """Initialize the group."""
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            # Explicitly None, to clear the service type earlier versions set.
            entry_type=None,
            model="Aligned cover group",
        )
        self._group = group
        self._pico = pico
        self._entity_ids = [shade.entity_id for shade in group.shades]
        self._positions_pct_by_id: dict[str, int] = {}
        # Set while a plan is (believed to be) running. Tracked from the plan
        # itself: Caseta shades don't report opening/closing.
        self._moving = False
        self._direction: Direction | None = None
        self._timers: list[CALLBACK_TYPE] = []
        # Moves of the running plan, keyed by entity id.
        self._running_moves: dict[str, _RunningMove] = {}
        # Bumped whenever a plan is abandoned, so a failing command can tell
        # whether its plan is still the current one.
        self._generation = 0

    async def async_added_to_hass(self) -> None:
        """Track the member shades."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._entity_ids, self._async_member_changed
            )
        )
        self.async_on_remove(self._abandon_plan)
        self._update_positions()

    @callback
    def _async_member_changed(self, event: Event[EventStateChangedData]) -> None:
        self._update_positions()
        if self._moving:
            self._check_for_outside_command(event.data["entity_id"])
        self.async_write_ha_state()

    @callback
    def _check_for_outside_command(self, entity_id: str) -> None:
        """Stop following the plan if a shade reports a position we didn't send.

        Caseta shades report their destination as soon as they're commanded,
        so any other position means another command (a physical Pico, another
        automation) has taken over.
        """
        running_move = self._running_moves.get(entity_id)
        position_pct = self._positions_pct_by_id.get(entity_id)
        if running_move is None or position_pct is None:
            return
        if position_pct in running_move.expected_reports():
            return
        _LOGGER.info(
            "%s: %s reported %s%%, which isn't part of the running plan; "
            "another command took over, so the group stops following its plan",
            self.entity_id,
            entity_id,
            position_pct,
        )
        self._abandon_plan()

    @callback
    def _update_positions(self) -> None:
        self._positions_pct_by_id = {
            entity_id: position_pct
            for entity_id in self._entity_ids
            if (position_pct := self._current_shade_position(entity_id)) is not None
        }

    def _current_shade_position(self, entity_id: str) -> int | None:
        if (state := self.hass.states.get(entity_id)) is None:
            return None
        position_pct = state.attributes.get(ATTR_CURRENT_POSITION)
        return None if position_pct is None else int(position_pct)

    @property
    def available(self) -> bool:
        """Available while any shade reports a position."""
        return bool(self._positions_pct_by_id)

    @property
    def current_cover_position(self) -> int | None:
        """Group position derived from the shades' hemlines."""
        return self._group.current_position(self._displayed_positions())

    @property
    def is_closed(self) -> bool | None:
        """Closed when every shade with a known position is closed."""
        if not self._positions_pct_by_id:
            return None
        return all(pct <= 0 for pct in self._displayed_positions().values())

    @property
    def is_opening(self) -> bool:
        """Whether a planned motion is opening the group."""
        return self._moving and self._direction is Direction.OPENING

    @property
    def is_closing(self) -> bool:
        """Whether a planned motion is closing the group."""
        return self._moving and self._direction is Direction.CLOSING

    @callback
    def diagnostics(self) -> dict[str, Any]:
        """Settings as the group uses them, the shades' state, and any move."""
        now = dt_util.utcnow()
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "current_position": self.current_cover_position,
            "aligned": self.extra_state_attributes["aligned"],
            "pico": asdict(self._pico) if self._pico else None,
            "roll_profile": asdict(self._group.view.profile),
            "group_profile_range_pct": [
                self._group.view.closed_pct,
                self._group.view.open_pct,
            ],
            "shades": [
                {
                    "entity_id": shade.entity_id,
                    "closed_height": shade.closed_height,
                    "open_height": shade.open_height,
                    "travel_time_s": shade.travel_time_s,
                    "profile_range_pct": [
                        shade.view.closed_pct,
                        shade.view.open_pct,
                    ],
                    "reported_position_pct": (
                        position_pct := self._positions_pct_by_id.get(shade.entity_id)
                    ),
                    "hemline_height": (
                        None
                        if position_pct is None
                        else shade.height_for_position(position_pct)
                    ),
                }
                for shade in self._group.shades
            ],
            "moving": self._moving,
            "direction": self._direction,
            "moves": [
                {
                    "entity_id": entity_id,
                    "from_pct": running_move.move.from_pct,
                    "target_pct": running_move.move.target_pct,
                    "delay_s": running_move.move.delay_s,
                    "needs_command": running_move.move.needs_command,
                    "start": running_move.start.isoformat(),
                    "estimated_pct": running_move.estimate_position(now),
                }
                for entity_id, running_move in self._running_moves.items()
            ],
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose members and alignment.

        Members go in `entity_id` rather than HA's `group_entities`: that comes
        from setting `self.group`, which makes HA send service calls straight
        to the members, bypassing alignment and the Pico.
        """
        positions_pct_by_id = self._displayed_positions()
        return {
            ATTR_ENTITY_ID: self._entity_ids,
            "aligned": (
                self._group.common_hemline_height(positions_pct_by_id) is not None
            ),
            ATTR_HEMLINE_HEIGHTS: {
                shade.entity_id: round(
                    shade.height_for_position(positions_pct_by_id[shade.entity_id]),
                    1,
                )
                for shade in self._group.shades
                if shade.entity_id in positions_pct_by_id
            },
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open every shade."""
        await self._async_set_group_position(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close every shade."""
        await self._async_set_group_position(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the shared hemline to a group position."""
        await self._async_set_group_position(kwargs[ATTR_POSITION])

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop every shade at once."""
        was_moving = self._moving
        self._abandon_plan()
        self.async_write_ha_state()
        # A shade Pico's middle button goes to the favorite position when the
        # shades are stationary, so only press it while we think they're moving.
        if self._pico and was_moving:
            await self._async_press(self._pico.stop)
        else:
            took_s = await self._async_call(
                COVER_DOMAIN, SERVICE_STOP_COVER, self._entity_ids
            )
            _LOGGER.debug(
                "%s: stopped each shade (%s) in %.3fs",
                self.entity_id,
                "no Pico"
                if not self._pico
                else "not moving, Pico would go to favorite",
                took_s,
            )

    async def _async_set_group_position(self, target_pct: int) -> None:
        """Plan the moves to `target_pct` and run the plan."""
        positions_pct_by_id = self._planning_positions()
        positions_source = "estimated" if self._moving else "reported"
        moving_entity_ids = set(self._running_moves) if self._moving else set()
        if missing := [e for e in self._entity_ids if e not in positions_pct_by_id]:
            _LOGGER.warning(
                "%s: leaving out shades with no position: %s",
                self.entity_id,
                ", ".join(missing),
            )
        self._abandon_plan()
        plan = self._group.plan_moves(
            positions_pct_by_id,
            target_pct=target_pct,
            pico_available=self._pico is not None,
            moving_entity_ids=moving_entity_ids,
        )
        direction = self._group_direction(positions_pct_by_id, target_pct)
        _LOGGER.debug(
            "%s: planned %s%% (hemline height %.1f) from %s positions %s -> "
            "Pico %s%s, moves %s, %.1fs",
            self.entity_id,
            target_pct,
            self._group.height_for_position(target_pct),
            positions_source,
            positions_pct_by_id,
            plan.pico,
            f" ({plan.pico_blocker})" if plan.pico_blocker else "",
            [
                f"{m.shade.entity_id}->{m.target_pct}%@{m.delay_s:.1f}s"
                + ("" if m.needs_command else " (Pico)")
                for m in plan.moves
            ],
            plan.duration_s(),
        )
        if plan.moves:
            await self._async_run_plan(plan, direction)
        else:
            self.async_write_ha_state()  # a previous plan may have just been abandoned

    @callback
    def _displayed_positions(self) -> dict[str, int]:
        """Shade positions the group's state is reported from.

        While a plan runs, each moving shade's target, so the group reports
        where it's heading, like Caseta shades themselves (they report their
        destination as soon as they're commanded). Reporting progress instead
        would make dashboard sliders, which always show the reported position,
        jump back from where they were dropped. Otherwise, what the shades
        reported.
        """
        return {
            entity_id: (
                self._running_moves[entity_id].move.target_pct
                if entity_id in self._running_moves
                else position_pct
            )
            for entity_id, position_pct in self._positions_pct_by_id.items()
        }

    @callback
    def _planning_positions(self) -> dict[str, int]:
        """Where the shades are now, as far as the group can tell.

        Caseta shades report their destination as soon as they're commanded,
        so while a plan runs, its shades' positions are estimated from their
        moves; otherwise they're what the shades reported.
        """
        if not self._moving:
            return dict(self._positions_pct_by_id)
        now = dt_util.utcnow()
        return {
            entity_id: (
                self._running_moves[entity_id].estimate_position(now)
                if entity_id in self._running_moves
                else position_pct
            )
            for entity_id, position_pct in self._positions_pct_by_id.items()
        }

    def _group_direction(
        self, positions_pct_by_id: dict[str, int], target_pct: int
    ) -> Direction | None:
        """Which way the group's own position moves, whatever each shade does."""
        current_pct = self._group.current_position(positions_pct_by_id)
        if current_pct is None or target_pct == current_pct:
            return None
        return Direction.OPENING if target_pct > current_pct else Direction.CLOSING

    async def _async_run_plan(self, plan: Plan, direction: Direction | None) -> None:
        self._moving = True
        self._direction = direction
        started = dt_util.utcnow()
        pico_endpoint_pct = None
        if plan.pico is not None:
            pico_endpoint_pct = 100 if plan.pico is Direction.OPENING else 0
        self._running_moves = {
            move.shade.entity_id: _RunningMove(
                move=move,
                start=started + timedelta(seconds=move.delay_s),
                pico_endpoint_pct=pico_endpoint_pct,
            )
            for move in plan.moves
        }
        # Timers are set before sending any command, so slow commands below
        # don't delay them.
        for running_move in self._running_moves.values():
            if running_move.move.needs_command and running_move.move.delay_s > 0:
                self._timers.append(
                    async_call_later(
                        self.hass,
                        running_move.move.delay_s,
                        partial(self._async_delayed_start, running_move),
                    )
                )
        self._timers.append(
            async_call_later(self.hass, plan.duration_s(), self._async_plan_done)
        )
        self.async_write_ha_state()

        generation = self._generation
        try:
            if plan.pico is not None and self._pico:
                await self._async_press(self._pico.toward(plan.pico))
            await self._async_set_positions(
                move for move in plan.moves if move.needs_command and move.delay_s <= 0
            )
        except HomeAssistantError as err:
            if generation == self._generation:
                _LOGGER.debug(
                    "%s: plan abandoned: starting command failed: %s",
                    self.entity_id,
                    err,
                )
                self._abandon_plan()
                self.async_write_ha_state()
            raise

    async def _async_delayed_start(
        self, running_move: _RunningMove, _now: datetime
    ) -> None:
        move = running_move.move
        _LOGGER.debug(
            "%s: starting %s -> %s%% (timer %+.3fs from schedule)",
            self.entity_id,
            move.shade.entity_id,
            move.target_pct,
            (dt_util.utcnow() - running_move.start).total_seconds(),
        )
        try:
            await self._async_set_positions([move])
        except HomeAssistantError as err:
            _LOGGER.error(
                "%s: couldn't start %s: %s", self.entity_id, move.shade.entity_id, err
            )

    @callback
    def _async_plan_done(self, _now: datetime) -> None:
        # Caseta shades report their destination immediately, so the plan's
        # timing is the only sign they've stopped. Ending late is worse than
        # early: a Pico stop on stationary shades sends them to favorite.
        _LOGGER.debug("%s: plan finished: planned travel time elapsed", self.entity_id)
        self._abandon_plan()
        self.async_write_ha_state()

    @callback
    def _abandon_plan(self) -> None:
        """Stop following the current plan.

        Cancels starts that haven't happened yet and forgets the position
        estimates. Shades that are already moving keep moving.
        """
        self._generation += 1
        for cancel in self._timers:
            cancel()
        self._timers.clear()
        self._running_moves.clear()
        self._moving = False
        self._direction = None

    async def _async_set_positions(self, moves: Iterable[Move]) -> None:
        """Send the moves' commands at once and log how long each took."""
        moves = list(moves)
        if not moves:
            return
        took_s = await asyncio.gather(
            *(
                self._async_call(
                    COVER_DOMAIN,
                    SERVICE_SET_COVER_POSITION,
                    move.shade.entity_id,
                    {ATTR_POSITION: move.target_pct},
                )
                for move in moves
            )
        )
        _LOGGER.debug(
            "%s: set positions in %.3fs: %s",
            self.entity_id,
            max(took_s),
            {
                move.shade.entity_id: f"{move.target_pct}% in {seconds:.3f}s"
                for move, seconds in zip(moves, took_s, strict=True)
            },
        )

    async def _async_press(self, button_entity_id: str) -> None:
        took_s = await self._async_call(BUTTON_DOMAIN, SERVICE_PRESS, button_entity_id)
        _LOGGER.debug(
            "%s: pressed Pico %s in %.3fs", self.entity_id, button_entity_id, took_s
        )

    async def _async_call(
        self,
        domain: str,
        service: str,
        entity_id: str | list[str],
        data: dict[str, Any] | None = None,
    ) -> float:
        """Call a service and return how long it took, in seconds."""
        started = time.monotonic()
        await self.hass.services.async_call(
            domain,
            service,
            {ATTR_ENTITY_ID: entity_id, **(data or {})},
            blocking=True,
            context=self._context,
        )
        return time.monotonic() - started
