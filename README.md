<p align="center">
  <img src="https://raw.githubusercontent.com/alexblanck/aligned-shade-group/main/custom_components/aligned_shade_group/brand/icon@2x.png" alt="" width="128">
</p>

# Aligned Shade Group

Keep side-by-side window shades level and moving in sync, even when the windows
are different sizes. Each group is a single cover entity that you control like
any other.

## Why

### Different-size shades

[Home Assistant cover groups](https://www.home-assistant.io/integrations/group/)
send every shade the same percentage. Set one to 50% and each shade goes to 50%
of *its own* travel, so their bottom edges (hemlines) end up at different
heights. Aligned Shade Group works out the position that puts each shade's
hemline at the same height, and times their starts so they stay level while
moving. Roller shades' hemlines move faster near the top, where the roll is
thickest. Aligned Shade Group can't change that, but does take it into account
when working out each shade's position, so their hemlines still line up.

### Same-size shades

Same-size Lutron shades benefit too. The Caseta bridge handles commands one at
a time, so shades in a cover group start a fraction of a second apart. A Pico
remote or Lutron scene starts them together, but only if you remember to use it
instead of the group in every automation. Aligned Shade Group uses your Picos
and scenes for you: it starts the shades using a Pico or scene, then sends
each its exact position once they're moving.

## Install and configure

1. HACS → ⋮ → Custom repositories → add
   `https://github.com/alexblanck/aligned-shade-group` as an **Integration**.
2. Install **Aligned Shade Group** and restart Home Assistant.
3. Settings → Devices & services → Add integration → **Aligned Shade Group**.

Setup asks for each shade's hemline height when fully open and fully closed
(measured from a common point, such as the floor) and how long the tallest
shade takes to travel fully. Picos and scenes are optional. Each group also
gets a **Realign** button that levels its shades again if they drift. See the
[installation and setup guide](docs/INSTALLATION.md) for details, including
Picos, scenes and troubleshooting.

## Works with

Built and tested with Lutron Serena roller shades on a Caseta bridge. Other
motorized shades that move at a steady speed and report a position may work but
are untested; see [Supported shades](docs/INSTALLATION.md#supported-shades).

## More

- [How it works](docs/DESIGN.md): design notes and decisions
- [Development](DEVELOPMENT.md): working on the integration

By [@alexblanck](https://github.com/alexblanck), built with assistance from
[Claude Code](https://claude.com/claude-code). [MIT licensed](LICENSE).
