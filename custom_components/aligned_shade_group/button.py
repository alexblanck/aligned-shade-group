"""The group's Realign button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the group's Realign button to its device."""
    async_add_entities([RealignButton(entry)])


class RealignButton(ButtonEntity):
    """Sets the group to its current position, which sends every shade its
    position: shades out of line meet at their average height, and any that
    have drifted from what they report are re-seated.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "realign"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_realign"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_press(self) -> None:
        # The cover entity, set up alongside (see cover.async_setup_entry).
        await self._entry.runtime_data.async_realign()
