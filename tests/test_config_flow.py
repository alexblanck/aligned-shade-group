"""Setting up and editing a group through its forms.

Validation errors use bare fake shades; whole flows run in a simulated room
(sim.py), so the group they create or edit is real. How the group then moves
is tested in test_room.py.
"""

import copy
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant import config_entries
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.aligned_shade_group.const import (
    DOMAIN,
    missing_entities_issue_id,
)

from .common import HIGH_SILL, LOW_SILL
from .sim import (
    CONTROLS_MENUS,
    GROUP,
    HEIGHT_TOLERANCE,
    build_room,
    living_room_all,
    matched_rolls,
    prefilled_answers,
    same_tops,
)

FEATURES = (
    CoverEntityFeature.OPEN
    | CoverEntityFeature.CLOSE
    | CoverEntityFeature.STOP
    | CoverEntityFeature.SET_POSITION
)


def set_shade(hass: HomeAssistant, entity_id: str, position_pct: int) -> None:
    """Set a bare fake shade's state, for validation tests."""
    hass.states.async_set(
        entity_id,
        "closed" if position_pct == 0 else "open",
        {"current_position": position_pct, ATTR_SUPPORTED_FEATURES: FEATURES},
    )


async def start_flow(hass: HomeAssistant) -> dict[str, Any]:
    set_shade(hass, HIGH_SILL, 0)
    set_shade(hass, LOW_SILL, 0)
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def submit_group(hass: HomeAssistant, flow: dict[str, Any]) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "x", "shades": [HIGH_SILL, LOW_SILL]}
    )


async def reach_controls(hass: HomeAssistant) -> dict[str, Any]:
    """Set up the two shades, through to the Picos and scenes menu."""
    configure = hass.config_entries.flow.async_configure
    result = await submit_group(hass, await start_flow(hass))
    for heights in ({"open_height": 84, "closed_height": 24}, {"closed_height": 12}):
        result = await configure(result["flow_id"], {"open_height": 84, **heights})
    result = await configure(
        result["flow_id"], {"travel_time_s": 36, "roller_curve": {}}
    )
    assert result["step_id"] in CONTROLS_MENUS
    return result


async def choose(
    hass: HomeAssistant, result: dict[str, Any], option: str
) -> dict[str, Any]:
    """Pick an option on a menu screen."""
    assert result["type"] == "menu", result
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": option}
    )


def add_pico(
    hass: HomeAssistant, buttons: list[str], disabled: tuple[str, ...] = ()
) -> str:
    """Register a Caseta Pico device with these buttons; returns its device id."""
    entry = MockConfigEntry(domain="lutron_caseta")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("lutron_caseta", "pico")},
        name="Living Room Pico",
    )
    for name in buttons:
        er.async_get(hass).async_get_or_create(
            "button",
            "lutron_caseta",
            f"pico_{name}",
            config_entry=entry,
            device_id=device.id,
            original_name=f"Living Room Pico {name}",
            disabled_by=(
                er.RegistryEntryDisabler.INTEGRATION if name in disabled else None
            ),
        )
    return device.id


async def test_too_few_shades(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "x", "shades": [HIGH_SILL]}
    )
    assert result["errors"] == {"base": "too_few_shades"}


async def add_pico_answers(
    hass: HomeAssistant, result: dict[str, Any], device_id: str, shades: list[str]
) -> dict[str, Any]:
    result = await choose(hass, result, "add_pico")
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"device_id": device_id, "shades": shades}
    )


async def test_pico_needs_on_stop_and_off_buttons(hass: HomeAssistant) -> None:
    result = await reach_controls(hass)
    # A two-button Pico can't stop shades.
    pico = add_pico(hass, ["On", "Off"])
    result = await add_pico_answers(hass, result, pico, [HIGH_SILL])
    assert result["errors"] == {"base": "pico_not_a_shade_pico"}


async def test_pico_buttons_must_be_enabled(hass: HomeAssistant) -> None:
    result = await reach_controls(hass)
    shade_pico = ["On", "Stop", "Off", "Raise", "Lower"]
    pico = add_pico(hass, shade_pico, disabled=("Stop",))
    result = await add_pico_answers(hass, result, pico, [HIGH_SILL])
    assert result["errors"] == {"base": "pico_buttons_disabled"}


async def test_picos_and_scenes_are_stored_with_their_shades(
    hass: HomeAssistant,
) -> None:
    result = await reach_controls(hass)
    # A Pico paired to the low-sill shade only, and a scene opening both.
    pico = add_pico(hass, ["On", "Stop", "Off", "Raise", "Lower"])
    result = await add_pico_answers(hass, result, pico, [LOW_SILL])
    assert result["step_id"] in CONTROLS_MENUS
    assert "Living Room Pico" in result["description_placeholders"]["controls"]
    result = await choose(hass, result, "add_scene")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"scene": "scene.open_both", "shades": [HIGH_SILL, LOW_SILL], "position": 100},
    )
    # Listed on the menu: each in bold, a scene's position once, then its shades.
    assert result["description_placeholders"]["controls"] == (
        "\n\n**Living Room Pico**\n- cover.low_sill"
        "\n\n**scene.open_both** → 100%\n- cover.high_sill\n- cover.low_sill"
    )
    result = await choose(hass, result, "save")

    assert result["type"] == "create_entry"
    button = er.async_get(hass).async_get_entity_id
    assert result["data"]["controls"] == [
        {
            "type": "pico",
            "shades": [LOW_SILL],
            "open": button("button", "lutron_caseta", "pico_On"),
            "stop": button("button", "lutron_caseta", "pico_Stop"),
            "close": button("button", "lutron_caseta", "pico_Off"),
        },
        {
            "type": "scene",
            "entity_id": "scene.open_both",
            "positions": {HIGH_SILL: 100, LOW_SILL: 100},
        },
    ]


async def test_picos_and_scenes_can_only_be_added_once(hass: HomeAssistant) -> None:
    result = await reach_controls(hass)
    pico = add_pico(hass, ["On", "Stop", "Off", "Raise", "Lower"])
    result = await add_pico_answers(hass, result, pico, [LOW_SILL])
    result = await add_pico_answers(hass, result, pico, [HIGH_SILL])
    assert result["errors"] == {"base": "control_already_added"}

    result = await reach_controls(hass)
    scene = {"scene": "scene.open_both", "shades": [HIGH_SILL], "position": 100}
    configure = hass.config_entries.flow.async_configure
    result = await configure(
        (await choose(hass, result, "add_scene"))["flow_id"], scene
    )
    result = await configure(
        (await choose(hass, result, "add_scene"))["flow_id"], scene
    )
    assert result["errors"] == {"base": "control_already_added"}


async def test_picos_and_scenes_can_be_changed_or_removed_one_at_a_time(
    hass: HomeAssistant,
) -> None:
    result = await reach_controls(hass)
    pico = add_pico(hass, ["On", "Stop", "Off", "Raise", "Lower"])
    result = await add_pico_answers(hass, result, pico, [LOW_SILL])
    result = await choose(hass, result, "add_scene")
    configure = hass.config_entries.flow.async_configure
    result = await configure(
        result["flow_id"],
        {"scene": "scene.open_both", "shades": [HIGH_SILL, LOW_SILL], "position": 100},
    )

    # Change the scene: its form opens with its current settings.
    result = await choose(hass, result, "change_control")
    result = await configure(result["flow_id"], {"control": "1"})
    assert result["step_id"] == "change_scene"
    assert prefilled_answers(result) == {
        "scene": "scene.open_both",
        "shades": [HIGH_SILL, LOW_SILL],
        "position": 100,
    }
    result = await configure(
        result["flow_id"],
        {"scene": "scene.open_both", "shades": [HIGH_SILL], "position": 50},
    )

    # The Pico's form opens with its device and shades.
    result = await choose(hass, result, "change_control")
    result = await configure(result["flow_id"], {"control": "0"})
    assert result["step_id"] == "change_pico"
    assert prefilled_answers(result) == {"device_id": pico, "shades": [LOW_SILL]}
    result = await configure(result["flow_id"], prefilled_answers(result))

    # Remove just the Pico.
    result = await choose(hass, result, "remove_controls")
    result = await configure(result["flow_id"], {"controls": ["0"]})
    result = await choose(hass, result, "save")

    assert result["data"]["controls"] == [
        {
            "type": "scene",
            "entity_id": "scene.open_both",
            "positions": {HIGH_SILL: 50},
        }
    ]


async def test_menu_suggests_shades_that_start_level_together(
    hass: HomeAssistant,
) -> None:
    # Both shades open at 84 but close at different heights: all of them start
    # level whenever the group leaves a level position (or open), so a Pico or
    # scene for both is suggested in each direction until one covers them.
    result = await reach_controls(hass)
    assert result["step_id"] == "controls"
    assert result["description_placeholders"]["suggestions"] == (
        f"\n- ↑ {HIGH_SILL}, {LOW_SILL}\n- ↓ {HIGH_SILL}, {LOW_SILL}"
    )

    # A scene opening both covers the first; a Pico for both covers both.
    result = await choose(hass, result, "add_scene")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"scene": "scene.open_both", "shades": [HIGH_SILL, LOW_SILL], "position": 100},
    )
    assert result["step_id"] == "controls_configured_suggested"
    assert result["description_placeholders"]["suggestions"] == (
        f"\n- ↓ {HIGH_SILL}, {LOW_SILL}"
    )
    pico = add_pico(hass, ["On", "Stop", "Off", "Raise", "Lower"])
    result = await add_pico_answers(hass, result, pico, [HIGH_SILL, LOW_SILL])
    assert result["step_id"] == "controls_configured"


async def test_living_room_all_suggests_opening_the_four_identical_shades(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # The five-shade Pico covers all five both ways; the four that close at
    # the same height (all but left_1) start level opening from closed, and
    # nothing starts just them yet.
    room = await build_room(hass, freezer, living_room_all())
    flow = await room.start_reconfigure()
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], prefilled_answers(flow)
    )
    flow = await room.answer_shade_steps(flow)

    assert flow["step_id"] == "controls_configured_suggested"
    assert flow["description_placeholders"]["suggestions"] == (
        "\n- ↑ left_2, right_3, right_4, right_5"
    )


async def test_scene_with_different_positions_cannot_be_changed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # The form sets one position for all of a scene's shades, so changing one
    # that sets them differently would flatten them; it's refused instead.
    room = await build_room(hass, freezer, same_tops(), pico=False)
    scene = {
        "type": "scene",
        "entity_id": "scene.staggered",
        "positions": {HIGH_SILL: 30, LOW_SILL: 50},
    }
    hass.config_entries.async_update_entry(
        room.entry, data={**room.entry.data, "controls": [scene]}
    )
    flow = await room.start_reconfigure()
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], prefilled_answers(flow)
    )
    flow = await room.answer_shade_steps(flow)

    flow = await room.choose(flow, "change_control")
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"control": "0"}
    )
    assert flow["step_id"] == "change_control"
    assert flow["errors"] == {"base": "scene_positions_differ"}


async def test_leaving_the_pico_or_scene_empty_goes_back(
    hass: HomeAssistant,
) -> None:
    configure = hass.config_entries.flow.async_configure
    for option in ("add_pico", "add_scene"):
        result = await choose(hass, await reach_controls(hass), option)
        result = await configure(result["flow_id"], {"shades": [HIGH_SILL]})
        assert result["step_id"] == "controls", option
        assert result["description_placeholders"]["controls"] == ""


async def test_removing_or_changing_nothing_goes_back(hass: HomeAssistant) -> None:
    result = await reach_controls(hass)
    pico = add_pico(hass, ["On", "Stop", "Off", "Raise", "Lower"])
    result = await add_pico_answers(hass, result, pico, [LOW_SILL])
    result = await choose(hass, result, "remove_controls")
    # Nothing ticked: the form sends no value at all.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] in CONTROLS_MENUS
    assert "Living Room Pico" in result["description_placeholders"]["controls"]

    # Likewise choosing nothing to change.
    result = await choose(hass, result, "change_control")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] in CONTROLS_MENUS


async def test_controls_need_shades_and_a_scene(hass: HomeAssistant) -> None:
    result = await reach_controls(hass)
    result = await choose(hass, result, "add_scene")
    configure = hass.config_entries.flow.async_configure
    answers = {"scene": "scene.open_both", "shades": [], "position": 100}
    result = await configure(result["flow_id"], answers)
    assert result["errors"] == {"base": "no_control_shades"}
    answers = {**answers, "scene": "light.lamp", "shades": [HIGH_SILL]}
    result = await configure(result["flow_id"], answers)
    assert result["errors"] == {"base": "not_a_scene"}


async def test_covers_must_set_position_and_stop(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    hass.states.async_set(
        HIGH_SILL, "open", {"supported_features": CoverEntityFeature.SET_POSITION}
    )
    result = await submit_group(hass, flow)
    assert result["errors"] == {"base": "cover_unsupported"}


async def test_shades_must_be_covers(hass: HomeAssistant) -> None:
    # The picker only offers covers, but submitted values aren't filtered.
    flow = await start_flow(hass)
    hass.states.async_set("light.lamp", "on", {"supported_features": 255})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"name": "x", "shades": [HIGH_SILL, "light.lamp"]},
    )
    assert result["errors"] == {"base": "cover_unsupported"}


async def test_unavailable_cover_checked_through_the_registry(
    hass: HomeAssistant,
) -> None:
    entry = er.async_get(hass).async_get_or_create(
        "cover",
        "test",
        "high_sill",
        suggested_object_id="high_sill",
        supported_features=CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE,
    )
    assert entry.entity_id == HIGH_SILL
    flow = await start_flow(hass)
    hass.states.async_set(HIGH_SILL, "unavailable")
    result = await submit_group(hass, flow)
    assert result["errors"] == {"base": "cover_unsupported"}


async def test_shades_are_prefilled_from_the_previous_one(
    hass: HomeAssistant,
) -> None:
    result = await submit_group(hass, await start_flow(hass))
    assert result["step_id"] == "shade"
    assert prefilled_answers(result) == {}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 84, "closed_height": 24}
    )

    # Shade 2 starts with shade 1's heights, and its description says so.
    assert result["step_id"] == "shade_prefilled"
    assert prefilled_answers(result) == {"open_height": 84, "closed_height": 24}


async def test_shade_heights_validated(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await submit_group(hass, flow)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 10, "closed_height": 20}
    )
    assert result["errors"] == {"base": "closed_not_below_open"}
    assert result["description_placeholders"]["index"] == "1"


async def test_shade_ranges_must_overlap(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await submit_group(hass, flow)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 40, "closed_height": 10}
    )
    # Stacked rather than side by side: no height is in both ranges.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 80, "closed_height": 50}
    )
    assert result["errors"] == {"base": "ranges_do_not_overlap"}
    assert result["description_placeholders"]["index"] == "2"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 80, "closed_height": 20}
    )
    assert result["step_id"] == "travel"


async def test_halfway_height_must_fit_a_roller(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await submit_group(hass, flow)
    for heights in (
        {"closed_height": 24, "open_height": 84},
        {"closed_height": 12, "open_height": 84},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], heights
        )
    assert result["step_id"] == "travel"
    # Tallest shade: 12 to 84, so 50% must be above 30 and at most the midpoint, 48.
    assert result["description_placeholders"]["low"] == "30"
    assert result["description_placeholders"]["high"] == "48"

    for too_far in (50, 30):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"travel_time_s": 36, "roller_curve": {"halfway_height": too_far}},
        )
        assert result["errors"] == {"base": "halfway_out_of_range"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"travel_time_s": 36, "roller_curve": {"halfway_height": 44}}
    )
    result = await choose(hass, result, "save")
    assert result["type"] == "create_entry"
    # Stored with the shade they were measured on: the tallest.
    assert result["data"] == {
        "shades": [
            {"entity_id": HIGH_SILL, "closed_height": 24, "open_height": 84},
            {
                "entity_id": LOW_SILL,
                "closed_height": 12,
                "open_height": 84,
                "travel_time_s": 36,
                "halfway_height": 44,
            },
        ],
        "controls": [],
    }


async def configure_shades(
    hass: HomeAssistant, heights: list[tuple[float, float]]
) -> dict[str, Any]:
    """Set up two shades with these (closed, open) heights; returns the travel step."""
    result = await submit_group(hass, await start_flow(hass))
    for closed, opened in heights:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"closed_height": closed, "open_height": opened}
        )
    assert result["step_id"] == "travel"
    return result


async def test_halfway_height_extends_to_shades_above_the_tallest(
    hass: HomeAssistant,
) -> None:
    # The taller shade (20 to 80) doesn't reach the other's top (90); its
    # curve is extended up to it.
    result = await configure_shades(hass, [(50, 90), (20, 80)])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"travel_time_s": 30, "roller_curve": {"halfway_height": 48}}
    )
    assert result["step_id"] in CONTROLS_MENUS


async def test_halfway_height_whose_curve_cannot_reach_every_shade(
    hass: HomeAssistant,
) -> None:
    # A strong curve on the taller shade (40 to 100) flattens out at 37.5,
    # as if the roll ran out of fabric, short of the other shade's 30.
    result = await configure_shades(hass, [(40, 100), (30, 70)])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"travel_time_s": 30, "roller_curve": {"halfway_height": 60}}
    )
    assert result["errors"] == {"base": "halfway_cant_reach"}


async def test_reconfigure_applies_to_running_group(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), start_pct=100)

    # Rename the group and remove its Pico; the group reloads with both.
    flow = await room.start_reconfigure()
    assert flow["step_id"] == "reconfigure"
    assert flow["description_placeholders"]["name"] == "Living Room"
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "Den", "shades": [HIGH_SILL, LOW_SILL]}
    )
    flow = await room.answer_shade_steps(flow)
    # The saved Pico is listed; remove it.
    assert "Pico" in flow["description_placeholders"]["controls"]
    flow = await room.remove_all_controls(flow)
    flow = await room.choose(flow, "save")
    assert flow["type"] == "abort", flow
    assert flow["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert room.entry.title == "Den"
    assert room.entry.data["controls"] == []
    assert room.group.attributes["friendly_name"] == "Den"

    await room.command("close_cover")
    await room.run_until_still()
    assert room.pico["close"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_shade_picker_offers_only_shades(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    await build_room(hass, freezer, same_tops())

    # Starting a second group: its picker leaves out the first group, and
    # covers that aren't shades (such as garage doors).
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    config = flow["data_schema"].schema["shades"].config
    assert config["exclude_entities"] == [GROUP]
    (shade_filter,) = config["filter"]
    assert shade_filter["device_class"] == ["shade", "blind", "shutter"]


async def test_reconfigure_fixes_renamed_pico_buttons(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(), start_pct=100)
    assert room.entry is not None
    issue_id = missing_entities_issue_id(room.entry.entry_id)
    er.async_get(hass).async_update_entity(
        "button.pico_stop", new_entity_id="button.pico_stop_renamed"
    )
    await hass.async_block_till_done()
    await room.command("close_cover")  # notices the missing button
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # Adding the same Pico again finds its buttons under their new names.
    flow = await room.start_reconfigure()
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], prefilled_answers(flow)
    )
    flow = await room.answer_shade_steps(flow)
    flow = await room.remove_all_controls(flow)
    flow = await room.add_controls(flow)
    flow = await room.choose(flow, "save")
    await hass.async_block_till_done()

    assert flow["reason"] == "reconfigure_successful", flow
    (pico,) = room.entry.data["controls"]
    assert pico["stop"] == "button.pico_stop_renamed"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_reconfigure_mid_run(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops())

    await room.command("open_cover")
    await room.run(3)  # high-sill shade's start still pending
    flow = await room.start_reconfigure()
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"name": "Living Room", "shades": [HIGH_SILL, LOW_SILL]},
    )
    flow = await room.answer_shade_steps(flow)
    flow = await room.choose(flow, "save")
    await hass.async_block_till_done()
    await room.run(20)

    # The reloaded group forgot the old plan, including its pending start.
    assert room[HIGH_SILL].starts() == []
    assert room.group.state != "opening"


async def test_group_has_its_own_device(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops())

    entity = er.async_get(hass).async_get("cover.living_room")
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device.name == "Living Room"
    assert device.entry_type is None
    assert device.config_entry_id == room.entry.entry_id


async def test_reconfigure_changes_only_what_was_edited(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Three shades with a curve and a Pico, then Reconfigure changing only the
    # name and accepting every other prefilled answer as it is.
    room = await build_room(hass, freezer, matched_rolls(), pico=True, start_pct=50)
    # Deep-copied in case a change ever edits the stored data in place.
    saved = copy.deepcopy(dict(room.entry.data))

    flow = await room.start_reconfigure()
    configure = hass.config_entries.flow.async_configure
    flow = await configure(flow["flow_id"], {**prefilled_answers(flow), "name": "Den"})
    while flow["type"] == "form":
        flow = await configure(flow["flow_id"], prefilled_answers(flow))
    flow = await room.choose(flow, "save")  # keeping the saved Pico
    await hass.async_block_till_done()

    assert flow["reason"] == "reconfigure_successful", flow
    assert room.entry.title == "Den"
    assert room.entry.data == saved
    assert room.entry.data["controls"], "the Pico was kept"
