# Aligned Cover Group — design

A Home Assistant custom integration (HACS-installable) that groups side-by-side
window shades of different sizes into a single `cover` entity whose bottom edges
("hemlines") line up — both at rest and while moving.

## Goals

1. **Alignment** — shades of different heights/positions present an even
   hemline. The group's position stands for a shared hemline height, not a
   percentage passed to every shade.
2. **Synchronized motion** — when a Lutron Pico is paired (on the Caseta bridge)
   to exactly the shades in the group, the group uses Pico button presses so
   every shade starts/stops at the same instant.

Pico support is optional; without one the group still aligns shades using
per-shade commands.

Some shade systems do this natively: Lutron's [Intelligent Hembar
Alignment](https://www.lutron.com/us/en/window-treatments/shades/roller-shades)
(IHA, on lines such as Sivoia QS and Palladiom) monitors shade speed hundreds of
times per second to keep grouped shades within 1/8 in (3 mm), in motion or
stopped, even when windows are different sizes. This integration is a
workaround for shades without it, such as Lutron Serena.

## Target hardware and assumptions

Built and tested with Lutron Serena roller shades on a Caseta bridge. The
design relies on:

- **Positions count motor rotation at a steady speed** (most motorized roller
  shades): 50% is half the run time from fully open, not necessarily half the
  height. See "Roll profiles".
- **Shades report their destination immediately** when commanded, and their
  real position only when stopped; never opening/closing. See "Motion".
- **For the roller-curve correction, matched rolls:** at any given hemline
  height, every shade has the same amount of fabric on the same kind of roll.
- **All shades move at the same speed** (same motor and roll), for staying
  level while moving.

## Configuration (UI only)

Created via a config flow, edited via an options flow. No YAML. The name is
the config entry's title; everything else is stored in its options:

```json
{
  "shades": [
    {"entity_id": "cover.left_1", "closed_height": 49.75, "open_height": 125.125},
    {"entity_id": "cover.left_2", "closed_height": 17.875, "open_height": 125.125,
     "travel_time_s": 24.0, "halfway_height": 67.125}
  ],
  "controls": [
    {"type": "pico", "shades": ["cover.left_1", "cover.left_2"],
     "open": "button.pico_on", "stop": "button.pico_stop", "close": "button.pico_off"}
  ]
}
```

| Field | Notes |
|---|---|
| shades | 2+ cover entities that support `set_position` and `stop`, in the order chosen |
| closed_height | hemline height when fully closed |
| open_height | hemline height when fully open (> closed_height) |
| travel_time_s | the measured shade's full close→open travel, in seconds |
| halfway_height | optional: the measured shade's hemline height at 50% (see "Roll profiles") |
| controls | ways to start several shades at once through the bridge (see below); may be empty |

Measurements are stored with the shade they were taken on (the measured
shade), not for the group: exactly one shade has `travel_time_s`. Setup
measures the tallest shade (longest range), which is the most accurate to
time; editing the group asks again, suggesting the previous answers only if
the same shade is still the tallest. Storing them with a shade also leaves
room for mismatched rolls, where each shade would have its own.

Each control has a `type` and the `shades` it moves, which may be only some of
the group's (a Pico paired to two of three windows, say). Today the only type
is `pico`. The setup form asks for the Pico device and stores the entity ids
of its On, Stop and Off buttons (`open`, `stop`, `close`), found by
`pico.py`: the Caseta integration names each button entity after the Pico,
ending in the button's name (not Raise or Lower, which nudge shades). The
form rejects a Pico without all three, or with any of them disabled (Caseta
disables them by default). Storing the buttons keeps the settings readable
and means only the form depends on Caseta's naming; like the shades, they're
stored by entity id, so renaming one means editing the group. Editing
suggests the device the stored buttons are on.

The setup form only offers one Pico, paired to every shade, and the planner
uses only such a Pico. The list leaves room for Picos paired to subsets of the
shades, and for Lutron scenes (`"type": "scene"` with each shade's scene
position): pressing or activating one starts its shades together, and
follow-up `set_position` commands retarget any that should stop elsewhere.

Heights use any unit, as long as every shade uses the same reference (e.g.
inches from the floor). Tops do not need to match, but every shade's range must
overlap the others' (side-by-side windows, not stacked ones): with no shared
height, "level" has no meaning.

Every shade is assumed to move at one shared speed (height units per second),
the same up and down: hemlines can only stay level while moving if they move
at the same speed. The speed comes from the measured shade, and each shade's
travel time is derived from its own range. Shades with
different speeds still end level, since each stops at its own target, but
drift apart mid-move.

## Integration and device type

Semantically this is a helper (a virtual cover computed from real shades),
but it's declared as a `device` integration, and each group gets a device:

- Helpers have no easily reachable integration page; a device integration is
  listed under Integrations, with its own page and debug-logging toggle.
- Each group's device can be assigned to an area (HA offers this right after
  setup), which helper entities can't do from the setup flow.
- Device rather than service: the group controls real shades, if by proxy,
  so it's listed and used like the devices it stands for.

## Position math

### Roll profiles

A shade's position counts motor rotation, not height. The motor turns steadily,
so 50% is half the shade's run time from fully open. A roller's roll is fattest
when open, though, so the hemline moves fastest near the top and slows steadily
as the roll shrinks; half the run time covers more than half the height.

A roll that shrinks steadily makes hemline height a quadratic in position, so
the measured shade has a `RollProfile`: the quadratic through its heights at
0% (closed), 50% (`halfway_height`) and 100% (open). With `halfway_height` at
the midpoint it's a straight line, which is also what's used when no 50% height
is set. Valid halfway heights are above a quarter of the way up and at most the
midpoint (a roll can't get fatter as it unwinds, and below a quarter the curve
would turn back on itself).

**Matched rolls (the only case configurable today).** Every shade's roll is
assumed to match the others' at any given hemline height: same fabric and tube,
and the same amount of fabric wound on whenever the hemlines are level. Then
every shade shares one roll profile, differing only in the heights its view
runs between.
`matched_roll_group` builds the group from the settings:

- The measured shade (the one with a travel time; the tallest, at setup)
  defines the only `RollProfile`, validated once.
- Every shade sees a `RollProfileView` of it (`profile.view(closed, open)`):
  the profile rescaled to its own limits, as its own 0-100%. Within a view,
  positions rescale in a straight line (the motor turns at a steady speed). A shade reaching above or
  below the measured one has a view extending beyond the profile's 0-100%:
  that's the matched-roll assumption applied beyond the measured range. If the
  profile would flatten out before reaching a shade (as if the roll ran out of
  fabric), the setup flow rejects the measurement.
- The group's own position is a view too, from the lowest closed to the
  highest open height, so it matches the position of a shade spanning that
  whole range: the tallest shade, when its range covers every other one's
  (such as shades sharing a top).
- A shade's travel time is its view's share of the measured shade's, times
  the measured travel time.
- Aligned shades keep pace while moving: both have the same remaining roll at
  the same height.

**Room for mismatched rolls.** The planner only relies on each shade's own
view, so shades with different fabrics or tubes could each get their own
profile (and a view of all of it) from their own 50% measurement. Positions
would still match at rest; while moving they'd drift (their hemline speeds
differ), and staggered starts would only line them up as each one starts.

Fitted on two shades from a 67 1/8 in reading at 50%, the curve predicted the
other five measurements (25/50/75% on both) to within 3/8 in, where a straight
line was off by up to 4 3/8 in.

### Group position

- Group range: lowest `closed_height` (0%) to highest `open_height` (100%),
  following the shared roll profile, so a shade spanning that range sits at
  exactly the group's position (identical shades, or the tallest when its
  range covers every other one's).
- Group position → hemline `H` (group curve) → each shade's position for `H`
  (its own curve, clamped to its range).
- Aligned: every shade's hemline is within 1% of the group's range of one
  height `H`, where a shade at a limit counts as level with any height past
  it (fully closed: any height at or below its closed height). So `H` must
  be at least the hemline of every shade that isn't fully closed, and at most
  that of every shade that isn't fully open; it's the middle of that range, or
  the group's closed/open height when every shade is fully closed/open.
- Reported position: `H` when aligned, otherwise the average hemline. This
  degrades to the average (like HA's cover group) while keeping the slider
  stable after the group itself moved the shades.

## Motion

Commands are open-loop. Caseta shades report their *destination* as soon as
they're commanded (and their real position only when stopped), never
opening/closing, so timing comes from the shared speed. Each command makes a
*plan*: a *move* for each shade (target and start delay), plus an optional
Pico press. Running the plan carries out its moves. While a plan runs, a new
command plans from *estimated* positions (start position, start time, speed)
rather than the reported ones, which already show the destinations.

While a plan runs, the group reports where it's heading, as Caseta shades do:
its position and attributes come from each moving shade's target. Dashboard
sliders always show the reported position, so reporting progress would make a
slider jump back from where it was dropped. Once the plan ends or is stopped,
the group reports from the shades again, which by then report where they
really are.

A shade still on an earlier move that already sits at its new target
is sent a command to hold there, otherwise it would carry on to its old target.

**Pico path** — used when a Pico is configured *and* every shade the Pico would
move (all shades not already at the endpoint in the direction of travel) starts
from the same hemline. Press Pico open/close, then immediately send
`set_position` to shades whose target is short of the endpoint.

**Staggered path** — otherwise. Shades moving up start in order from lowest
hemline; each one starts when the leader's estimated hemline reaches it (and
vice versa for moving down). Implemented as delayed `set_position` calls.

Staggered starts: a follower starts once the leader has moved from where it
started to the follower's hemline; since positions change at a constant rate,
that's the leader's change in position as a share of its travel time.

Delayed starts and the end of the run are scheduled at absolute times from
when the run began, so slow commands to the bridge don't push later starts
back. A run ends exactly at its planned end: since shades report their
destination immediately, there's no way to see them actually arrive. There's
deliberately no margin, because ending late is worse than ending early: a stop
that's still sent through the Pico after the shades have stopped makes them go
to their favorite position, while one sent just too early merely stops each
shade separately. For the same reason, round a measured travel time down. If
a shade reports a position that isn't part of the plan (another command, such
as a physical Pico press, took over), the group stops following its plan. If a
starting command fails, the plan is abandoned and the error returned to the
caller.

**Stop** — cancel any pending staggered starts, then:
- Pico configured and the group believes it is moving → press Pico stop.
- Otherwise → `stop_cover` on each shade.

Caveat to verify: on a shade Pico the middle button means "stop" while moving
but "go to favorite" when stationary, so the Pico stop is only pressed while
the group thinks it's moving.

## Out of scope (v1)

Tilt, non-linear calibration, YAML config, non-Lutron remotes, multiple presets,
separate up/down speeds.
