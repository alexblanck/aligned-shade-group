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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import ButtonEntity
from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
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

from custom_components.aligned_cover_group.const import DOMAIN

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


@dataclass
class ShadeSpec:
    """A physical shade: geometry as configured, and its true speed.

    Its position counts motor turns. With `roll_curvature`, it's a roller whose
    roll shrinks as it unwinds, so each turn lowers the hemline a little less
    than the one before. Turns are counted from the hemline height where the
    roll would be fully wound, `roll_top_height` (by default its open height),
    in units of fabric length at that point; shades sharing a `roll_top_height`
    and curvature have matching rolls at every hemline height.
    """

    name: str
    closed_height: float
    open_height: float
    travel_time_s: float
    position_pct: float = 0
    roll_curvature: float = 0.0
    roll_top_height: float | None = None

    def _turns_down_to(self, height: float) -> float:
        top = self.open_height if self.roll_top_height is None else self.roll_top_height
        drop = top - height
        c = self.roll_curvature
        return drop if c == 0 else (1 - (1 - 4 * c * drop) ** 0.5) / (2 * c)

    @property
    def full_turns(self) -> float:
        """Turns from open to closed."""
        return self._turns_down_to(self.closed_height) - self._turns_down_to(
            self.open_height
        )

    def hemline_at(self, position_pct: float) -> float:
        top = self.open_height if self.roll_top_height is None else self.roll_top_height
        turns = (
            self._turns_down_to(self.open_height)
            + (1 - position_pct / 100) * self.full_turns
        )
        return top - (turns - self.roll_curvature * turns * turns)

    @classmethod
    def from_config(
        cls, config: dict[str, Any], speed: float, position_pct: float = 0
    ) -> ShadeSpec:
        """A shade matching a setup-flow config (as in `common.SHADES`)."""
        span = config["open_height"] - config["closed_height"]
        return cls(
            name=config["entity_id"].removeprefix("cover."),
            closed_height=config["closed_height"],
            open_height=config["open_height"],
            travel_time_s=span / speed,
            position_pct=position_pct,
        )

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

    def __init__(self, spec: ShadeSpec, bridge: Bridge) -> None:
        self.spec = spec
        self._bridge = bridge
        self.entity_id = spec.entity_id
        self._attr_name = spec.name
        self._attr_unique_id = spec.name
        # The motor's true position: fractional, unlike what HA reports.
        self.position_pct = float(spec.position_pct)
        self.target_pct = self.position_pct
        self._reported = round(self.position_pct)
        self._last = dt_util.utcnow()
        # (time, target_pct) log of motion starts, for synchronization checks.
        self.starts: list[tuple[float, float]] = []

    @property
    def moving(self) -> bool:
        return self.position_pct != self.target_pct

    @property
    def hemline_height(self) -> float:
        return self.spec.hemline_at(self.position_pct)

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
        step_pct = elapsed_s * 100 / self.spec.travel_time_s
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
        flow = await self.answer_shade_steps(
            flow, self.hass.config_entries.flow.async_configure
        )
        assert flow["type"] == "create_entry", flow
        await self.hass.async_block_till_done()
        self.entry = self.hass.config_entries.async_entries(DOMAIN)[0]

    async def answer_shade_steps(
        self, flow: Any, configure: Callable[..., Awaitable[Any]]
    ) -> Any:
        """Enter each shade's heights, then the tallest shade's travel time.

        The travel time entered is the tallest shade's real one unless the
        room was built with a different `configured_travel_time_s`.
        """
        for shade in self.shades.values():
            assert flow["step_id"] == "shade", flow
            flow = await configure(
                flow["flow_id"],
                {
                    "open_height": shade.spec.open_height,
                    "closed_height": shade.spec.closed_height,
                },
            )
        assert flow["step_id"] == "travel", flow
        tallest = max(
            self.shades.values(),
            key=lambda shade: shade.spec.open_height - shade.spec.closed_height,
        )
        answers: dict[str, float] = {
            "travel_time_s": self._configured_travel_time_s
            or tallest.spec.travel_time_s
        }
        if tallest.spec.roll_curvature:
            answers["halfway_height"] = tallest.spec.hemline_at(50)
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
            return specs[eid].hemline_at(positions_pct_by_id[eid])

        best = float("inf")
        for candidate in (hemline_height(eid) for eid in specs):
            worst = max(
                abs(
                    min(max(candidate, spec.closed_height), spec.open_height)
                    - hemline_height(eid)
                )
                for eid, spec in specs.items()
            )
            best = min(best, worst)
        return best

    def worst_misalignment(self) -> float:
        """Worst misalignment over the recorded history."""
        return max(self.misalignment(snapshot) for snapshot in self.history)


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
) -> Room:
    """Set up simulated shades (and Pico), then the group via its config flow."""
    bridge = Bridge(freezer=freezer)
    shades = [SimShade(spec, bridge) for spec in specs]
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
