"""End-to-end scenarios: a user drives the group; simulated shades move over time."""

import asyncio
import logging

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.aligned_shade_group.const import (
    DOMAIN,
    missing_entities_issue_id,
)

from .common import HIGH_SILL, LOW_SILL
from .sim import (
    FAVORITE,
    GROUP,
    HEIGHT_TOLERANCE,
    LIVING_ROOM_EMPTY_HEIGHT,
    LIVING_ROOM_ROLL,
    STEP_S,
    Room,
    ShadeSpec,
    build_room,
    living_room_all,
    living_room_left,
    matched_rolls,
    same_tops,
)


def started_together(room: Room, entity_ids: list[str]) -> bool:
    """Whether these shades' first starts were sent together.

    Commands sent together still reach the shades one bridge latency apart, so
    allow for that; shades started by one Pico press or scene start at exactly
    the same instant (checked with == where that's the point).
    """
    starts = [room[entity_id].starts()[0][0] for entity_id in entity_ids]
    return max(starts) - min(starts) <= len(entity_ids) * room.bridge.latency_s


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_open_from_closed_stays_aligned(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(), pico)
    assert room.group.state == "closed"

    await room.command("open_cover")
    assert room.group.state == "opening"
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    # The high-sill shade waited until the other's hemline reached its sill.
    low_start = room[LOW_SILL].starts()[0][0]
    high_start = room[HIGH_SILL].starts()[0][0]
    assert high_start - low_start == pytest.approx(6, abs=0.5)
    assert room.group.state == "open"
    assert room.group.attributes["current_position"] == 100
    if pico:
        # Different sills: the Pico would have started both at once.
        assert room.pico["open"].presses == 0


async def test_close_from_open_uses_pico_in_lockstep(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), True, start_pct=100)

    await room.command("close_cover")
    await room.run_until_still()

    assert room.pico["close"].presses == 1
    assert room[HIGH_SILL].starts()[0][0] == room[LOW_SILL].starts()[0][0]
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.state == "closed"


async def test_partial_close_then_reopen(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), True, start_pct=100)

    await room.command("set_cover_position", position=50)
    await room.run_until_still()
    # Hemline 48: (48 - 24) / 60 and (48 - 12) / 72.
    assert room.positions_pct_by_id() == {HIGH_SILL: 40, LOW_SILL: 50}
    assert room.group.attributes["current_position"] == 50
    assert room.group.attributes["aligned"] is True
    assert room.group.attributes["hemline_heights"] == {HIGH_SILL: 48, LOW_SILL: 48}

    await room.command("open_cover")
    await room.run_until_still()
    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}
    assert room.pico["close"].presses == 1
    assert room.pico["open"].presses == 1
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_stop_during_staggered_open(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), True)

    await room.command("open_cover")
    await room.run(3)  # low-sill shade is moving; high-sill hasn't started
    await room.command("stop_cover")
    await room.run(20)

    assert room.pico["stop"].presses == 1
    assert not room[HIGH_SILL].starts(), "pending start fired after stop"
    assert room.positions_pct_by_id()[LOW_SILL] == pytest.approx(8, abs=1)
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.state not in ("opening", "closing")
    assert room.group.attributes["aligned"] is True
    # The group spans the low-sill shade's range, so it matches that shade.
    low_sill_pct = room.positions_pct_by_id()[LOW_SILL]
    assert room.group.attributes["current_position"] == low_sill_pct


async def test_stop_while_aligned_and_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), True, start_pct=100)

    await room.command("close_cover")
    await room.run(10)
    await room.command("stop_cover")
    await room.run(20)

    # Both stopped by the same Pico press, about 20 in down from the top.
    assert room.positions_pct_by_id() == {HIGH_SILL: 66, LOW_SILL: 72}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.attributes["current_position"] == 72


async def test_stop_while_idle_does_not_trigger_favorite(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), start_pct=100)

    await room.command("stop_cover")
    await room.run(30)

    assert room.pico["stop"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}
    assert all(shade.position_pct != FAVORITE for shade in room.shades.values())


async def test_reverse_while_opening(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), True)

    await room.command("open_cover")
    await room.run(15)  # both shades moving up
    await room.command("close_cover")
    await room.run(1)
    # Where the Pico's Stop left them isn't quite the estimate, but the run
    # carries on rather than taking that for another command.
    assert room.group.state == "closing"
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    # Pressed on moving shades, Close would only stop them: the Pico's Stop
    # goes first, then Close turns them around together.
    assert room.pico["stop"].presses == 1
    assert room.pico["close"].presses == 1
    reversals = {room[entity_id].starts()[-1] for entity_id in (HIGH_SILL, LOW_SILL)}
    assert len(reversals) == 1


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_reverse_holds_a_shade_that_starts_later(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, pico: bool
) -> None:
    # Opening, the low-sill shade leads and the high-sill one waits above it.
    # Reversed, the high-sill one leads down, so the low-sill one must stop
    # rising until the high-sill one comes down to it.
    room = await build_room(
        hass, freezer, same_tops(), pico, start_pct={LOW_SILL: 10, HIGH_SILL: 20}
    )

    await room.command("open_cover")
    await room.run(1)
    reversed_at = len(room.history)
    # A slow bridge: the stop lands well after the plan's estimate was made.
    room.bridge.latency_s = 0.3
    await room.command("set_cover_position", position=5)
    await room.run(2)
    # Held with a stop, so where it reports stopping (a little past the
    # estimate) is part of the run, not another command.
    assert room.group.state == "closing"
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 5}
    # It stops rising once the bridge gets to its stop, after high_sill's
    # command.
    low_sill_pcts = [snapshot[LOW_SILL] for snapshot in room.history]
    assert max(low_sill_pcts[reversed_at:]) <= low_sill_pcts[reversed_at - 1] + 3
    assert room.misalignment(room.history[-1]) <= HEIGHT_TOLERANCE
    # Stopped, not sent back to its estimated position: only ever its targets.
    assert room[LOW_SILL].commanded() == [100, 5]


async def test_retarget_while_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), True, start_pct=100)

    await room.command("close_cover")
    await room.run(5)
    await room.command("set_cover_position", position=50)
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 40, LOW_SILL: 50}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    # They keep going: a Pico press while they move would stop them first.
    assert all(len(shade.starts()) == 1 for shade in room.shades.values())


async def test_small_move_doesnt_overshoot(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Seen on the two left shades, moving down 1%: the Pico's Off started
    # them, and by the time the bridge got to their targets they'd gone past.
    room = await build_room(
        hass,
        freezer,
        living_room_left(),
        start_pct={"cover.left_1": 77, "cover.left_2": 85},
    )
    # Each command took 0.07-0.23 s on the living room's bridge.
    room.bridge.latency_s = 0.15

    await room.command("set_cover_position", position=84)
    await room.run_until_still()

    assert room.positions_pct_by_id() == {"cover.left_1": 76, "cover.left_2": 84}
    assert all(shade.overshoot_pct() == 0 for shade in room.shades.values())
    assert room.pico["close"].presses == 0
    assert started_together(room, ["cover.left_1", "cover.left_2"])
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_different_tops_and_sills(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    # Shades offset vertically, same speed (2 in/s).
    room = await build_room(
        hass,
        freezer,
        [
            ShadeSpec.even(
                name="left",
                closed_height=10,
                open_height=60,
                travel_time_s=25,
            ),
            ShadeSpec.even(
                name="right",
                closed_height=20,
                open_height=80,
                travel_time_s=30,
            ),
        ],
        pico,
    )

    await room.command("set_cover_position", position=50)  # hemline 45
    await room.run_until_still()
    assert room.positions_pct_by_id() == {"cover.left": 70, "cover.right": 42}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE

    await room.command("open_cover")
    await room.run_until_still()
    assert room.positions_pct_by_id() == {"cover.left": 100, "cover.right": 100}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.attributes["current_position"] == 100

    await room.command("close_cover")
    await room.run_until_still()
    assert room.positions_pct_by_id() == {"cover.left": 0, "cover.right": 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


def missing_entities_issue(hass: HomeAssistant, room: Room) -> ir.IssueEntry | None:
    assert room.entry is not None
    return ir.async_get(hass).async_get_issue(
        DOMAIN, missing_entities_issue_id(room.entry.entry_id)
    )


async def test_renamed_pico_button_falls_back_to_each_shade(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Pressing a button that no longer exists does nothing and raises
    # nothing, so the group must notice instead of waiting for shades that
    # never start.
    room = await build_room(hass, freezer, same_tops(), start_pct=100)
    er.async_get(hass).async_update_entity(
        "button.pico_stop", new_entity_id="button.pico_stop_renamed"
    )
    await hass.async_block_till_done()

    # Level shades would normally close with a Pico press.
    await room.command("close_cover")
    await room.run(10)
    await room.command("stop_cover")
    await room.run(20)

    assert all(button.presses == 0 for button in room.pico.values())
    assert all(0 < pct < 100 for pct in room.positions_pct_by_id().values())
    assert not any(shade.moving for shade in room.shades.values())
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert "not using button.pico_stop" in caplog.text
    issue = missing_entities_issue(hass, room)
    assert issue is not None
    assert issue.translation_placeholders["entities"] == "- button.pico_stop"


async def test_renamed_shade_is_left_out_and_raised(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), pico=False)
    er.async_get(hass).async_update_entity(HIGH_SILL, new_entity_id="cover.renamed")
    await hass.async_block_till_done()

    await room.command("open_cover")
    await room.run_until_still()

    # The room still names the renamed shade by its original id.
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 100}
    issue = missing_entities_issue(hass, room)
    assert issue is not None
    assert issue.translation_placeholders["entities"] == f"- {HIGH_SILL}"


async def test_renames_raise_and_clear_the_issue_without_a_move(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops())
    registry = er.async_get(hass)

    registry.async_update_entity(LOW_SILL, new_entity_id="cover.renamed")
    await room.run(2)
    issue = missing_entities_issue(hass, room)
    assert issue is not None
    assert issue.translation_placeholders["entities"] == f"- {LOW_SILL}"

    # Renamed back to the id the group knows: fixed without reconfiguring.
    registry.async_update_entity("cover.renamed", new_entity_id=LOW_SILL)
    await room.run(2)
    assert missing_entities_issue(hass, room) is None


async def test_pico_paired_to_some_shades_starts_and_stops_them(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # A Pico paired to only the low-sill shade: it starts that shade, and the
    # other is commanded at the same moment; likewise for stopping.
    room = await build_room(hass, freezer, same_tops(), [LOW_SILL], start_pct=100)

    await room.command("close_cover")
    await room.run(10)
    # Stopping: the Pico stops its shade, and the other gets its own stop.
    await room.command("stop_cover")
    await room.run_until_still()

    assert room.pico["close"].presses == 1
    assert room.pico["stop"].presses == 1
    assert started_together(room, [HIGH_SILL, LOW_SILL])
    assert all(0 < pct < 100 for pct in room.positions_pct_by_id().values())
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_late_starter_leaves_earlier_shades_alone(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # A scene sets both shades to 30%. high_sill starts there (hemline 42 in)
    # and low_sill at 40% (40.8 in): not level, so going to 10% high_sill leads
    # at 0 s, and low_sill starts when high_sill reaches it, about 0.6 s later.
    # By then high_sill has left 30%, so activating the scene for low_sill
    # would send high_sill back up to 30%.
    room = await build_room(
        hass,
        freezer,
        same_tops(),
        pico=False,
        start_pct={HIGH_SILL: 30, LOW_SILL: 40},
        scenes={"both_to_30": {HIGH_SILL: 30, LOW_SILL: 30}},
    )

    await room.command("set_cover_position", position=10)
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 10}
    assert room.misalignment(room.history[-1]) <= HEIGHT_TOLERANCE


async def test_scene_doesnt_undo_a_picos_start(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Level at 30% and 41%, closing together. A Pico paired to high_sill
    # would start it; the scene would start low_sill and leave high_sill at
    # 30%. Fired together, the scene would send high_sill back to 30% after
    # the Pico had started it, so they can't both be used.
    room = await build_room(
        hass,
        freezer,
        same_tops(),
        [HIGH_SILL],
        start_pct={HIGH_SILL: 30, LOW_SILL: 41},
        scenes={"both_to_30": {HIGH_SILL: 30, LOW_SILL: 30}},
    )

    await room.command("close_cover")
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_stop_before_a_picos_shades_start(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # A Pico paired to high_sill only. Opening from closed, low_sill leads and
    # high_sill is due to start at about 6 s. Stopping at 3 s mustn't press
    # that Pico: with none of its shades moving, its middle button would send
    # high_sill to its favorite position.
    room = await build_room(hass, freezer, same_tops(), [HIGH_SILL])

    await room.command("open_cover")
    await room.run(3)
    await room.command("stop_cover")
    await room.run(20)

    assert room.pico["stop"].presses == 0
    assert room[HIGH_SILL].position_pct == 0
    assert not any(shade.moving for shade in room.shades.values())


async def test_pico_listed_twice_is_pressed_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Stored twice (the setup form rejects that, but stored data could still
    # have it). Pressing its Stop twice would stop the shades and then, with
    # them still, send them to their favorite position.
    room = await build_room(hass, freezer, same_tops(), start_pct=100)
    (pico,) = room.entry.data["controls"]
    hass.config_entries.async_update_entry(
        room.entry, data={**room.entry.data, "controls": [pico, pico]}
    )
    await hass.config_entries.async_reload(room.entry.entry_id)
    await hass.async_block_till_done()

    await room.command("close_cover")
    await room.run(10)
    await room.command("stop_cover")
    await room.run(20)

    assert room.pico["close"].presses == 1
    assert room.pico["stop"].presses == 1
    assert room.positions_pct_by_id() == {HIGH_SILL: 66, LOW_SILL: 72}


async def test_failed_delayed_start_abandons_the_plan(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # The Pico paired to high_sill is due to start it at about 6 s, but the
    # bridge fails by then. The group abandons its plan rather than
    # reporting high_sill as opening while it never started.
    room = await build_room(hass, freezer, same_tops(), [HIGH_SILL])

    await room.command("open_cover")
    await room.run(3)
    room.bridge.fail = True
    await room.run(4)

    assert not room[HIGH_SILL].starts()
    assert room.group.state not in ("opening", "closing")
    assert room.group.attributes["hemline_heights"][HIGH_SILL] == 24


async def test_unavailable_shade(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = await build_room(hass, freezer, same_tops())
    room[HIGH_SILL].set_available(False)
    await hass.async_block_till_done()

    # The remaining shade alone decides the group's state.
    assert room.group.state == "closed"
    assert room.group.attributes["current_position"] == 0

    with caplog.at_level(logging.WARNING):
        await room.command("set_cover_position", position=50)
    await room.run_until_still()
    assert f"leaving out shades with no position: {HIGH_SILL}" in caplog.text
    # Positions are unknown, so the Pico (which would move both) isn't used.
    assert room.pico["open"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 50}
    assert room.group.attributes["current_position"] == 50
    assert room.group.attributes["hemline_heights"] == {LOW_SILL: 48}

    # It comes back where it was, now out of line with the other shade.
    room[HIGH_SILL].set_available(True)
    await hass.async_block_till_done()
    assert room.group.attributes["aligned"] is False

    await room.command("set_cover_position", position=50)
    await room.run_until_still()
    assert room.positions_pct_by_id() == {HIGH_SILL: 40, LOW_SILL: 50}
    assert room.group.attributes["aligned"] is True


async def test_all_shades_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops())
    for shade in room.shades.values():
        shade.set_available(False)
    await hass.async_block_till_done()
    assert room.group.state == "unavailable"

    room[LOW_SILL].set_available(True)
    await hass.async_block_till_done()
    assert room.group.state == "closed"


async def test_shades_moving_opposite_ways(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Out of line: hemlines at 54 and 48 (group at 54%).
    room = await build_room(hass, freezer, same_tops(), True, start_pct=50)
    assert room.group.attributes["aligned"] is False
    assert room.group.attributes["current_position"] == 54

    # Hemline 52.3: the high-sill shade goes down, the low-sill shade goes up,
    # and the group's own position goes up.
    await room.command("set_cover_position", position=56)
    assert room.group.state == "opening"
    assert room.pico["open"].presses == room.pico["close"].presses == 0
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 47, LOW_SILL: 56}
    assert room.group.attributes["aligned"] is True
    assert room.group.attributes["current_position"] == 56


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
@pytest.mark.parametrize(
    ("service", "end_pct", "end_state"),
    [("open_cover", 100, "open"), ("close_cover", 0, "closed")],
    ids=["open", "close"],
)
@pytest.mark.parametrize(
    "start_pcts",
    [(0, 100, 50), (100, 0, 0), (30, 70, 10), (99, 1, 50)],
    ids=lambda pcts: "-".join(map(str, pcts)),
)
async def test_open_and_close_from_shuffled_positions(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    start_pcts: tuple[int, int, int],
    service: str,
    end_pct: int,
    end_state: str,
    pico: bool,
) -> None:
    # Three shades of different sizes and speeds, starting out of line.
    specs = [
        ShadeSpec.even(
            name="a",
            closed_height=24,
            open_height=84,
            travel_time_s=30,
        ),
        ShadeSpec.even(
            name="b",
            closed_height=12,
            open_height=84,
            travel_time_s=36,
        ),
        ShadeSpec.even(
            name="c",
            closed_height=30,
            open_height=72,
            travel_time_s=15,
        ),
    ]
    room = await build_room(
        hass,
        freezer,
        specs,
        pico,
        start_pct=dict(zip(["cover.a", "cover.b", "cover.c"], start_pcts, strict=True)),
    )

    await room.command(service)
    await room.run_until_still()

    assert set(room.positions_pct_by_id().values()) == {end_pct}
    assert room.group.state == end_state
    assert room.group.attributes["current_position"] == end_pct
    assert room.group.attributes["aligned"] is True


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_retarget_to_where_the_shades_are(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(), pico, start_pct=100)

    await room.command("close_cover")
    await room.run(10)  # hemlines at 64: low-sill shade at 72%
    await room.command("set_cover_position", position=72)
    await room.run_until_still()

    # Both stop here rather than carrying on to closed.
    assert room.positions_pct_by_id() == {HIGH_SILL: 66, LOW_SILL: 72}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.attributes["current_position"] == 72


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_shades_already_there_are_sent_their_position_again(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, pico: bool
) -> None:
    # Like a shade's own set_position, which re-seats a shade even when it
    # reports being there already: a way to realign shades that aren't.
    room = await build_room(hass, freezer, same_tops(), pico, start_pct=100)

    await room.command("open_cover")
    await room.run_until_still()

    assert all(shade.commanded() == [100] for shade in room.shades.values())
    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}
    if pico:
        assert all(button.presses == 0 for button in room.pico.values())


async def test_pico_also_resends_a_shade_already_there(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # left_1 is already closed. The Pico's Off closes left_2 and sends left_1
    # to closed again, so left_1 needs no command of its own.
    room = await build_room(
        hass,
        freezer,
        living_room_left(),
        start_pct={"cover.left_1": 0, "cover.left_2": 20},
    )

    await room.command("close_cover")
    await room.run_until_still()

    assert room.pico["close"].presses == 1
    assert all(shade.commanded() == [] for shade in room.shades.values())
    assert set(room.positions_pct_by_id().values()) == {0}


REALIGN_BUTTON = "button.living_room_realign"


async def test_shades_starting_to_move_are_commanded_first(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # The bridge takes commands one at a time; high_sill, already open, is
    # only re-sent its position, so low_sill's command goes first.
    room = await build_room(
        hass, freezer, same_tops(), False, start_pct={HIGH_SILL: 100, LOW_SILL: 0}
    )
    commanded_at = dt_util.utcnow().timestamp()

    await room.command("open_cover")

    (started_at, _), *_ = room[LOW_SILL].starts()
    assert started_at - commanded_at == pytest.approx(room.bridge.latency_s, abs=0.01)


@pytest.mark.parametrize(
    "start_pct",
    [{HIGH_SILL: 20, LOW_SILL: 50}, {HIGH_SILL: 50, LOW_SILL: 58}],
    ids=["out-of-line", "level"],
)
@pytest.mark.parametrize("how", ["button", "set_position"])
async def test_realign(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    start_pct: dict[str, int],
    how: str,
) -> None:
    # Setting the group to its current position, or pressing its Realign
    # button (which does that): shades out of line meet at their average
    # height, and every shade is sent its position, re-seating any that have
    # drifted from what they report.
    room = await build_room(hass, freezer, same_tops(), False, start_pct=start_pct)
    current_pct = room.group.attributes["current_position"]

    if how == "button":
        await hass.services.async_call(
            "button", "press", {"entity_id": REALIGN_BUTTON}, blocking=True
        )
    else:
        await room.command("set_cover_position", position=current_pct)
    await room.run_until_still()

    assert room.group.attributes["aligned"] is True
    assert room.group.attributes["current_position"] == current_pct
    assert all(len(shade.commanded()) == 1 for shade in room.shades.values())


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_staggered_start_with_slow_commands(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(), pico)
    room.bridge.latency_s = 1.0

    await room.command("open_cover")
    await room.run_until_still()

    # Each shade starts a second after its command; the gap between them must
    # still be what the hemlines need (12 in at 2 in/s).
    low_start = room[LOW_SILL].starts()[0][0]
    high_start = room[HIGH_SILL].starts()[0][0]
    assert high_start - low_start == pytest.approx(6, abs=0.5)
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_retarget_while_a_staggered_start_is_pending(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(), pico)

    await room.command("open_cover")
    await room.run(3)  # low-sill shade moving; high-sill shade not started yet
    await room.command("set_cover_position", position=50)
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 40, LOW_SILL: 50}
    # The original start (to 100%) never fired; only the new one did.
    assert [target for _, target in room[HIGH_SILL].starts()] == [40]
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_new_command_while_the_first_commands_are_in_flight(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), pico=False)
    room.bridge.gate = asyncio.Event()

    # Opening: the low-sill shade's command is held up at the bridge.
    opening = hass.async_create_task(room.command("open_cover"))
    for _ in range(5):
        await asyncio.sleep(0)
    closing = hass.async_create_task(room.command("close_cover"))
    for _ in range(5):
        await asyncio.sleep(0)
    room.bridge.gate.set()
    await opening
    await closing
    await room.run_until_still()

    # The opening plan's delayed start for the high-sill shade must not fire.
    assert room[HIGH_SILL].starts() == []
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}


async def test_failed_command(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops())
    room.bridge.fail = True

    with pytest.raises(HomeAssistantError):
        await room.command("open_cover")
    assert room.group.state == "closed"

    # Nothing is moving, so a stop mustn't press the Pico (that would send
    # the shades to their favorite position).
    room.bridge.fail = False
    await room.command("stop_cover")
    await room.run(10)
    assert room.pico["stop"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}


async def test_motion_ends_at_the_planned_end(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Configured slower than the shades really are. They report their
    # destination immediately, so the group can't tell they arrived early.
    room = await build_room(
        hass, freezer, same_tops(), configured_travel_time_s=45, start_pct=100
    )

    await room.command("close_cover")
    await room.run(40)  # shades closed at 36 s
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.group.state == "closing"

    await room.run(6)  # planned end 45 s
    assert room.group.state == "closed"
    await room.command("stop_cover")
    await room.run(10)
    assert room.pico["stop"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}


async def test_outside_command_takes_over(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = await build_room(hass, freezer, same_tops(), pico=False)

    await room.command("open_cover")
    await room.run(3)  # high-sill shade's start still pending
    with caplog.at_level(logging.INFO):
        # Someone else sends the low-sill shade somewhere.
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": LOW_SILL, "position": 20},
            blocking=True,
        )
    assert "another command took over" in caplog.text
    assert room.group.state != "opening"

    await room.run(20)
    assert room[HIGH_SILL].starts() == []
    assert room.positions_pct_by_id()[LOW_SILL] == 20


async def test_physical_pico_stop_takes_over(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), start_pct=100)

    await room.command("close_cover")
    await room.run(10)
    # Someone presses Stop on the Pico itself.
    await hass.services.async_call(
        "button", "press", {"entity_id": room.pico["stop"].entity_id}, blocking=True
    )
    await room.run(1)

    assert room.group.state not in ("opening", "closing")
    assert room.positions_pct_by_id() == {HIGH_SILL: 66, LOW_SILL: 72}


async def test_diagnostics_mid_run(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_client: ClientSessionGenerator,
) -> None:
    assert await async_setup_component(hass, "diagnostics", {})
    room = await build_room(hass, freezer, same_tops())

    await room.command("open_cover")
    await room.run(3)  # low-sill shade moving; high-sill shade not started yet
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, room.entry)

    assert diagnostics["title"] == "Living Room"
    # The Pico is chosen by device, and stored as the buttons found on it.
    assert diagnostics["data"]["controls"] == [
        {
            "type": "pico",
            "shades": [HIGH_SILL, LOW_SILL],
            "open": "button.pico_open",
            "stop": "button.pico_stop",
            "close": "button.pico_close",
        }
    ]
    group = diagnostics["group"]
    # The Pico's buttons, found from its device.
    assert group["controls"] == [
        {
            "type": "pico",
            "shades": [HIGH_SILL, LOW_SILL],
            "open": "button.pico_open",
            "stop": "button.pico_stop",
            "close": "button.pico_close",
        }
    ]
    assert group["state"] == "opening"
    assert group["moving"] is True
    shades = {shade["entity_id"]: shade for shade in group["shades"]}
    assert shades[LOW_SILL]["travel_time_s"] == 36
    assert shades[HIGH_SILL]["hemline_height"] == 24
    moves = {move["entity_id"]: move for move in group["moves"]}
    assert moves[LOW_SILL]["estimated_pct"] == 8  # 6 in of 72 after 3 s
    assert moves[HIGH_SILL]["delay_s"] == pytest.approx(6)
    # Started by their own commands: the shades don't start level for the Pico.
    assert moves[HIGH_SILL]["starter"] is None
    assert moves[HIGH_SILL]["estimated_pct"] == 0

    # The device page offers the same download.
    device_id = er.async_get(hass).async_get(GROUP).device_id
    device = dr.async_get(hass).async_get(device_id)
    assert await get_diagnostics_for_device(hass, hass_client, room.entry, device) == (
        await get_diagnostics_for_config_entry(hass, hass_client, room.entry)
    )


async def test_diagnostics_count_commands_and_runs(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    hass_client: ClientSessionGenerator,
) -> None:
    assert await async_setup_component(hass, "diagnostics", {})
    room = await build_room(hass, freezer, same_tops(), start_pct=100)

    await room.command("close_cover")  # level: a Pico press
    await room.run(5)
    await room.command("set_cover_position", position=50)  # on down: commands
    await room.run(2)
    await room.command("open_cover")  # turned around: Pico Stop, then Open
    await room.run(2)
    await room.command("stop_cover")
    await room.run_until_still()
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, room.entry)

    counts = diagnostics["group"]["counts"]
    assert counts["runs"] == {
        "started": 3,
        "with_pico_or_scene": 2,
        "ended": {"replaced": 2, "stopped": 1},
    }
    # What the group sent matches what the room received.
    calls = counts["service_calls"]
    for button in room.pico.values():
        assert calls.get(button.entity_id, {}).get("press", 0) == button.presses
    for entity_id, shade in room.shades.items():
        sent = calls.get(entity_id, {}).get("set_cover_position", 0)
        assert sent == len(shade.commanded())
    assert room.pico["stop"].presses == 2


async def test_roller_measurements_match_the_simulation() -> None:
    left_1, left_2 = living_room_left()
    measured = {
        0: (49.75, 17.875),
        25: (67, 41.375),
        50: (85.125, 67.125),
        75: (104.75, 95),
        100: (125.125, 125.125),
    }
    for position_pct, (left_1_height, left_2_height) in measured.items():
        assert left_1.hemline_at_position(position_pct) == pytest.approx(
            left_1_height, abs=0.4
        )
        assert left_2.hemline_at_position(position_pct) == pytest.approx(
            left_2_height, abs=0.1
        )


@pytest.mark.parametrize("target_pct", [25, 50, 75])
async def test_rollers_level_at_rest(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, target_pct: int
) -> None:
    room = await build_room(
        hass, freezer, living_room_left(), pico=False, start_pct=100
    )

    await room.command("set_cover_position", position=target_pct)
    await room.run_until_still()

    final = {eid: shade.position_pct for eid, shade in room.shades.items()}
    assert room.misalignment(final) <= HEIGHT_TOLERANCE
    assert room.group.attributes["aligned"] is True


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_rollers_stay_level_while_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, pico: bool
) -> None:
    room = await build_room(hass, freezer, living_room_left(), pico)

    await room.command("open_cover")
    await room.run_until_still()
    await room.command("set_cover_position", position=40)
    await room.run_until_still()
    await room.command("close_cover")
    await room.run_until_still()

    assert room.positions_pct_by_id() == {"cover.left_1": 0, "cover.left_2": 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_living_room_all_stays_level(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, living_room_all())
    tall = ["cover.left_2", "cover.right_3", "cover.right_4", "cover.right_5"]

    # Closed, left_1 sits higher than the other four, so the Pico (which
    # would start all five) isn't used: the four start together, and left_1
    # when they reach it.
    await room.command("open_cover")
    await room.run_until_still()
    assert room.pico["open"].presses == 0
    starts = {eid: room[eid].starts()[0][0] for eid in [*tall, "cover.left_1"]}
    assert started_together(room, tall)
    assert starts["cover.left_1"] - starts["cover.left_2"] == pytest.approx(8, abs=0.5)

    # Level at the top, so the Pico starts everything from here on.
    await room.command("set_cover_position", position=40)
    await room.run_until_still()
    await room.command("close_cover")
    await room.run_until_still()
    assert room.pico["close"].presses == 2

    assert set(room.positions_pct_by_id().values()) == {0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_living_room_all_stops_level(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, living_room_all(), start_pct=100)

    await room.command("close_cover")
    await room.run(10)
    await room.command("stop_cover")
    await room.run_until_still()

    assert room.pico["close"].presses == 1
    assert room.pico["stop"].presses == 1
    assert all(0 < pct < 100 for pct in room.positions_pct_by_id().values())
    assert room.group.attributes["aligned"] is True
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


# A Lutron scene opening the four identical shades (all but left_1).
IDENTICAL = ["cover.left_2", "cover.right_3", "cover.right_4", "cover.right_5"]
OPEN_IDENTICAL = "scene.lutron_bridge_open_identical_shades"


async def build_living_room_all_with_scene(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> Room:
    return await build_room(
        hass,
        freezer,
        living_room_all(),
        scenes={OPEN_IDENTICAL.removeprefix("scene."): dict.fromkeys(IDENTICAL, 100)},
    )


async def test_living_room_all_opens_with_the_scene(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_living_room_all_with_scene(hass, freezer)

    # Closed, the four identical shades are level but left_1 isn't, so the
    # five-shade Pico can't be used. The scene starts the four at one instant,
    # with no commands of their own, and left_1 joins when they reach it.
    await room.command("open_cover")
    await room.run_until_still()

    assert room.scenes[OPEN_IDENTICAL].activations == 1
    assert room.pico["open"].presses == 0
    assert len({room[eid].starts()[0][0] for eid in IDENTICAL}) == 1
    assert all(room[eid].commanded() == [] for eid in IDENTICAL)
    left_1_delay = (
        room["cover.left_1"].starts()[0][0] - room["cover.left_2"].starts()[0][0]
    )
    assert left_1_delay == pytest.approx(8, abs=0.5)
    assert set(room.positions_pct_by_id().values()) == {100}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_living_room_all_goes_to_half_with_the_scene(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_living_room_all_with_scene(hass, freezer)

    # The scene still starts the four at once, though it sends them to 100%:
    # each is then sent 50% straight away.
    await room.command("set_cover_position", position=50)
    await room.run_until_still()

    assert room.scenes[OPEN_IDENTICAL].activations == 1
    assert len({room[eid].starts()[0][0] for eid in IDENTICAL}) == 1
    assert all(room[eid].commanded() == [50] for eid in IDENTICAL)
    left_1_delay = (
        room["cover.left_1"].starts()[0][0] - room["cover.left_2"].starts()[0][0]
    )
    assert left_1_delay == pytest.approx(8, abs=0.5)
    positions = room.positions_pct_by_id()
    assert {positions[eid] for eid in IDENTICAL} == {50}
    assert 0 < positions["cover.left_1"] < 50
    assert room.group.attributes["current_position"] == 50
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_level_shades_start_together_without_a_pico(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # At 62% and 75% the two left shades are level to within 0.4 in: what
    # the group calls aligned, and would start with one Pico press. Without a
    # Pico they should still start together, not a fraction of a second apart.
    room = await build_room(
        hass,
        freezer,
        living_room_left(),
        pico=False,
        start_pct={"cover.left_1": 62, "cover.left_2": 75},
    )
    assert room.group.attributes["aligned"] is True

    await room.command("close_cover")
    await room.run_until_still()

    assert started_together(room, ["cover.left_1", "cover.left_2"])
    assert room.positions_pct_by_id() == {"cover.left_1": 0, "cover.left_2": 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_leader_is_commanded_at_once(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Seen on the two left shades: converting the leader's hemline back to
    # a position left a tiny delay, so its command went through a timer.
    room = await build_room(
        hass,
        freezer,
        living_room_left(),
        pico=False,
        start_pct={"cover.left_1": 56, "cover.left_2": 66},
    )

    await room.command("close_cover")

    assert room["cover.left_1"].starts(), "the leader waited for a timer"


async def test_level_followers_start_together(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # "a" and "b" sit level (hemlines 50 and 50.4 in) below "lead" (80 in).
    # Closing, they're followers, and start together when "lead" reaches them.
    # All move at 2 in/s.
    room = await build_room(
        hass,
        freezer,
        [
            ShadeSpec.even(
                name="lead",
                closed_height=20,
                open_height=80,
                travel_time_s=30,
            ),
            ShadeSpec.even(
                name="a",
                closed_height=10,
                open_height=60,
                travel_time_s=25,
            ),
            ShadeSpec.even(
                name="b",
                closed_height=30,
                open_height=70,
                travel_time_s=20,
            ),
        ],
        pico=False,
        start_pct={"cover.lead": 100, "cover.a": 80, "cover.b": 51},
    )

    await room.command("close_cover")
    await room.run(16)  # "lead" has reached the others, who have joined it
    room.history.clear()
    await room.run_until_still()

    lead_start = room["cover.lead"].starts()[0][0]
    a_start = room["cover.a"].starts()[0][0]
    assert started_together(room, ["cover.a", "cover.b"])
    assert a_start - lead_start == pytest.approx(15, abs=0.5)  # 30 in at 2 in/s
    # Level from when they joined until each reached its sill.
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_position_reports_the_target_while_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Both at 50%, so misaligned; the lower one starts first and the other
    # joins later. Dashboard sliders show the reported position, so reporting
    # progress (or a mix of where each shade is heading) would make a slider
    # jump back from where it was dropped.
    room = await build_room(hass, freezer, living_room_left(), pico=False, start_pct=50)

    await room.command("set_cover_position", position=80)
    reported = []
    while room.group.state == "opening":
        reported.append(room.group.attributes["current_position"])
        await room.run(STEP_S)

    assert len(reported) > 50
    assert set(reported) == {80}
    assert room.group.attributes["current_position"] == 80


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_stop_mid_run_reports_where_the_shades_stopped(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, pico: bool
) -> None:
    room = await build_room(hass, freezer, living_room_left(), pico, start_pct=100)

    await room.command("set_cover_position", position=20)
    await room.run(8)
    assert room.group.state == "closing"
    assert room.group.attributes["current_position"] == 20
    await room.command("stop_cover")
    await room.run_until_still()

    stopped = room.positions_pct_by_id()
    assert all(30 < pct < 100 for pct in stopped.values()), stopped
    assert room.group.state == "open"
    # The group spans the tallest shade's range, so it matches that shade.
    assert room.group.attributes["current_position"] == stopped["cover.left_2"]
    assert room.group.attributes["aligned"] is True
    # Shades report whole percents, so compare with the hemline at the
    # reported position (the attribute is rounded to 0.1).
    for entity_id, height in room.group.attributes["hemline_heights"].items():
        reported_height = room[entity_id].spec.hemline_at_position(stopped[entity_id])
        assert height == pytest.approx(reported_height, abs=0.06)
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


@pytest.mark.parametrize("target_pct", [25, 50, 75])
async def test_identical_rollers_follow_the_group_position(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, target_pct: int
) -> None:
    # Two identical rollers: the group's percentage should pass straight
    # through, with no remapping by height.
    room = await build_room(
        hass,
        freezer,
        [
            ShadeSpec(
                name=name,
                roll=LIVING_ROOM_ROLL,
                empty_height=LIVING_ROOM_EMPTY_HEIGHT,
                closed_turns=2.1702,
                open_turns=18.8592,
            )
            for name in ("a", "b")
        ],
        pico=False,
        start_pct=100,
    )

    await room.command("set_cover_position", position=target_pct)
    await room.run_until_still()

    assert room.positions_pct_by_id() == {"cover.a": target_pct, "cover.b": target_pct}
    assert room.group.attributes["current_position"] == target_pct


@pytest.mark.parametrize("target_pct", [25, 50, 75])
async def test_matched_rolls_beyond_the_tallest_level_at_rest(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, target_pct: int
) -> None:
    room = await build_room(hass, freezer, matched_rolls(), pico=False, start_pct=100)

    await room.command("set_cover_position", position=target_pct)
    await room.run_until_still()

    final = {eid: shade.position_pct for eid, shade in room.shades.items()}
    assert room.misalignment(final) <= HEIGHT_TOLERANCE
    assert room.group.attributes["aligned"] is True


async def test_matched_rolls_beyond_the_tallest_stay_level_while_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, matched_rolls(), pico=False)

    await room.command("open_cover")
    await room.run_until_still()
    await room.command("set_cover_position", position=40)
    await room.run_until_still()
    await room.command("close_cover")
    await room.run_until_still()

    assert set(room.positions_pct_by_id().values()) == {0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
