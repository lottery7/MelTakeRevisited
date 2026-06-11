MelTake
=======

Python rewrite of the original C++/Qt MelTake music player.

Run with:

```sh
uv run meltake
```

Optional visualizer background:

```sh
uv run meltake /path/to/background.png
```

Custom icons can be placed in `icons/` as SVG or PNG files. SVG is preferred
when both exist. These names are used when present, otherwise the bundled icons
are used:

`previous.png`, `play.png`, `pause.png`, `next.png`, `random.png`, `loop.png`,
`volume_on.png`, `volume_off.png`, `cross_clicked.png`, `lock.png`, `menu.png`.

The app keeps using the original GLSL fragment shader at `MelTake/shaders/main.frag`
and the existing image assets under `MelTake/resources`.
