"""Config flow validation errors (the happy paths run in test_room.py)."""

from typing import Any

from homeassistant import config_entries
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aligned_shade_group.const import DOMAIN

from .common import HIGH_SILL, LOW_SILL, set_shade


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
            result["flow_id"], {"travel_time_s": 36, "halfway_height": too_far}
        )
        assert result["errors"] == {"base": "halfway_out_of_range"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"travel_time_s": 36, "halfway_height": 44}
    )
    assert result["type"] == "create_entry"
    # Stored with the shade they were measured on: the tallest.
    assert result["options"] == {
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
        result["flow_id"], {"travel_time_s": 30, "halfway_height": 48}
    )
    assert result["type"] == "create_entry"


async def test_halfway_height_whose_curve_cannot_reach_every_shade(
    hass: HomeAssistant,
) -> None:
    # A strong curve on the taller shade (40 to 100) flattens out at 37.5,
    # as if the roll ran out of fabric, short of the other shade's 30.
    result = await configure_shades(hass, [(40, 100), (30, 70)])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"travel_time_s": 30, "halfway_height": 60}
    )
    assert result["errors"] == {"base": "halfway_cant_reach"}
