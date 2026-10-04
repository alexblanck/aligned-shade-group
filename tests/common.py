"""Test helpers."""

from homeassistant.components.cover import CoverEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

HIGH_SILL = "cover.high_sill"
LOW_SILL = "cover.low_sill"

FEATURES = (
    CoverEntityFeature.OPEN
    | CoverEntityFeature.CLOSE
    | CoverEntityFeature.STOP
    | CoverEntityFeature.SET_POSITION
)

# Same top, different sills, as entered in the setup flow.
SHADES = [
    {"entity_id": HIGH_SILL, "open_height": 84, "closed_height": 24},
    {"entity_id": LOW_SILL, "open_height": 84, "closed_height": 12},
]
# Both shades move at this speed, in inches per second.
SPEED = 2.0


def set_shade(hass: HomeAssistant, entity_id: str, position_pct: int) -> None:
    """Set a fake shade's state."""
    hass.states.async_set(
        entity_id,
        "closed" if position_pct == 0 else "open",
        {"current_position": position_pct, ATTR_SUPPORTED_FEATURES: FEATURES},
    )
