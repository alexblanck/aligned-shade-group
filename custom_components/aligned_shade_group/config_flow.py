"""Setup and Reconfigure screens for Aligned Shade Group."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_SUPPORTED_FEATURES,
    CONF_ENTITY_ID,
    CONF_TYPE,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .alignment import ShadeConfig, matched_roll_group
from .const import (
    CONF_CLOSED_HEIGHT,
    CONF_CONTROLS,
    CONF_HALFWAY_HEIGHT,
    CONF_OPEN_HEIGHT,
    CONF_PICO_CLOSE,
    CONF_PICO_OPEN,
    CONF_PICO_STOP,
    CONF_SHADES,
    CONF_TRAVEL_TIME_S,
    DOMAIN,
    ControlType,
)
from .pico import PicoButtons, PicoButtonsError, find_pico_buttons
from .roll_profile import halfway_height_range

# Setup form fields that aren't stored as they are (CONF_* keys are stored):
# the name becomes the entry's title, the chosen shades become each shade's
# settings, and the Pico device becomes its buttons.
FIELD_NAME = "name"
FIELD_SHADES = "shades"
FIELD_PICO = "pico"  # a section, holding:
FIELD_PICO_DEVICE = "device_id"
# A section holding CONF_HALFWAY_HEIGHT, stored with CONF_TRAVEL_TIME_S.
FIELD_ROLLER_CURVE = "roller_curve"

HEIGHT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any")
)

SHADE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OPEN_HEIGHT): HEIGHT_SELECTOR,
        vol.Required(CONF_CLOSED_HEIGHT): HEIGHT_SELECTOR,
    }
)


def _travel_schema(curve_collapsed: bool) -> vol.Schema:
    """Schema for the travel time and, in a section, the optional 50% height."""
    return vol.Schema(
        {
            vol.Required(CONF_TRAVEL_TIME_S): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=300,
                    step=0.1,
                    unit_of_measurement="s",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(FIELD_ROLLER_CURVE): section(
                vol.Schema({vol.Optional(CONF_HALFWAY_HEIGHT): HEIGHT_SELECTOR}),
                {"collapsed": curve_collapsed},
            ),
        }
    )


# Covers offered as shades: ones that raise and lower to a position, so not
# garage doors, gates or sideways curtains. A shade with no device class can
# be offered by setting its "Show as" to Shade in its entity settings.
SHADE_FILTER = selector.EntityWithDeviceFilterSelectorConfig(
    domain="cover",
    device_class=["shade", "blind", "shutter"],
    supported_features=["cover.CoverEntityFeature.SET_POSITION"],
)


def _choose_shades_schema(hass: HomeAssistant, pico_collapsed: bool) -> vol.Schema:
    """Schema for the name, the shades and, in a section, an optional Pico.

    Aligned shade groups, this one included, aren't offered as shades.
    """
    aligned_shade_groups = [
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN
    ]
    return vol.Schema(
        {
            vol.Required(FIELD_NAME): selector.TextSelector(),
            vol.Required(FIELD_SHADES): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    filter=SHADE_FILTER,
                    multiple=True,
                    exclude_entities=aligned_shade_groups,
                )
            ),
            vol.Required(FIELD_PICO): section(
                vol.Schema(
                    {
                        vol.Optional(FIELD_PICO_DEVICE): selector.DeviceSelector(
                            selector.DeviceSelectorConfig(
                                integration="lutron_caseta",
                                entity=[
                                    selector.EntityFilterSelectorConfig(domain="button")
                                ],
                            )
                        )
                    }
                ),
                {"collapsed": pico_collapsed},
            ),
        }
    )


# Checked here too: the picker's filter only hides covers in the form, and it
# matches any of its features, so it can't require both.
REQUIRED_FEATURES = CoverEntityFeature.SET_POSITION | CoverEntityFeature.STOP


def _shared_curve_fits(shades: list[dict[str, Any]]) -> bool:
    """Whether the measured shade's curve extends over every shade."""
    try:
        matched_roll_group(ShadeConfig(**shade) for shade in shades)
    except ValueError:
        return False
    return True


def _validate_shades(hass: HomeAssistant, entity_ids: list[str]) -> str | None:
    """Return an error key if these can't be a group's shades, or None."""
    if len(entity_ids) < 2:
        return "too_few_shades"
    for entity_id in entity_ids:
        if split_entity_id(entity_id)[0] != COVER_DOMAIN:
            return "cover_unsupported"
        features = _supported_features(hass, entity_id)
        if features is not None and features & REQUIRED_FEATURES != REQUIRED_FEATURES:
            return "cover_unsupported"
    return None


def _supported_features(hass: HomeAssistant, entity_id: str) -> int | None:
    """A cover's features, from its state or, if unavailable, the registry."""
    state = hass.states.get(entity_id)
    if state is not None and state.state != STATE_UNAVAILABLE:
        return int(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
    if (entry := er.async_get(hass).async_get(entity_id)) is not None:
        return entry.supported_features
    return None


def _ranges_overlap(shades: list[dict[str, Any]]) -> bool:
    """Whether some hemline height is within every shade's range."""
    highest_closed = max(shade[CONF_CLOSED_HEIGHT] for shade in shades)
    lowest_open = min(shade[CONF_OPEN_HEIGHT] for shade in shades)
    return bool(highest_closed < lowest_open)


class AlignedShadeGroupConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create an aligned shade group, or edit one.

    Creating starts at the "user" step and editing (Reconfigure, in the
    entry's menu) at the "reconfigure" step. Both choose the name, shades and
    an optional Pico, then run one "shade" step per shade to collect its
    heights, then a "travel" step that measures the tallest shade: its travel
    time (all shades are assumed to move at the same speed) and, optionally,
    its 50% height. Those are stored with that shade.

    Each form opens with its prefill (saved settings, when editing), or, when
    shown again after an error, with what was just entered: `user_input or`.
    """

    VERSION = 3

    def __init__(self) -> None:
        """Initialize the flow."""
        self._name = ""
        self._entity_ids: list[str] = []
        self._pico: PicoButtons | None = None
        # Each shade's settings by entity id: those entered so far, in the order
        # the shades were chosen, and, when editing, those saved before.
        self._new_shades: dict[str, dict[str, Any]] = {}
        self._old_shades: dict[str, dict[str, Any]] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a group: choose its name, shades and optional Pico."""
        return await self._async_step_choose_shades("user", user_input, prefill={})

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit a group, starting from its current settings."""
        entry = self._get_reconfigure_entry()
        self._old_shades = {
            shade[CONF_ENTITY_ID]: shade for shade in entry.data[CONF_SHADES]
        }
        prefill = {
            FIELD_NAME: entry.title,
            FIELD_SHADES: list(self._old_shades),
            FIELD_PICO: self._pico_prefill(entry.data[CONF_CONTROLS]),
        }
        return await self._async_step_choose_shades("reconfigure", user_input, prefill)

    async def _async_step_choose_shades(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        prefill: dict[str, Any],
    ) -> ConfigFlowResult:
        """Choose the name, shades and Pico, then go on to the first shade."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := self._choose_shades(user_input):
                errors["base"] = error
            else:
                return await self.async_step_shade()

        has_pico = bool(prefill.get(FIELD_PICO))
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                _choose_shades_schema(self.hass, pico_collapsed=not has_pico),
                user_input or prefill,
            ),
            errors=errors,
            description_placeholders={"name": prefill.get(FIELD_NAME, "")},
        )

    def _choose_shades(self, user_input: dict[str, Any]) -> str | None:
        """Take the chosen name, shades and Pico, or return an error key."""
        if error := _validate_shades(self.hass, user_input[FIELD_SHADES]):
            return error
        pico = None
        if device_id := user_input[FIELD_PICO].get(FIELD_PICO_DEVICE):
            try:
                pico = find_pico_buttons(self.hass, device_id)
            except PicoButtonsError as err:
                return err.reason
        self._name = user_input[FIELD_NAME]
        self._entity_ids = user_input[FIELD_SHADES]
        self._pico = pico
        self._new_shades = {}
        return None

    def _pico_prefill(self, controls: list[dict[str, Any]]) -> dict[str, str]:
        """The Pico section's answer for stored controls: the Pico's device."""
        for control in controls:
            if control[CONF_TYPE] != ControlType.PICO:
                continue
            button = er.async_get(self.hass).async_get(control[CONF_PICO_OPEN])
            if button and button.device_id:
                return {FIELD_PICO_DEVICE: button.device_id}
        return {}

    async def async_step_shade(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect heights for the next shade."""
        entity_id = self._entity_ids[len(self._new_shades)]
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input[CONF_CLOSED_HEIGHT] >= user_input[CONF_OPEN_HEIGHT]:
                errors["base"] = "closed_not_below_open"
            elif not _ranges_overlap([*self._new_shades.values(), user_input]):
                errors["base"] = "ranges_do_not_overlap"
            else:
                self._new_shades[entity_id] = {CONF_ENTITY_ID: entity_id, **user_input}
                if len(self._new_shades) == len(self._entity_ids):
                    return await self.async_step_travel()
                return await self.async_step_shade()  # the next shade's form

        return self.async_show_form(
            step_id="shade",
            data_schema=self.add_suggested_values_to_schema(
                SHADE_SCHEMA, user_input or self._old_shades.get(entity_id, {})
            ),
            errors=errors,
            description_placeholders={
                "name": self._friendly_name(entity_id),
                "index": str(len(self._new_shades) + 1),
                "count": str(len(self._entity_ids)),
            },
        )

    async def async_step_travel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Measure the tallest shade's travel time and, optionally, its curve."""
        tallest = max(
            self._new_shades.values(),
            key=lambda shade: shade[CONF_OPEN_HEIGHT] - shade[CONF_CLOSED_HEIGHT],
        )
        low, high = halfway_height_range(
            tallest[CONF_CLOSED_HEIGHT], tallest[CONF_OPEN_HEIGHT]
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            measurements = {
                CONF_TRAVEL_TIME_S: user_input[CONF_TRAVEL_TIME_S],
                **user_input[FIELD_ROLLER_CURVE],
            }
            halfway_height = measurements.get(CONF_HALFWAY_HEIGHT)
            shades = [
                {**shade, **measurements} if shade is tallest else shade
                for shade in self._new_shades.values()
            ]
            if halfway_height is None:
                pass
            elif not low < halfway_height <= high:
                errors["base"] = "halfway_out_of_range"
            elif not _shared_curve_fits(shades):
                errors["base"] = "halfway_cant_reach"
            if not errors:
                return self._async_save(shades)

        # Prefilled only if the same shade was measured before.
        old = self._old_shades.get(tallest[CONF_ENTITY_ID], {})
        prefill: dict[str, Any] = {FIELD_ROLLER_CURVE: {}}
        if CONF_TRAVEL_TIME_S in old:
            prefill[CONF_TRAVEL_TIME_S] = old[CONF_TRAVEL_TIME_S]
        if CONF_HALFWAY_HEIGHT in old:
            prefill[FIELD_ROLLER_CURVE][CONF_HALFWAY_HEIGHT] = old[CONF_HALFWAY_HEIGHT]
        shown = user_input or prefill
        return self.async_show_form(
            step_id="travel",
            data_schema=self.add_suggested_values_to_schema(
                _travel_schema(
                    curve_collapsed=CONF_HALFWAY_HEIGHT not in shown[FIELD_ROLLER_CURVE]
                ),
                shown,
            ),
            errors=errors,
            description_placeholders={
                "name": self._friendly_name(tallest[CONF_ENTITY_ID]),
                "low": f"{low:.4g}",
                "high": f"{high:.4g}",
            },
        )

    @callback
    def _async_save(self, shades: list[dict[str, Any]]) -> ConfigFlowResult:
        """Create the group, or update the one being edited."""
        controls = []
        if self._pico:
            # The form only offers a Pico paired to every shade.
            controls.append(
                {
                    CONF_TYPE: ControlType.PICO,
                    CONF_SHADES: self._entity_ids,
                    CONF_PICO_OPEN: self._pico.open,
                    CONF_PICO_STOP: self._pico.stop,
                    CONF_PICO_CLOSE: self._pico.close,
                }
            )
        data = {CONF_SHADES: shades, CONF_CONTROLS: controls}
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), title=self._name, data=data
            )
        return self.async_create_entry(title=self._name, data=data)

    def _friendly_name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return (
            str(state.attributes.get(ATTR_FRIENDLY_NAME, entity_id))
            if state
            else entity_id
        )
