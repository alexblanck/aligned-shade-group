"""Hemline alignment and motion planning.

Pure math with no Home Assistant imports so it can be unit tested directly.

Naming:

- `*_pct` values are positions in percent, following Home Assistant's
  convention (0 fully closed, 100 fully open). Positions shades report or are
  sent are whole numbers (`int`); positions worked out along a curve are
  fractional (`float`) and only rounded when a shade is commanded.
- `*_height` values are hemline heights: the height of a shade's bottom edge,
  in whatever unit the user measured in.
- `*_s` values are seconds.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, KeysView, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from .roll_profile import RollProfile, RollProfileView

# A shade counts as aligned when its hemline is within this fraction of the
# group's range from the shared height. Whole-percent positions alone can put a
# shade up to 0.5% of its span off, so this leaves headroom while staying small.
ALIGN_TOLERANCE_FRACTION = 0.01


class Direction(StrEnum):
    """Direction of travel."""

    OPENING = "opening"
    CLOSING = "closing"


@dataclass(frozen=True)
class Shade:
    """One shade: its view of a roll profile (between its limits), and how long
    a full travel takes. Positions change at a constant rate while moving.
    """

    entity_id: str
    view: RollProfileView
    travel_time_s: float

    @property
    def closed_height(self) -> float:
        """Hemline height when fully closed."""
        return self.view.closed_height

    @property
    def open_height(self) -> float:
        """Hemline height when fully open."""
        return self.view.open_height

    def height_for_position(self, position_pct: float) -> float:
        """Hemline height at a shade position."""
        return self.view.height_for_position(position_pct)

    def position_for_height(self, hemline_height: float) -> float:
        """Position that puts the hemline at `hemline_height`."""
        return self.view.position_for_height(hemline_height)


@dataclass(frozen=True)
class ShadeConfig:
    """One shade's settings, as stored and as `matched_roll_group` takes them.

    The measured shade also has `travel_time_s`, its full travel time, and
    optionally `halfway_height`, its hemline at 50% (without it, height is
    proportional to position).
    """

    entity_id: str
    closed_height: float
    open_height: float
    travel_time_s: float | None = None
    halfway_height: float | None = None


def matched_roll_group(shades: Iterable[ShadeConfig]) -> AlignmentGroup:
    """Create an AlignmentGroup for shades whose rolls match.

    Matched rolls are the same size whenever their hemlines are at the same
    height: the same fabric and tube, with the same amount of fabric wound on,
    differing only in where each shade's limits are. Every shade, and the
    group's own position, then sees a view of a shared roll profile: the
    measured shade's, the one shade with a travel time. Raises ValueError if
    there isn't exactly one, or if the profile can't reach every shade.
    """
    shades = list(shades)
    measurements = [
        (shade, shade.travel_time_s)
        for shade in shades
        if shade.travel_time_s is not None
    ]
    if len(measurements) != 1:
        raise ValueError(
            f"Expected one shade with a travel time, got {len(measurements)}"
        )
    [(measured, measured_travel_time_s)] = measurements
    if measured.halfway_height is None:
        profile = RollProfile.straight(
            closed_height=measured.closed_height, open_height=measured.open_height
        )
    else:
        profile = RollProfile(
            closed_height=measured.closed_height,
            open_height=measured.open_height,
            halfway_height=measured.halfway_height,
        )
    lowest_height = min(shade.closed_height for shade in shades)
    highest_height = max(shade.open_height for shade in shades)

    group_shades = []
    for shade in shades:
        view = profile.view(
            closed_height=shade.closed_height, open_height=shade.open_height
        )
        # Matched rolls turn at the same rate, and a shade's position is linear
        # in profile position within its view, so profile positions change at
        # one constant rate for every shade. The measured shade's view is the
        # whole profile (a fraction of 1), so a shade's full travel takes its
        # view's fraction of the profile times the measured travel time.
        group_shades.append(
            Shade(
                entity_id=shade.entity_id,
                view=view,
                travel_time_s=view.profile_fraction * measured_travel_time_s,
            )
        )
    return AlignmentGroup(
        shades=group_shades,
        view=profile.view(closed_height=lowest_height, open_height=highest_height),
    )


@dataclass(frozen=True)
class Move:
    """One shade's part of a plan: where it goes and when it starts.

    `needs_command` is False when a Pico press already sends the shade to its
    target, so no `set_position` command is needed.
    """

    shade: Shade
    from_pct: int
    target_pct: int
    delay_s: float = 0.0
    needs_command: bool = True

    def direction(self) -> Direction:
        """Which way the shade moves."""
        if self.target_pct > self.from_pct:
            return Direction.OPENING
        return Direction.CLOSING

    def from_height(self) -> float:
        """Hemline height where the shade starts."""
        return self.shade.height_for_position(self.from_pct)

    def arrival_s(self) -> float:
        """Seconds from the start of the plan until the shade arrives."""
        travel_pct = abs(self.target_pct - self.from_pct)
        return self.delay_s + travel_pct / 100 * self.shade.travel_time_s


@dataclass(frozen=True)
class Plan:
    """The moves that bring the group to a target hemline height.

    Running a plan carries out its moves. `moves` has one entry per shade that
    moves, sorted by start delay, plus "hold" moves (start equals target) for
    shades still heading to an earlier target. If `pico` is set, press that
    Pico button first: it starts every paired shade at once toward the
    endpoint, and only moves that stop short of it need a command. Pico plans
    never need holds: a held shade partway would have blocked the Pico, and one
    at the endpoint is carried there anyway. `pico_blocker` says why an
    available Pico wasn't used.
    """

    pico: Direction | None
    moves: tuple[Move, ...]
    pico_blocker: str | None = None

    def duration_s(self) -> float:
        """Seconds until every shade has arrived."""
        return max((move.arrival_s() for move in self.moves), default=0.0)


class AlignmentGroup:
    """A set of shades whose hemlines are kept aligned.

    The group's own position is a view of the shades' shared roll profile,
    from the lowest closed height (0%) to the highest open height (100%). So
    a shade spanning that whole range sits at the group's position. That's
    the tallest shade when its range covers every other shade's (such as
    shades sharing a top), and identical shades match the group exactly.
    """

    def __init__(self, shades: Iterable[Shade], view: RollProfileView) -> None:
        """Initialize the group."""
        self.shades = tuple(shades)
        self.view = view
        self._entity_ids = {shade.entity_id for shade in self.shades}
        self._height_tolerance = (
            view.open_height - view.closed_height
        ) * ALIGN_TOLERANCE_FRACTION

    def height_for_position(self, position_pct: int) -> float:
        """Hemline height for a group position."""
        return self.view.height_for_position(position_pct)

    def position_for_height(self, hemline_height: float) -> float:
        """Group position for a hemline height."""
        return self.view.position_for_height(hemline_height)

    def common_hemline_height(
        self, positions_pct_by_id: Mapping[str, int]
    ) -> float | None:
        """The group hemline height every shade is at, or None if misaligned.

        A shade at a limit is also at any height beyond it: one fully closed
        is level with any group height at or below its closed height (its
        hemline can't go lower), and one fully open with any height at or
        above its open height. Each shade must be within the alignment
        tolerance of the height.
        """
        floor_height = self.view.closed_height
        ceiling_height = self.view.open_height
        for shade in self._shades_for(positions_pct_by_id.keys()):
            position_pct = positions_pct_by_id[shade.entity_id]
            height = shade.height_for_position(position_pct)
            if position_pct > 0:
                floor_height = max(floor_height, height)
            if position_pct < 100:
                ceiling_height = min(ceiling_height, height)
        if floor_height - ceiling_height > 2 * self._height_tolerance:
            return None
        if all(pct <= 0 for pct in positions_pct_by_id.values()):
            return floor_height
        if all(pct >= 100 for pct in positions_pct_by_id.values()):
            return ceiling_height
        return (floor_height + ceiling_height) / 2

    def current_position(self, positions_pct_by_id: Mapping[str, int]) -> int | None:
        """Group position to report for the given shade positions.

        When the shades are aligned, it's the group position of their common
        hemline height, so after the group moves them it reports the position
        it was sent to. Otherwise it's the group position of their average
        hemline height: like Home Assistant's cover group, which averages
        its members' positions, but by height, since the same position means
        a different height on each shade. None when no position is known.
        """
        shades = self._shades_for(positions_pct_by_id.keys())
        if not shades:
            return None
        height = self.common_hemline_height(positions_pct_by_id)
        if height is None:
            height = sum(
                shade.height_for_position(positions_pct_by_id[shade.entity_id])
                for shade in shades
            ) / len(shades)
        return round(self.position_for_height(height))

    def plan_moves(
        self,
        positions_pct_by_id: Mapping[str, int],
        target_pct: int,
        pico_available: bool,
        moving_entity_ids: Collection[str] = (),
    ) -> Plan:
        """Plan the moves that bring the group to `target_pct`.

        `moving_entity_ids` are shades that may still be heading somewhere
        else; any already at their new target get a move that holds them there.
        """
        target_height = self.height_for_position(target_pct)
        moves: list[Move] = []
        holds: list[Move] = []
        for shade in self._shades_for(positions_pct_by_id.keys()):
            move = Move(
                shade=shade,
                from_pct=positions_pct_by_id[shade.entity_id],
                target_pct=round(shade.position_for_height(target_height)),
            )
            if move.from_pct != move.target_pct:
                moves.append(move)
            elif shade.entity_id in moving_entity_ids:
                holds.append(move)
        if not moves:
            return Plan(pico=None, moves=tuple(holds))
        directions = {move.direction() for move in moves}
        direction = directions.pop() if len(directions) == 1 else None

        blocker = None
        if pico_available:
            if direction is None:
                blocker = "shades are moving in different directions"
            else:
                blocker = self._pico_blocker(positions_pct_by_id, direction, len(moves))
                if blocker is None:
                    return _pico_plan(direction, moves)

        staggered = [
            staggered_move
            for direction_ in Direction
            for staggered_move in _staggered(
                [move for move in moves if move.direction() is direction_],
                self._height_tolerance,
            )
        ]
        staggered.sort(key=lambda move: move.delay_s)
        return Plan(pico=None, moves=(*holds, *staggered), pico_blocker=blocker)

    def _pico_blocker(
        self,
        positions_pct_by_id: Mapping[str, int],
        direction: Direction,
        moving_count: int,
    ) -> str | None:
        """Why a Pico press can't be used, or None if it's safe.

        The Pico moves every paired shade that isn't already at the endpoint,
        so all of those must need to move and share a hemline height (and all
        positions must be known).
        """
        if len(self._shades_for(positions_pct_by_id.keys())) != len(self.shades):
            return "some shade positions are unknown"
        endpoint_pct = 100 if direction is Direction.OPENING else 0
        heights = [
            shade.height_for_position(positions_pct_by_id[shade.entity_id])
            for shade in self.shades
            if positions_pct_by_id[shade.entity_id] != endpoint_pct
        ]
        if len(heights) != moving_count:
            return "it would move a shade that is already in place"
        shared_height = sum(heights) / len(heights)
        if any(
            abs(height - shared_height) > self._height_tolerance for height in heights
        ):
            return (
                f"shades start from different hemline heights "
                f"({min(heights):.1f} to {max(heights):.1f})"
            )
        return None

    def _shades_for(self, entity_ids: KeysView[str]) -> list[Shade]:
        """The group's shades with these entity ids, in group order."""
        if unexpected := entity_ids - self._entity_ids:
            raise ValueError(f"Shades not in the group: {unexpected}")
        return [shade for shade in self.shades if shade.entity_id in entity_ids]


def _level_groups(moves: list[Move], tolerance_height: float) -> list[list[Move]]:
    """Moves in one direction, grouped by shades that start level.

    Groups are ordered from the furthest from the target, as are moves within
    each group. A shade joins the current group while its hemline is within
    `tolerance_height` of the group's first shade: the same tolerance the
    Pico and the aligned check use, so level shades can be started together.
    """
    if not moves:
        return []
    sign = 1 if moves[0].direction() is Direction.OPENING else -1
    groups: list[list[Move]] = []
    for move in sorted(moves, key=lambda move: sign * move.from_height()):
        if groups and (
            abs(move.from_height() - groups[-1][0].from_height()) <= tolerance_height
        ):
            groups[-1].append(move)
        else:
            groups.append([move])
    return groups


def _staggered(moves: list[Move], tolerance_height: float) -> list[Move]:
    """Delay moves in one direction so each starts when the leader reaches it.

    The leader is the shade furthest from the target. Each group of shades
    starting level (see `_level_groups`) starts together, when the leader
    reaches the group's first shade.
    """
    groups = _level_groups(moves, tolerance_height)
    if not groups:
        return []
    leader = groups[0][0]
    staggered: list[Move] = []
    for group in groups:
        # How long the leader takes to move from where it starts to this
        # group's hemline: positions change at a constant rate.
        delay_s = (
            abs(
                leader.shade.position_for_height(group[0].from_height())
                - leader.from_pct
            )
            / 100
            * leader.shade.travel_time_s
        )
        staggered.extend(replace(move, delay_s=delay_s) for move in group)
    return staggered


def _pico_plan(direction: Direction, moves: list[Move]) -> Plan:
    """The Pico starts every move; only those stopping short need a command."""
    endpoint_pct = 100 if direction is Direction.OPENING else 0
    return Plan(
        pico=direction,
        moves=tuple(
            replace(move, needs_command=move.target_pct != endpoint_pct)
            for move in moves
        ),
    )
