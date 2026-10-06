<img src="https://raw.githubusercontent.com/alexblanck/aligned-shade-group/main/custom_components/aligned_shade_group/brand/icon@2x.png" alt="" width="128" align="right">

# Aligned Shade Group

A Home Assistant cover group for side-by-side window shades of different
sizes: one cover whose bottom edges (hemlines) stay level — at rest and while
moving. Optionally uses Lutron Caseta Picos and scenes so shades start (and
stop) at exactly the same moment.

"Shades" means any window covering that raises and lowers with a position,
such as roller or cellular shades or blinds; the optional roller-curve
correction is for roller shades.

Some higher-end shades do this themselves: Lutron's [Intelligent Hembar
Alignment](https://www.lutron.com/us/en/window-treatments/shades/roller-shades)
(on lines such as Sivoia QS and Palladiom) monitors motor speed to keep grouped
shades within 1/8 in (3 mm) of each other, moving or stopped, even across
different window sizes. This integration is a workaround that brings simpler
shades, such as Lutron Serena, closer to that behavior.

## Supported shades

Built and tested with **Lutron Serena roller shades** on a **Caseta** bridge,
and it relies on how those behave:

- A shade's position counts motor rotation, and the motor turns at a steady
  speed, so 50% is half the run time from fully open (not necessarily half the
  height). This is true of most motorized roller shades.
- Shades report their destination as soon as they're commanded, not their
  position while moving, so the integration times their movement itself.
- For the optional roller-curve correction, the shades' rolls match: same
  fabric and tube, and the same amount of fabric wound on whenever their
  hemlines are level. Shades can still differ in where they start and stop.

Other shades that work the same way should work too, but haven't been tested.

## How it works

- You tell it each shade's hemline height when fully closed and fully open,
  measured from a common reference (e.g. inches from the floor), plus how long
  the tallest shade takes to travel fully.
- All shades are assumed to move at the same speed (inches per second), as
  shades from the same product line usually do. Shades that move at different
  speeds still end up level, but drift apart while moving.
- Roller shades don't move evenly: the roll is fattest when open, so the
  hemline drops faster near the top, and shades of different lengths drift out
  of line partway even when level at the ends. If that happens, enter the
  tallest shade's hemline height at 50% during setup. This assumes the shades'
  rolls match (see Supported shades).
- The group's 0–100% position stands for a shared hemline height (with the
  roller-curve correction, it matches the position of a shade spanning the
  group's whole range, such as the tallest one when all the tops line up). Each
  shade is sent to whatever position puts its hemline there (clamped to its
  own range).
- When shades start from different heights, the lowest (or highest) one starts
  first and the others join as its hemline reaches theirs.
- Like a single shade, every shade is sent its position even if it's already
  there. So setting the group to its current position realigns it: shades out
  of line meet at their average height, and ones that have drifted from where
  they report are re-seated. Each group's device has a **Realign** button
  that does this.
- Shades that start level start together: with a Pico press or scene where
  one covers them, so the bridge starts them in lockstep, and stops while
  moving go through the Picos. (While any of a shade Pico's shades is moving,
  every button stops them, and when all are still its middle button sends
  them to their favorite position; so the group only presses Stop while
  moving, and On or Off while still, or just after Stop to turn moving
  shades around together.)

See [docs/DESIGN.md](docs/DESIGN.md) for details.

## Installation (HACS)

1. HACS → ⋮ → Custom repositories → add this repository as an **Integration**.
2. Install **Aligned Shade Group** and restart Home Assistant.
3. Settings → Devices & services → Add integration → **Aligned Shade Group**.
   Each group gets its own device, which you can assign to an area.

## Picos and scenes (optional)

A Pico or Lutron scene starts several shades with one command, so they start
at exactly the same moment. When some of the group's shades start level, the
group uses any Pico or scene that covers just those shades, and commands the
rest itself. They're added on the last screen of the group's setup (or
Reconfigure).

- **Pico:** pair it in the Lutron app to some or all of the group's shades.
  In Home Assistant, open the Pico's device (Lutron Caseta integration) and
  enable its **On**, **Stop** and **Off** button entities, which are disabled
  by default; until they're enabled, the Pico isn't offered. Then choose the
  Pico and the shades it's paired to. The group finds its buttons itself, and
  also uses its Stop button to stop them.
- **Scene:** a Lutron scene that sets some of the shades to one position (for
  example, opens them). Choose the scene, its shades and that position. Use
  only scenes that move nothing else. Scenes start shades but can't stop
  them.

## Attributes

Besides the usual cover state and position, the group exposes:

| Attribute | Meaning |
|---|---|
| `entity_id` | The shades in the group |
| `aligned` | Whether every shade's hemline is level with the others (allowing for shades that are fully open or closed) |
| `hemline_heights` | Each shade's hemline height, in your configured unit |

Like the group's position, `aligned` and `hemline_heights` show where the
shades are heading while the group moves (as Caseta shades report their
destination), not where they are at that moment.

## Troubleshooting

**Download diagnostics** (in the ⋮ menu on the integration's page, or on a
group's device page) saves a JSON file with the group's settings, the heights
and speeds it derived from them, each shade's position and hemline height,
the plan being run, if any, and counts since Home Assistant started: runs (how
many used a Pico or scene, and how they ended) and the commands sent to each
shade, Pico button and scene. Attach it when reporting a problem.

Turn on debug logging to see each plan the group makes: which Picos and
scenes it used, every shade command with its delay, what the shades
report while it runs, and stops. Use **Enable
debug logging** on the integration's page, or for logging that survives
restarts:

```yaml
logger:
  logs:
    custom_components.aligned_shade_group: debug
```

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md).

## Credits

Designed, developed and tested with assistance from
[Claude](https://claude.com/claude-code) (Anthropic), working with
@alexblanck.

## License

[MIT](LICENSE)
