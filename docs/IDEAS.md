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
  physical position matches what it reports (costs a small extra movement).
- If the error is consistent per direction, correct for it.

## Enhancements

### Start shades with Lutron scenes

Scenes could start shades together like a Pico press: activating a scene
starts all its shades at once toward their scene positions, then follow-up
`set_position` commands retarget any that should stop elsewhere (verified:
retargeting works whether the new target is short of or beyond the original).

- Config: a `controls` entry of `"type": "scene"` with the scene entity and
  each shade's scene position (Caseta doesn't expose a scene's contents, so
  they're entered). The shades a scene moves are the keys of its positions.
- Usable for a move when every shade it moves needs to move, starts in one
  level group (`_level_groups`), and its scene position is in the same
  direction as that shade's target. A scene that also moves devices outside
  the group (other shades, lights) shouldn't be used; document that rather
  than detect it.
- No stop: a run started by a scene is stopped with the Pico (if one covers
  the moving shades) or `stop_cover` on each shade.
- Setup flow: an optional, repeatable "add a control" step; or config
  subentries (an "Add scene" / "Add Pico" button on the group's page).

### Picos paired to some of the shades

The config already stores Picos with the shades they're paired to, but only a
Pico paired to every shade is used. With several Picos (say a whole-room Pico
and one per pair of windows), the planner could press whichever cover each
level group, falling back to commands for the rest; stop would press each Pico
whose shades are moving. Shares most of its logic with scenes.

### Overlapping groups

Example: five shades across one wall, two on the left (with their own Pico)
and three on the right (with theirs). The wish: control the left pair, the
right three, and all five, each kept level.

Options:

1. **Three independent groups** (left, right, all), which works today. Each
   plans on its own; a command to one looks like an outside command to the
   others, which simply stop following their plans. Downsides: the
   measurements are entered three times, and the five-shade group can't use
   either Pico (neither covers all five) until Picos paired to some of the
   shades are supported (above). Then it could press both Picos when each
   pair starts level.
2. **Groups of groups:** an "all" group whose members are the left and right
   groups. Not supported: the picker hides aligned groups as members, since
   commanding a group as if it were one shade bypasses its own staggered
   starts. Making it work means the outer group delegating to the inner
   ones' planners, which gets complicated.
3. **One group with subgroups:** one config entry for all five shades, with
   optional subgroups (say, as config subentries) that each get their own
   cover entity. One planner and one set of position estimates serve them
   all, so they can't conflict, measurements are entered once, and each
   Pico is a control on the shades it's paired to. Probably the cleanest
   long term, and it builds on the controls list and level groups.

Worth trying option 1 in practice first, to see what actually goes wrong.

### Follow entity renames automatically

Today a renamed shade or Pico button raises a repair issue asking to
reconfigure. The group could instead listen for renames and update its stored
entity ids itself.

### Shades whose rolls don't match

Each shade could get its own roll profile from its own 50% measurement (the
planner only relies on each shade's view). Positions would match at rest;
while moving they'd drift, since their hemlines move at different speeds.
