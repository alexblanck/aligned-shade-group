"""The Picos and Lutron scenes a group starts several shades with at once:
read from and written to its stored settings, and turned into the starters
its plans use. No Home Assistant imports.

Saved groups must keep loading the same (see DEVELOPMENT.md), so each class
reads and writes its stored form side by side, and tests/test_controls.py
pins that form.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .alignment import Starter
from .const import (
    CONF_ENTITY_ID,
    CONF_PICO_CLOSE,
    CONF_PICO_OPEN,
    CONF_PICO_STOP,
    CONF_SCENE_POSITIONS,
    CONF_SHADES,
    CONF_TYPE,
    ControlType,
)


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


@dataclass(frozen=True)
class Pico:
    """A Pico paired to some (or all) of the group's shades."""

    buttons: PicoButtons
    shades: tuple[str, ...]

    def entities(self) -> tuple[str, ...]:
        """Entities it needs, to check they're there."""
        return self.buttons.buttons()

    def shade_ids(self) -> tuple[str, ...]:
        """The shades it moves."""
        return self.shades

    def starters(self) -> list[Starter]:
        """Its On button sends its shades to 100%, its Off button to 0%."""
        return [
            Starter(name=self.buttons.open, targets=dict.fromkeys(self.shades, 100)),
            Starter(name=self.buttons.close, targets=dict.fromkeys(self.shades, 0)),
        ]

    @classmethod
    def from_stored(cls, stored: Mapping[str, Any]) -> Pico:
        """A Pico from its stored settings."""
        return cls(
            buttons=PicoButtons(
                open=_stored_str(stored, CONF_PICO_OPEN),
                stop=_stored_str(stored, CONF_PICO_STOP),
                close=_stored_str(stored, CONF_PICO_CLOSE),
            ),
            shades=_stored_strs(stored, CONF_SHADES),
        )

    def to_stored(self) -> dict[str, Any]:
        """Its stored settings."""
        return {
            CONF_TYPE: ControlType.PICO,
            CONF_SHADES: list(self.shades),
            CONF_PICO_OPEN: self.buttons.open,
            CONF_PICO_STOP: self.buttons.stop,
            CONF_PICO_CLOSE: self.buttons.close,
        }


@dataclass(frozen=True)
class Scene:
    """A Lutron scene that sets some of the group's shades to positions."""

    entity_id: str
    positions: Mapping[str, int]

    def entities(self) -> tuple[str, ...]:
        """Entities it needs, to check they're there."""
        return (self.entity_id,)

    def shade_ids(self) -> tuple[str, ...]:
        """The shades it moves."""
        return tuple(self.positions)

    def starters(self) -> list[Starter]:
        """Activating it sends each of its shades to its position."""
        return [Starter(name=self.entity_id, targets=self.positions)]

    @classmethod
    def from_stored(cls, stored: Mapping[str, Any]) -> Scene:
        """A scene from its stored settings."""
        return cls(
            entity_id=_stored_str(stored, CONF_ENTITY_ID),
            positions=_stored_positions(stored, CONF_SCENE_POSITIONS),
        )

    def to_stored(self) -> dict[str, Any]:
        """Its stored settings."""
        return {
            CONF_TYPE: ControlType.SCENE,
            CONF_ENTITY_ID: self.entity_id,
            CONF_SCENE_POSITIONS: dict(self.positions),
        }


type Control = Pico | Scene


def controls_from_stored(stored: Iterable[Mapping[str, Any]]) -> list[Control]:
    """The Picos and scenes in a group's stored settings, leaving out any of
    a type this version doesn't know.
    """
    controls: list[Control] = []
    for control in stored:
        if control[CONF_TYPE] == ControlType.PICO:
            controls.append(Pico.from_stored(control))
        elif control[CONF_TYPE] == ControlType.SCENE:
            controls.append(Scene.from_stored(control))
    return controls


class StoredControlError(ValueError):
    """A stored Pico or scene isn't in a form this version can load."""


def _stored_str(stored: Mapping[str, Any], key: str) -> str:
    """An entity id, say, read from stored settings."""
    value = stored[key]
    if not isinstance(value, str):
        raise StoredControlError(f"{key!r} should be text, not {value!r}")
    return value


def _stored_strs(stored: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """A list of entity ids, say, read from stored settings."""
    values = stored[key]
    # A string is iterable too, but would become one id per character.
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise StoredControlError(f"{key!r} should be a list of text, not {values!r}")
    return tuple(values)


def _stored_positions(stored: Mapping[str, Any], key: str) -> dict[str, int]:
    """Positions in whole percent by entity id, read from stored settings (a
    copy, so the stored settings can't change through it).
    """
    positions = stored[key]
    if not isinstance(positions, Mapping) or not all(
        isinstance(entity_id, str)
        # bool is a subclass of int, so a stored true would pass as 1%.
        and isinstance(pct, int)
        and not isinstance(pct, bool)
        and 0 <= pct <= 100
        for entity_id, pct in positions.items()
    ):
        raise StoredControlError(
            f"{key!r} should map entity ids to percentages, not {positions!r}"
        )
    return dict(positions)
