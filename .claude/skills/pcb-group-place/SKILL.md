---
name: pcb-group-place
description: Places a KiCad board by function, each functional group in its own board region. The agent sorts the parts into groups by what they do (power input and conditioning, the core and its clock, an interface and its protection, user interface, I/O), reading pin functions and datasheets; plan_regions.py computes a region per group from the agent's hints and the board's wiring; place_seed then fills the regions in one pass, or place_groups.py seeds the groups one at a time. Hands back the placed board, the groups file, the region intent and renders. Use it for a from-scratch or re-done placement where human-like structure matters, or when the free agent wants a functional seed. It places only; to route, verify and film, continue with pcb-free-agent.
---

# Grouped placement

A human lays a board out by function: the USB connector with its resistors
right behind it, the regulator and its caps between the power entry and the
loads, the MCU with its crystal on its oscillator pins, the LEDs and the button
along an edge. This skill does that. You decide what belongs together and
where it should sit; the tools compute the regions and do the seating.

Invocation: `/pcb-group-place <board.kicad_pcb> [intent.json]`. The board may
be a pile (every part off the outline; build one with
`python3 -X utf8 .claude/skills/pcb-free-agent/scripts/make_unplaced.py <src> <dst>`)
or placed; either way every part not locked in the file is re-placed.

It places only. Hand the board to `/pcb-free-agent route` (or its `full` mode,
which re-checks the placement) to route it, verify it and make the film.

## Measured basis

Measured 2026-10-15 on three human-placed corpus boards, from a pile with
their connectors fixed at the human poses. Five seeds per flow; every
placement was routed by the board's own recorded corpus chain (ending in
`route.py`) and scored by `board_score`. Each cell is the mean blocking count,
then the mean via count:

| board | all at once, no regions | regions, one pass | regions, group by group | human placement |
|---|---|---|---|---|
| ch32v003_usb | 1.8 / 95.4 | 1.0 / 55.8 | 0 / 47.6 | 0 / 36 |
| esp_prog | 1.0 / 41.0 | 0 / 35.0 | 0 / 37.2 | 0 / 38 |
| ch32v006_dev | 1.0 / 78.4 | 0 / 58.4 | 0 / 65.4 | 0 / 82 |

- **The regions are the gain.** On all three boards both grouped flows route
  with less blocking and 10-50% fewer vias than placing everything at once.
- **Neither grouped flow wins everywhere.** Group by group is ahead on
  ch32v003_usb; one pass is ahead on the other two. Both take seconds, so
  section 5 runs both.

## 1. Set up

- **Work in `wk/<run>/`.** Copy every board with
  `python3 -X utf8 py_router/copy_board.py <src> <dst>`, never a bare `cp`: the
  `.kicad_pro` beside a board carries its DRC floor.
- **Read the board.**
  `python3 -X utf8 py_tools/board_brief.py <board> --json wk/<run>/brief.json`
  (is it a pile; which nets a pour serves) and
  `python3 -X utf8 py_tools/board_context.py <board> --md` (every part's value,
  pin functions, nets by face, and what each cap serves).
- **Read `.claude/skills/pcb-free-agent/references/part-roles.md` before you
  decide what any part is.** A prefix, a footprint name or pins named IN/OUT
  mislead; that page lists what gets misread on real boards.
- **Read `.claude/skills/pcb-free-agent/references/placement-lessons.md`
  before you group and hint.** Its "Functional groups" section says where
  humans put each kind of group and what changed routed results; the rest
  says how the parts inside a group are arranged.

## 2. Place and lock the fixed parts

Connectors at the edge, module headers, mounting holes and fiducials are
mechanical decisions, not placement results. Place each with
`python3 -X utf8 py_placer/place_pose.py <in> <out> set REF X Y --rot DEG` and
lock it in the same call (`... lock REF ...`). Take the pose from a design
brief or `mechanical.json` when the board has one, from the user when they
state it, and from the input board only when it is a believable layout (not a
pile). Everything locked is an obstacle the region plan works around, and a
fixed part in a group anchors that group's region. A mounting hole left
unlocked is named by `plan_regions.py` in a WARN line; on a pile with four
holes it puts each at the legal pose nearest a free board corner, where
humans put them, which is a default, not the enclosure's answer -- lock the
holes where the enclosure needs them and plan again.

Then emit the intent the seed is graded against:

```bash
python3 -X utf8 py_tools/check_floorplan.py wk/<run>/fixed.kicad_pcb --emit-intent wk/<run>/intent.json
```

Add `--decaps-from <reference.kicad_pcb>` when a placed reference of the
design exists (a human layout); it arms the decap limits.

## 3. Group the parts (your judgement)

Write `wk/<run>/groups.json` -- the format is in `py_placer/placement/grouping.py`:

```json
{"schema": 1,
 "groups": [
   {"name": "usb_in", "role": "interface", "refs": ["J1", "R2", "R3", "R4", "R5"],
    "anchor": "J1", "edge": "N", "near": ["core"],
    "why": "USB-C with its CC pull-downs and D+/D- series resistors"},
   {"name": "core", "role": "core", "refs": ["U3", "C3", "R1"], "anchor": "U3",
    "why": "the MCU, its VDD decap, the D- pull-up its GPIO switches"}]}
```

- **Every part the file does not lock is in exactly one group.** Fixed parts
  may be listed too: a connector in its interface's group anchors that group.
- **Group by function, not by sheet or by net count.** Typical groups: the
  power entry and its conditioning (regulator, input and output caps, a
  reverse-polarity FET, a fuse); the core (MCU or FPGA and its decaps, reset
  pull-ups); its clock (crystal, load caps, jumpers on the oscillator pins); an
  interface (connector, ESD, series resistors, pull-ups); signal input or
  output conditioning (an amplifier and its feedback network); user interface
  (LEDs with their resistors, buttons); a level shifter or reset circuit.
  Five to eight groups suit a small board; more than about twelve makes every
  region tiny.
- **A cap belongs with the IC whose supply pin it decouples.** A load cap or an
  RC cap belongs with its crystal or its button: say so by its group. The
  decap rule would otherwise claim it at the IC.
- **Crystals take care of themselves.** Wherever you group a crystal and its
  load caps, `plan_regions.py` plans them in their IC's region (no crystal
  region), claims the crystal at the IC's oscillator pins and each load cap
  at the crystal, and the seeder seats them there. Jumpers or 0 Ω parts on
  the oscillator pins stay in the clock group and get its region.
  `check_groups.py` names each crystal it finds; one it does not name (no
  pin functions, an oscillator, a ceramic resonator) needs your judgement.
- **Indicator banks take care of themselves.** An LED and the resistor that
  shares a net with it alone are a channel; `plan_regions.py` lays the
  channels of one group on one rail out as one row, LED and resistor
  alternating. Keep each LED and its resistor in the same group: a resistor
  in another group keeps its LED out of the bank. `--no-arrays` writes none.
- **Faces:** sort parts by what they do, not by face. A group with parts on
  both faces gets a region on each (`<group>@B` beside its main one, the
  decaps under their chip). Give a group a `side` only to move all its parts
  to that face, then mirror them before seeding:
  `python3 -X utf8 py_placer/place_intent_sides.py <in> <out> --intent <regions_intent.json>`.
- **Hints:** `edge` (`N`/`S`/`E`/`W`) for a group that belongs on a board side
  (a group holding an edge connector needs none: the connector anchors it);
  `near` / `apart` name other groups. Use `apart` for a switching converter
  and an analog front end. **Do not send the power group to an edge**: on
  437 human boards the regulator sits by its power entry, not by the outline.
- **Look parts up.** A part number the board and `part-roles.md` do not settle
  is a datasheet question: WebSearch for it, and WebFetch the datasheet page.
  If you must save a file (a PDF with `curl`), save it only under
  `$KRT_DATASHEET_DIR` when that is set (GUI runs, which delete it at the end),
  else under `wk/<run>/datasheets/`, and delete that folder before you hand
  back.

Check the file against the board:

```bash
python3 -X utf8 py_tools/check_groups.py wk/<run>/fixed.kicad_pcb --groups wk/<run>/groups.json
```

ERRORs (a part in no group or in two, a ref not on the board) must be fixed.
Read every WARN:

- **"has N signal links to G and M to its own group"**: a satellite that may
  belong elsewhere. Move it, or keep it and know why (a power LED's resistor
  sits on VBUS but belongs with its LED).
- **"is a decap of U.. to the seeder's decap rule ... exempts it"**: right for
  a load or RC cap, wrong for a real decap. Move a real one to its IC's group.

It also prints the crystals it finds (each with its IC's oscillator pins and
its load caps), the bridges (series parts between two groups), the links
between groups, the rails left out of every vote, and the placement order.

## 4. Plan the regions

```bash
python3 -X utf8 py_placer/plan_regions.py wk/<run>/fixed.kicad_pcb --groups wk/<run>/groups.json \
    --intent wk/<run>/intent.json --out wk/<run>/regions_intent.json --png wk/<run>/regions.png
```

**LOOK at the PNG.** A region should sit where a designer would put that
group: the interface at its connector, the clock between the core and
whatever its jumpers reach, power between its entry and its loads. When one
is wrong, change the input, not the output:
- add an `edge`, `near` or `apart` hint;
- move a part between groups;
- split a group that is two things.

Then re-plan. Exit 4 names a face whose plan is infeasible: a region that
cannot hold its largest part or its parts' area. Merge small groups or free
the board. Then check the plan before any part moves:

```bash
python3 -X utf8 py_tools/check_floorplan.py wk/<run>/fixed.kicad_pcb --intent wk/<run>/regions_intent.json --plan-only
```

## 5. Place into the regions

Two flows fill the same regions: **one pass** (`place_seed --intent`, every
group at once, each part held to its group's zone) and **group by group**
(`py_placer/place_groups.py`: each group seeded into its region in order, the
groups not yet placed deleted from the step board, a cap claimed only at its
own group's IC). Neither wins on every board, so place both, several seeds
each, and keep the best. One command does it, four candidates at a time:

```bash
python3 -X utf8 py_placer/place_candidates.py wk/<run>/fixed.kicad_pcb wk/<run>/placed.kicad_pcb \
    --groups wk/<run>/groups.json --intent wk/<run>/regions_intent.json --json wk/<run>/candidates.json
```

It places seeds 0, 1 and 2 of each flow, or seed 0 alone on a board of more
than 300 movable parts (`--seeds auto`, the default; `--flows`): a 272-part
board takes about 75 s for all six. It grades every candidate the same way and keeps the
best by:
1. buildable;
2. then floorplan errors;
3. then airwire crossings;
4. then HPWL.

It prints one line per candidate and copies the best to the output; every
candidate stays in `wk/<run>/placed.candidates/` with its log. Exit 4 means
the kept board was written, but its grade found something. Read the lines:
- **UNSEATED parts** (in the candidate's log): give the group more room, or
  check its region.
- **floorplan errors:** decap limits are the usual ones. An error on a part
  locked in the input is listed apart (`pinned`) and is the board's or the
  intent's to settle, not the placement's.
- **NOT buildable.**

When routing the candidates is affordable, route the best of each flow and
keep the one that routes better. Section "Measured basis" ranks by routed
result, and there the two flows split. A single flow or seed is
`py_placer/place_seed.py ... --intent wk/<run>/regions_intent.json --seed N`
or `py_placer/place_groups.py ... --groups ... --intent ... --seed N`
(`--final-polish 1` adds one bounded whole-board polish, kept only when it
improves crossings without losing anything else).

Render and look:

```bash
python3 -X utf8 py_tools/render_placement.py wk/<run>/placed.kicad_pcb -o wk/<run>/placed.png
python3 -X utf8 py_tools/check_assembly.py wk/<run>/placed.kicad_pcb --baseline wk/<run>/fixed.kicad_pcb
```

Check the render against `.claude/skills/pcb-free-agent/references/placement-lessons.md`:
a switching regulator's stage in one tight cluster, parts on the side of the chip
where their pins are with the connecting pad toward the pin, a clear ring around
fine-pitch chips, same-footprint passives in rows, repeated channels as copies.

## 6. Hand back

- The placed board (with its `.kicad_pro`), `groups.json`,
  `regions_intent.json`, `regions.png` and the placement render.
- A short table: per group, its parts and region; the grade (buildable,
  floorplan errors, crossings, HPWL); which flow and seed you kept, and why.
- Every judgement call you made in grouping, with its reason. The next run
  depends on those reasons.
- The datasheet folder deleted (`rm -rf wk/<run>/datasheets`).

To continue to a routed, verified board:
`/pcb-free-agent route wk/<run>/placed.kicad_pcb wk/<run>/regions_intent.json`.
