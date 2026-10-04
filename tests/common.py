"""Names and settings shared by the tests."""

HIGH_SILL = "cover.high_sill"
LOW_SILL = "cover.low_sill"

# Same top, different sills, as entered in the setup flow.
SHADES = [
    {"entity_id": HIGH_SILL, "open_height": 84, "closed_height": 24},
    {"entity_id": LOW_SILL, "open_height": 84, "closed_height": 12},
]
# Both shades move at this speed, in inches per second.
SPEED = 2.0
