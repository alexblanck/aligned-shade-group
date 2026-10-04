<img src="https://raw.githubusercontent.com/alexblanck/aligned-shade-group/main/custom_components/aligned_shade_group/brand/icon@2x.png" alt="" width="128" align="right">

# Aligned Shade Group

A Home Assistant cover group for side-by-side window shades of different
sizes: one cover whose bottom edges (hemlines) stay level — at rest and while
moving. Optionally drives a Lutron Caseta Pico so every shade starts and stops
at exactly the same moment.

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
- With a Pico configured, movements that start from a level hemline begin with
  a Pico press, and stops while moving go through the Pico, so the bridge
  starts and stops every shade in lockstep. (A shade Pico's middle button
  sends stationary shades to their favorite position, so it's only pressed
  while the group is moving.)

See [docs/DESIGN.md](docs/DESIGN.md) for details.

## Installation (HACS)

1. HACS → ⋮ → Custom repositories → add this repository as an **Integration**.
2. Install **Aligned Shade Group** and restart Home Assistant.
3. Settings → Devices & services → Add integration → **Aligned Shade Group**.
   Each group gets its own device, which you can assign to an area.

## Pico setup (optional)

1. In the Lutron app, pair a Pico to **exactly** the shades in the group.
2. In Home Assistant, open the Pico's device (Lutron Caseta integration) and
   enable its **On**, **Stop** and **Off** button entities, which are
   disabled by default.
3. When creating the group, choose the Pico in the **Pico remote** section.
   The group finds its On (up), Stop and Off (down) buttons itself.

## Attributes

Besides the usual cover state and position, the group exposes:

| Attribute | Meaning |
|---|---|
| `entity_id` | The shades in the group |
| `aligned` | Whether every shade's hemline is level with the others (allowing for shades that are fully open or closed) |
| `hemline_heights` | Each shade's current hemline height, in your configured unit |

## Troubleshooting

**Download diagnostics** (in the ⋮ menu on the integration's page, or on a
group's device page) saves a JSON file with the group's settings, the heights
and speeds it derived from them, each shade's position and hemline height, and
the plan being run, if any. Attach it when reporting a problem.

Turn on debug logging to see each plan the group makes: whether it used the
Pico (and why not), every shade command with its delay, and stops. Use **Enable
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
