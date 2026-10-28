"""Picos and scenes as saved groups store them, which must keep loading the
same: if a change breaks these, saved groups need a migration instead.
"""

import json
from typing import Any

import pytest

from custom_components.aligned_shade_group.controls import (
    Pico,
    PicoButtons,
    Scene,
    StoredControlError,
    controls_from_stored,
)

# A group's "controls", as saved by 0.5 (config flow VERSION 3). Don't edit
# this to match the code: add a migration, and a copy for the new format.
STORED_V3 = """
[
  {
    "type": "pico",
    "shades": ["cover.left_1", "cover.left_2"],
    "open": "button.living_room_pico_on",
    "stop": "button.living_room_pico_stop",
    "close": "button.living_room_pico_off"
  },
  {
    "type": "scene",
    "entity_id": "scene.living_room_half_open",
    "positions": {"cover.left_1": 50, "cover.right_3": 40}
  }
]
"""


def test_stored_controls_load_and_save_unchanged() -> None:
    stored = json.loads(STORED_V3)

    controls = controls_from_stored(stored)

    assert controls == [
        Pico(
            buttons=PicoButtons(
                open="button.living_room_pico_on",
                stop="button.living_room_pico_stop",
                close="button.living_room_pico_off",
            ),
            shades=("cover.left_1", "cover.left_2"),
        ),
        Scene(
            entity_id="scene.living_room_half_open",
            positions={"cover.left_1": 50, "cover.right_3": 40},
        ),
    ]
    # Saved again as they were, through JSON as Home Assistant stores them.
    assert json.loads(json.dumps([c.to_stored() for c in controls])) == stored


def test_controls_of_unknown_types_are_left_out() -> None:
    # Say, saved by a newer version before going back to this one.
    stored = [{"type": "keypad", "entity_id": "button.keypad"}]

    assert controls_from_stored(stored) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("shades", "cover.left_1"),  # would load as one shade per character
        ("shades", [1, 2]),
        ("stop", None),
        ("positions", {"cover.left_1": 50.5}),
        ("positions", {"cover.left_1": True}),
        ("positions", {"cover.left_1": 150}),
        ("positions", ["cover.left_1"]),
    ],
)
def test_stored_controls_of_the_wrong_types_are_refused(key: str, value: Any) -> None:
    stored = json.loads(STORED_V3)
    control = next(control for control in stored if key in control)
    control[key] = value

    with pytest.raises(StoredControlError, match=key):
        controls_from_stored(stored)
