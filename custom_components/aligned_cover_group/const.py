"""Constants for Aligned Cover Group."""

DOMAIN = "aligned_cover_group"

# Each shade's settings. The measured shade (the tallest when set up) also has
# its travel time and, optionally, its hemline height at 50%.
CONF_SHADES = "shades"
CONF_CLOSED_HEIGHT = "closed_height"
CONF_OPEN_HEIGHT = "open_height"
CONF_TRAVEL_TIME_S = "travel_time_s"
CONF_HALFWAY_HEIGHT = "halfway_height"

# Ways to start several shades at once through the bridge, each with its
# `type` and the shades (CONF_SHADES) it moves. Only Picos so far, with the
# button entities found from the Pico chosen in the setup form.
CONF_CONTROLS = "controls"
CONTROL_PICO = "pico"
CONF_PICO_OPEN = "open"
CONF_PICO_STOP = "stop"
CONF_PICO_CLOSE = "close"
# The setup form's section for a Pico paired to every shade in the group.
PICO_SECTION = "pico"
