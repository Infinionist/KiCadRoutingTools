---
name: plan-pcb-placement
description: Places a KiCad board by function, and in full mode routes and verifies it. The agent sorts the parts into functional groups, place_simple.py anneals one objective over several seeds, a fast route of each keeps the best, and in full mode that board is routed end to end; when no seed's fast route is clean, the agent reviews the best, changes the groups, hints or claims and places again, up to --rounds rounds (default 3). One independent verifier grades the result, and the board comes back with REPORT.md. Modes are place and full (place + route). Use it to place, arrange, re-place or optimise components, handle an unplaced board or parts piled off the outline, place and route from scratch, or run the GUI's Place / Place + Route. It never changes the outline. To route a placed board without moving parts, use plan-pcb-routing; for sign-off QA, review-routed-board.
---

# Plan PCB placement

A human lays a board out by function: the USB connector with its resistors
right behind it, the regulator and its caps between the power entry and the
loads, the MCU with its crystal on its oscillator pins, the LEDs and the
button along an edge. This skill places that way. You decide what belongs
together; `py_placer/place_simple.py` arranges it over several seeds; a fast
route of each keeps the best. When none routes clean, you change what you
gave it and place again.

Invocation: `/plan-pcb-placement <mode> <board.kicad_pcb> [intent.json] [--rounds N]`,
where mode is `place` or `full`. With no mode, use `full`. A given
`intent.json` is the intent: use it in place of emitting one (§3.2). `--rounds N` is
the most placement rounds (§3.6), default 3. The run stops at the first round
whose best seed's fast route has no blocking net, so a board that routes
clean after one round gets no review and no input changes.

## 1. The goal, per mode

| mode | input | DONE means | better, once DONE |
|---|---|---|---|
| `place` | an unplaced or placed board | `check_assembly` buildable, `check_floorplan --intent` 0 errors, no pad, graphic or keep-out copper off the outline, mechanical facts met, and a fast route of it with no blocking net | fewer vias, then less copper_mm, then fewer segments, on that fast route |
| `full` | an unplaced or placed board | `board_score` blocking 0, zero unrouted, zero broken, `check_complete` DONE | fewer vias, then less copper_mm, then fewer segments |

- **"Better" is lexicographic, never a weighted sum.** A lower via count never
  buys back an open net.
- **In `place` mode the fast route is your own check.** `scripts/grade.py
  --mode place` and the verifier grade the placement, not a route; keep the
  fast-routed board and its `board_score` JSON as the evidence for the "no
  blocking net" condition.
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
- **Keep each round in `wk/<run>/r<N>/`**, with these names, which the GUI's
  candidates table reads: `s<S>.kicad_pcb` and `s<S>.json` (seed S's
  placement and its `place_simple` report), `s<S>_route/` (the fast route's
  working files), `s<S>_fast.kicad_pcb` (the fast-routed board, copied out of
  that folder with `copy_board.py`) and `s<S>_fast.score.json` (its
  `board_score`). The kept board goes to `wk/<run>/final.kicad_pcb`.
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
  it from net names.
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
and slides it along. On a pile the emitted entry carries no `edge` -- the
staging pose says nothing -- so write it from the brief, `mechanical.json`
or the user.

### 3.2 Emit the intent

```bash
python3 -X utf8 py_tools/check_floorplan.py wk/<run>/fixed.kicad_pcb --emit-intent wk/<run>/intent.json
```

- When a placed reference of the design exists (a human layout, an earlier
  placement), add `--decaps-from <reference.kicad_pcb>`: it arms the decap
  limits, and say in your report that you did.
- **Never emit the intent from a damaged board.** It records the damage as
  the spec. Emit it from the brief, or edit it down.
- `place_simple` reads the intent's `proximity` claims, keep-outs, zones,
  declared faces, `fixed_poses` and the decap limits it grades at error.

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
  crystal at its IC's oscillator pins and its load caps at the crystal, and
  moves all three into the IC's group. The rest of their clock group follows
  only when the crystal is that group's `anchor`; any other member stays in
  the clock group. Every intent `proximity` claim works the same way: the
  claimed part joins its partner's group.
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
  wrong on 3 of 50 side-entry connectors in the corpus. A `Vertical` part has
  no face to declare.

Write these into the intent's `edge_connectors[]` entries (or into the brief,
then emit again, §3.2). Check the fixed parts -- mounting holes, switches, LEDs
a panel shows -- against the brief or `mechanical.json`, and move any that is
wrong with `place_pose ... set ... lock` as in §3.1. Run check_floorplan again:
no `edge_connector` error.

### 3.5 Place

```bash
python3 -X utf8 py_placer/place_simple.py wk/<run>/fixed.kicad_pcb 'wk/<run>/r1/s{seed}.kicad_pcb' \
    --groups wk/<run>/groups.json --intent wk/<run>/intent.json --seeds 0-3 \
    --json 'wk/<run>/r1/s{seed}.json'
```

- **The seeds run in parallel, then the fast routes one at a time.** `--seeds`
  starts one process per seed (`--jobs N` caps how many at once; default one per seed, at most 4),
  each giving exactly the placement a `--seed` run would, its log beside its
  board as `s<S>.log`; one placement process takes about 120 MB. A route
  takes about 1.3 GB, so on a machine with 2 GB to spare never run
  placements beside a route.

- Every part not locked or seated by the intent starts at a random place and
  is annealed; the same board, inputs and seed give the same placement.
  The run is 500 moves per movable part by default (`--moves N` sets the
  total), roughly 2 s for 25 parts.
- **Place seeds 0-3** (or more when the board is small): seeds differ more
  than most input changes. Exit 4 if any seed left a part illegal, 2 if any
  failed outright.
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

### 3.6 Fast-route the seeds; review and place again until one routes clean

A round is: place the seeds (§3.5), fast-route each, keep the best (steps
1-2). **If the best fast route has no blocking net, stop: that board is the
placement.** Otherwise run the review loop (steps 3-6), which finds what the
objective missed, and place again, up to `--rounds` rounds in all (default
3). Judge "clean" on the fast route in both modes: a full route can close a
net the fast route left open, but a placement whose fast routes all leave
nets open is one the review can still improve.

1. **Fast-route each candidate** with the board's routing chain (the recipe
   is `plan-pcb-routing`'s: planes, fanout, the decap clearance step,
   `route.py`), giving `route.py` `--quick-pass`: it skips the recovery tail
   (net rescue, terminal escalation, plane finalize, reconciliation laps),
   where a poor placement's routing time goes, and names the same trouble
   nets as the full route (19 of 20 placements on 10 corpus boards). Use the
   SAME chain for every candidate, so they compare. Run it in
   `r<N>/s<S>_route/` and copy its last board to `r<N>/s<S>_fast.kicad_pcb`.
   Never ship a quick-pass board.
2. **Score each** with `py_tools/board_score.py r<N>/s<S>_fast.kicad_pcb --baseline r<N>/s<S>.kicad_pcb --json r<N>/s<S>_fast.score.json`
   and keep the best by blocking, then vias, copper_mm, segments. **If its
   blocking is 0, stop here**: that board is the placement.
3. **Review the best:**
   ```bash
   python3 -X utf8 py_tools/route_review.py <routed.kicad_pcb> --json wk/<run>/review.json
   ```
   It lists the open nets with the parts holding their loose pads, the signal
   nets with the most vias and the longest detours (routed length over the
   pads' spanning tree), the congested areas with their parts and nets, and
   the two-pad parts turned against their wiring. Compare its worst nets with
   the render: `python3 -X utf8 py_tools/render_nets.py -o <png> --nets <NET> human=<ref routed board> run=<routed>`
   draws chosen nets side by side.
4. **Change what you give `place_simple`, not the board.** It re-places every
   unlocked part, so a part moved by hand is moved back. For each finding:
   - a group placed away from what it serves (a regulator far from its power
     entry, a connector's resistors across the board): a `near` hint between
     the two groups;
   - a part far from the pins it serves: move it into their group, or give it
     a `proximity` claim in the intent (`{"ref": "C6", "near": "SW1", "max_mm": 2}`,
     pad edge to the nearest pad on a shared net);
   - two groups crowding one area: an `apart` hint, or split a group that is
     two things;
   - a part whose pose is right and should stay: lock it in the board
     (`place_pose ... lock REF`) or declare it in `fixed_poses`;
   - a turned two-pad part: leave it for the end (`place_pose ... rotate`
     after the last placement).
5. **Place again** with the same seeds, fast-route, score, and keep the
   better board. An input change reshuffles the whole anneal, so judge it on
   every seed, not only the net you aimed at.
6. **Stop** at the first round whose best fast route has no blocking net,
   or after `--rounds` rounds; keep the best board of all rounds (fewest
   blocking, then vias, copper_mm, segments). Record each round in the
   ledger (§5.6).

In `place` mode the best fast-routed placement is the result. Run
`route_review` on its fast route (step 3, even when the first round was
clean): turn each two-pad part it lists as turned against its wiring with
`place_pose ... rotate`, then check the board again (`check_assembly`,
`check_floorplan --intent`, `render_placement`).

### 3.7 Route (full mode)

Route the kept placement with the full chain (`plan-pcb-routing`'s recipe,
no `--quick-pass`), then `board_score` and `check_complete` with every spec
flag. If DONE does not come, classify the failure (§4) before you re-place.

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
    placement. Change the inputs (§3.6), re-place, strip, re-route. A finer
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
   - **Placement rounds stop at the first clean fast route** (§3.6), or
     after `--rounds`.
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
6. **Record milestones in the ledger:** the first legal placement, each kept
   placement, each review round, the first routed board, the first DONE, the
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
   - **the groups and every judgement call** in them, with its reason, and
     each review round: what the route showed, what you changed, and what it
     did to the routed result;
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
