"""Constants for Aligned Shade Group."""

from enum import StrEnum

DOMAIN = "aligned_shade_group"

# CONF_* constants are keys in a group's stored settings (its config entry's
# data), shared by the setup form and the entity. Form-only fields are
# FIELD_* constants in config_flow.py.

# Each shade's settings. The measured shade (the tallest when set up) also has
# its travel time and, optionally, its hemline height at 50%.
CONF_SHADES = "shades"
CONF_CLOSED_HEIGHT = "closed_height"
CONF_OPEN_HEIGHT = "open_height"
CONF_TRAVEL_TIME_S = "travel_time_s"
CONF_HALFWAY_HEIGHT = "halfway_height"

# Ways to start several shades at once through the bridge, each with its
# `type` (CONF_TYPE, a ControlType) and the shades (CONF_SHADES) it moves.
# Only Picos so far, with the button entities found from the Pico chosen in the
# setup form.
CONF_CONTROLS = "controls"
CONF_PICO_OPEN = "open"
CONF_PICO_STOP = "stop"
CONF_PICO_CLOSE = "close"


class ControlType(StrEnum):
    """Values of a control's CONF_TYPE."""

    PICO = "pico"


def missing_entities_issue_id(entry_id: str) -> str:
    """The repair issue raised when a group's shades or Pico buttons are missing."""
    return f"entities_missing_{entry_id}"
