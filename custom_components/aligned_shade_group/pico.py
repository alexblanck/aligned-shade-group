"""Finding a Lutron Caseta shade Pico's buttons from its device."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .alignment import Direction

# Caseta names a Pico's button entities after the Pico, ending in the button's
# name (such as "Living Room Pico On"). A shade Pico's Raise and Lower buttons
# nudge shades, so they aren't used.
_BUTTON_NAMES = {"open": "On", "stop": "Stop", "close": "Off"}


@dataclass(frozen=True)
class PicoButtons:
    """Button entities of a Pico paired to the group's shades.

    Stored in the group's settings as found when it was set up.
    """

    open: str
    stop: str
    close: str

    def buttons(self) -> tuple[str, str, str]:
        """All three button entities."""
        return (self.open, self.stop, self.close)

    def toward(self, direction: Direction) -> str:
        """The button that sends every shade toward that direction's end."""
        return self.open if direction is Direction.OPENING else self.close


class PicoButtonsError(Exception):
    """A Pico's buttons can't be used; `reason` is a setup form error key."""

    def __init__(self, reason: str) -> None:
        """Initialize with the error key."""
        super().__init__(reason)
        self.reason = reason


def find_pico_buttons(hass: HomeAssistant, device_id: str) -> PicoButtons:
    """The On, Stop and Off button entities of a Pico device.

    Raises PicoButtonsError if the device doesn't have exactly one of each, or
    if any is disabled (Caseta disables Pico buttons by default).
    """
    entries = [
        entry
        for entry in er.async_entries_for_device(
            er.async_get(hass), device_id, include_disabled_entities=True
        )
        if entry.domain == BUTTON_DOMAIN
    ]
    buttons: dict[str, str] = {}
    for role, name in _BUTTON_NAMES.items():
        matches = [
            entry
            for entry in entries
            if (entry.original_name or "").rsplit(" ", 1)[-1] == name
        ]
        if len(matches) != 1:
            raise PicoButtonsError("pico_not_a_shade_pico")
        if matches[0].disabled:
            raise PicoButtonsError("pico_buttons_disabled")
        buttons[role] = matches[0].entity_id
    return PicoButtons(
        open=buttons["open"], stop=buttons["stop"], close=buttons["close"]
    )
