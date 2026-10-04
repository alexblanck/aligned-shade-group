# Development

How to work on Aligned Cover Group. For how alignment and motion work, see
[docs/DESIGN.md](docs/DESIGN.md).

## Project layout

| Path | What's there |
|---|---|
| `custom_components/aligned_cover_group/roll_profile.py` | Pure math: how a roller's hemline height follows its position (`RollProfile`), and a shade's view of one. No Home Assistant imports |
| `custom_components/aligned_cover_group/alignment.py` | Pure math: building a group from its settings, alignment, motion plans. No Home Assistant imports |
| `custom_components/aligned_cover_group/cover.py` | The group entity: runs plans as service calls and timers, estimates positions while a plan runs |
| `custom_components/aligned_cover_group/pico.py` | Finding a Pico's On/Stop/Off buttons from its device |
| `custom_components/aligned_cover_group/config_flow.py` | Create/edit screens (config and options flows) |
| `custom_components/aligned_cover_group/diagnostics.py` | "Download diagnostics": settings plus the group's live state |
| `custom_components/aligned_cover_group/translations/en.json` | UI text for those screens |
| `tests/sim.py` | Simulated shades and Pico used by the scenario tests |
| `tests/test_room.py` | End-to-end scenarios (most tests live here) |
| `docs/DESIGN.md` | Design notes and decisions |

## Setup

Requires Python 3.14+ (the minimum for current Home Assistant).

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements_test.txt ruff mypy
```

No Python 3.14 installed? [uv](https://docs.astral.sh/uv/) can fetch one:
`uv venv --python 3.14 .venv`, then `uv pip install -r requirements_test.txt ruff mypy`.

`requirements_test.txt` pulls in `pytest-homeassistant-custom-component`, which
installs Home Assistant itself. mypy needs that installed in the same venv to
see Home Assistant's type hints. Ruff and mypy are deliberately not in the
requirements yet.

## Checks

Run all three before considering a change done:

```bash
pytest
ruff check custom_components tests && ruff format custom_components tests
mypy --strict custom_components/aligned_cover_group
```

Handy pytest variations:

```bash
pytest tests/test_room.py -k reverse                      # one scenario by name
pytest -k reverse -o log_cli=true --log-cli-level=DEBUG   # with the integration's debug logs
```

## Testing approach

Most bugs are at the seams, so tests favor whole flows over individual methods.

- [tests/sim.py](tests/sim.py) simulates a room: shades are real cover
  entities that move over (frozen, manually advanced) time at their travel
  speed, and a Pico that behaves like a Caseta shade Pico, including the middle
  button going to the favorite position when nothing is moving. Everything goes
  through real Home Assistant services, and the group is created through its
  config flow. Commands pass through a simulated bridge (`room.bridge`) that
  can add latency, hold commands until released, or fail, for testing timing
  and races.
- [tests/test_room.py](tests/test_room.py) drives the group like a user and
  checks hemline alignment after every tick, using math independent of the
  integration's. Simulated shades report like Caseta shades: their destination
  as soon as they're commanded, and their real position only when stopped.
- [tests/test_alignment.py](tests/test_alignment.py) keeps only edge cases of
  the math that are awkward to reach end to end;
  [tests/test_config_flow.py](tests/test_config_flow.py) covers validation
  errors.

When adding or changing behavior:

- Write a scenario in `test_room.py` first: set up the room, command the group
  through its services the way a user or automation would, advance time, and
  assert on what physically happened (shade positions, hemline alignment over
  the whole run, Pico presses, group state). Avoid asserting on internal
  method calls.
- If the real hardware does something the simulator doesn't model, extend
  `sim.py` to model it rather than working around it in a test.
- Add a unit test only when the math is subtle and hard to reach end to end.

The simulator fires timers with `async_fire_time_changed_exact`: the plain
`async_fire_time_changed` bumps the time by up to 0.5 s (to suit HA's polling
helpers), which would hide timing bugs.

## Code conventions

- Keep `roll_profile.py` and `alignment.py` free of Home Assistant imports; the entity in `cover.py`
  turns its plans into service calls.
- `*_pct` names hold positions in percent (0 closed, 100 open): whole numbers
  (`int`, like Home Assistant's `current_position`) where shades report or are
  sent them, fractional (`float`) where worked out along a curve, rounded only
  when commanding a shade. `*_height` names hold hemline heights in the user's
  unit; `*_s` names hold seconds.
- Construct dataclasses with keyword arguments (`Move(shade=shade,
  from_pct=0, target_pct=40)`), so each value's meaning is clear at the call.
- Make anything that calculates a method, not a property, so the call shows
  that work happens. Properties are for stored values, and for the ones Home
  Assistant requires (`current_cover_position`, `is_closed` and so on).
- A plan is a set of moves, one per shade; running a plan carries out its
  moves. Use "run" for the group's motion as a whole and "move" for a shade's
  part in it.
- Home Assistant's `async_` prefix means a coroutine or a `@callback`
  function, never a plain undecorated function.
- Debug logs should name the group (`self.entity_id`) so multiple groups can be
  told apart.

Adding a setting touches several files: a key in `const.py`, the schema and
validation in `config_flow.py` (both the create and edit flows), its label in
`translations/en.json`, and reading it in `cover.py`. Settings are stored in
the config entry's `options`.

## Trying it on real hardware

Either:

- **HACS:** develop on `main`; in HACS, Redownload the integration, pick
  `main`, and restart Home Assistant after each push.
- **Manual:** copy `custom_components/aligned_cover_group` into your Home
  Assistant config's `custom_components/` folder and restart.

Turn on debug logging (see the README) to see each plan the group makes: the positions
it planned from, whether it used the Pico (and why not), and every shade
command with its delay.

## Icon

The source is [assets/icon.svg](assets/icon.svg). Home Assistant shows the PNG
exports in `custom_components/aligned_cover_group/brand/` (`icon.png` at
256 px, `icon@2x.png` at 512 px, transparent background). After editing the
SVG, re-export both, for example with headless Chrome:

```bash
for size in 256 512; do
  echo "<body style=margin:0><img src=\"file://$PWD/assets/icon.svg\" width=$size height=$size style=display:block>" > /tmp/icon.html
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
    --hide-scrollbars --default-background-color=00000000 \
    --window-size=$size,$size --allow-file-access-from-files \
    --screenshot=/tmp/icon_$size.png file:///tmp/icon.html
done
cp /tmp/icon_256.png custom_components/aligned_cover_group/brand/icon.png
cp /tmp/icon_512.png custom_components/aligned_cover_group/brand/icon@2x.png
```

## Releasing

1. Bump `version` in `custom_components/aligned_cover_group/manifest.json`.
2. Commit, tag `vX.Y.Z`, and push the tag.
3. Publish a GitHub release for the tag; HACS offers releases as versions.

`hacs.json` sets the minimum Home Assistant version; raise it if the code
starts depending on newer Home Assistant APIs.
