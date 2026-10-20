---
name: plan-pcb-placement
description: Places a KiCad board by function, and in full mode routes and verifies it. The agent sorts the parts into functional groups, copies each switching regulator's datasheet layout into a template its support parts are held to, place_simple.py anneals one objective over 12 seeds (6 started scattered, 6 with each group gathered), a quick judging route of the 6 its objective rates best keeps the best, and in full mode that board is then routed by /plan-pcb-routing. One independent verifier grades the result, and the board comes back with REPORT.md. Modes are place and full (place + route). Use it to place, arrange, re-place or optimise components, handle an unplaced board or parts piled off the outline, place and route from scratch, or run the GUI's Place / Place + Route. It never changes the outline. To route a placed board without moving parts, use plan-pcb-routing; for sign-off QA, review-routed-board.
---

# Plan PCB placement

A human lays a board out by function: the USB connector with its resistors
right behind it, the regulator and its caps between the power entry and the
loads, the MCU with its crystal on its oscillator pins, the LEDs and the
button along an edge. This skill places that way. You decide what belongs
together; `py_placer/place_simple.py` arranges it over several seeds; a fast
route of each keeps the best.

Invocation: `/plan-pcb-placement <mode> <board.kicad_pcb> [intent.json]
[--scattered N] [--clustered N] [--anchored N] [--judge N]`,
where mode is `place` or `full`. With no mode, use `full`. A given
`intent.json` is the intent: use it in place of emitting one (§3.2).
`--scattered`, `--clustered`, `--anchored` and `--judge` are the seeds of
each start and how many are judged (§3.6), passed to `place_simple` as
given; its defaults (6, 6, 0, 6) apply when they are not. Anchored seeds
(each group started round the point its wiring prefers) are opt-in.

## 1. The goal, per mode

| mode | input | DONE means | better, once DONE |
|---|---|---|---|
| `place` | an unplaced or placed board | `check_assembly` buildable, `check_floorplan --intent` 0 errors, no pad, graphic or keep-out copper off the outline, mechanical facts met, and a fast route of it with no blocking net | fewer vias, then less copper_mm, then fewer segments, on that fast route |
| `full` | an unplaced or placed board | `board_score` blocking 0, zero unrouted, zero broken, `check_complete` DONE | fewer vias, then less copper_mm, then fewer segments |

- **"Better" is lexicographic, never a weighted sum.** A lower via count never
  buys back an open net.
- **In `place` mode the fast route is your own check.** `scripts/grade.py
  --mode place` and the verifier grade the placement, not a route; keep the
  fast-routed board and its score JSON (`quick_score`) as the evidence for
  the "no blocking net" condition.
- **The routed board judges a placement**, not the objective's cost, crossings
  or hpwl. Those only rank candidates you have not routed yet.
- **DONE is measured with every spec the board declares.** Pass
  `--net-min-widths`, `--impedance-nets`, `--length-groups` (a FILE) and
  `--min-track-width` / `--min-via-diameter` / `--min-via-drill` to
  `board_score` and `check_complete`. An `ungraded` component is UNEXAMINED,
  not passed.
- **Matching the original layout does not count.** A human layout is a
  benchmark to approach, not a pose to match.

## 2. Before the first command

- **Work in `wk/<run>/`.** Copy every board with
  `python3 -X utf8 py_router/copy_board.py <src> <dst>`, never a bare `cp`:
  the `.kicad_pro` beside a board carries its DRC floor.
- **Keep the seeds in `wk/<run>/`**, with these names, which the GUI's
  candidates table reads: `s<S>.kicad_pcb` and `s<S>.json` (seed S's
  placement and its `place_simple` report), `s<S>_route/` (the fast route's
  working files), `s<S>_fast.kicad_pcb` (the fast-routed board, copied out of
  that folder with `copy_board.py`) and `s<S>_fast.score.json` (its
  `quick_score`). The kept board goes to `wk/<run>/final.kicad_pcb`.
- **Read the board.**
  `python3 -X utf8 py_tools/board_brief.py <board> --json wk/<run>/brief.json`
  (read `pile` and `has_copper` on its `JSON_SUMMARY` line; `poured_nets`
  names the nets a pour already serves) and
  `python3 -X utf8 py_tools/board_context.py <board> --md` (every part's value,
  pin functions, nets by face, what each cap serves, `CROSSED` pin-order pairs).
- **Read `references/part-roles.md` before you judge what a part is.** A
  prefix, a footprint name or pins named IN/OUT mislead: an RF amplifier
  reads as a regulator, a crystal's load cap as a decap.
- **Read `references/placement-lessons.md` before you group and hint.** It is
  the one file of placement lessons: what 437 human boards do (where each
  functional group sits, spacing, decaps, regulator stages, connectors,
  clocks, rows, repeated channels), how a mistake shows on a routed board,
  and what to change.
- **Declared facts win.** If the board has a sibling
  `<board>.design-brief.json` or `mechanical.json`, the intent is compiled
  from it (next section). If the user states a mechanical fact (a
  connector's edge, a mounting-hole position), it is a requirement, and the
  verifier checks it.
- **Ask for the EMC rows the board cannot state.** Run
  `python3 -X utf8 py_tools/board_context.py <board> --json -o <ctx.json>`
  and read `emc_roles.roles`: each converter, crystal, oscillator and ESD
  array its pin functions name, every leg measured pad edge to pad edge, and
  a ready `proximity` row with `max_mm` left null. Ask the user for the
  limits (crystal to its load pins, input cap to the regulator's VIN and
  PGND, inductor to SW, ESD array to its connector), put the rows in the
  brief's or the intent's `proximity`, and report which you were given. Ask
  in the same breath about `emc_roles.keep_away`: per crystal or oscillator,
  a rule keeping its clock nets away from every switch node, with its
  measured gap and a `brief_row` ready to paste into the brief's
  `keep_away` list. Which nets are noisy is the user's to say; do not infer
  it from net names. A converter you give a datasheet template (§3.5) needs
  only the `keep_away` rows: the template places its support parts, so do
  not ask for their limits.
- **Nothing is sacred except a KiCad lock.** `place_simple` re-places every
  part the board does not lock (`(locked yes)`) or the intent does not seat
  (`fixed_poses[]`). For a from-scratch experiment with everything off the
  board, build the input with
  `python3 -X utf8 .claude/skills/plan-pcb-placement/scripts/make_unplaced.py <src> <dst>`.
  It refuses a routed input: strip that first with
  `python3 -X utf8 tests/stress/strip_copper_only.py <src> <dst>`.
- **Git Bash:** `export MSYS2_ARG_CONV_EXCL='*'` before any command that
  carries a net name. Quote `--nets '*'`.

## 3. The workflow

### 3.1 Place and lock the fixed parts

Connectors at the edge, module headers, mounting holes and fiducials are
mechanical decisions, not placement results. Place each with
`python3 -X utf8 py_placer/place_pose.py <in> <out> set REF X Y --rot DEG lock REF`.
Take the pose from the design brief or `mechanical.json` when the board has
one, from the user when they state it, and from the input board only when it
is a believable layout (not a pile). A pose the intent declares in
`fixed_poses[]` needs no lock: `place_simple` seats it there and holds it.
A connector whose EDGE is decided but not its place along it needs no lock
either: leave it unlocked with its `edge` in the intent's
`edge_connectors[]` entry (and an `along_edge_band` when only part of the
edge will do), and `place_simple` holds it to that edge, mating face out,
and slides it along. On a pile the emitter writes no `edge` -- the staging
pose says nothing; `--emit-intent --declare-classes` writes each edge part's
entry without one, plain `--emit-intent` no entry -- so write it from the
brief, `mechanical.json` or the user.

### 3.2 Emit the intent

```bash
python3 -X utf8 py_tools/check_floorplan.py wk/<run>/fixed.kicad_pcb --emit-intent wk/<run>/intent.json
```

- When a placed reference of the design exists (a human layout, an earlier
  placement), add `--decaps-from <reference.kicad_pcb>`: it arms the decap
  limits, and say in your report that you did.
- **Never emit the intent from a damaged board.** It records the damage as
  the spec. Emit it from the brief, or edit it down.
- **On a pile** (`pile` in board_brief's summary), give every
  `check_floorplan` call before the placement `--allow-unplaced`: without it
  the tool refuses a board that does not look placed (exit 3, "does not look
  PLACED"). That gate is for tools that refine a placement, not a refusal of
  this run.
- `place_simple` reads the intent's `proximity` claims, keep-outs, zones,
  declared faces, `fixed_poses`, the edges of `edge_connectors[]` and the
  decap limits it grades at error.

### 3.3 Group the parts (your judgement)

Write `wk/<run>/groups.json` (the format is in `py_placer/placement/grouping.py`):

```json
{"schema": 1,
 "groups": [
   {"name": "usb_in", "role": "interface", "refs": ["J1", "R2", "R3", "R4", "R5"],
    "anchor": "J1", "near": ["power", "core"],
    "why": "USB-C with its CC pull-downs and D+/D- series resistors"},
   {"name": "power", "role": "power", "refs": ["U1", "C1", "C2"], "anchor": "U1",
    "near": ["usb_in"], "why": "the LDO from VBUS with its input and output caps"},
   {"name": "core", "role": "core", "refs": ["U3", "C3", "R1"], "anchor": "U3",
    "why": "the MCU, its VDD decap, the D- pull-up its GPIO switches"}]}
```

- **Every part the board does not lock (and the intent does not seat in
  `fixed_poses[]`) is in exactly one group.** Fixed parts may be listed too:
  a connector in its interface's group anchors it.
- **Group by function, not by sheet or by net count:** the power entry and its
  conditioning, the core and its decaps, its clock, an interface with its
  protection and series parts, signal conditioning, the user interface (LEDs
  with their resistors, buttons). Five to eight groups suit a small board.
- **A cap belongs with the IC whose supply pin it decouples.** A load or RC
  cap belongs with its crystal or its button.
- **Crystals take care of themselves.** `place_simple` claims each passive
  crystal at its IC's oscillator pins (link by link through a series
  resistor on a leg) and its load caps at the crystal, and moves them all
  into the IC's group. The rest of their clock group follows
  only when the crystal is that group's `anchor`; any other member stays in
  the clock group. Every intent `proximity` claim works the same way: the
  claimed part joins its partner's group. The emitted intent carries these
  crystal claims as `proximity[]` rows (`context.source`
  `placement.crystals`, `max_mm` 8.1 and 2.2), graded as warnings; edit a
  row's `max_mm` to loosen or tighten that loop, or drop the `source` to
  make it an error-graded claim.
- **Hints, each a soft term of the objective:** `near` (a list of groups to
  sit against), `apart` (groups to keep 3 mm from), `edge` (`N`/`S`/`E`/`W`,
  a group that belongs on a board side; a group holding an edge connector
  needs none). **Do not send the power group to an edge**: on 437 human boards
  the regulator sits by its power entry -- say `near` its entry's group.
- **Faces:** give a group a `side` (`F`/`B`) only to put all its parts on that
  face; without one each part keeps its face.
- **Look parts up.** A part number the board and `part-roles.md` do not
  settle is a datasheet question: WebSearch it and WebFetch the datasheet.
  If you must save a file, save it only under `$KRT_DATASHEET_DIR` when that
  is set (GUI runs), else under `wk/<run>/datasheets/`, and delete that
  folder before you hand back.

```bash
python3 -X utf8 py_tools/check_groups.py wk/<run>/fixed.kicad_pcb --groups wk/<run>/groups.json \
    --intent wk/<run>/intent.json
```

It reads the intent the way `place_simple` will: seated parts are fixed,
and it names each claimed part that will follow its partner into the
partner's group. The groups it lists and checks are the file as written, so
a follower still shows in the group you gave it. Fix every ERROR (a part
in no group or in two, a ref not on the board) and
read every WARN: a part with more signal links to another group may belong
there; a decap "exempt" because no IC of its group carries its rail is right
for a load or RC cap and wrong for a real decap.

### 3.4 Orient and check the connectors and fixed parts (your judgement)

```bash
python3 -X utf8 py_tools/check_floorplan.py wk/<run>/fixed.kicad_pcb --intent wk/<run>/intent.json
```

For each edge connector, read its edge, its facing (`facing ... pass`, or
`facing abstained (...)` when its body names no face) and the group you gave
it. Then decide, from the part's function and its datasheet:

- **The edge, and roughly where along it** when that matters (a USB or
  barrel jack where the enclosure opens, a debug header out of the user's
  way): `edge`, and `along_edge_band` as fractions of the edge.
- **The mating face of a connector whose facing abstained** (a side-entry
  JST drawn symmetric about its pads): find the side its opening is on in the
  datasheet's PCB-layout drawing and write `mating_face` (`+x`/`-x`/`+y`/`-y`,
  the footprint's own frame). The side of its solder tabs, away from the
  contact row, is a hint to check against the drawing, not a rule: it is
  wrong on 3 of 50 side-entry connectors in the corpus.
- **The side a vertical header's cable leaves.** A `Vertical` header mates
  off the board and names no face, but its cable leaves on one side: a
  2xN ribbon off a long side. Write that side as its `mating_face`, so the
  seat lays the header with its long side along the edge and the cable runs
  off the board. Without it, a 2x05 header lay across its edge on most
  seeds, its cable along the edge.
- **The pin rows of a plug-in module** (a DIP or breadboard module, rows
  that go into a socket): the socket sets their spacing, so declare each row
  in `fixed_poses[]` (`basis: derived_default`), not on its edge: parallel,
  along the long edges, pins on one 0.1 in grid, centred on the board, a
  whole number of 2.54 mm apart (the socket's, else the most that keeps
  every pad the board's edge clearance inside the outline). Seated flush on
  their edges instead, ch32v003_usb's two 1x10 rows ended 13.21 mm apart,
  not 12.70, and the module no longer fits a breadboard.
- **How far an edge receptacle reaches past the edge.** The seat puts a
  receptacle flush with the low end of its `overhang_mm` band, which the
  emitter writes as 0. Where the datasheet's PCB-layout drawing puts the
  board edge inside the body (a USB-C or barrel jack's shell past the edge),
  write that distance as `overhang_mm.min`, `max` at or above it. Seated
  flush instead, ch32v003_usb's USB-C J1 sat 0.88 mm deeper than its drawing
  and the board routed with about 15 mm more copper a seed.

Write these into the intent's `edge_connectors[]` entries, the rows into
`fixed_poses[]` instead (a ref in both is refused), or into the brief,
then emit again (§3.2). Check the fixed parts -- mounting holes, switches, LEDs
a panel shows -- against the brief or `mechanical.json`, and move any that is
wrong with `place_pose ... set ... lock` as in §3.1. Run check_floorplan again:
no `edge_connector` error.

### 3.5 Datasheet layouts (your judgement)

A switching regulator's datasheet draws where each of its support parts goes:
the input caps across VIN and ground, the inductor on the switch pin, the boot
cap across BOOT and SW, the feedback divider at FB, away from the switch node.
The router cannot see whether the board follows that drawing, so a buck can
route clean and still work badly. Copy the drawing into the intent as a
**template**. `place_simple` seats each part at its template pose, carries it
with every move of the chip, and makes leaving that pose expensive.

1. **Which chips.** Every row of `emc_roles.roles` (`board_context.py
   --json`) whose `role` is `switching converter`, and every row whose `role`
   is `unknown` with `candidate: switching converter`: its pads carry no pin
   functions, so read its pins off the datasheet's pinout. Only converters:
   an LDO's caps and a crystal's load caps are held by the decap and crystal
   rules, with no datasheet to read. **Skip** a converter, and say why in
   REPORT.md, when no datasheet can be found for its value, when the
   datasheet has no layout figure, or when its support parts are locked.
   A skipped converter keeps its §2 `proximity[]` rows.
2. **Get the datasheet.** WebSearch the part number with "datasheet" and
   take the manufacturer's PDF (TI's is
   `https://www.ti.com/lit/ds/symlink/<part>.pdf`, the base part number in
   lower case; a search usually returns its HTML viewer instead). Save it
   under `$KRT_DATASHEET_DIR` when that is set, else under
   `wk/<run>/datasheets/<part>/`, one folder per part: a download is data,
   not code. WebFetch mangles a PDF, so download it (`curl -sSL -o
   <folder>/<part>.pdf <url>`) and open it with the Read tool and `pages`, a
   few at a time. You need three things from it:
   - the **layout guidelines** (they say what matters most, usually the
     input cap nearest, and give distances);
   - the **layout example** figure ("Layout Example" or "Recommended
     Layout", near the end);
   - the **application schematic** whose designators that figure uses
     ("Typical Application" or "Design Example"). Without it the figure's
     C1 and Q2 mean nothing.

   The table of contents names their pages. Where poppler is installed,
   `pdftotext -layout -f N -l N <pdf> -` prints page N, to grep for those
   headings, and a figure too small to read on the Read page is rendered
   larger by `pdftoppm -r 300 -f N -l N -png <pdf> <prefix>`: crop the figure
   out of the PNG and Read the crop.
3. **Match the drawing to the board, by net and pin function, never by
   designator.** The datasheet's C1 is not the board's C1: a bitaxe agent
   found Q1, Q2, R4, R10 and C1 naming different parts in the two, so
   matching by name would have swapped the FETs.
   `python3 -X utf8 py_tools/fit_template.py wk/<run>/fixed.kicad_pcb --pins <REF>`
   lists the chip's pads: number, pin function, position and side in the
   footprint's own frame (`centre` for an exposed pad, `inner -x` and so on
   for an inner pad off the middle), net, and the other parts on that net
   with their values and pads. Name each part of the drawing by its board
   reference, from the pin it connects to and its value: CIN is the 4.7 uF
   on VIN and ground, CBOOT the cap from BOOT to SW, the inductor the part
   on SW, the divider the resistors on FB, COUT the caps on the inductor's
   far pad (`--pins` takes any part, so `--pins <inductor>` lists them). Where several parts qualify (four
   4.7 uF caps on VIN), take the ones your groups file puts in the chip's
   group; the others serve something else. Leave out what the drawing has
   and the board does not (a CFF not fitted), and what the board has and the
   drawing does not.

   **A controller with external FETs** (no VIN pin of its own; `emc_roles`
   notes it) takes its FETs as members too. Their input caps go across the
   high-side FET's drain and the low-side FET's source, and the inductor
   on the switch node between the FETs. Those members' legs name the FETs'
   pads (an earlier member), not the controller's pins, and the FETs' legs
   name the gate-drive pins.
4. **Turn the drawing into the footprint's frame.** The template is in the
   footprint's own frame: rotation 0, +x right, +y down. From `--pins`, see
   where each pin group lies, then map the drawing's directions onto it. For
   example, the LMR36006's Figure 57 draws SW (pin 12) at the top, and
   fusb302_pd_dev's footprint has pad 12 at `inner -x`, so the drawing's up
   is -x there. **Check whether it is a turn or a mirror** on pin 1 and the
   way the numbers run from it. If they run the same way round in the drawing
   and in `--pins`, it is a turn. If they run the other way, the drawing is a
   bottom view or the chip is on the back, so mirror it. A back-side chip's
   frame is mirrored, so read its sides from `--pins` and never from the drawing.
5. **Write the sketch:** the chip as `anchor`, then the members in the
   drawing's priority order. Seating is greedy, so order matters:
   - the parts the datasheet wants closest first (input caps, the boot cap,
     the inductor, a controller's FETs);
   - then the small signal parts;
   - a part that sits beyond another member (COUT past the inductor) after
     that member.

   Give each member:
   - `pads`: its own pads, and for each partner (the chip, or a member
     listed earlier) the partner's pads it belongs beside. They are matched by
     net, so their order does not matter, and one member may name several
     partners. A second part like one already seated (the second input cap)
     goes beside the first, so name the first as its partner;
   - `side`: where the drawing puts it, in the footprint frame. `-x`, `+x`,
     `-y` or `+y` means its centre is beyond that edge of the chip's
     courtyard, anywhere along it. Two of different axes (`+x-y`) mean a
     corner, beyond both edges;
   - `rot` where the drawing fixes which way the part faces (a FET, an
     inductor whose pads differ): its turn relative to the chip, in degrees
     (0, 90, 180 or 270), so on the board it sits at the chip's rotation plus
     `rot`. Leave
     it out and the fit picks the turn that brings its pads closest;
   - a `note` naming what the drawing calls it.

   Cite the datasheet (title, revision, section, figure) in `source`:
   ```json
   {"anchor": "U2", "source": "TI LMR36006 SNVSB48C, 11.1 and Figure 57",
    "members": [
     {"ref": "C2", "pads": {"C2": ["1", "2"], "U2": ["2", "1"]}, "side": "+y", "note": "CHF across VIN 2 / PGND 1"},
     {"ref": "C5", "pads": {"C5": ["1", "2"], "U2": ["4", "3"]}, "side": "+y", "note": "CBOOT across BOOT 4 / NC-SW 3"},
     {"ref": "L1", "pads": {"L1": ["1"], "U2": ["12"]}, "side": "-x", "note": "inductor on SW 12"},
     {"ref": "R2", "pads": {"R2": ["1"], "U2": ["7"]}, "side": "+x", "note": "RFBB at FB 7"},
     {"ref": "C7", "pads": {"C7": ["2"], "L1": ["2"]}, "side": "-x", "note": "COUT past the inductor"}]}
   ```
6. **Fit, look, adjust.**
   ```bash
   python3 -X utf8 py_tools/fit_template.py wk/<run>/fixed.kicad_pcb --sketch <sketch> \
       --render <png> --intent wk/<run>/intent.json --write-intent wk/<run>/intent.json
   ```
   It seats each member in order, within `--reach` mm (4) of its partner
   pads, as close to them as the courtyards already seated allow. They are
   kept the board's clearance apart, the placer's own rule. It tries each
   quarter turn and keeps the one that brings the pads closest. It prints
   each member's pose and leg gaps, and writes the template into the intent,
   replacing that chip's row if it has one, so re-fit as often as you like.
   - **If a member cannot be seated**, the fit stops. It says why: its
     `side`, or which members already seated take its spot. `--render`
     then draws the members seated so far. Move the member earlier in the
     order, change its side, make a like part its partner, or raise
     `--reach`.
   - **Look at the PNG** beside the datasheet's figure, turned to the
     footprint's frame. Check that every part sits on its side and faces the
     pin it serves. Each red leg runs across a pad-edge gap, so it should be
     short. A leg of several mm usually means the member lost its spot to one
     listed before it.
   - **A part with no courtyard** is fitted, and placed, as the box round
     its pads; the run names it, and the render labels it. Check that box
     against the part's real body. A custom pad, a power FET's drain, can
     be modelled larger than its copper. The placer uses the same box, so
     note any such part in REPORT.md rather than working round it.

   **Give up on the template**, keep the §2 rows and say why in REPORT.md,
   when after adjusting a member's legs still run several mm past the
   distances the guidelines give.
7. **The rest is automatic.** `place_simple` seats and carries the template
   (`--template-weight`, default 100 per mm of drift), and `check_floorplan`
   grades each member's drift from its template pose (`template`, a WARN,
   past 0.3 mm by default, the row's `tolerance_mm`). Report each template in
   REPORT.md: the datasheet and figure, the parts matched, and the drift
   graded on the final board.

- **Never copy coordinates from the drawing.** A datasheet packs its parts
  tighter than this board's courtyards allow (on fusb302_pd_dev the 0402s
  cannot come closer than 0.6 mm to the chip's pins); the fit sizes the
  template to the board's own footprints.
- One part belongs to at most one template. A template's members need no
  EMC `proximity[]` rows (§2): the template holds them.

### 3.6 Place

```bash
python3 -X utf8 py_placer/place_simple.py wk/<run>/fixed.kicad_pcb 'wk/<run>/s{seed}.kicad_pcb' \
    --groups wk/<run>/groups.json --intent wk/<run>/intent.json --jobs 4 \
    --json 'wk/<run>/s{seed}.json'
```

- **The seeds run in parallel, then the fast routes.** An OUT carrying
  `{seed}` places the default seeds, one process per seed (`--jobs N` caps how many at once; default one per seed, at most 4),
  each giving exactly the placement a `--seed` run would, its log beside its
  board as `s<S>.log`; one placement process takes about 120 MB. A judging
  route takes up to about 1.3 GB on a small board and 5 GB on a 350-part one:
  run as many at once as memory allows, and on a machine with 2 GB to spare
  one at a time, never beside a placement.

- Every part not locked or seated by the intent starts at a random place and
  is annealed; the same board, inputs and seed give the same placement.
  It places `--scattered` seeds (default 6) that way, then
  `--clustered` seeds (default 6, numbered after them) that start with each
  group's parts gathered round one random point instead: on boards dominated
  by one big IC those route clearly better, on others the scattered ones do,
  and the judge takes whichever routes best.
  The run is 500 moves per movable part by default (`--moves N` sets the
  total), roughly 2 s for 25 parts.
- **Place 12 seeds (6 scattered, 6 clustered); judge the 6 the objective
  rates best.** place_simple ends by naming them: printed, and written to
  `wk/<run>/judge.json` (`judge`, cheapest first; a seed that left a part
  illegal is never named). Seeds differ more than most input changes, and the anneal's own
  cost (`final.cost` in each `s<S>.json`) says which are worth a judging
  route: the seed the judge would pick out of 16 is in the cheaper half
  85-100% of the time. Over 11 boards from 25 to 350 parts (each seed's
  full route, 2000 resampled pools per board), this ships 50 open nets in
  all against 57 for 24 scattered seeds with the 12 cheapest judged, 71
  blocking against 101 and 6426 vias against 6495, in half the placing and
  half the judging; one board of the 11 does worse (3.8 open nets against
  0.6). Judging all 12 instead of the 6 cheapest ships worse
  (60 open nets): the judge is wrong more often with more to choose from.
  Exit 4 if any seed left a part illegal (its `illegal` list is not empty):
  never judge or keep that seed. Exit 2 if any failed outright.
- A run that leaves a part with no legal pose anneals again from a fresh
  start, up to two more times (`runs` in the `--json` report). Exit 4 names
  parts still left illegal; exit 2 a face or pose the inputs contradict.
  Read the `cost` line: `hints` and `groups` say which group or hint the
  result could not satisfy.
- **Check and LOOK:**
  `python3 -X utf8 py_tools/check_assembly.py <placed> --baseline wk/<run>/fixed.kicad_pcb`
  (read `buildable`) and
  `python3 -X utf8 py_tools/render_placement.py <placed> -o <placed>.png`.
  Check the render against `references/placement-lessons.md`: the power
  stage between its entry and its loads, a regulator's caps tight around it,
  parts on the side of the chip where their pins are, connectors' partners
  right behind them.

### 3.7 Fast-route the judged seeds and keep the best

1. **Fast-route the seeds `wk/<run>/judge.json` names** (the `--judge` legal
   seeds of lowest `final.cost`) with the board's routing
   chain (the recipe
   is `plan-pcb-routing`'s: planes, fanout, the decap clearance step,
   `route.py`), giving `route.py` and every `route_diff.py` step `--quick 2`.
   Level 2 skips the recovery tail (net rescue, terminal escalation, plane
   finalize, reconciliation laps), where a poor placement's routing time
   goes, and every pass that only polishes copper for shipping: about half
   the time of `--quick` alone, and it picks seeds as well (measured over 16
   seeds of five corpus boards against their full routes). Use the SAME
   chain for every candidate, so they compare. Run it in `s<S>_route/` and
   copy its last board to `s<S>_fast.kicad_pcb`. Never ship a `--quick`
   board.
2. **Score each** with `py_tools/quick_score.py wk/<run>/s<S>_fast.kicad_pcb --json wk/<run>/s<S>_fast.score.json`:
   the board's open nets, graded as `board_score` grades a full route but
   over the nets without a zone (level 2 skips the plane finalize, which
   joins a poured net's pads; the score lists those apart as `poured`), and
   `board_score`'s via, copper and segment counts. (`board_score` itself
   would count DRC grazes that level 2 leaves for the full route's cleanup,
   which say nothing about the placement.)
3. **Keep the best** by blocking, then vias, copper_mm, segments: copy its
   placement (`s<S>.kicad_pcb`, never the `--quick` board) to
   `wk/<run>/final.kicad_pcb` with `copy_board.py`.

In `place` mode that placement is the result. Run
`python3 -X utf8 py_tools/route_review.py wk/<run>/s<S>_fast.kicad_pcb --json wk/<run>/review.json`
on its fast route: turn each two-pad part it lists as turned against its
wiring with `place_pose ... rotate`, then check the board again
(`check_assembly`, `check_floorplan --intent`, `render_placement`).

### 3.8 Route (full mode)

Route the kept placement by running `/plan-pcb-routing` on
`wk/<run>/final.kicad_pcb`: the routing is that skill's, chain and checks
included, never a `--quick` run (the judging chain is a starting point it may
use). Then `board_score` and `check_complete` with every spec flag. If DONE
does not come, classify the failure (§4).

## 4. Traps measured in real runs

**Placement**
- **Rotation and pin order.** A `CROSSED` pin-order pair in `board_context`
  costs a via per net at every rotation.
- **Repeated sheet instances (channel strips, one buffer per bank): arrange
  ONE, then copy it**:
  `python3 -X utf8 py_placer/place_pose.py <in> <out> replicate --from CH1 --to CH2 --to CH3 --anchor J1`
  (#1211). It matches parts by schematic symbol and refuses unless the
  instances are wired alike, naming the pads (`--allow-net-mismatch REF.PAD`
  waives one by design). Lock the copies before placing the rest.
- **`place_pose` refuses a pad stack, but "legal" is still not
  "buildable".** Courtyards, bodies and coincident origins are
  `check_assembly`'s alone: after every pose change, run it and read
  `buildable`, not `blocking`.
- **`render_placement`'s pad-clearance list is the grader's** (#1065); its
  pad-stack list and JSON `metrics` are bounding-box counts. Read stacks
  from `check_assembly`.
- **Keep-out bands.** A pad in a `(keepout (tracks not_allowed))` band cannot
  be routed even on an empty board (#1031). Treat
  `checklist.a_off_outline.keepout_copper` like off-outline pad copper.
- **A module antenna needs its copper keep-out WRITTEN, after placement.** A
  brief's `keepouts[]` grades and places only; no routing step reads it:
  `python3 -X utf8 py_router/add_rule_area.py <in> <out> --name ANT_KEEPOUT --ref U1 --rect X0 Y0 X1 Y1`
  (#1200).
- **Re-placing strands routed copper.** Strip the copper and re-route.

**Routing**
- **Classify a routing failure before retrying it.**
  - Failed net's `blockers` empty, and `boxed_in[].geometry` above the
    board/fab floor: parameters (grid, ripup budget, width).
  - Geometry already at the floor, failures clustered at one part's escape
    face, or `py_tools/check_reachability.py --pad REF.PAD` says CAGED:
    placement. Itemise it in the report with that measurement (§6). A finer
    grid alone never widens a gap.
- **`route.py`'s failure tally covers only the nets that step owns** (#1069).
  Count the board's open nets with `check_connected` or `board_score`.
- **`route.py` finishes the planes; a repair step does not (#562, #1112).**
  Pour first (`route_planes.py`), then route with the plane nets inside
  `--nets` (`'*'` covers them). Do not end on `repair_planes.py`.
- **Widths are requests.** After each route, read
  `power_widths.<net>.under_mm`; grade power widths with
  `board_score --net-min-widths`.
- **Around a 0.8 mm BGA, the router can run out of lanes:** finer tracks and
  vias, a cheaper ground-plane layer cost (`--layer-costs`). Both go below
  the board's authored floors, so disclose that (§6). On later steps of a
  chain, pass `--clearance-ceiling`, not `--clearance`.
- **Hand-written copper:** stage each join with `py_tools/check_join.py`
  and stamp it `(locked yes)`; a lock freezes the WHOLE net.

## 5. Rules

1. **Follow the workflow; choose within it.** The groups, hints and claims
   are your judgement; the stop rules and the verifier are the process.
2. **Never change the board outline** (Edge.Cuts), and never move a part that
   draws it.
3. **Stop rules:**
   - **First reach DONE**; nothing else before it.
   - **Watch long jobs.** Never leave a background job unwatched for more than
     20 minutes.
   - **If DONE will not come:** after three consecutive DIFFERENT approaches
     that each fail to lower `unrouted` + `broken` (then the rest of
     `blocking`), measured, stop and ship the best board with every remaining
     blocker itemised with its measurement.
   - **Hard cap:** 10 hours of wall clock, unless the user sets another.
4. **An "impossible" claim needs its measurement.** Quote
   `check_reachability` (PASSABLE is a router problem, CAGED is geometry) or
   the log block, not only its first refusal line.
5. **One verifier, at most 3 calls.** When you believe you have your best
   board, spawn ONE fresh subagent (not a fork) with
   `references/verifier.md`, filled in for your mode, board, sha256, baseline,
   intent and spec flags. On FAIL, fix what it names and continue.
6. **Record milestones in the ledger:** the first legal placement, the kept
   placement, the first routed board, the first DONE, the
   final board, and tried-and-worse boards with `--rejected`.
   ```bash
   python3 -X utf8 py_tools/board_score.py <board> --intent <i> --baseline <input> --json <board>.score.json --quiet
   python3 -X utf8 py_placer/converge.py record --ledger wk/<run>/ledger.jsonl \
       --board <board> --kind placement --parent <the board it was made from> \
       --lever "<what you changed, one line>" --score-file <board>.score.json
   ```
   Use `--kind completion` for a routed board, and close the ledger with one
   `record --final --stop-condition <1|2|3|4>` row, passing the verifier's
   per-lens files as `--lens-file`. The stop conditions: 1 done, 2 budget
   spent, 3 plateau, 4 measured-unfixable. `record` refuses 4 unless the
   ledger's last `--kind classification` row (its `--shape`: parameter,
   placement or floorplan) is not `--rejected` and no lap was recorded after
   it: the measurement behind the claim must be on the record.

## 6. Hand-back

1. **The board**, with its `.kicad_pro`, its sha256, `groups.json` and the
   intent.
2. **`wk/<run>/REPORT.md`**, containing:
   - **the result first:** a table of the mode's DONE conditions with measured
     values, the `blocking_by` breakdown, and vias / copper_mm / segments;
   - **the groups and every judgement call** in them, with its reason;
   - **which stop rule fired**, and every remaining blocker BY NAME with its
     measurement;
   - **floors:** where the run went below the board's authored or fab floors
     (route.py's `Design rules [...]` line, `check_complete --authored-from <input>`);
   - **a timeline** and **where the time went**;
   - **tool gaps**, including any tool that said "fine" about something that
     was not;
   - **each verifier verdict.**
3. **Independent grade and timing**, for the report's own table:
   ```bash
   python3 -X utf8 .claude/skills/plan-pcb-placement/scripts/grade.py <board> --baseline <input> --intent <i> --mode <mode>
   python3 -X utf8 .claude/skills/plan-pcb-placement/scripts/measure.py --self --evidence wk/<run>
   ```
   Add `--spec <name>=<value>` to `grade.py` for each spec flag the board
   declares. `measure.py` must run from your own shell; if it refuses (exit
   3), quote the refusal.
4. The datasheet folder deleted (`rm -rf wk/<run>/datasheets`).
