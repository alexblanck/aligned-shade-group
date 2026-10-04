"""Diagnostics for Aligned Shade Group."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """The group's settings, plus its live state if it's running."""
    group = getattr(entry, "runtime_data", None)
    return {
        "title": entry.title,
        "data": dict(entry.data),
        "group": group.diagnostics() if group is not None else None,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Each group has one device, so it's the same as the entry's."""
    return await async_get_config_entry_diagnostics(hass, entry)
