"""Roll profiles: how a roller shade's hemline height follows its position.

Pure math with no Home Assistant imports, like `alignment`, which builds shades
and groups on top of these. See `alignment` for the naming conventions.

Positions here are percentages, 0 to 100 (not fractions, 0 to 1), and are
fractional: rounding only happens when a shade is commanded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def halfway_height_range(
    closed_height: float, open_height: float
) -> tuple[float, float]:
    """Halfway heights (exclusive low, inclusive high) a roller can have.

    A roll is fattest when open, so the hemline moves fastest at the top and
    slows on the way down without stopping. With x the position (0 closed, 1
    open) and y the fraction of the way up, the two extremes are y = x (never
    slows: 50% is at the midpoint) and y = x**2 (slows to a stop right at
    closed: 50% is 0.5**2 = a quarter of the way up). A real roller is in
    between.
    """
    span = open_height - closed_height
    return closed_height + span / 4, closed_height + span / 2


@dataclass(frozen=True)
class RollProfile:
    """How hemline height follows motor rotation for one kind of roll.

    Most motorized roller shades, including the Lutron Serena shades this was
    built with, measure position as motor rotation at a steady speed, so 50%
    is half the run time. Rotation isn't proportional to height, though: the
    roll is fattest when open, so the hemline moves faster near the top. A roll
    that shrinks steadily as it unwinds makes height a quadratic in position,
    so the profile is the quadratic through the measured shade's heights at
    closed (0%), halfway (50%) and open (100%). With the halfway height at the
    midpoint it's a straight line.

    Shades whose rolls match at every hemline height all share one
    RollProfile, each through a view rescaled to its own limits (see `view`).
    Views of shades reaching past the measured one extend beyond its 0-100%.
    """

    closed_height: float
    open_height: float
    halfway_height: float
    # Coefficients of height = closed + span * (rise * x + bend * x**2), where
    # x is the position as a fraction.
    rise: float = field(init=False, repr=False)
    bend: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Reject curves no shade could follow (see `halfway_height_range`)."""
        if not self.open_height > self.closed_height:
            raise ValueError(
                f"Open height {self.open_height} must be above "
                f"closed height {self.closed_height}"
            )
        low, high = halfway_height_range(self.closed_height, self.open_height)
        if not low < self.halfway_height <= high:
            raise ValueError(
                f"Halfway height {self.halfway_height} must be above {low} "
                f"and at most {high}"
            )
        halfway_fraction = (self.halfway_height - self.closed_height) / self._span()
        # The class is frozen, so derived fields are set by bypassing its
        # __setattr__ (the standard way for frozen dataclasses).
        object.__setattr__(self, "rise", 4 * halfway_fraction - 1)
        object.__setattr__(self, "bend", 2 - 4 * halfway_fraction)

    @classmethod
    def straight(cls, closed_height: float, open_height: float) -> RollProfile:
        """A profile where height is proportional to position."""
        return cls(
            closed_height=closed_height,
            open_height=open_height,
            halfway_height=closed_height + (open_height - closed_height) / 2,
        )

    def height_for_position(self, position_pct: float) -> float:
        """Hemline height at a position (which may be beyond 0-100%)."""
        x = position_pct / 100
        return self.closed_height + self._span() * (self.rise * x + self.bend * x**2)

    def position_for_height(self, hemline_height: float) -> float:
        """Position for a height, possibly beyond 0-100%.

        Raises ValueError for heights the profile never reaches: below its
        range, it eventually flattens out, as if the roll ran out of fabric.
        """
        fraction_up = (hemline_height - self.closed_height) / self._span()
        # Solves rise * x + bend * x**2 = fraction_up for x. The usual formula,
        # (-rise + sqrt(discriminant)) / (2 * bend), loses accuracy as bend
        # nears zero (a near-straight profile) and fails at zero; this
        # equivalent form doesn't.
        discriminant = self.rise**2 + 4 * self.bend * fraction_up
        denominator = self.rise + math.sqrt(max(discriminant, 0.0))
        if discriminant < 0 or denominator <= 0:
            raise ValueError(f"The roll profile never reaches {hemline_height}")
        return 100 * 2 * fraction_up / denominator

    def view(self, closed_height: float, open_height: float) -> RollProfileView:
        """This profile rescaled to run from `closed_height` to `open_height`."""
        return RollProfileView(
            profile=self, closed_height=closed_height, open_height=open_height
        )

    def _span(self) -> float:
        return self.open_height - self.closed_height


@dataclass(frozen=True)
class RollProfileView:
    """A roll profile rescaled to two heights, as its own 0-100%.

    Each shade has a view, and so does the group (for its own position). A
    shade's view runs from its closed height to its open height; the group's
    runs from the lowest closed height of all its shades to the highest open
    height. Either can extend beyond the parent profile's 0-100%.
    A view's 0% and 100% (`closed_height` and `open_height`) fall at parent
    profile positions `closed_pct` and `open_pct`.

    Positions in between map linearly onto that stretch of the parent
    profile; heights and positions outside the view are clamped to its ends.
    Raises ValueError if the parent profile can't reach one of the heights.
    """

    profile: RollProfile
    closed_height: float
    open_height: float
    closed_pct: float = field(init=False)
    open_pct: float = field(init=False)
    # How much of the parent profile's rotation the view covers, as a fraction
    # of the profile's full travel (above 1 for a view reaching past it).
    profile_fraction: float = field(init=False)

    def __post_init__(self) -> None:
        """Find where the view's ends fall on the profile."""
        closed_pct = self.profile.position_for_height(self.closed_height)
        open_pct = self.profile.position_for_height(self.open_height)
        # The class is frozen, so derived fields are set by bypassing its
        # __setattr__ (the standard way for frozen dataclasses).
        object.__setattr__(self, "closed_pct", closed_pct)
        object.__setattr__(self, "open_pct", open_pct)
        object.__setattr__(self, "profile_fraction", (open_pct - closed_pct) / 100)

    def height_for_position(self, position_pct: float) -> float:
        """Hemline height at a position in this view (exact at its ends)."""
        if position_pct <= 0:
            return self.closed_height
        if position_pct >= 100:
            return self.open_height
        return self.profile.height_for_position(
            self.closed_pct + position_pct * self.profile_fraction
        )

    def clamp(self, hemline_height: float) -> float:
        """The closest height within the view."""
        return min(max(hemline_height, self.closed_height), self.open_height)

    def position_for_height(self, hemline_height: float) -> float:
        """Position in this view for a height."""
        profile_pct = self.profile.position_for_height(self.clamp(hemline_height))
        return (profile_pct - self.closed_pct) / self.profile_fraction
