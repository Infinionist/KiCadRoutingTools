# movie3d: a 3D placement movie (prototype)

A 3D take on `py_placer/place_movie.py`. It tells the same placement story
(the input with its piles, the fixed parts flying to their places, the
scatter, the anneal, the best placement) on a 3D board, rendered to an .mp4.
It covers placement only, with no routing. It is a prototype, to judge whether
a 3D movie is worth more work.

Everything lives in this directory. It imports the repo's Python modules
read-only (`place_simple` and its `placement` package, `kicad_parser`,
`render_theme`, `render_placement.group_palette`) and changes no file outside
`movie3d/`. Its Node libraries are installed in `movie3d/node_modules` only.

## Setup

```bash
cd movie3d && npm ci
```

This installs `playwright-core` 1.52.0 and `three` 0.186.1, pinned by
`package-lock.json` (about 31 MB, in `movie3d/node_modules`).

**Browser.** playwright-core 1.52.0 drives Chromium build 1169. If
Playwright's per-user cache already holds `chromium_headless_shell-1169`, you
need nothing more. Any project on Playwright 1.52 leaves it there
(`~/Library/Caches/ms-playwright` on macOS). Otherwise, either run
`npx playwright-core install chromium-headless-shell` from `movie3d/`, which
downloads it into that per-user cache, or point `--browser` (or
`$MOVIE3D_CHROMIUM`) at a Chrome or Chromium executable.

Also needed: `ffmpeg` on PATH and Node 18 or newer. Python needs nothing
beyond the repo's own `requirements.txt`.

## Make a movie

```bash
python3 -X utf8 movie3d/make_movie.py BOARD OUT.kicad_pcb \
    --groups groups.json --intent intent.json --seed 0
```

BOARD and OUT come first. Every argument other than make_movie's own is passed
to `place_simple.py`. OUT is place_simple's placed board, the same placement
place_simple gives for that seed. The movie is written to OUT's name +
`_3d.mp4`, or to `--movie`. Three stills go beside it: `_start.png`,
`_anneal.png` (mid-anneal) and `_final.png`. The JSON and the frames go to
OUT's name + `_movie3d/` (or `--work`).

From a fresh clone (tigard, 89 parts):

```bash
mkdir -p movie3d/out
python3 py_tools/check_floorplan.py kicad_files/tigard.kicad_pcb --emit-intent movie3d/out/tigard.intent.json
python3 -X utf8 movie3d/make_movie.py kicad_files/tigard.kicad_pcb movie3d/out/tigard_placed.kicad_pcb \
    --groups docs/examples/tigard.groups.json --intent movie3d/out/tigard.intent.json --seed 0
```

On an M-series Mac this takes about 35 s: record 12 s (place_simple's own
anneal), render 21 s (213 frames at 90 ms each, SwiftShader), encode 1 s. The
result is 18 s of movie at 12 fps, 3.1 MB.

Own options: `--movie`, `--fps` (12), `--width`/`--height` (1000x560),
`--frames` (anneal frames, 120), `--speedup` (how much faster the anneal plays
at its end, 4), `--work`, `--browser`, `--crf` (20).

## What it shows

- **The board**: its outline extruded to the stackup's thickness, standing
  above a table.
- **Each part**:
  - its pads, with dark drills;
  - its courtyard, as a plate tinted in its group's colour;
  - a body box. Connectors and switches use the courtyard, inset 0.25 mm; all
    other parts use the pad field, inset. The height is a guess from the
    footprint name or the reference prefix (`NAME_HEIGHTS` and
    `PREFIX_HEIGHTS` in `record.py`).
  - Fixed parts are grey and see-through, so they never hide the parts that
    move. Unassembled parts with no model (test pads, solder jumpers) get no
    body. A back-side part hangs below the board. Parts off the board lie on
    the table.
- **Airwires**: the nets the objective prices (ground is left out), as one
  spanning tree per net, recomputed every frame.
- **Moves**: parts that moved since the last frame are outlined in white.
- **Text**: place_movie's caption at the top left, and the group key along the
  bottom.

The steps, in order:

1. The input: the board, with the piles on the table beside it.
2. The fixed parts arc to their places, biggest first.
3. The scatter: the pile takes off part by part and arcs to the seeded places,
   while the camera closes in on the board.
4. Edge seats, when the intent declares any.
5. The anneal, sampled on place_movie's schedule and playing faster as it goes.
6. The best placement.
7. Placed, held for 3 s.

Over the whole movie the camera orbits slowly, from 25 to 80 degrees of
azimuth at 38 degrees of elevation. Each frame is fitted to the board, or to
the board and the piles.

## How it works

- `record.py` runs `place_simple.main` in-process. It uses place_movie's hooks
  (`build`, `scatter`, `anneal`, and `Annealer.start_temperature`/`trial`/`overlap`),
  which only read the state and pass every call through. It writes two files:
  - `scene.json`: every part in its own frame (pads, courtyard, body, side,
    colour), plus the nets and the key;
  - `timeline.json`: one row per frame, with every part's
    `[x, y, rot, height]`, the caption, the parts that moved, the camera's box
    and its azimuth.
- `render.mjs` uses Node and playwright-core to open `page/index.html` in
  headless Chromium on SwiftShader (CPU WebGL, so the pixels do not depend on
  the GPU). The page is served through `page.route` from a fake host, so
  nothing leaves the machine. For each frame it calls `m3dFrame(i)` and
  screenshots the result to `frames/f00000.png`, and so on.
- `page/movie3d.mjs` is the three.js scene. It is a pure function of
  (scene, timeline, frame index).
- `make_movie.py` runs the three steps above, then ffmpeg.

**Reused from the removed `py_router/stage3d/` (#1081, still on `main`):**
- the page design: one pure function per frame, an importmap, served through
  `page.route`;
- the skeleton of `render.mjs`: SwiftShader, CDP screenshots, JSON-lines
  progress;
- the board extrusion and the frame mapping (KiCad (x, y) to three (x, z), and
  a KiCad rotation to `rotation.y`);
- the body and not-assembled rules;
- the three.js version (r186).

The real-model (kicad-cli GLB), copper-timeline and Blender paths were not
carried over.

## Limits

- Placement only: there is no `--route` flow and no round of seeds.
- Bodies are boxes with guessed heights, with no real 3D models. A custom pad
  is drawn as its bounding box. No copper, silkscreen or zones are drawn.
- A part the intent puts on the other face, or at a declared pose, is drawn
  there from the first frame. The 2D movie shows it moving there.
- Anneal frames are snapshots, so parts jump between frames, as in the 2D
  movie.
- The story code (piles, schedule, captions) is a copy of place_movie's, not
  shared with it, so the two can drift apart.
- Airwires are 1 px lines, because WebGL draws lines 1 px wide.
- The fixed orbit can put a tall part in front of the action.
- Tested on two boards: a 25-part one and tigard.
