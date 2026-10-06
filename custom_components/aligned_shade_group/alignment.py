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

from collections.abc import Collection, Iterable, KeysView, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType

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
class Starter:
    """A way to start several shades at once: a Pico button or a scene.

    `name` is its entity (the button or scene), and `targets` is where it sends
    each of its shades. It's one command to the bridge, so they start together.
    """

    name: str
    targets: Mapping[str, int]


@dataclass(frozen=True)
class Move:
    """One shade's part of a plan: where it goes and when it starts.

    `starter` is the starter that starts the shade, or None when its own
    `set_position` does. A shade a starter starts still needs its own command
    (`needs_command`) unless the starter already sends it to its target. A
    shade still travelling from an earlier plan that starts later is `held`
    where it is from the start of the plan until then.
    """

    shade: Shade
    from_pct: int
    target_pct: int
    delay_s: float = 0.0
    starter: Starter | None = None
    needs_command: bool = True
    held: bool = False

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
    moves, plus "hold" moves (start equals target) for shades already there,
    all sorted by start delay: those still heading to an earlier
    target stop there, and the others are sent their position again, as a
    shade's own `set_position` would (it re-seats the shade on the bridge,
    which realigns one that's drifted): by a starter the plan fires anyway
    that sends it there, if there is one, else by a command. At each start
    delay, fire the
    starters of that moment's moves, each once, then send the commands for
    moves that need one.

    A plan is its moves, not its starts (each listing the moves it begins):
    most questions are about one shade (where is it heading, where is it now,
    is this report expected), and grouping by starter is only needed when
    sending commands.
    """

    moves: tuple[Move, ...]

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
        starters: Sequence[Starter] = (),
        travelling: Mapping[str, Direction] = MappingProxyType({}),
    ) -> Plan:
        """Plan the moves that bring the group to `target_pct`.

        Shades moving the same way start in level groups (see `_level_groups`),
        each when the leader reaches it. A group starts with the starters that
        fit it (see `_starter_fit`), and its other shades with commands.
        Shades already at their target get a hold move. `travelling` are
        shades still heading somewhere else, by the way they're going: any
        starting later are `held` until then. A starter
        is only used on those starting now to turn them around (see
        `_starter_fit`).
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
            else:
                holds.append(move)
        # Shades moving in this plan (or held after travelling), which a
        # starter mustn't touch unless it's starting them. One already still
        # at its target may be: a starter leaves it where it is.
        moving = {move.shade.entity_id for move in moves} | {
            move.shade.entity_id for move in holds if move.shade.entity_id in travelling
        }

        timed = [
            timed_group
            for direction in Direction
            for timed_group in _timed_groups(
                [move for move in moves if move.direction() is direction],
                self._height_tolerance,
            )
        ]
        planned: list[Move] = []
        for group, delay_s in timed:
            # Travelling shades that start later are held still until then.
            travelling_now = travelling if delay_s == 0 else {}
            fits: list[tuple[Starter, set[str]]] = []
            for starter in starters:
                covered = _starter_fit(
                    starter, group, positions_pct_by_id, moving, travelling_now
                )
                if covered:
                    fits.append((starter, covered))
            # Starters covering more of the group first. No two may share any
            # of the group's shades, counting ones a starter leaves where they
            # are: fired together, it would send back a shade the other starts.
            group_ids = {move.shade.entity_id for move in group}
            starter_of: dict[str, Starter] = {}
            claimed: set[str] = set()
            for starter, covered in sorted(fits, key=lambda fit: -len(fit[1])):
                touched = starter.targets.keys() & group_ids
                if not touched & claimed:
                    claimed |= touched
                    starter_of |= dict.fromkeys(covered, starter)
            for move in group:
                by = starter_of.get(move.shade.entity_id)
                planned.append(
                    replace(
                        move,
                        delay_s=delay_s,
                        starter=by,
                        needs_command=by is None
                        or by.targets[move.shade.entity_id] != move.target_pct,
                        held=delay_s > 0 and move.shade.entity_id in travelling,
                    )
                )
        # A still shade already at its target that a starter in the plan sends
        # there anyway is re-sent by that starter, not a command of its own.
        fired = {
            move.starter.name: (move.starter, move.delay_s)
            for move in planned
            if move.starter
        }
        holds = [
            next(
                (
                    replace(hold, starter=starter, delay_s=delay_s, needs_command=False)
                    for starter, delay_s in fired.values()
                    if starter.targets.get(hold.shade.entity_id) == hold.target_pct
                ),
                hold,
            )
            if hold.shade.entity_id not in travelling
            else hold
            for hold in holds
        ]
        return Plan(moves=tuple(sorted((*holds, *planned), key=lambda m: m.delay_s)))

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


def _timed_groups(
    moves: list[Move], tolerance_height: float
) -> list[tuple[list[Move], float]]:
    """Level groups of moves in one direction, each with its start delay.

    Each group (see `_level_groups`) starts when the leader, the shade
    furthest from the target, reaches the group's first shade.
    """
    groups = _level_groups(moves, tolerance_height)
    if not groups:
        return []
    leader = groups[0][0]
    # The leader's group starts at once: converting the leader's hemline back
    # to a position can leave a tiny nonzero delay.
    timed = [(groups[0], 0.0)]
    for group in groups[1:]:
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
        timed.append((group, delay_s))
    return timed


def _starter_fit(
    starter: Starter,
    group: list[Move],
    positions_pct_by_id: Mapping[str, int],
    moving: Collection[str],
    travelling: Mapping[str, Direction],
) -> set[str] | None:
    """The group's shades a starter would start, or None if it can't be used.

    A starter moves every one of its shades that isn't already at its target,
    all at once, so each of those must be in this group (moving now, from this
    level) and be sent the way its move goes. A shade that should end up
    elsewhere gets a follow-up command. A shade already at the starter's
    position isn't started by it, and mustn't be moving separately.

    Its shades already travelling must all be turned around: shades going on
    the way they're going need only their new targets, and one the starter
    would send to where it's estimated to be might be stopped or turned back.
    (A Pico pressed while its shades move stops them, so its Stop is pressed
    first.)
    """
    moves_by_id = {move.shade.entity_id: move for move in group}
    covered = set()
    for entity_id, starter_pct in starter.targets.items():
        if entity_id not in positions_pct_by_id:
            return None
        from_pct = positions_pct_by_id[entity_id]
        if starter_pct == from_pct and entity_id in travelling:
            return None
        if starter_pct == from_pct:
            # It doesn't start a shade already at its position: fine if that
            # shade stays put or starts now with this group, but one moving
            # with another group may have left by the time the starter fires,
            # and would be sent back.
            if entity_id in moves_by_id or entity_id not in moving:
                continue
            return None
        if (move := moves_by_id.get(entity_id)) is None:
            return None
        if (starter_pct > from_pct) != (move.direction() is Direction.OPENING):
            return None
        if travelling.get(entity_id) is move.direction():
            return None
        covered.add(entity_id)
    return covered or None
