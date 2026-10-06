"""The aligned shade group entity."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from types import MappingProxyType
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
    CONF_ENTITY_ID,
    CONF_TYPE,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .alignment import (
    AlignmentGroup,
    Direction,
    Move,
    Plan,
    ShadeConfig,
    Starter,
    matched_roll_group,
)
from .const import (
    CONF_CONTROLS,
    CONF_PICO_CLOSE,
    CONF_PICO_OPEN,
    CONF_PICO_STOP,
    CONF_SCENE_POSITIONS,
    CONF_SHADES,
    DOMAIN,
    ControlType,
    missing_entities_issue_id,
)
from .pico import PicoButtons

_LOGGER = logging.getLogger(__name__)

ATTR_HEMLINE_HEIGHTS = "hemline_heights"


@dataclass(frozen=True)
class _RunningMove:
    """A move being carried out, used to estimate where its shade is."""

    move: Move
    start: datetime
    # Where its starter (a Pico or scene) sends it, if one starts it.
    starter_pct: int | None = None
    # The span of its last move, when a Pico's Stop stopped it partway there
    # first: it reports wherever it stopped, which is only estimated.
    stopped_within: tuple[int, int] | None = None

    def expects_report(self, position_pct: int) -> bool:
        """Whether the shade may report `position_pct` during this move."""
        if position_pct in (self.move.from_pct, self.move.target_pct):
            return True
        if position_pct == self.starter_pct:
            return True
        if self.stopped_within is None:
            return False
        low, high = sorted(self.stopped_within)
        return low <= position_pct <= high

    def travelling(self, now: datetime) -> bool:
        """Whether the shade is estimated to be moving at `now`: started, not
        yet arrived, and not a hold.
        """
        move = self.move
        travel_s = abs(move.target_pct - move.from_pct) / 100 * move.shade.travel_time_s
        return self.start <= now < self.start + timedelta(seconds=travel_s)

    def estimate_position(self, now: datetime) -> int:
        move = self.move
        elapsed_s = max(0.0, (now - self.start).total_seconds())
        moved_pct = elapsed_s / move.shade.travel_time_s * 100
        if move.target_pct > move.from_pct:
            return round(min(move.target_pct, move.from_pct + moved_pct))
        return round(max(move.target_pct, move.from_pct - moved_pct))


@dataclass(frozen=True)
class _Pico:
    """A Pico paired to some (or all) of the group's shades."""

    buttons: PicoButtons
    shades: tuple[str, ...]

    def entities(self) -> tuple[str, ...]:
        """Entities it needs, to check they're there."""
        return self.buttons.buttons()

    def starters(self) -> list[Starter]:
        """Its On button sends its shades to 100%, its Off button to 0%."""
        return [
            Starter(name=self.buttons.open, targets=dict.fromkeys(self.shades, 100)),
            Starter(name=self.buttons.close, targets=dict.fromkeys(self.shades, 0)),
        ]


@dataclass(frozen=True)
class _Scene:
    """A Lutron scene that sets some of the group's shades to positions."""

    entity_id: str
    positions: Mapping[str, int]

    def entities(self) -> tuple[str, ...]:
        """Entities it needs, to check they're there."""
        return (self.entity_id,)

    def starters(self) -> list[Starter]:
        """Activating it sends each of its shades to its position."""
        return [Starter(name=self.entity_id, targets=self.positions)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the aligned shade group entity."""
    data = entry.data
    group = matched_roll_group(ShadeConfig(**shade) for shade in data[CONF_SHADES])
    controls: list[_Pico | _Scene] = []
    for control in data[CONF_CONTROLS]:
        if control[CONF_TYPE] == ControlType.PICO:
            buttons = PicoButtons(
                open=control[CONF_PICO_OPEN],
                stop=control[CONF_PICO_STOP],
                close=control[CONF_PICO_CLOSE],
            )
            controls.append(_Pico(buttons=buttons, shades=tuple(control[CONF_SHADES])))
        elif control[CONF_TYPE] == ControlType.SCENE:
            controls.append(
                _Scene(
                    entity_id=control[CONF_ENTITY_ID],
                    positions=control[CONF_SCENE_POSITIONS],
                )
            )
    entity = AlignedShadeGroup(entry=entry, group=group, controls=controls)
    entry.runtime_data = entity  # for diagnostics
    async_add_entities([entity])


class AlignedShadeGroup(CoverEntity):
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
        controls: list[_Pico | _Scene],
    ) -> None:
        """Initialize the group."""
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            # Explicitly None, to clear the service type earlier versions set.
            entry_type=None,
            model="Aligned shade group",
        )
        self._group = group
        self._entry = entry
        self._controls = controls
        self._entity_ids = [shade.entity_id for shade in group.shades]
        self._positions_pct_by_id: dict[str, int] = {}
        # Set while a plan is (believed to be) running. Tracked from the plan
        # itself: Caseta shades don't report opening/closing.
        self._moving = False
        self._direction: Direction | None = None
        self._timers: list[CALLBACK_TYPE] = []
        # When the running plan started; its delays count from here.
        self._started = dt_util.utcnow()
        # Numbers each plan, so its log lines ("plan #N", "run #N") go together.
        self._plan_number = 0
        # Counts since Home Assistant started, for diagnostics.
        self._counting_since = dt_util.utcnow()
        self._runs_started = 0
        self._runs_with_starters = 0
        self._run_endings: Counter[str] = Counter()
        self._service_calls: defaultdict[str, Counter[str]] = defaultdict(Counter)
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
        self.async_on_remove(partial(self._abandon_plan, "removed"))
        self._update_positions()
        # After startup, so shades without a registry entry have a state.
        self.async_on_remove(async_at_started(self.hass, self._async_check_missing))
        # And whenever a shade or Pico button is renamed, removed or disabled.
        self.async_on_remove(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED,
                self._async_check_missing,
                event_filter=self._concerns_this_group,
            )
        )

    @callback
    def _concerns_this_group(self, data: er.EventEntityRegistryUpdatedData) -> bool:
        # Both ids, so renaming one back to its original id counts too.
        watched = {*self._entity_ids, *self._control_entities()}
        return data["entity_id"] in watched or data.get("old_entity_id") in watched

    @callback
    def _async_check_missing(self, _hass_or_event: object) -> None:
        """Recheck for missing entities, once started or after a registry change.

        Called with Home Assistant (at startup) or a registry event; neither
        is needed.
        """
        self._async_update_missing_issue()

    @property
    def _run(self) -> str:
        """The log tag for running the current plan."""
        return f"run #{self._plan_number}"

    @callback
    def _async_member_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        before_pct = self._positions_pct_by_id.get(entity_id)
        self._update_positions()
        if self._moving:
            position_pct = self._positions_pct_by_id.get(entity_id)
            if position_pct != before_pct:
                _LOGGER.debug(
                    "%s: %s: %s reported %s%%",
                    self.entity_id,
                    self._run,
                    entity_id,
                    position_pct,
                )
            self._check_for_outside_command(entity_id)
        self.async_write_ha_state()

    @callback
    def _check_for_outside_command(self, entity_id: str) -> None:
        """Abandon the plan if a shade reports a position we didn't send.

        Caseta shades report their destination as soon as they're commanded,
        so any other position means another command (a physical Pico, another
        automation) has taken over.
        """
        running_move = self._running_moves.get(entity_id)
        position_pct = self._positions_pct_by_id.get(entity_id)
        if running_move is None or position_pct is None:
            return
        if running_move.expects_report(position_pct):
            return
        _LOGGER.info(
            "%s: %s: abandoned: %s reported %s%%, which isn't part of the run, "
            "so another command took over; shades keep going where they were "
            "sent, and starts still to come are cancelled",
            self.entity_id,
            self._run,
            entity_id,
            position_pct,
        )
        self._abandon_plan("outside_command")

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
            "controls": [
                {"type": ControlType.PICO, "shades": list(control.shades)}
                | asdict(control.buttons)
                if isinstance(control, _Pico)
                else {
                    "type": ControlType.SCENE,
                    "entity_id": control.entity_id,
                    "positions": dict(control.positions),
                }
                for control in self._controls
            ],
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
                    "starter": (
                        running_move.move.starter.name
                        if running_move.move.starter
                        else None
                    ),
                    "needs_command": running_move.move.needs_command,
                    "start": running_move.start.isoformat(),
                    "estimated_pct": running_move.estimate_position(now),
                }
                for entity_id, running_move in self._running_moves.items()
            ],
            "counts": {
                "since": self._counting_since.isoformat(),
                "runs": {
                    "started": self._runs_started,
                    "with_pico_or_scene": self._runs_with_starters,
                    "ended": dict(self._run_endings),
                },
                "service_calls": {
                    entity_id: dict(calls)
                    for entity_id, calls in self._service_calls.items()
                },
            },
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose members and alignment.

        Members go in `entity_id`, as in Home Assistant's own groups, so the
        UI lists the member shades (for example in the more-info dialog).
        Not in HA's `group_entities`: that comes from setting `self.group`,
        which makes HA send service calls straight to the members, bypassing
        alignment and the Pico.

        Like the group's position, `aligned` and `hemline_heights` are worked
        out from where shades are heading while a plan runs.
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

    def _missing(self, entity_ids: Iterable[str]) -> list[str]:
        """Entities that are disabled, or have neither a registry entry nor a
        state (say, renamed or removed).
        """
        registry = er.async_get(self.hass)
        missing = []
        for entity_id in entity_ids:
            entry = registry.async_get(entity_id)
            if entry is None:
                if self.hass.states.get(entity_id) is None:
                    missing.append(entity_id)
            elif entry.disabled:
                missing.append(entity_id)
        return missing

    @callback
    def _async_update_missing_issue(self) -> list[str]:
        """Raise or clear the repair issue for missing shades and controls.

        Fixing them means reconfiguring the group. Returns them.
        """
        missing = self._missing([*self._entity_ids, *self._control_entities()])
        issue_id = missing_entities_issue_id(self._entry.entry_id)
        if not missing:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return []
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="entities_missing",
            translation_placeholders={
                "name": self._entry.title,
                # A Markdown list, one entity per line.
                "entities": "\n".join(f"- {entity_id}" for entity_id in missing),
            },
        )
        return missing

    def _control_entities(self) -> list[str]:
        return [entity for control in self._controls for entity in control.entities()]

    @callback
    def _async_usable_controls(self) -> list[_Pico | _Scene]:
        """Controls whose entities can be used right now.

        Pressing a button that doesn't exist does nothing and raises nothing,
        so without this check the group would wait for shades that never
        started. Shades without a usable control are commanded one by one.
        """
        usable = []
        for control in self._controls:
            if missing := self._missing(control.entities()):
                _LOGGER.warning(
                    "%s: not using %s, which is missing or disabled; "
                    "reconfigure the group to choose it again",
                    self.entity_id,
                    ", ".join(missing),
                )
                continue
            unavailable = [
                entity_id
                for entity_id in control.entities()
                if (state := self.hass.states.get(entity_id)) is None
                or state.state == STATE_UNAVAILABLE
            ]
            if unavailable:
                _LOGGER.warning(
                    "%s: not using %s, which is unavailable",
                    self.entity_id,
                    ", ".join(unavailable),
                )
                continue
            usable.append(control)
        return usable

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
        """Stop every shade: with Picos where it's safe, else one by one."""
        travelling = self._travelling_moves().keys()
        self._abandon_plan("stopped")
        self.async_write_ha_state()
        # Every shade gets a stop, since others may be moving that the plan
        # doesn't know about. A shade Pico's middle button stops its shades
        # while any of them is moving, but sends them to their favorite
        # position when all are still, so only Picos covering a shade the plan
        # has travelling right now (not waiting to start, nor arrived) are
        # pressed; every other shade gets its own stop.
        picos = [
            control
            for control in self._async_usable_controls()
            if isinstance(control, _Pico) and travelling & set(control.shades)
        ]
        # Each button once: a second press would find the shades still and
        # send them to favorite.
        stop_buttons = dict.fromkeys(pico.buttons.stop for pico in picos)
        await asyncio.gather(
            *(self._async_press(button, "stop") for button in stop_buttons)
        )
        covered = {shade for pico in picos for shade in pico.shades}
        if rest := [e for e in self._entity_ids if e not in covered]:
            took_s = await self._async_call(COVER_DOMAIN, SERVICE_STOP_COVER, rest)
            _LOGGER.debug(
                "%s: stop: stopped %s one by one (%.3fs)",
                self.entity_id,
                ", ".join(rest),
                took_s,
            )

    def _travelling_moves(self) -> dict[str, Move]:
        """The running plan's moves travelling right now (started, and not yet
        arrived), by shade.
        """
        now = dt_util.utcnow()
        return {
            entity_id: running_move.move
            for entity_id, running_move in self._running_moves.items()
            if self._moving and running_move.travelling(now)
        }

    async def _async_set_group_position(self, target_pct: int) -> None:
        """Plan the moves to `target_pct` and run the plan."""
        positions_pct_by_id = self._planning_positions()
        replaced_run = self._run if self._moving else None
        travelling = self._travelling_moves()
        self._async_update_missing_issue()
        if missing := [e for e in self._entity_ids if e not in positions_pct_by_id]:
            _LOGGER.warning(
                "%s: leaving out shades with no position: %s",
                self.entity_id,
                ", ".join(missing),
            )
        self._abandon_plan("replaced")
        self._plan_number += 1
        plan = self._group.plan_moves(
            positions_pct_by_id,
            target_pct=target_pct,
            starters=[
                starter
                for control in self._async_usable_controls()
                for starter in control.starters()
            ],
            travelling={
                entity_id: move.direction() for entity_id, move in travelling.items()
            },
        )
        direction = self._group_direction(positions_pct_by_id, target_pct)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            for line in self._describe_plan(plan, target_pct, replaced_run):
                _LOGGER.debug(
                    "%s: plan #%s: %s", self.entity_id, self._plan_number, line
                )
        if plan.moves:
            self._runs_started += 1
            if any(move.starter for move in plan.moves):
                self._runs_with_starters += 1
            await self._async_run_plan(plan, direction, travelling)
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

    def _describe_plan(
        self,
        plan: Plan,
        target_pct: int,
        replaced_run: str | None,
    ) -> list[str]:
        """A plan as log lines: a summary, what starts shades, then each move.

        Logged one line each, tagged "plan #N", so every line can be found by
        grepping for the group, a shade, or the plan.
        """
        summary = (
            f"to {target_pct}% "
            f"(hemline {self._group.height_for_position(target_pct):.1f}), "
            f"{plan.duration_s():.1f}s"
        )
        if replaced_run:
            summary += f", replacing {replaced_run} from estimated positions"
        lines = [summary]
        if not self._controls:
            lines.append("no Picos or scenes set up")
        lines.extend(
            f"starts {name} at {delay_s:.1f}s"
            for name, delay_s in dict.fromkeys(
                (move.starter.name, move.delay_s) for move in plan.moves if move.starter
            )
        )
        for move in plan.moves:
            if move.from_pct == move.target_pct:
                again = (
                    f"{move.starter.name} sends it again at {move.delay_s:.1f}s"
                    if move.starter
                    else "sent it again"
                )
                lines.append(
                    f"{move.shade.entity_id} is already at {move.target_pct}%; {again}"
                )
                continue
            if move.starter is None:
                start = f"at {move.delay_s:.1f}s"
            else:
                start = f"with {move.starter.name} at {move.delay_s:.1f}s"
                if move.needs_command:
                    start += ", then is sent its target"
            lines.append(
                f"{move.shade.entity_id} {move.from_pct}% "
                f"(hemline {move.from_height():.1f}) -> {move.target_pct}%, "
                f"{'held until it starts' if move.held else 'starts'} {start}, "
                f"arrives at {move.arrival_s():.1f}s"
            )
        return lines

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

    async def _async_run_plan(
        self, plan: Plan, direction: Direction | None, travelling: Mapping[str, Move]
    ) -> None:
        """Run `plan`; `travelling` are the last plan's moves still travelling."""
        # A Pico pressed while its shades move stops them, so one turning
        # travelling shades around has its Stop pressed first, by starter.
        stop_first = {
            starter.name: control.buttons.stop
            for control in self._async_usable_controls()
            if isinstance(control, _Pico) and travelling.keys() & set(control.shades)
            for starter in control.starters()
        }
        self._moving = True
        self._direction = direction
        self._started = started = dt_util.utcnow()
        self._running_moves = {
            move.shade.entity_id: _RunningMove(
                move=move,
                start=started + timedelta(seconds=move.delay_s),
                starter_pct=(
                    move.starter.targets[move.shade.entity_id] if move.starter else None
                ),
                stopped_within=(
                    (old.from_pct, old.target_pct)
                    if move.delay_s == 0
                    and move.starter
                    and move.starter.name in stop_first
                    and (old := travelling.get(move.shade.entity_id))
                    else None
                ),
            )
            for move in plan.moves
        }
        # Everything starting at the same moment goes out together: starters
        # first, then the commands. Timers are set before sending anything, so
        # slow commands now don't delay later starts.
        delays = sorted(
            {move.delay_s for move in plan.moves if move.starter or move.needs_command}
        )
        for delay_s in delays:
            if delay_s > 0:
                self._timers.append(
                    async_call_later(
                        self.hass,
                        delay_s,
                        partial(self._async_delayed_start, plan, delay_s),
                    )
                )
        if plan.duration_s() > 0:
            self._timers.append(
                async_call_later(self.hass, plan.duration_s(), self._async_plan_done)
            )
        self.async_write_ha_state()

        generation = self._generation
        try:
            await self._async_start(plan, 0.0, stop_first=stop_first)
            # Nothing travels (shades only sent where they are): done once sent.
            if plan.duration_s() == 0 and generation == self._generation:
                self._async_plan_done(dt_util.utcnow())
        except HomeAssistantError as err:
            if generation == self._generation:
                _LOGGER.debug(
                    "%s: %s: abandoned: starting command failed: %s",
                    self.entity_id,
                    self._run,
                    err,
                )
                self._abandon_plan("start_failed")
                self.async_write_ha_state()
            raise

    async def _async_start(
        self,
        plan: Plan,
        delay_s: float,
        late_s: float | None = None,
        stop_first: Mapping[str, str] = MappingProxyType({}),
    ) -> None:
        """Fire the starters and send the commands due `delay_s` into the plan.

        `late_s` is how late the timer that started them fired, for the log.
        `stop_first` maps starters to a Stop button to press just before them.
        """
        tag = f"{self._run} at {delay_s:.1f}s"
        if late_s is not None:
            tag += f" (timer {late_s:+.3f}s)"
        due = [move for move in plan.moves if move.delay_s == delay_s]
        # Each starter once, though it starts several moves.
        starters = {move.starter.name: move.starter for move in due if move.starter}
        await asyncio.gather(
            *(
                self._async_fire(starter, tag, stop_first.get(name))
                for name, starter in starters.items()
            )
        )
        commands = [move for move in due if move.needs_command]
        if delay_s == 0:
            # Shades still travelling from the last plan that start later stop
            # where they are (as estimated) until then.
            commands += [
                replace(move, target_pct=move.from_pct)
                for move in plan.moves
                if move.held
            ]
        await self._async_set_positions(commands, tag)

    async def _async_delayed_start(
        self, plan: Plan, delay_s: float, _now: datetime
    ) -> None:
        late_s = (dt_util.utcnow() - self._started).total_seconds() - delay_s
        generation = self._generation
        try:
            await self._async_start(plan, delay_s, late_s)
        except HomeAssistantError as err:
            _LOGGER.error(
                "%s: %s: abandoned: couldn't start shades: %s",
                self.entity_id,
                self._run,
                err,
            )
            if generation == self._generation:
                self._abandon_plan("start_failed")
                self.async_write_ha_state()

    async def _async_fire(
        self, starter: Starter, tag: str, stop_button: str | None = None
    ) -> None:
        """Press a Pico button (after `stop_button`, if any) or activate a
        scene.
        """
        name = starter.name
        if stop_button is not None:
            await self._async_press(stop_button, tag)
        if split_entity_id(name)[0] == Platform.SCENE:
            took_s = await self._async_call(Platform.SCENE, SERVICE_TURN_ON, name)
            _LOGGER.debug(
                "%s: %s: activated %s (%.3fs)", self.entity_id, tag, name, took_s
            )
        else:
            await self._async_press(name, tag)

    @callback
    def _async_plan_done(self, _now: datetime) -> None:
        # Caseta shades report their destination immediately, so the plan's
        # timing is the only sign they've stopped. Ending late is worse than
        # early: a Pico stop on stationary shades sends them to favorite.
        _LOGGER.debug(
            "%s: %s: finished: planned travel time elapsed", self.entity_id, self._run
        )
        self._abandon_plan("finished")
        self.async_write_ha_state()

    @callback
    def _abandon_plan(self, ending: str) -> None:
        """Stop following the current plan, if one is running.

        Cancels starts that haven't happened yet and forgets the position
        estimates. Shades that are already moving keep moving. `ending` says
        why, for the counts in diagnostics.
        """
        if self._moving:
            self._run_endings[ending] += 1
        self._generation += 1
        for cancel in self._timers:
            cancel()
        self._timers.clear()
        self._running_moves.clear()
        self._moving = False
        self._direction = None

    async def _async_set_positions(self, moves: Iterable[Move], tag: str) -> None:
        """Send the moves' commands at once and log how long each took."""
        moves = list(moves)
        if not moves:
            return
        # Sent at once, but the Caseta bridge handles them one at a time, so
        # each lands about 0.1 s after the last; starters avoid that spread.
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
            "%s: %s: sent %s",
            self.entity_id,
            tag,
            ", ".join(
                f"{move.shade.entity_id} -> {move.target_pct}% ({seconds:.3f}s)"
                for move, seconds in zip(moves, took_s, strict=True)
            ),
        )

    async def _async_press(self, button_entity_id: str, tag: str) -> None:
        """Press a button; `tag` says what for in the log (a run, or "stop")."""
        took_s = await self._async_call(BUTTON_DOMAIN, SERVICE_PRESS, button_entity_id)
        _LOGGER.debug(
            "%s: %s: pressed %s (%.3fs)", self.entity_id, tag, button_entity_id, took_s
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
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {ATTR_ENTITY_ID: entity_id, **(data or {})},
                blocking=True,
                context=self._context,
            )
        except HomeAssistantError:
            self._count_call(entity_id, f"{service}_failed")
            raise
        self._count_call(entity_id, service)
        return time.monotonic() - started

    def _count_call(self, entity_id: str | list[str], service: str) -> None:
        for each in [entity_id] if isinstance(entity_id, str) else entity_id:
            self._service_calls[each][service] += 1
