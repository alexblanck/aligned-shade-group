# Ideas and known problems

Things to look into later. Newest thinking at the top of each entry; move an
entry to DESIGN.md (or delete it) once it's done.

## Problems

### Positions reported after a stop are inaccurate

Seen on the living room Serena shades: after a stop, re-driving a shade to the
percent it reported moves it noticeably. So the position Caseta reports once a
shade stops mid-travel is only approximate, worse than the whole-percent
rounding the integration already allows for.

Effects: the group plans its next move from those positions, so staggered
starts after a stop can be off, and the group may report shades as aligned (or
not) when they aren't.

To find out:

- How far off is it, and is it consistent? Stop a shade, note the reported
  percent, measure the hemline, re-drive to the same percent and measure again.
  Try stops while opening and while closing, and with the Pico vs `stop_cover`.
- Does the error depend on direction (overshoot after stopping)?

Possible approaches:

- When the group itself stopped the shades, trust its own estimate (from the
  plan's timing) over the reported percent for planning the next move.
- After a stop, re-drive each shade to the reported percent so the shade's
  physical position matches what it reports (see "Sync positions after a
  stop" below).
- If the error is consistent per direction, correct for it.

## Enhancements

### A hub for overlapping groups

Overlapping groups work today as independent groups. Example: five shades
across one wall, set up as "left" (two shades, with their Pico), "right"
(three, with theirs) and "all" (five, with a five-shade Pico).

What's awkward about that:

- The measurements are entered once per group.
- Each group has its own idea of where the shades are. If "all" is running
  and "left" takes over its two shades, "all" sees positions it didn't send,
  treats it as an outside command and stops following its plan, so it loses
  track of the right three too until its next command.

A hub would be one integration entry for the wall: the shades, the
measurements (entered once) and the Picos, with groups defined inside it
(say, as config subentries via an "Add group" button), each its own cover
entity and device. All the groups would share one planner and one view of
each shade's position and motion, so a group taking over some shades hands
them over cleanly and the others keep tracking the rest. Each group would
use the Picos (or scenes) that cover its shades.

The cost is a different setup flow and the coordination logic, so it's
worth it only if overlapping independent groups prove annoying in practice.

### Sync positions after a stop

An option to send each shade a follow-up `set_position` to the percent it
reported once it has stopped, so its physical position matches its report
(addresses the inaccurate reports above).

- Wait until the shades have actually stopped and reported, rather than
  sending it with the stop.
- Costs a small extra movement after every stop, hence optional.
- Should apply however the stop happened: the group's stop button, a Pico
  press or another command; or only to stops the group made, which are the
  ones it can tell happened.
- After the sync the shades are where they say, but not necessarily level
  with each other: whole-percent positions can still leave them slightly
  apart.

### Skip measurements for identical shades

Shades with the same limits (same size, hung at the same height) and matched
rolls are level whenever they're at the same percentage, so heights and the
50% measurement add nothing: the group's percentage can go straight to every
shade. A setup option ("my shades are identical") could skip the per-shade
height steps and the roll curve.

- The group would still be useful for its Pico or scene handling: starting
  and stopping the shades together, the steady slider while moving, and
  missing-device repairs.
- It still needs the travel time: the group only knows the shades are moving
  from its own plan, and pressing the Pico's Stop when nothing is moving
  sends the shades to their favorite position. Without a travel time it
  would have to stop each shade on its own instead.
- Staggering still applies if they start at different percentages, and it
  works from percentages alone: for identical shades, hemline order and
  percentage order are the same.

### Follow entity renames automatically

Today a renamed shade or Pico button raises a repair issue asking to
reconfigure. The group could instead listen for renames and update its stored
entity ids itself.

### Shades whose rolls don't match

Each shade could get its own roll profile from its own 50% measurement (the
planner only relies on each shade's view). Positions would match at rest;
while moving they'd drift, since their hemlines move at different speeds.
