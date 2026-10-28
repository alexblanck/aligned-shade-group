"""Whether the entities a group uses are there, shared by the group (which
leaves out what's missing or unavailable, and raises a repair for what's
missing) and its setup screens (which mark them).
"""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


def missing_entities(hass: HomeAssistant, entity_ids: Iterable[str]) -> list[str]:
    """Entities that are disabled, or have neither a registry entry nor a
    state (say, renamed or removed). Fixing these means editing the group.
    """
    registry = er.async_get(hass)
    missing = []
    for entity_id in entity_ids:
        entry = registry.async_get(entity_id)
        if entry is None:
            if hass.states.get(entity_id) is None:
                missing.append(entity_id)
        elif entry.disabled:
            missing.append(entity_id)
    return missing


def unavailable_entities(hass: HomeAssistant, entity_ids: Iterable[str]) -> list[str]:
    """Entities with no state or an unavailable one, such as when their
    device is offline. They come back on their own.
    """
    return [
        entity_id
        for entity_id in entity_ids
        if (state := hass.states.get(entity_id)) is None
        or state.state == STATE_UNAVAILABLE
    ]
