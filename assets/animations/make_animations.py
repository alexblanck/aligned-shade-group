# ruff: noqa: E501  (long lines of SVG markup)
"""Draw the open -> 50% -> open animations, in the icon's style.

Writes two pairs of SVGs next to this script. Each pair compares a plain Home
Assistant cover group with an Aligned Shade Group, on the same timeline so
the two play in step side by side:

- different-size-cover-group.svg / different-size-aligned.svg: windows of different sizes.
  The cover group sends every shade 50% of its own travel; the aligned group
  staggers starts so the hemlines meet and stay level.
- same-size-cover-group.svg / same-size-aligned.svg: windows of the
  same size. The cover group's commands reach the shades one at a time, so
  they move out of step; the aligned group starts them together (with a Pico
  or scene).

Run with `python assets/animations/make_animations.py`. Heights are in the icon's
coordinates (y grows downward), and hemlines move in a straight line with
position (no roller curve).
"""

from dataclasses import dataclass
from pathlib import Path

DIFFERENT_WINDOWS = [(10, 22, 204), (94, 58, 140), (178, 40, 206)]  # x, top, height
EQUAL_WINDOWS = [(10, 34, 200), (94, 34, 200), (178, 34, 200)]
SPEED = 40.0  # hemline speed shared by every shade, units/s
PAUSE = 1.0  # wait before each command, s
# Gap between a cover group's commands reaching successive shades. The
# Caseta bridge takes about 0.1 s per command; this is exaggerated so the
# effect shows at this size.
COMMAND_GAP_S = 0.25
LINEAR = "0 0 1 1"


def open_hemline(top: float) -> float:
    return top + 6


def closed_hemline(top: float, height: float) -> float:
    return top + height - 4


def travel_s(from_h: float, to_h: float) -> float:
    return abs(to_h - from_h) / SPEED


@dataclass
class Run:
    """One version of the sequence: where each shade goes and when it starts."""

    title: str
    windows: list[tuple[float, float, float]]
    targets: list[float]
    down_delays: list[float]  # after the down command, s
    up_delays: list[float]  # after the up command, s
    hemline: bool  # draw the group's shared hemline

    def opens(self) -> list[float]:
        return [open_hemline(top) for _, top, _ in self.windows]

    def down_s(self) -> float:
        return max(
            d + travel_s(o, t)
            for o, t, d in zip(self.opens(), self.targets, self.down_delays)
        )

    def up_s(self) -> float:
        return max(
            d + travel_s(t, o)
            for o, t, d in zip(self.opens(), self.targets, self.up_delays)
        )


@dataclass
class Timeline:
    """When the commands go out, shared by both runs of a pair."""

    down_at: float
    up_at: float
    loop_s: float


def timeline(runs: list[Run]) -> Timeline:
    down_at = PAUSE
    up_at = down_at + max(run.down_s() for run in runs) + PAUSE
    return Timeline(
        down_at=down_at,
        up_at=up_at,
        loop_s=up_at + max(run.up_s() for run in runs) + PAUSE,
    )


def keyframes(
    when: Timeline, open_h: float, target_h: float, down_delay: float, up_delay: float
) -> list[tuple[float, float]]:
    """(time, hemline) for one shade: down to its target, then back up."""
    down_start = when.down_at + down_delay
    up_start = when.up_at + up_delay
    return [
        (0, open_h),
        (down_start, open_h),
        (down_start + travel_s(open_h, target_h), target_h),
        (up_start, target_h),
        (up_start + travel_s(target_h, open_h), open_h),
        (when.loop_s, open_h),
    ]


def animate(when: Timeline, attr: str, frames: list[tuple[float, float]], value) -> str:
    times = ";".join(f"{t / when.loop_s:.4f}" for t, _ in frames)
    values = ";".join(f"{value(h):.2f}" for _, h in frames)
    splines = ";".join([LINEAR] * (len(frames) - 1))
    return (
        f'<animate attributeName="{attr}" dur="{when.loop_s:.3f}s" repeatCount="indefinite" '
        f'calcMode="spline" keyTimes="{times}" values="{values}" keySplines="{splines}"/>'
    )


def svg(run: Run, when: Timeline) -> str:
    opens = run.opens()
    shades, rails = [], []
    for (x, top, _), open_h, target, down_delay, up_delay in zip(
        run.windows, opens, run.targets, run.down_delays, run.up_delays
    ):
        frames = keyframes(when, open_h, target, down_delay, up_delay)
        shades.append(
            f'<rect x="{x}" y="{top}" width="68" height="{open_h - top}" rx="6">'
            + animate(when, "height", frames, lambda h, top=top: h - top)
            + "</rect>"
        )
        rails.append(
            f'<rect x="{x + 1}" y="{open_h - 5}" width="66" height="10" rx="2.5">'
            + animate(when, "y", frames, lambda h: h - 5)
            + "</rect>"
        )

    line = ""
    if run.hemline:
        # The group's hemline: the highest shade's, which every other shade
        # joins on the way down and leaves at its own top on the way up.
        leader = opens.index(min(opens))
        frames = keyframes(
            when,
            opens[leader],
            run.targets[leader],
            run.down_delays[leader],
            run.up_delays[leader],
        )

        def y(h: float) -> float:
            return 12.8 + 0.9 * h + 0.6  # into the page's coordinates

        line = (
            f'\n  <line x1="3" y1="{y(opens[leader]):.2f}" x2="253" y2="{y(opens[leader]):.2f}" '
            'stroke="#FF6B3D" stroke-width="3.2" stroke-linecap="round" stroke-dasharray="6 6">'
            + animate(when, "y1", frames, y)
            + animate(when, "y2", frames, y)
            + "</line>"
        )

    frames_svg = "\n      ".join(
        f'<rect x="{x}" y="{top}" width="68" height="{height}" rx="6"/>'
        for x, top, height in run.windows
    )
    nl = "\n      "
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <title>{run.title}</title>
  <!-- Generated by make_animations.py -->
  <defs>
    <linearGradient id="glass" gradientUnits="userSpaceOnUse" x1="0" y1="122" x2="0" y2="246">
      <stop offset="0" stop-color="#FFCF6E"/>
      <stop offset="1" stop-color="#A9CCDB"/>
    </linearGradient>
  </defs>

  <g transform="translate(12.8 12.8) scale(0.9)">
    <g fill="url(#glass)">
      {frames_svg}
    </g>
    <g fill="#2E7182">
      {nl.join(shades)}
    </g>
    <g fill="none" stroke="#3B4A5A" stroke-width="8" stroke-linejoin="round">
      {frames_svg}
    </g>
    <g fill="#1F4F5C">
      {nl.join(rails)}
    </g>
  </g>{line}
</svg>
"""


def different_size_runs() -> list[Run]:
    windows = DIFFERENT_WINDOWS
    opens = [open_hemline(top) for _, top, _ in windows]
    closeds = [closed_hemline(top, height) for _, top, height in windows]
    # Aligned: one height halfway through the group's range. Moving down, the
    # highest hemline starts first and each other shade when it reaches it;
    # moving up from level, they all start at once and each stops at its top.
    level = (min(opens) + max(closeds)) / 2
    return [
        Run(
            title="Cover group: shades set to 50% end at different heights",
            windows=windows,
            targets=[(o + c) / 2 for o, c in zip(opens, closeds)],
            down_delays=[0.0] * len(windows),
            up_delays=[0.0] * len(windows),
            hemline=False,
        ),
        Run(
            title="Aligned Shade Group: shades set to 50% stay level",
            windows=windows,
            targets=[level] * len(windows),
            down_delays=[travel_s(min(opens), o) for o in opens],
            up_delays=[0.0] * len(windows),
            hemline=True,
        ),
    ]


def equal_size_runs() -> list[Run]:
    windows = EQUAL_WINDOWS
    _, top, height = windows[0]
    middle = (open_hemline(top) + closed_hemline(top, height)) / 2
    one_at_a_time = [i * COMMAND_GAP_S for i in range(len(windows))]
    together = [0.0] * len(windows)
    return [
        Run(
            title="Cover group: same-size shades start one after another",
            windows=windows,
            targets=[middle] * len(windows),
            down_delays=one_at_a_time,
            up_delays=one_at_a_time,
            hemline=False,
        ),
        Run(
            title="Aligned Shade Group: same-size shades start together",
            windows=windows,
            targets=[middle] * len(windows),
            down_delays=together,
            up_delays=together,
            hemline=True,
        ),
    ]


if __name__ == "__main__":
    here = Path(__file__).parent
    for names, runs in [
        (
            ["different-size-cover-group.svg", "different-size-aligned.svg"],
            different_size_runs(),
        ),
        (
            ["same-size-cover-group.svg", "same-size-aligned.svg"],
            equal_size_runs(),
        ),
    ]:
        when = timeline(runs)
        for name, run in zip(names, runs):
            (here / name).write_text(svg(run, when))
