---
name: make-3d-movie
description: Makes the 3D movie of a board's placement, and with a recorded routing chain its routes (movie3d/make_movie.py). First looks on the web for the 3D models of parts the installed KiCad library lacks, so they are drawn as parts rather than boxes. Use it to make, remake or improve a 3D placement or flow movie, or to fill in a board's missing 3D models.
---

# Make a 3D movie

The movie itself is `movie3d/make_movie.py`; `movie3d/README.md` says what it
shows and every option. A part whose 3D model the installed KiCad library does
not have is drawn as a box in its group's colour. This skill first finds those
models on the web, so the movie draws the parts, then makes the movie.

`movie3d/find_models.py` lists what to look for and records each answer; you
do the looking. The movie never runs an agent: it uses whatever has been
recorded.

## Step 1: Setup

From the repo root, check:
- `movie3d/node_modules` exists. If not, run `cd movie3d && npm ci`.
- `node` and `ffmpeg` are on PATH, and KiCad 10's `kicad-cli` is installed.

The README's "Setup" section covers the browser.

## Step 2: What is missing

```bash
python3 movie3d/find_models.py missing BOARD
```

BOARD is the board the movie places (for `--route`, the same board). It prints
JSON:
- `board`: what identifies the project (file name, title, company, comments,
  footprint library nicknames).
- `missing`: one entry per model the library lacks. Each has its `key`, the
  path(s) the footprint names (`referenced_as`), the parts that use it
  (reference, value, footprint, description, pad count, face), and
  `library_near` (installed files whose names are close to it).
- `not_found_before`: models recorded as not found earlier, left out.
  `--retry` puts them back.

If `missing` is empty, go to Step 5.

## Step 3: Look for each model

Search with WebSearch and WebFetch. Take the first of these that exists:

1. **The exact file the footprint names.** The same file name counts; a
   `.step` and a `.wrl` of one name are equally good.
   - A `${KIPRJMOD}/...` path is the board's own project. Find its repository
     from `board`: the title, a company or copyright comment, and a footprint
     library nickname that is not KiCad's own (e.g. `tigard`). The model is in
     the repository's `*.3dshapes` folder.
   - A `${KISYS3DMOD}/...` or `${KICADn_3DMODEL_DIR}/...` path is KiCad's
     library, at https://gitlab.com/kicad/libraries/kicad-packages3D. Check its
     older branches (`7.0`, `8.0`) for models since removed or renamed. A raw
     file there is `https://gitlab.com/kicad/libraries/kicad-packages3D/-/raw/<branch>/<Lib>.3dshapes/<file>`.
   - When a `library_near` file is the same model under a new name (KiCad
     renamed `SW_CuK_JS202011CQN...` to `SW_CK_JS202011CQN...`), use it. Do
     not download anything for it.
2. **The manufacturer's model of the exact part**, found from the value, the
   part number or the description. Only one that downloads without a login or
   a payment.
3. **KiCad's model of an equivalent package**: same pin count, pitch and
   body. It is often a `library_near` file of the same series.

Give a model a few searches; if nothing suitable turns up, record it as not
found and move on.

## Step 4: Record each answer

```bash
python3 movie3d/find_models.py add KEY URL --match exact --source "<repository or site>"
python3 movie3d/find_models.py library KEY PATH --match equivalent --note "<why it fits>"
python3 movie3d/find_models.py none KEY --note "<what you tried>"
```

- **`add`** downloads the file and checks it:
  - https only, at most 40 MB;
  - a zip is read in memory and only its model taken;
  - the file is kept only if it really is a STEP or VRML model.

  A refusal prints the reason. "An HTML page" means you gave a page, not the
  file: give its raw-file URL. A GitHub or GitLab `blob` link is turned into
  its raw link for you. If one candidate is refused, try the next.
- **`library`** records an installed file.
- **`none`** keeps the model out of later `missing` lists.

A footprint that names two models (e.g. KiCad's and the project's own) needs
only one. Once one is recorded, the other is no longer listed. Run `missing`
again to see what is left.

Found models live in `movie3d/cache/found/` (gitignored). Never commit them:
they carry their sources' licences.

## Step 5: Make the movie

Run it from the repo root, with the arguments the user asked for:

```bash
python3 -X utf8 movie3d/make_movie.py BOARD OUT.kicad_pcb --groups G.json --intent I.json --seed 0
```

`--route BOARD:SET` adds the routing flow. Every argument other than
make_movie's own is passed to `place_simple.py`. A small board takes one to
two minutes; `--route` takes longer.

## Step 6: Check it

- **The `record:` line** says how many parts are drawn from models, which
  fell back to a box, and which models were found on the web.
- **The `make_movie: rendering` line** names any found model that does not
  sit on its footprint. Those are kept as boxes, because the model is wrong
  for the footprint (rotated, offset, or another part). Replace it (`add`
  another URL, or `none`) and make the movie again.
- **The stills** beside the movie: look at `_final.png`, and at `_bottom.png`
  (the board turned over).

## Step 7: Report

Report:
- the movie's path and length;
- each model found: its key, where it came from, and whether it is exact or
  equivalent;
- the parts still drawn as boxes, and why.
