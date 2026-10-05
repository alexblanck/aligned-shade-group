"""A simulated room of Lutron shades and a Pico, driven through real HA services.

Shades move over (frozen, manually advanced) time at their travel speed. Like
Caseta shades, they report their destination as soon as they're commanded, and
their real position only when stopped. The Pico behaves like a Caseta shade
Pico paired on the bridge: Up/Down send every paired shade to open/closed at
the same instant, and the middle button stops moving shades but sends
stationary shades to their favorite position.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import ButtonEntity
from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntry, ConfigFlow
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_fire_time_changed_exact,
    mock_config_flow,
    mock_integration,
    mock_platform,
    setup_test_component_platform,
)

from custom_components.aligned_shade_group.const import DOMAIN

from .common import SHADES, SPEED

GROUP = "cover.living_room"
FAVORITE = 50
# How the sim Pico's device is identified, and its buttons' names (as Caseta
# names them: the Pico's name, then the button's).
PICO_IDENTIFIER = ("lutron_caseta", "living_room_pico")
PICO_BUTTON_NAMES = {"open": "On", "stop": "Stop", "close": "Off"}
STEP_S = 0.1  # simulated time per tick


@dataclass
class Bridge:
    """How the simulated Lutron bridge delivers commands.

    `latency_s` is how long each command takes to reach a shade (the clock
    moves on meanwhile). `gate`, when set, holds position commands until the
    test sets the event. `fail` makes every command raise, like an unreachable
    bridge.
    """

    freezer: FrozenDateTimeFactory
    latency_s: float = 0.0
    gate: asyncio.Event | None = None
    fail: bool = False

    async def deliver(self, gated: bool = False) -> None:
        if self.fail:
            raise HomeAssistantError("Bridge unreachable")
        if gated and self.gate is not None:
            await self.gate.wait()
        if self.latency_s:
            self.freezer.tick(timedelta(seconds=self.latency_s))


@dataclass(frozen=True)
class Roll:
    """A roller's build: its tube, its fabric and its motor.

    Each turn of the tube winds one more layer of fabric, so the roll's radius
    grows by `fabric_thickness` per turn, and the fabric wound on is the sum
    of every turn's circumference. The motor turns the tube `turns_per_s`
    times a second. A thickness of 0 makes the hemline move evenly.
    """

    tube_diameter: float
    fabric_thickness: float
    turns_per_s: float

    def wound_length(self, turns: float) -> float:
        """Length of fabric wound onto an empty tube by `turns` turns."""
        tube_radius = self.tube_diameter / 2
        radius = tube_radius + self.fabric_thickness * turns
        # Circumferences grow linearly, so their sum is turns times the mean:
        #   wound = integral of 2*pi*(r0 + t*n) dn from 0 to N
        #         = N * 2*pi * (r0 + (r0 + t*N)) / 2 = pi * N * (r0 + radius)
        return math.pi * turns * (tube_radius + radius)


@dataclass
class ShadeSpec:
    """A physical shade: its roll, where it hangs, and its motor's limits.

    `empty_height` is where the hemline would be with no fabric on the tube,
    set by where the roll is mounted and how long the fabric is. The limits
    count turns wound onto the tube, and the position counts turns between
    them, so it changes at a constant rate while moving. Shades with the same
    `Roll` and `empty_height` have matching rolls.
    """

    name: str
    roll: Roll
    empty_height: float
    closed_turns: float
    open_turns: float

    @classmethod
    def even(
        cls,
        name: str,
        closed_height: float,
        open_height: float,
        travel_time_s: float,
    ) -> ShadeSpec:
        """A shade whose hemline moves evenly, taking `travel_time_s` to open."""
        tube_diameter = 1.5
        turns = (open_height - closed_height) / (math.pi * tube_diameter)
        return cls(
            name=name,
            roll=Roll(
                tube_diameter=tube_diameter,
                fabric_thickness=0,
                turns_per_s=turns / travel_time_s,
            ),
            empty_height=closed_height,
            closed_turns=0,
            open_turns=turns,
        )

    @classmethod
    def from_config(cls, config: dict[str, Any], speed: float) -> ShadeSpec:
        """An even shade matching a setup-flow config (as in `common.SHADES`)."""
        return cls.even(
            name=config["entity_id"].removeprefix("cover."),
            closed_height=config["closed_height"],
            open_height=config["open_height"],
            travel_time_s=(config["open_height"] - config["closed_height"]) / speed,
        )

    def hemline_at_turns(self, turns: float) -> float:
        """Hemline height with `turns` turns wound onto the tube."""
        return self.empty_height + self.roll.wound_length(turns)

    def closed_height(self) -> float:
        return self.hemline_at_turns(self.closed_turns)

    def open_height(self) -> float:
        return self.hemline_at_turns(self.open_turns)

    def travel_time_s(self) -> float:
        """Seconds from closed to open."""
        return (self.open_turns - self.closed_turns) / self.roll.turns_per_s

    def hemline_at_position(self, position_pct: float) -> float:
        # Checked against real measurements by
        # test_roller_measurements_match_the_simulation.
        span = self.open_turns - self.closed_turns
        return self.hemline_at_turns(self.closed_turns + position_pct / 100 * span)

    @property
    def entity_id(self) -> str:
        return f"cover.{self.name}"


class SimShade(CoverEntity):
    """A shade that moves toward its target at a constant speed."""

    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, spec: ShadeSpec, bridge: Bridge, start_pct: float) -> None:
        self.spec = spec
        self._bridge = bridge
        self.entity_id = spec.entity_id
        self._attr_name = spec.name
        self._attr_unique_id = spec.name
        # The motor's true position: fractional, unlike what HA reports.
        self.position_pct = float(start_pct)
        self.target_pct = self.position_pct
        self._reported = round(self.position_pct)
        self._last = dt_util.utcnow()
        # (time, target_pct) log of motion starts, for synchronization checks.
        self.starts: list[tuple[float, float]] = []

    @property
    def moving(self) -> bool:
        return self.position_pct != self.target_pct

    @property
    def current_cover_position(self) -> int:
        return self._reported

    @property
    def is_closed(self) -> bool:
        return self._reported == 0

    def settle(self) -> None:
        """Advance the motor to the current time."""
        now = dt_util.utcnow()
        elapsed_s = (now - self._last).total_seconds()
        self._last = now
        step_pct = elapsed_s * 100 / self.spec.travel_time_s()
        if self.target_pct > self.position_pct:
            self.position_pct = min(self.target_pct, self.position_pct + step_pct)
        elif self.target_pct < self.position_pct:
            self.position_pct = max(self.target_pct, self.position_pct - step_pct)

    def go(self, target_pct: float) -> None:
        self.settle()
        if not self.moving and target_pct != self.position_pct:
            self.starts.append((dt_util.utcnow().timestamp(), target_pct))
        self.target_pct = target_pct
        self._reported = round(target_pct)
        self.async_write_ha_state()

    def stop(self) -> None:
        self.settle()
        self.target_pct = self.position_pct
        self._reported = round(self.position_pct)
        self.async_write_ha_state()

    def set_available(self, available: bool) -> None:
        """Drop off or come back, like a shade losing contact with the bridge."""
        self._attr_available = available
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._bridge.deliver(gated=True)
        self.go(kwargs["position"])

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._bridge.deliver(gated=True)
        self.go(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._bridge.deliver(gated=True)
        self.go(0)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._bridge.deliver()
        self.stop()


class SimPicoButton(ButtonEntity):
    """One button of a shade Pico paired to `shades`."""

    _attr_should_poll = False

    def __init__(self, role: str, shades: list[SimShade], bridge: Bridge) -> None:
        self.role = role
        self._bridge = bridge
        self.entity_id = f"button.pico_{role}"
        self._attr_name = f"Living Room Pico {PICO_BUTTON_NAMES[role]}"
        self._attr_unique_id = f"pico_{role}"
        self._attr_device_info = DeviceInfo(
            identifiers={PICO_IDENTIFIER}, name="Living Room Pico"
        )
        self._shades = shades
        self.presses = 0

    async def async_press(self) -> None:
        await self._bridge.deliver()
        self.presses += 1
        for shade in self._shades:
            shade.settle()
        if self.role == "open":
            targets = [100] * len(self._shades)
        elif self.role == "close":
            targets = [0] * len(self._shades)
        elif any(shade.moving for shade in self._shades):
            for shade in self._shades:
                shade.stop()
            return
        else:
            targets = [FAVORITE] * len(self._shades)
        for shade, target in zip(self._shades, targets, strict=True):
            shade.go(target)


class Room:
    """The simulated shades, Pico and aligned group under test."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        shades: list[SimShade],
        pico: dict[str, SimPicoButton] | None,
        bridge: Bridge,
        configured_travel_time_s: float | None,
    ) -> None:
        self.hass = hass
        self.freezer = freezer
        self.bridge = bridge
        self._configured_travel_time_s = configured_travel_time_s
        self.shades = {shade.entity_id: shade for shade in shades}
        self.pico = pico
        self.entry: MockConfigEntry | None = None
        # Snapshot of every shade's hemline at every tick.
        self.history: list[dict[str, float]] = []

    def pico_device_id(self) -> str:
        """The device the sim Pico's buttons belong to."""
        assert self.pico is not None
        entry = er.async_get(self.hass).async_get(self.pico["open"].entity_id)
        assert entry is not None and entry.device_id is not None
        return entry.device_id

    def __getitem__(self, entity_id: str) -> SimShade:
        return self.shades[entity_id]

    async def add_group(self) -> None:
        """Create the aligned group through the config flow, like a user would."""
        flow = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        pico = {"device_id": self.pico_device_id()} if self.pico else {}
        group_input = {"name": "Living Room", "shades": list(self.shades), "pico": pico}
        flow = await self.hass.config_entries.flow.async_configure(
            flow["flow_id"], group_input
        )
        flow = await self.answer_shade_steps(flow)
        assert flow["type"] == "create_entry", flow
        await self.hass.async_block_till_done()
        self.entry = self.hass.config_entries.async_entries(DOMAIN)[0]

    async def start_reconfigure(self) -> Any:
        """Open the group's Reconfigure form, like a user would."""
        assert self.entry is not None
        return await self.hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_RECONFIGURE, "entry_id": self.entry.entry_id},
        )

    async def answer_shade_steps(self, flow: Any) -> Any:
        """Enter each shade's heights, then the tallest shade's travel time.

        The travel time entered is the tallest shade's real one unless the
        room was built with a different `configured_travel_time_s`.
        """
        configure = self.hass.config_entries.flow.async_configure
        for shade in self.shades.values():
            assert flow["step_id"] in ("shade", "shade_prefilled"), flow
            flow = await configure(
                flow["flow_id"],
                {
                    "open_height": shade.spec.open_height(),
                    "closed_height": shade.spec.closed_height(),
                },
            )
        assert flow["step_id"] == "travel", flow
        tallest = max(
            self.shades.values(),
            key=lambda shade: shade.spec.open_height() - shade.spec.closed_height(),
        )
        answers: dict[str, Any] = {
            "travel_time_s": self._configured_travel_time_s
            or tallest.spec.travel_time_s()
        }
        if tallest.spec.roll.fabric_thickness:
            answers["roller_curve"] = {
                "halfway_height": tallest.spec.hemline_at_position(50)
            }
        else:
            answers["roller_curve"] = {}
        return await configure(flow["flow_id"], answers)

    async def command(self, service: str, **data: Any) -> None:
        """Call a cover service on the group."""
        await self.hass.services.async_call(
            "cover", service, {"entity_id": GROUP, **data}, blocking=True
        )
        self._record()

    async def run(self, seconds: float) -> None:
        """Advance simulated time, letting timers fire and motors move."""
        for _ in range(math.ceil(round(seconds / STEP_S, 6))):
            self.freezer.tick(timedelta(seconds=STEP_S))
            async_fire_time_changed_exact(self.hass)
            await self.hass.async_block_till_done()
            for shade in self.shades.values():
                shade.settle()
                shade.async_write_ha_state()
            await self.hass.async_block_till_done()
            self._record()

    async def run_until_still(self, limit_s: float = 300) -> None:
        """Run until no shade is moving and the group isn't either."""
        elapsed_s = 0.0
        while elapsed_s < limit_s:
            await self.run(STEP_S)
            elapsed_s += STEP_S
            if not any(
                s.moving for s in self.shades.values()
            ) and self.group.state not in (
                "opening",
                "closing",
            ):
                return
        raise AssertionError("shades never stopped")

    @property
    def group(self):
        return self.hass.states.get(GROUP)

    def positions_pct_by_id(self) -> dict[str, int]:
        return {eid: round(shade.position_pct) for eid, shade in self.shades.items()}

    def _record(self) -> None:
        self.history.append(
            {eid: shade.position_pct for eid, shade in self.shades.items()}
        )

    def misalignment(self, positions_pct_by_id: dict[str, float]) -> float:
        """Smallest possible hemline spread, allowing shades to sit clamped.

        Independent of the integration's math: tries each shade's hemline as
        the shared height and measures the worst shade's distance from it.
        """
        specs = {eid: shade.spec for eid, shade in self.shades.items()}

        def hemline_height(eid: str) -> float:
            return specs[eid].hemline_at_position(positions_pct_by_id[eid])

        best = float("inf")
        for candidate in (hemline_height(eid) for eid in specs):
            worst = max(
                abs(
                    min(max(candidate, spec.closed_height()), spec.open_height())
                    - hemline_height(eid)
                )
                for eid, spec in specs.items()
            )
            best = min(best, worst)
        return best

    def worst_misalignment(self) -> float:
        """Worst misalignment over the recorded history."""
        return max(self.misalignment(snapshot) for snapshot in self.history)


# Hemline spread allowed while moving, in inches: timers fire on the next
# 0.1 s tick (up to about 0.5 in for a roller near the top of its travel), plus
# whole-percent position rounding.
HEIGHT_TOLERANCE = 1.0


def same_tops() -> list[ShadeSpec]:
    return [ShadeSpec.from_config(config, SPEED) for config in SHADES]


def matched_rolls() -> list[ShadeSpec]:
    """Three shades whose rolls match at every hemline height.

    "high" has the longest range, so it's the one measured; "tall" reaches
    below it and "low" further still, so the curve must be extended. "tall"
    covers more of the group's positions than "high" (it's lower on the roll,
    where the hemline moves slower), so the travel time must be tied to the
    measured shade rather than the widest window.
    """
    # Thicker fabric on a thinner tube than the living room's, so the curve is
    # stronger and extension errors show. All three hang from the same height.
    roll = Roll(tube_diameter=1, fabric_thickness=0.1, turns_per_s=0.25)
    # Limits in turns, about 30-100, 50-125 and 12-60 in.
    return [
        ShadeSpec(
            name=name,
            roll=roll,
            empty_height=10,
            closed_turns=closed_turns,
            open_turns=open_turns,
        )
        for name, closed_turns, open_turns in (
            ("tall", 4.416, 12.6488),
            ("high", 7.342, 14.7751),
            ("low", 0.6006, 8.5704),
        )
    ]


def prefilled_answers(flow: Any) -> dict[str, Any]:
    """A form's prefilled values, as if submitted without changing anything."""

    def answers(schema: vol.Schema) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in schema.schema.items():
            if isinstance(value, section):
                result[str(key)] = answers(value.schema)
            elif key.description and "suggested_value" in key.description:
                result[str(key)] = key.description["suggested_value"]
        return result

    return answers(flow["data_schema"])


class _PicoEntryFlow(ConfigFlow):
    """A do-nothing flow for the sim Pico's config entry.

    Home Assistant checks an entry's version against its integration's flow
    before setting it up, so the entry needs one, though nothing runs it.
    """

    VERSION = 1


async def _async_add_pico(hass: HomeAssistant, buttons: list[SimPicoButton]) -> None:
    """Add the Pico's buttons as a mock Caseta integration would.

    They're set up from a config entry, as devices need one, so they belong to
    a Pico device like real Caseta buttons do.
    """

    async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.BUTTON])
        return True

    async def async_setup_buttons(
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        async_add_entities(buttons)

    mock_integration(
        hass, MockModule("lutron_caseta", async_setup_entry=async_setup_entry)
    )
    mock_platform(hass, "lutron_caseta.config_flow", None)
    mock_platform(
        hass,
        "lutron_caseta.button",
        MockPlatform(async_setup_entry=async_setup_buttons),
    )
    entry = MockConfigEntry(domain="lutron_caseta")
    entry.add_to_hass(hass)
    with mock_config_flow("lutron_caseta", _PicoEntryFlow):
        assert await hass.config_entries.async_setup(entry.entry_id)


async def build_room(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    specs: list[ShadeSpec],
    pico: bool = True,
    configured_travel_time_s: float | None = None,
    *,
    start_pct: float | Mapping[str, float] | None = None,
) -> Room:
    """Set up simulated shades (and Pico), then the group via its config flow.

    `start_pct` is where the shades start, in percent (0 closed, 100 open):

    - None (the default): every shade fully closed.
    - A number: every shade at that position.
    - A mapping of entity id to position, such as
      `{"cover.left_1": 62, "cover.left_2": 75}`: each shade at its own. It
      must name every shade, so a mistyped id fails rather than quietly
      leaving that shade closed.
    """
    entity_ids = [spec.entity_id for spec in specs]
    if start_pct is None:
        start_pct = 0
    if isinstance(start_pct, Mapping):
        if set(start_pct) != set(entity_ids):
            raise ValueError(
                f"start_pct names {sorted(start_pct)}, not the room's shades "
                f"{sorted(entity_ids)}"
            )
        start_pcts = dict(start_pct)
    else:
        start_pcts = dict.fromkeys(entity_ids, start_pct)
    bridge = Bridge(freezer=freezer)
    shades = [SimShade(spec, bridge, start_pcts[spec.entity_id]) for spec in specs]
    setup_test_component_platform(hass, "cover", shades)
    assert await async_setup_component(hass, "cover", {"cover": {"platform": "test"}})
    buttons = None
    if pico:
        buttons = {
            role: SimPicoButton(role, shades, bridge)
            for role in ("open", "stop", "close")
        }
        await _async_add_pico(hass, list(buttons.values()))
    await hass.async_block_till_done()
    room = Room(hass, freezer, shades, buttons, bridge, configured_travel_time_s)
    await room.add_group()
    return room
