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
    get_schema_suggested_value,
)

from custom_components.aligned_shade_group.const import (
    DOMAIN,
    missing_entities_issue_id,
)

from .common import HIGH_SILL, LOW_SILL
from .sim import (
    GROUP,
    HEIGHT_TOLERANCE,
    build_room,
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


async def submit_group(
    hass: HomeAssistant, flow: dict[str, Any], **pico: str
) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"name": "x", "shades": [HIGH_SILL, LOW_SILL], "pico": pico},
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


async def test_pico_section_starts_collapsed(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    assert flow["data_schema"].schema["pico"].options["collapsed"] is True


async def test_too_few_shades(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "x", "shades": [HIGH_SILL], "pico": {}}
    )
    assert result["errors"] == {"base": "too_few_shades"}


async def test_pico_needs_on_stop_and_off_buttons(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    # A two-button Pico can't stop shades.
    result = await submit_group(hass, flow, device_id=add_pico(hass, ["On", "Off"]))
    assert result["errors"] == {"base": "pico_not_a_shade_pico"}


async def test_pico_buttons_must_be_enabled(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    shade_pico = ["On", "Stop", "Off", "Raise", "Lower"]
    result = await submit_group(
        hass, flow, device_id=add_pico(hass, shade_pico, disabled=("Stop",))
    )
    assert result["errors"] == {"base": "pico_buttons_disabled"}


async def test_shade_pico_is_accepted(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    shade_pico = ["On", "Stop", "Off", "Raise", "Lower"]
    result = await submit_group(hass, flow, device_id=add_pico(hass, shade_pico))
    assert result["step_id"] == "shade"


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
        {"name": "x", "shades": [HIGH_SILL, "light.lamp"], "pico": {}},
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
    assert result["type"] == "create_entry"


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
    room = await build_room(hass, freezer, same_tops(100))

    # Rename the group and remove its Pico; the group reloads with both.
    flow = await room.start_reconfigure()
    assert flow["step_id"] == "reconfigure"
    assert flow["description_placeholders"]["name"] == "Living Room"
    # The Pico section opens expanded, since this group has one.
    pico_section = flow["data_schema"].schema["pico"]
    assert pico_section.options["collapsed"] is False
    # ...and suggests the Pico its stored buttons belong to.
    assert (
        get_schema_suggested_value(pico_section.schema.schema, "device_id")
        == room.pico_device_id()
    )
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"name": "Den", "shades": [HIGH_SILL, LOW_SILL], "pico": {}},
    )
    flow = await room.answer_shade_steps(flow)
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
    await build_room(hass, freezer, same_tops(0))

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
    room = await build_room(hass, freezer, same_tops(100))
    assert room.entry is not None
    issue_id = missing_entities_issue_id(room.entry.entry_id)
    er.async_get(hass).async_update_entity(
        "button.pico_stop", new_entity_id="button.pico_stop_renamed"
    )
    await hass.async_block_till_done()
    await room.command("close_cover")  # notices the missing button
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # Choosing the same Pico again finds its buttons under their new names.
    flow = await room.start_reconfigure()
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {**prefilled_answers(flow), "pico": {"device_id": room.pico_device_id()}},
    )
    flow = await room.answer_shade_steps(flow)
    await hass.async_block_till_done()

    assert flow["reason"] == "reconfigure_successful", flow
    (pico,) = room.entry.data["controls"]
    assert pico["stop"] == "button.pico_stop_renamed"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_reconfigure_mid_run(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0))

    await room.command("open_cover")
    await room.run(3)  # high-sill shade's start still pending
    flow = await room.start_reconfigure()
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"name": "Living Room", "shades": [HIGH_SILL, LOW_SILL], "pico": {}},
    )
    flow = await room.answer_shade_steps(flow)
    await hass.async_block_till_done()
    await room.run(20)

    # The reloaded group forgot the old plan, including its pending start.
    assert room[HIGH_SILL].starts == []
    assert room.group.state != "opening"


async def test_group_has_its_own_device(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0))

    entity = er.async_get(hass).async_get("cover.living_room")
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device.name == "Living Room"
    assert device.entry_type is None
    assert device.config_entries == {room.entry.entry_id}


async def test_reconfigure_changes_only_what_was_edited(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Three shades with a curve and a Pico, then Reconfigure changing only the
    # name and accepting every other prefilled answer as it is.
    room = await build_room(hass, freezer, matched_rolls(50), pico=True)
    # Deep-copied in case a change ever edits the stored data in place.
    saved = copy.deepcopy(dict(room.entry.data))

    flow = await room.start_reconfigure()
    configure = hass.config_entries.flow.async_configure
    flow = await configure(flow["flow_id"], {**prefilled_answers(flow), "name": "Den"})
    while flow["type"] == "form":
        flow = await configure(flow["flow_id"], prefilled_answers(flow))
    await hass.async_block_till_done()

    assert flow["reason"] == "reconfigure_successful", flow
    assert room.entry.title == "Den"
    assert room.entry.data == saved
    assert room.entry.data["controls"], "the Pico was kept"
