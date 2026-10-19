# Utility Scripts

This document describes the utility scripts included with the KiCad Grid Router.

## DRC Checker (`check_drc.py`)

Checks a routed PCB for Design Rule Check violations.

### Usage

```bash
python py_router/check_drc.py output.kicad_pcb [OPTIONS]

Options:
  --clearance FLOAT    Track-to-track clearance in mm. Default: auto-detected from
                       the sibling .kicad_pro Default net-class clearance (the value
                       the routing steps recorded as actually used, incl. auto-stepped
                       fine-pitch taps), floored at Board Setup min_clearance as
                       KiCad's DRC does (#1210); falls back to 0.2 if no project is
                       found.
  --via-clearance FLOAT  Via-to-track clearance in mm (uses --clearance if not set)
  --hole-to-hole-clearance FLOAT  Minimum drill hole edge-to-edge clearance in mm
                                  (default: 0.20, the JLC fab floor — same as routing)
  --board-edge-clearance FLOAT    Minimum clearance from board edge in mm (0 = use --clearance)
  --clearance-margin FLOAT  Fraction of --clearance used as tolerance (default: 0.05 = 5%).
                            Violations smaller than clearance*margin are ignored — filters
                            grid-quantization noise (~8 µm artifacts); raise to e.g. 0.1 to
                            suppress more borderline grazes
  --nets PATTERN       Only check nets matching pattern
  --debug-lines        Output debug lines on User.7 showing violation locations
  --max-print INT      Max violations listed per type before "... and N more"
                       (0 = print all; default 20)
  --no-size-checks     Skip the track-width and via/hole-size fab-floor checks
  --min-track-width FLOAT   Minimum manufacturable track width in mm
                            (default: JLC fab floor for the board layer count)
  --min-via-diameter FLOAT  Minimum via outer diameter in mm (default: fab floor)
  --min-via-drill FLOAT     Minimum via drill diameter in mm (default: fab floor)
  --size-margin FLOAT       Absolute tolerance in mm for the size checks (default: 0)
  --check-pad-edge          Also check pad-to-board-edge clearance. Off by default:
                            pad-edge violations are almost always pre-existing
                            edge-connector pads, not router-introduced.
  --fab-tier {standard,advanced,auto}  JLC fab capability floor the size checks grade against
                            (default: standard). Pass the tier the board was routed to so
                            legitimately-escalated fine geometry is not flagged
  --fab-overrides FILE      Fab-floor override file overlaying the selected --fab-tier
                            (see [Fab Tier Options](configuration.md#fab-tier-options))
```

The per-type listing is capped at `--max-print` (default 20); when a type has
more, a `... and N more (use --max-print 0 to show all)` marker is printed so
the listing is never silently truncated relative to the header count. Use
`--max-print 0` to dump everything for log-based triage.

### Examples

```bash
# Auto-grade at the clearance the board was routed to (read from its .kicad_pro)
python py_router/check_drc.py routed.kicad_pcb

# Override the grading clearance explicitly
python py_router/check_drc.py routed.kicad_pcb --clearance 0.15

# Override clearance and hole-to-hole explicitly (hole-to-hole defaults to the 0.20 fab floor)
python py_router/check_drc.py routed.kicad_pcb --clearance 0.2 --hole-to-hole-clearance 0.2

# Check specific nets only
python py_router/check_drc.py routed.kicad_pcb --nets "*DATA*"
```

### Checks Performed

The DRC checker validates:

1. **Track-to-track clearance** - Segments on the same layer maintain minimum clearance
2. **Track-to-pad clearance** - Tracks maintain clearance from pads on other nets
3. **Via-to-track clearance** - Vias maintain clearance from tracks on other nets. A via KiCad types blind, buried or micro is graded only on the copper layers between its end layers, here, in check 5 and inside a filled copper graphic (#1268); a via with no type word is a through via whatever its `(layers ...)` pair, as KiCad loads it
4. **Via-to-via clearance** - Vias maintain clearance from each other
5. **Pad-to-via clearance** - Vias maintain clearance from pads on other nets (an SMD pad off a blind/micro via's span is not graded against it)
6. **Pad-to-pad clearance** - Pads of different nets on a shared copper layer maintain clearance (catches overlaps/shorts, e.g. a placement step nudging a part onto a foreign pad). Pads of the *same* footprint are skipped — a component's own fine-pitch pin gaps are fixed library geometry, not a pipeline-introduced defect.
7. **Copper-to-hole clearance** - Tracks maintain clearance from NPTH (no-copper) drill holes of other nets — a real fab short the via-drill hole check misses (e.g. a track across a mounting hole)
8. **Hole-to-hole clearance** - Drill holes (via drills and through-hole pad drills) maintain edge-to-edge clearance, even on the same net (manufacturing constraint)
9. **Board edge clearance** - Tracks and vias maintain clearance from the real `Edge.Cuts` outline (outer ring **plus interior cutouts/slots**), so copper routed into a cutout — which sits inside the bounding box — is caught. Falls back to the bounding box when the parser finds no usable outline. With `--check-pad-edge`, pads are checked too.
10. **Same-net crossings** - Detects tracks crossing on the same layer within a net
11. **Track width** - Segments are at least the fab-floor minimum track width (the active `--fab-tier`'s deepest floor; standard = JLC 0.127mm on 2-layer, 0.0889mm on 4+ layer). Catches sub-fab copper a clearance-only check misses — a board's own `min_track_width` DRC rule can be lowered to match undersized tracks, so it never trips; the fab floor is the real limit.
12. **Via / hole size** - Via outer diameter and drill are at least the deepest fab via the tier can reach — the advanced (small/fine) via the router escalates to: JLC 0.25mm/0.15mm. Pass `--fab-tier` so grading matches how the board was routed (see [Fab Tier Options](configuration.md#fab-tier-options)).

13. **Footprint graphic copper past the outline** (`graphic-off-board`, #962) - Copper a footprint draws (a SOT-89 tab, an antenna) reaching past the board outline, measured with the stroke as drawn, circles on their true curve, and inside a FILLED shape. One row per shape. It runs even when the edge check is off (severity `ignore`), and regardless of `--nets` (graphic copper is net 0). Waived only for board-level art and for a footprint that owns the board outline; a lock is not a waiver. Copper the parser does not model (pad-less logos, bezier curves, copper text) is printed as not measured.
14. **Graphic copper grazing the edge** - Inside the outline but within the edge clearance, footprint graphic copper is accepted as `immutable-graphic` (library art no routing pass can fix). With `--baseline BOARD`, a graze on a part whose pose differs from the baseline is a `graphic-board-edge` violation (`origin: placement`); an unmoved one is accepted `inherited`. Without it, grazes are accepted `unverified` and a console line says so.
15. **Via in a solder-paste opening** (`via-in-paste`, #962) - A via whose barrel overlaps a paste opening of its own net and is not filled AND capped (IPC-4761 Type VII; the via's own spec, then the board setup, then KiCad's factory value, token by token). Paste wicks into such a barrel, and KiCad has no such check. A filled+capped via is accepted `protected-via-in-paste`; with `--baseline BOARD`, a via the baseline already had inside an opening, unprotected, is accepted `inherited-via-in-paste`; on a file older than KiCad 10 (version < 20250000), which cannot carry per-via capping/filling at all, every other such via is accepted `undeclarable-via-in-paste` (the requirement belongs on the fab drawing). A buried via, or a blind via that does not reach the paste side, is not a hit. A console line and the `--json` `via_in_paste` block count every class.

Checks 11–12 are on by default; pass `--no-size-checks` to skip them, or override the floors with `--min-track-width` / `--min-via-diameter` / `--min-via-drill`. The floor is derived from the board's copper-layer count unless overridden.

`--baseline BOARD` is the board this one was derived from (the unrouted input, or the placement run's starting board). It is a checker option, not a routing parameter; pass it whenever you have that board, or pre-existing vias in paste openings read as violations against the run.

### Clearance Margin

The DRC checker uses a 5% clearance margin by default to ignore tiny geometric artifacts from corner handling. For example, with a 0.1mm clearance, violations smaller than 0.005mm are ignored.

### Debug Visualization

With `--debug-lines`, the checker outputs debug lines on the `User.7` layer in the PCB file, showing the closest points between violating segments:

```bash
python py_router/check_drc.py routed.kicad_pcb --debug-lines
```

Output includes the distance of each violation:

```
Debug line: (10.200, 15.300) to (10.205, 15.305), distance: 0.0025mm
Writing 3 debug lines to User.7 layer
```

Open the PCB in KiCad and enable the `User.7` layer to visualize where clearance violations occur.

### Output

```
Checking clearances in routed.kicad_pcb...
Track clearance: 0.2mm
Via clearance: 0.2mm

Checking 1,234 segments and 89 vias...

Found 0 DRC violations
```

If violations are found:

```
VIOLATION: Track too close to track
  Net-(U2A-DATA_0) segment (10.2, 15.3)-(10.4, 15.5) on F.Cu
  Net-(U2A-DATA_1) segment (10.3, 15.2)-(10.5, 15.4) on F.Cu
  Distance: 0.185mm (minimum: 0.2mm)
```

## Hand-Join Verifier (`check_join.py`)

Verifies a CANDIDATE hand-join (a polyline of track points plus optional
vias) against all board copper, before the first segment is committed —
promoted from the run-7 endgame, where hand-authored copper verified only
against its target pads shipped 42 shorts.

```bash
python py_tools/check_join.py BOARD NET x,y,layer x,y,layer ... [via:x,y ...]
```

Consecutive same-layer points become track segments; a layer change must
happen at one coordinate over a `via:` token or it is a `missing-via`
violation. The candidate is staged onto a copy of the board and graded by
the real `check_drc` engine (staged-vs-baseline diff), so netclass pairwise
clearances, `.kicad_dru` layer rules, rotated pads, custom pad polygons,
board-edge and hole-to-hole all apply — plus a same-net via-stack check
KiCad's DRC deliberately never does. Track width and via geometry default
from the board's own Default netclass; override with `--width` /
`--via-size` / `--via-drill`. `--keep-staged PATH` writes the staged board
for inspection in KiCad. Exit 0 clean, 1 violations, 2 usage errors.

## Placer (`place_simple.py`)

Places a board by function: the parts sorted into groups, one objective,
lowered by simulated annealing, and exact pad legality at the end. It is the
placer the `plan-pcb-placement` skill runs in rounds. A round places seeds 0-15,
fast-routes each (`route.py --quick 2`, scored by `quick_score.py`) and keeps the best. The run stops at
the first round whose best fast route has no blocking net; otherwise the skill
reviews the best with `route_review.py`, changes the groups, hints or claims
and places again, up to `--rounds N` rounds in all (default 3). The kept board
is then routed in full (`/plan-pcb-routing`).

```bash
python3 -X utf8 py_placer/place_simple.py board.kicad_pcb placed.kicad_pcb \
    --groups groups.json --intent intent.json [--seed 0] [--json report.json]
# several seeds at once, one process each:
python3 -X utf8 py_placer/place_simple.py board.kicad_pcb 'r1/s{seed}.kicad_pcb' \
    --groups groups.json --intent intent.json --seeds 0-15 [--jobs 4] --json 'r1/s{seed}.json'
```

- **Inputs.** `--groups` is the groups file (`py_tools/check_groups.py`
  checks it): each group a set of parts kept together, with optional `near`,
  `apart` and `edge` hints and a `side`. `--intent` is the emitted intent
  (`check_floorplan --emit-intent`): its `proximity[]` claims, keep-outs,
  zones, declared faces, `fixed_poses[]`, and the decap limits it grades at
  error (priced per mm past each limit, `--decap-limit-weight`).
- **What stays put.** Parts the board locks (`(locked yes)`) and parts the
  intent seats at a declared pose, written there first. Faces the intent or a
  group's `side` declares are applied first, by the writer's flip.
- **What moves.** Every other part starts at a random place on the board and
  is annealed: 500 moves per movable part by default (`--moves` sets the
  total instead). A group gets no
  region or shape; its gap tree is a cost term that keeps it in one piece.
  The hints are soft terms; the intent's keep-outs and zones bind every part
  through the anneal's overlap term. Every weight is a flag (`--help`).
  A run that leaves a part with no legal pose is annealed again from a fresh
  start, up to two more times; the first legal placement is kept.
- **Deterministic.** The same board, inputs and `--seed` give the same
  placement. Seeds differ more than most input changes, so place several and
  judge them by routing.
- **Several seeds.** `--seeds 0-3` (or `0,1,2,3`) runs each seed as its own
  `--seed` process, `--jobs` at a time (default one per seed, at most 4), with
  `{seed}` in OUT and `--json` and each seed's log beside its board as
  `.log`; it prints one line per seed and exits 2 if any seed failed, else 4
  if any left parts illegal, else 0. One process takes about 120 MB.
- **Output.** The placed board with its siblings, a `JSON_SUMMARY:` line
  (final cost, moves, seed, parts moved to a legal pose after the anneal,
  parts left illegal, seconds) and, with `--json`, the full report (adding
  `runs`, the anneals it took, each move kind's tried and kept counts, the
  groups, the notes, and `unseated`: declared edge parts that found no seat on
  their edge and were placed as any other part, each also a WARNING line).

Exit 0 placed; 2 unreadable input, a part in two groups, or a face or pose
the inputs contradict (two declarations disagree, or a KiCad-locked part is
declared on the other face or at another pose), nothing written; 4 placed,
with parts left illegal, a declared pose that is not legal among them (named
on the console). The objective's terms and the anneal's moves are in
`py_placer/placement/README.md` ("The objective").

## Groups Checker (`check_groups.py`)

Checks a groups file against its board before `place_simple.py` places it.
Which parts belong together is a judgement (the `plan-pcb-placement` skill
makes it); this measures the judgement before any part moves.

```bash
python3 -X utf8 py_tools/check_groups.py board.kicad_pcb --groups groups.json
python3 -X utf8 py_tools/check_groups.py board.kicad_pcb --groups groups.json --intent intent.json
python3 -X utf8 py_tools/check_groups.py board.kicad_pcb --groups groups.json --json report.json
```

- **ERROR** (exit 4): a movable part in no group, a part in two groups, a ref
  the board does not have, a group with no part on the board.
- **WARN**: a part with more signal links to another group than to its own
  (rails are left out), and a decap whose IC is in another group while no IC
  of its own group carries its rail -- `place_simple` then leaves it out of
  the decap term, which is right for a load or RC cap and wrong for a real
  decap.
- **Reported**: bridges (two-pad parts linking two groups), the inter-group
  signal-link matrix and the rails.
- **Crystals**: each passive crystal with its IC and load caps.
  `place_simple` folds them into their IC's group and claims the crystal at
  the oscillator pins, so a load cap is no decap conflict (`--no-crystals`
  checks as if no claim were written).

Exit 0 placeable (warnings allowed); 2 unreadable input; 4 errors.

## Pose Setter (`place_pose.py`)

Applies a pose the MODEL chose, and lets the engine grade it, so a rotation or
a lock is a registered lever rather than a hand script around
`placement.writer`.

```bash
python py_placer/place_pose.py BOARD OUT set U1 129.9 98.3 --rot 270
python py_placer/place_pose.py BOARD OUT set U1 --near 130 98 --rot 270
python py_placer/place_pose.py BOARD OUT rotate CON2 180 [--relative]
python py_placer/place_pose.py BOARD OUT face U1 W USB1
python py_placer/place_pose.py BOARD OUT lock U1 CON1
python py_placer/place_pose.py BOARD OUT set U1 129.9 98.3 rotate CON2 180 lock U1
python py_placer/place_pose.py BOARD OUT replicate --from CH1 --to CH2 --to CH3 [--anchor REF | --offset DX DY] [--allow-net-mismatch REF.PAD ...] [--only REFS | --skip REFS] [--search MM]
python py_placer/place_pose.py BOARD OUT --intent intent.json replicate   # the intent's replicate[]
```

Several verbs in one call describe ONE arrangement: every op is resolved
against the INPUT board and written in a single pass, so no op sees another's
effect, and the arrangement is graded at the exact points given -- so a snap
needs a call with ONE pose op: `--near` beside a second pose op is refused at
exit 2 before anything is graded (issue one `set REF --near X Y` per
call; lock/unlock may ride along), and `--snap` there is skipped and says so.
`set` takes exact coordinates positionally; `--near X Y` is the same
point read as approximate and implies `--snap`, which takes the nearest legal
pose within `--radius` (ranked nearest first by `pose_score`, then RE-GRADED here — the
ranker's legality is an AABB gate and this verb's verdict is exact geometry,
so a candidate is verified, never trusted). `face REF FACE PARTNER` names a
pad row by the face it is on NOW and turns the part until that row points at
the named partner; the rotation is predicted from the rigid body and then
MEASURED on the board actually written, because `escape.face_of` takes an
argmin against a box that is not square and a corner pad can change sides
under a rotation that carries the row.

The verdict is `placement.legality.grade_pad_legality` — the same numbers
`check_assembly` and `render_placement`'s review sheet print, netclass- and
`.kicad_dru`-aware — on the candidate board against the same grade on the
input. A request is refused when it makes a category worse:

- the counts (pad conflicts, hole conflicts, pads off-board) **and their
  magnitudes** (`pad_shortfall`, `oob_pad_amount`);
- **pad stacks**: two parts' pad copper overlapping on a shared side, ANY net,
  measured by check_assembly's own `legality.pad_intersection_pairs`
  (`pad_stack_count`, `pad_stack_area` summed over every stacked pad pair, and
  `pad_stack_pairs` as a set, so a new stack is refused when the totals tie).
  The pad-conflict count skips same-net pads, so this is the category that
  catches a part set on another part's same-net pad;
- **pads on another part's footprint graphic copper** (a drawn SOT-89 tab, an
  antenna, its filled interior included): `graphic_pad_conflicts` short of
  clearance on a net the copper is not, `graphic_pad_stack_count` touching on
  any net but a net tie's own, `graphic_pad_shortfall`, and their `_pairs`,
  measured by `check_drc.footprint_graphic_pad_census`.

A request is **never refused for damage the board already had**: an absolute
gate is False for a large share of parts on a real board before anything
moves, so it would refuse poses no worse than where the part already sits, and
would make this tool useless on the unplaced pile it exists to arrange. The
summary carries the two facts under different names: `no_worse` is the
verdict the verb acts on, `legal` is whether the board is clean at this pose.
`--strict-legal` refuses unless the result is clean; `--force` writes anyway
and records `forced`. A KiCad `(locked yes)` refuses a direct move — name the
ref in `unlock` in the same call if you mean it; `--force` deliberately does
not open that, and the unlock is verified on the staged board before anything
is promoted.

With `--intent PATH` each MOVED part is also graded against that
floorplan intent's zones, by the grade's own `zone_containment` rule. A pose
that leaves a part further outside its block's zone than it was, as an ERROR
finding, refuses at exit 4, and nothing is written; with the rule demoted to
warn the pose is written and the row reported. `JSON_SUMMARY.zone_check` names the block, the
zone and the overrun before and after. The check is relative, like the legality
verdict: a move from the pile toward its zone is never refused for not
arriving. `--force` writes anyway and says so. A call that only locks or
unlocks moves nothing and records `zone_check.skipped`. With a plan in hand,
pass it.

`--snap` tries candidates from a ladder of rungs, nearest the aimed point
first, and **every** candidate from any rung is re-graded in this verb's own
currency before it is written:

1. **`ranked`** -- `pose_score.rank_poses`: the poses its gate admits within
   `--radius`, nearest the aimed point first. It judges the part from its pose
   on the INPUT board, the baseline the re-grade compares with, while it
   sweeps around the aimed point.
2. **the lattice** -- every pose on a grid around the aimed point. The
   ranker's gate is absolute, so it can return nothing where the relative
   re-grade would accept many poses; the lattice reaches those.

When the part's INPUT pose is off the board, the ranker's escape branch is
open (its incumbent is the input pose, `QuenchState.incumbent_at`) and admits
poses merely LESS off the board than the input. So the ranking with that
branch closed goes first, as its own rung (`ranked_on_board`, with its own
`--snap-tries`), ahead of `ranked`. A pose only the escape branch admits is
labelled `legal: false, escape: true` wherever it is reported
(`nearest_legal`), and the no-snap suggestion says so in words. A pose on two
rungs is re-graded once, and when `ranked_on_board` answers, a nearer lattice
pose must grade no worse than that answer too, or the lattice would trade it
for pad copper still past the outline. `snap_census` reports the rungs
(`ranked_on_board` is null from an input on the board) and `snapped.rung` says
which answered; the no-snap suggestion takes the same order. Neither the snap
nor the no-snap suggestion ever returns the request itself
(`requested_dropped`). `input_pose_vetoed` is the ranker's verdict on the
part's input pose: when it vetoes even that, its knobs are stricter than the
board, and the refusal says so.
When every candidate fails the re-grade, the refusal says what the census
found instead -- `regrade_refused_by` / `regrade_refused_pairs` per
category, e.g. `hole_conflicts x4 (H1/TP1)` -- plus the `untried` count
(raise `--snap-tries`) and the ranker's nearest pose past the bound
(`nearest_beyond_radius`, raise `--radius`). It never offers back a pose its
own re-grade refused, and never advises `--snap` to a call that snapped:
`nearest_legal` on a snap is the pose it wrote, or null.

`replicate` copies one placed instance of a hierarchical sheet onto
its sibling instances. Parts are matched by the symbol uuid that ends every
footprint's `(path ...)`, the correspondence KiCad's "Repeat Layout" uses.
Each target part takes its source part's rotation and side at the source
position plus one offset. By default each target keeps its anchor in place:
the source part with the most pads, or `--anchor REF` from any named sheet.
`--offset DX DY` gives the offset directly, with one `--to` only. It refuses
at exit 4, writing nothing, unless the instances are interchangeable: the
same parts, the same footprints and pads, and nets that correspond one to
one, with a net on both instances (GND, a rail) mapping to itself. The
pads are judged against the net map under which the most pad pairs agree,
chosen over all of them at once (ties go to the pairings met first), so a
strap names itself rather than the pads wired alike; a pad that
breaks the rule under any map (a shared net renamed, a net against no net)
takes no part in choosing it. The
refusal names the refs and pads and spells, for each pad, the waiver
`--allow-net-mismatch REF.PAD`: one pad per flag, for a pad wired
differently per instance BY DESIGN (glasgow_revC's IO_Buffer address
straps U19.2, U20.6, U21.2). REF on the `--from` sheet waives it in every
target, on a `--to` sheet in that one; `nets_waived` reports each waived pad
and whether it was needed. A copy that does not fit has two answers.
`--only REF[,REF]` / `--skip REF[,REF]` copy part of the channel (a `--from`
ref names that part in every target, a `--to` ref that one part); the rest
stay where they are, need no counterpart and are not checked, but a rail on
a part left out still counts as shared. `--search MM` (pitch
`--search-step`, default 0.5 mm) tries each target's offset on a lattice
within MM of the default, nearest first, and keeps the first one the call
grades no worse; targets are fitted in `--to` order, each judged with the
later targets left out of the grade (they are about to move), every
candidate written and re-graded, a move of a locked part skipped. None
within MM is exit 4, with `replicate.to[].search` saying what was tried
and why each was refused. An unknown or ambiguous sheet (a name such
as `CH1`, or a path or a unique start of one of 4+ characters) exits 2 and
lists the instances.
The copy is graded as one arrangement like any other call, with the same lock
rule; `--snap` does not apply. `summary.replicate` reports the mapping, the
offset and the renamed and shared nets, and each moved member lists its new
conflict pairs and the off-board / edge-band / keep-out findings it newly
carries (`ops[].new_conflicts`), which is the list to `--skip` from. The
same copy can be DECLARED in a floorplan intent (`replicate[]`, `min_reader`
11, see `docs/floorplan-intent.md`): `replicate --intent PATH` with no
`--from`/`--to` applies every declaration as one call, and `check_floorplan`
grades each target's distance from the source arrangement. `place_simple`
does not apply them: arrange and lock the source instance, apply the copies,
lock them, then place the rest. See `py_placer/placement/README.md`.

Knobs come from the board (`list_nets.board_floor_knobs`) unless given — a
CLI-supplied `--clearance` loosens the verdict on a tool whose job is to
refuse, so it is disclosed on stderr — and the siblings are carried.
Exit 0 written; 2 the request does not name a thing on this board (a typo you
rewrite); 3 the board carries copper (`--allow-routed` to override); 4 well
formed, and the board said no (a measurement you act on), nothing written —
note that 4 departs from `place_simple`, where it means "written, with parts
left illegal". Every exit the tool itself decides prints one `JSON_SUMMARY:`
line; argparse's own usage errors exit 2 from inside argparse, before there is
a board to summarise. There is no `--allow-unplaced`: this tool has no
unplaced gate, because arranging a pile one decision at a time is what it is
for.

## Assembly Checker (`check_assembly.py`)

Is this placement physically buildable? One verdict, `buildable (blocking 0)`
or `NOT BUILDABLE`, from the body-overlap channel (cross-footprint pad
intersections, fab and courtyard pairs with their waiver labels) and the pad,
hole and off-board legality echo.

```bash
python3 -X utf8 py_tools/check_assembly.py placed.kicad_pcb
python3 -X utf8 py_tools/check_assembly.py placed.kicad_pcb --baseline start.kicad_pcb --json grade.json
```

Read `buildable`. It needs no intent. `--baseline` compares with the board the
placement started from: it reports the advisory pairs that are NEW, and arms
the gates that only make sense for parts that moved -- a courtyard
interpenetration past the area and depth floors, a test point left under
another part's body, a pad on another part's footprint graphic copper.
Without a baseline those are report-only, because healthy human boards ship
such pairs by design. Always gating: pad copper off the outline, a pad
intersection between parts, copper landing on a KiCad-locked part, a stack of
parts at one origin, a containment, and (when `--intent` declares
`assembly.body_edge_mm`) a body inside the body-to-edge band. `--intent` also
reads `overlap_waivers` and a `mating:<ref>` keep-out. Courtyards are graded
at the project's own `courtyards_overlap` severity unless
`--ignore-project-severity`.

Exit 0 buildable; 2 usage or load error; 4 not buildable.

## Board Brief (`board_brief.py`)

Everything a placement author needs to read about a board, in one document:
whether it is a pile, its copper and poured nets, escape-lane deficits and
who causes them, block displacement, net affinity, part classes, groups, lock
advice and the design brief and `mechanical.json` it found beside the board.
It assembles; every number comes from the function that owns it, and
`sources` names that function per section. A section that cannot run says why
under `skipped`.

```bash
python3 -X utf8 py_tools/board_brief.py board.kicad_pcb --json brief.json
python3 -X utf8 py_tools/board_brief.py board.kicad_pcb --fit 15.5x15.5   # where a part this size fits
```

Read `pile`, `has_copper` and `poured_nets` on the `JSON_SUMMARY` line first.
`--requirements TEXT` carries the constraints the board cannot state (an
enclosure, connector edges) verbatim; the design brief (`--brief`, else the
sibling `<board>.design-brief.json`) is the structured channel for them.

Exit 0 written; 2 usage or load error; 3 no board outline (nothing to place
against).

## Board Context Sheet (`board_context.py`)

A per-part sheet a model can reason from: each part's value, package and body
size, each pad with its net and pin function, which board face its partners
are on, what each cap decouples, and the pin-order pairs marked `CROSSED`
(a pair whose order crosses costs a via per net at every rotation).

```bash
python3 -X utf8 py_tools/board_context.py board.kicad_pcb --md
python3 -X utf8 py_tools/board_context.py board.kicad_pcb --json -o context.json
```

The JSON adds `emc_roles`: each converter, crystal, oscillator and ESD array
its pin functions name, every leg measured pad edge to pad edge, a ready
`proximity` row with `max_mm` left null for the user to fill, and
`emc_roles.keep_away`, a proposed keep-away rule per clock with a `brief_row`
to paste into the design brief. Every derived fact carries its source, and
"unknown" is printed apart from "not looked at". `--panels DIR` writes one
cropped PNG per part.

Exit 0 written.

## Rule-Area Writer (`add_rule_area.py`)

Writes a copper keep-out rule area -- `(zone ... (keepout ...))` -- onto a board.
A module's PCB antenna needs one on every layer; the router stamps
a rule area (`obstacle_map.add_rule_area_keepout_obstacles`), placement grades
it, and KiCad reports copper inside it as `items_not_allowed`.

```bash
python3 py_router/add_rule_area.py in.kicad_pcb out.kicad_pcb \
    --name ANT_KEEPOUT --ref U1 --rect -9 -18 9 -12
```

The area is `--rect X0 Y0 X1 Y1` or `--polygon X,Y X,Y X,Y ...` in board mm,
or, with `--ref`, in that footprint's local frame as the file stores it -- the
frame its pads' `(at)` positions are written in, already mirrored for a part on
the back -- so re-running after the part moves puts the area where the part
now is. It goes on every copper layer unless `--layers` names some, and
forbids `tracks vias copperpour` unless `--forbid` lists others (pads and
footprints stay allowed by default). A board-level rule area of the same
`--name` is replaced, so the command is idempotent; the output gets the
input's siblings. Exit 0 written, 2 for a usage error, a `--ref` the board
does not have, or a layer it does not have.

## Connectivity Checker (`check_connected.py`)

Verifies that all nets are fully connected after routing. Detects two types of issues:

1. **Unrouted nets** - Nets with pads but no tracks (routing was never attempted or failed)
2. **Broken routes** - Nets with tracks that don't connect all pads (incomplete routing)

Unrouted detection is based on whether a net is actually covered by a copper
zone, not a hard-coded name list — a power net (GND, +3V3, …) with no plane on
this board *is* reported as unrouted. Same-net pads whose copper overlaps (e.g.
a castellated module's co-located through-hole + SMD pad pair) count as
connected even with no track between them.

### Usage

```bash
python py_router/check_connected.py output.kicad_pcb [OPTIONS]

Options:
  --nets PATTERN       Only check nets matching pattern
  --component, -C REF  Check all nets connected to a component (e.g., U1)
  --tolerance FLOAT    Connection tolerance in mm (default: 0.02)
  --verbose            Show detailed break location info
  --quiet              Only print summary line unless issues found
  --routed-only, -r    Only check routed nets (skip unrouted net detection)
```

### Examples

```bash
# Check all nets (detects both unrouted and broken routes)
python py_router/check_connected.py routed.kicad_pcb

# Check specific nets
python py_router/check_connected.py routed.kicad_pcb --nets "*lvds*"

# Check all nets on a component
python py_router/check_connected.py routed.kicad_pcb --component U102

# Check specific patterns on a component
python py_router/check_connected.py routed.kicad_pcb --component U1 --nets "*DATA*"

# Only check connectivity of routed nets (skip unrouted detection)
python py_router/check_connected.py routed.kicad_pcb --routed-only

# Verbose output with break locations
python py_router/check_connected.py routed.kicad_pcb --verbose
```

### Output

```
Checking connectivity in routed.kicad_pcb...

Checking 64 nets...

All nets are fully connected!
```

If issues are found:

```
============================================================
FOUND 3 ISSUES:

  Unrouted nets (2):
    /PROG- (2 pads)
    /PC-A3 (3 pads)

  Connectivity issues (1):

  Net-(U2A-DATA_5) (net 42):
    Segments: 12, Vias: 2, Pads: 3
    Disconnected components: 2
    Disconnected pads:
      (25.10, 30.40) on F.Cu [U2A]
============================================================
```

## Route Review (`route_review.py`)

What to move after a route. It reads one routed board and lists what a
placement pass that moves parts and routes again needs:

- **open nets**, each with the parts holding its loose pads (the
  connectivity checker's verdict). A net a pour serves is marked `poured`,
  since the fill rather than the placement decides it;
- **costly signal nets**, ranked by vias and by detour: routed length over the
  length of the minimum spanning tree of the net's pads. Rails and poured
  nets are left out;
- **congested areas**: the board on a grid (`--cell`, 2 mm). A cell's
  occupancy is its track length times the routing pitch (track width plus
  clearance), plus its vias' footprint on every layer they cross, over its
  area on the layers that carry signal copper. Cells at `--hot` (0.5) or more are
  joined into areas, each listed with its parts and the nets through it;
- **turned parts**: two-pad parts whose two nets would both be shorter with
  the part turned 180°.

```bash
python3 -X utf8 py_tools/route_review.py routed.kicad_pcb
python3 -X utf8 py_tools/route_review.py routed.kicad_pcb --top 12 --json review.json
```

What to do about each finding is in
`.claude/skills/plan-pcb-placement/references/placement-lessons.md`. The
`/plan-pcb-placement` skill (`.claude/skills/plan-pcb-placement/SKILL.md`,
§3.6) runs it on the best fast-routed candidate (`route.py --quick 2`) of
a round whose best fast route still has a blocking net, then changes the
groups, hints or claims it gives `place_simple` and places again, up to
`--rounds` rounds; `render_nets.py` draws a finding's nets beside another
routed board's. Exit 0 reviewed (findings or not), 2 unreadable input.

## Seed Judge Score (`quick_score.py`)

Scores a board a `route.py --quick 2` chain routed, the judging route the
placement skill gives each seed. `blocking` is the route step's own tally --
failed singles, open singles and the multipoint pad deficit -- read from its
`--json-out` file or the last `JSON_SUMMARY_MIN` line of its log; `quality`
(vias, copper_mm, segments) is `board_score`'s own count of the board. It
leaves DRC, assembly and the floorplan out: level 2 skips the passes that fix
grazes, so DRC on its board measures the polish it skipped, not the
placement. The JSON has `board_score`'s keys, so the GUI's candidates table
reads either.

```bash
python3 -X utf8 py_tools/quick_score.py r1/s0_fast.kicad_pcb --route r1/s0_route/route.json \
    --json r1/s0_fast.score.json
```

Exit 0 when no net blocks, 4 when some do, 2 when the summary holds no tally.
Never grade a board to ship with it: route that one in full and run
`board_score`.

## Board Renderer (`route_render.py`)

A still PNG of a board straight from its parsed geometry (segments, vias,
pads, zones, outline) with [Pillow](https://python-pillow.org/): no KiCad,
`kicad-cli`, SVG step or headless browser, ~0.2 s a board. It is the one
renderer the other pictures are drawn on: the GUI placement tab's live
preview (`render_board_file`, redrawn each time the board being placed
changes), `render_placement.py`'s stills, `check_drc.py --render DIR`'s
violation-cluster crops, `route.py --preview --preview-png`'s proposed
copper, and the stress-run images.

```bash
python3 py_router/route_render.py BOARD.kicad_pcb [-o OUT.png] [--size 1600]
    [--supersample 2] [--layer-alpha N] [--no-pads] [--no-zones]
    [--layers F.Cu,B.Cu] [--view X0,Y0,X1,Y1] [--refs | --no-refs]
    [--ruler | --no-ruler] [--theme light|dark]
```

- `-o` — the PNG to write (default: beside the board, same name).
- `--size` — longest image dimension in px. `--supersample` anti-aliases (1 = fastest, 2 = crisp stills).
- `--layer-alpha` — per-layer copper opacity 1–255; `<255` blends overlapping layers at crossings, `255` = opaque (default: the theme's own, dark 150, light 205).
- `--layers` — render only these copper layers (per-layer views).
- `--view` — crop to a world rect in board mm; the frame is labelled with the rect.
- `--refs` / `--ruler` — reference designators at footprint origins, and mm ticks along the top and left edges, so the picture can be matched to JSON coordinates. On by default for a `--view` crop, off for the whole board.
- `--theme` — `light` (the default, or `$KICAD_RENDER_THEME`) or `dark` (KiCad's own canvas).

![the same board in both themes](946-themes.png)

Library API:

```python
import sys
sys.path.insert(0, "py_router")
from kicad_parser import parse_kicad_pcb
from route_render import BoardRenderer

r = BoardRenderer(parse_kicad_pcb("board.kicad_pcb"))
r.render().save("board.png")                       # whole board
r.frame(segments=subset, vias=[]).save("f.png")    # an arbitrary subset
```

`BoardRenderer` computes the world→pixel transform and the static substrate
(outline, zones, pads) **once**; each `frame(...)` composites a chosen subset of
copper on top, with optional highlight colors and `overlays=` drawing
callables. `render_board_file(board, out_png, ...)` is the one-call form
(parse, render, save; the same options as the CLI) that the GUI preview uses.

## Placement Renderer (`render_placement.py`)

Headless PNG stills of a placement, drawn on `route_render`: courtyards, pads,
reference designators, airwires, and a legality overlay (pad and hole conflict
rings, NPTH keep-outs, pad copper past the outline), with a metrics caption
that carries the verdict. A board with back-side parts gets an F and a B
panel by default. It draws a broken or unplaced board rather than refusing it
(an unplaced pile is framed to the parts, not the outline).

```bash
python3 -X utf8 py_tools/render_placement.py placed.kicad_pcb -o placed.png
python3 -X utf8 py_tools/render_placement.py placed.kicad_pcb --before start.kicad_pcb -o delta.png
python3 -X utf8 py_tools/render_placement.py placed.kicad_pcb --intent intent.json \
    --review-sheet sheet.png --json-out render.json
```

- `--before BOARD` — dashed rects where each part started and displacement
  arrows; `--pair` instead renders both boards with identical settings and
  diffs their legality findings by name (what the move fixed and broke).
- `--side F|B`, `--per-side`, `--flat` — one face, both faces, or one
  flattened panel.
- `--view X0,Y0,X1,Y1` or `--zoom-group BLOCK` (`--list-groups` prints the
  blocks) — crop; `--focus` with `--summary-json` (a route.py
  `JSON_SUMMARY`) adds one panel per failed-net cluster.
- `--ratsnest-nets PATTERN ...` — airwires for just these nets (route.py
  `--nets` glob syntax), with their parts labelled; `--ratsnest-all` draws
  every net.
- `--intent PATH` — the floorplan intent: its `overlap_waivers` and
  `assembly.body_edge_mm` are graded, and its zones, keep-outs and edge
  bands are drawn under the parts (`render_plan.py`).
- `--review-sheet PATH` — one composite image: F and B side by side over a
  facts strip (courtyard census, each connector's distance to the nearest
  edge).
- `--json` / `--json-out PATH` — the summary document, including the
  `checklist` (`a_off_outline.pad_copper_gating` names the parts whose pad
  copper lies outside the outline; `b_courtyard_blocking_pairs`,
  `c_hole_conflicts`, ...).
- `--clearance` defaults to the board's own Default net-class clearance; the
  value used and its source are printed.

Exit 0 drawn, whatever the board's state; 2 for arguments that cannot be
honoured (an unreadable `--intent`, `--pair` without `--before`, `--view`
with `--zoom-group`, an unknown block, a requested review sheet that was not
written); with `--gate`, 4 when the checklist finds anything. Every toggle,
with pictures, is in `py_placer/placement/README.md` ("Reviewing a
placement: render it").

## Floorplan Overlay (`render_plan.py`)

A library module, not a command: it draws a floorplan intent's geometry onto
a `route_render` frame. Each block's `zone` is a dashed rectangle labelled
with its name and first refs, each keep-out (`rect` or `circle`) is outlined
and hatched, and each edge connector's `along_edge_band` is a thick stroke
on its edge. `render_placement.py --intent` draws it faint, under the parts.

It reads no intent file: it takes an `Intent` already loaded by
`placement.floorplan`, so the overlay cannot disagree with the grade about
what the intent means. `plan_overlay(intent, verdict=..., alpha=...)` returns
a callable for `BoardRenderer.frame(overlays=[...])`; a `verdict` map
(block name to held / drifted) recolours the zones by it. `plan_summary(intent)`
is the one line printed beside the render, and it says so when the intent
declares nothing, so an empty overlay is never mistaken for a plan that was
met. A drawing error never fails the render.

## Nets Side by Side (`render_nets.py`)

Draws chosen nets' copper on several routed boards side by side, one panel
per board over the same window, to compare them track by track: a routed
result against the human layout, or against another placement's route. The
chosen nets' tracks are drawn bright in their layer's colour (F.Cu red, B.Cu
blue, then the inner layers in a fixed order) with their vias, their pads
pink, and every other track, pad and via faint behind them. Each panel's
title is its label, the chosen nets' via count and their track length.

```bash
python3 -X utf8 py_tools/render_nets.py -o osc.png --nets PA1,PA2 \
    human=routed_human.kicad_pcb ours=routed.kicad_pcb
python3 -X utf8 py_tools/render_nets.py -o gnd.png --nets GND --view 120,60,150,90 a.kicad_pcb
```

- Boards are `LABEL=board.kicad_pcb`, or a bare path (labelled by its file
  name).
- `--nets` — exact names or fnmatch patterns, comma-separated; repeat the
  flag for more.
- `--view x0,y0,x1,y1` — the window in mm (default: the chosen nets' pads
  and copper on every board, plus 2 mm).
- `--scale` — pixels per mm (default 12).

Exit 0 drawn; 2 an unreadable board, or a pattern no board's net matches.

## Orphan Stub Checker (`check_orphan_stubs.py`)

Detects trace endpoints that end without a proper connection point (via or pad).

### Usage

```bash
python py_tools/check_orphan_stubs.py input.kicad_pcb [OPTIONS]

Options:
  --net NET_NAME       Only check this net
  --layer LAYER        Only check this layer
  --compare            Compare two files to find new/removed orphans
```

### Examples

```bash
# Check all nets
python py_tools/check_orphan_stubs.py board.kicad_pcb

# Check specific net and layer
python py_tools/check_orphan_stubs.py board.kicad_pcb --net "+3.3V" --layer F.Cu

# Compare before/after to find new orphans
python py_tools/check_orphan_stubs.py original.kicad_pcb modified.kicad_pcb --compare
```

### What It Detects

An orphan stub is a trace endpoint that:
1. Has only one connected segment (degree-1 node in the connectivity graph)
2. Does not overlap same-net copper with its end cap: a via, a pad (by its
   real outline), another track's body, or a same-net zone outline
3. Is NOT a reverse T: no other same-net track vertex or via lands on the
   stub's own body within 3 track widths of the free end (check_weird's
   mid-body-anchor rule, #1167)

These represent traces that end without a proper electrical connection.

### Output

```
Loading board.kicad_pcb...

Checking for orphan trace stubs...

============================================================
FOUND 2 ORPHAN STUBS:

  +3.3V on F.Cu: 1 orphans
    (142.50, 98.30)
  +3.3V on B.Cu: 1 orphans
    (155.20, 102.10)
============================================================
```

When no orphans are found:

```
Loading board.kicad_pcb...

Checking for orphan trace stubs...

============================================================
NO ORPHAN STUBS FOUND!
============================================================
```

When comparing two files:

```
Orphan Stub Comparison
============================================================
File 1 (original.kicad_pcb): 5 orphans
File 2 (modified.kicad_pcb): 3 orphans

New orphans in file 2: 0
Removed orphans (fixed): 2
```

## Pad Geometry Checker (`check_pads.py`)

Reports same-footprint, different-net pads whose copper **overlaps** - a short, and
almost always a sign that the pad's rotation or size is being modelled wrong. The
classic trigger is a QFN/QFP/BGA placed at a non-orthogonal board angle: if the pad
rectangles are mis-oriented, adjacent pads collide here and any fanout escapes its
stubs across them. Run this before fanout as a sanity check (the fanout tools also run
it automatically on their component and warn).

### Usage

```bash
python py_router/check_pads.py board.kicad_pcb [OPTIONS]

Options:
  --component, -c REF   Restrict the check to one footprint reference
  --cross-footprint     Also check overlaps across different footprints
                        (broader, noisier board-level short check)
  --tolerance MM        Minimum overlap depth to report (default 0.05)
  --quiet, -q           Print only the PASS/FAIL summary line
```

Only pads that share a copper layer are compared, so edge-connector fingers on opposite
sides and a part's top/bottom ground pads never false-trip. Net-0 (no-connection) pads -
fiducials, mechanical pads - are ignored. The exit code is the number of overlapping
pairs (0 = clean), so it gates a pipeline.

A pad is measured by its outline: rect corners turned by the pad's angle, and
round, oval and roundrect corners as arcs. A CUSTOM pad's outline is only its box,
so a pair involving one that overlaps on outlines is re-measured on the real copper
(#1111): the union of the pad's parsed primitives, and the depth is the thickness of
the shared copper. A solder jumper's interleaved teeth (KiCad's StickHub demo, JP1:
0.150 mm apart, 0.150 mm overlap on the boxes) no longer reads as a short, and the
re-measure can only remove a pair the outlines found. A custom pad the parser could
not draw (a `gr_curve` primitive) stays measured on its box. The placement
graders read the same copper (`check_pads.custom_pad_copper`, #1123): a part's
occupancy and its pad copper past the outline, at the file pose and at a
trial pose, where the pad's box is re-derived for the new angle. As in KiCad, two
copies of one UNCONNECTED pin (KiCad gives each its own `unconnected-(...)` net)
and a footprint's `net_tie_pad_groups` are not shorts; two copies of one number
on real nets are. An `F&B.Cu` pad is on both outer layers.

### Examples

```bash
# Whole board, checked per footprint
python py_router/check_pads.py board.kicad_pcb

# One footprint (e.g. before fanning it out)
python py_router/check_pads.py board.kicad_pcb --component U23

# Broader board-level short check across all footprints
python py_router/check_pads.py board.kicad_pcb --cross-footprint
```

### Output

```
FAILED: 2 overlapping different-net pad pair(s):
  U23.92 (SCL) <-> U23.93 (SDA)  overlap 0.480 mm  at (142.00,148.45)
  ...
Likely a pad-rotation modelling error - fix before running fanout.
```

```
OK: no overlapping different-net pads (tolerance 0.05 mm)
```

## Net Analyzer (`list_nets.py`)

Analyzes nets in a PCB file. Can list nets on a component, detect differential pairs, identify power/ground nets, and report the board's net-class design rules.

### Usage

```bash
python py_router/list_nets.py input.kicad_pcb [OPTIONS]

Options:
  --component, -c REF   Component reference (e.g., U1)
  --pads                Show pad-to-net assignments
  --diff-pairs, -d      Detect differential pairs (a name-based P/N pair is
                        suppressed when both nets land on a single
                        crystal/oscillator footprint, since a 2-terminal
                        resonator is not a differential signal — issue #145)
  --power, -p           Show power/ground nets with pad counts, plus each net's per-layer SMD/TH pad profile `(F.Cu n SMD, B.Cu n SMD, TH n)` and the outer-layer flood hint the plane-mapping skill reads
  --design-rules, -r    Show net-class clearance/track/via/diff-pair rules
                        and the CLI flags to pass them to the routing tools
  --top N               Show top N most-connected nets (default: 10)
  --pattern GLOB        Filter nets by pattern
```

### Design rules

The routers **auto-read** the board's net classes from the sibling `.kicad_pro`
and honor KiCad's cross-class `max(classA, classB)` spacing. `--clearance` is a
**ceiling**: given, every class (Default included) is capped at `min(class,
--clearance)`; omitted, each net routes at its own net-class clearance (base = the
board's Default class), and `--hole-to-hole-clearance` / `--board-edge-clearance`
default to the board's own `min_hole_to_hole` / `min_copper_edge_clearance`
constraint (#439). They do **not** infer per-net track/via nominals on their own,
so `--design-rules` reads the real rules for you to pass explicitly:

```bash
python py_router/list_nets.py board.kicad_pcb --design-rules
```

It reads two tiers of rules and combines them with the JLCPCB fab floor:

- **Net-class values** from the sibling `.kicad_pro` (KiCad 8+,
  `net_settings.classes`) or `(net_class …)` blocks (KiCad 6/7): `clearance`,
  `track_width`, `via_diameter`, `via_drill`, `diff_pair_gap`, `diff_pair_width`.
  These are KiCad *drawing defaults*; only `clearance` is a DRC minimum.
- **Board Constraints** from `board.design_settings.rules`: `min_clearance`,
  `min_track_width`, `min_via_diameter`, `min_hole_to_hole`,
  `min_through_hole_diameter`. **These are what DRC actually enforces** for the
  geometric rules — so a board can use a via/track *smaller* than the net-class
  nominal and still pass DRC (issues #111/#115).

From these it prints a **manufacturing floor** (the Constraint or the JLC fab
minimum for the board's layer count, whichever is larger — for **hole-to-hole**
and **board-edge**, which the rest of the toolchain pins up from the board's own
constraint). **Copper clearance is the deliberate exception:** it prints the fab
minimum alone, because `min_clearance` is an unreliable edit-floor (often 0,
sometimes stale-large), nothing downstream enforces it, and grading above what
was routed manufactures phantom violations (#439). A board minimum that raised
the hole-to-hole floor above the JLC figure is named on its own line, since that
is the value to route *and* grade at (#603 — printing the bare fab 0.2 while
`check_drc` graded at the board's 0.25 sent a value into every command that the
toolchain would not honour). Where an explicit `--hole-to-hole-clearance` /
`--board-edge-clearance` is below such a minimum, `check_drc` now says it is
being clamped (on stderr, so it survives `--quiet`) instead of substituting
silently. The floor spells out
two distinct rules the router honours: **hole-to-hole** (drill-to-drill) is
net-INDEPENDENT and applies to via/via, via/pad-drill and pad-drill/pad-drill on
*all* nets including same-net; **copper clearance** applies to via/pad and
via/via copper between *different* nets, while same-net via-pad copper clearance
may be 0 (via-in-pad) where the fab allows it. The floor's **track** value is the
DRC-enforced one (the board's own `min_track_width`); separately it reports a
**fine-pitch signal track floor** — the fab's *physical* minimum trace width,
which is layer-dependent (JLC: 3.5 mil / 0.0889 mm on 4+ layer, 5 mil / 0.127 mm
on 2-layer) and can be *below* the board's `min_track_width`. On dense/congested
boards, route ordinary signals at that fab floor (`route.py --track-width`), below
the board's own rule, like the human originals — thinner completes more nets and
routes faster; keep power nets on `--power-nets` and impedance nets at net-class
width. It then emits ready-to-paste flags: the small **working**
`--via-size`/`--via-drill` from the floor (not the net-class `via_diameter`), and
`check_drc.py` is graded at the floor
(`--clearance <floor> --hole-to-hole-clearance <floor>`), not the inflated
net-class clearance. Nets assigned to a non-Default class are reported so they
can be routed separately with that class's values. When neither net classes nor
Constraints exist, it falls back to the JLC fab floor for the layer count.

### Examples

```bash
# Board summary with top connected nets
python py_router/list_nets.py board.kicad_pcb

# Detect differential pairs
python py_router/list_nets.py board.kicad_pcb --diff-pairs

# Show power/ground nets
python py_router/list_nets.py board.kicad_pcb --power

# Full board analysis
python py_router/list_nets.py board.kicad_pcb --diff-pairs --power

# List all nets on U2A
python py_router/list_nets.py board.kicad_pcb --component U2A

# Show pad-to-net assignments
python py_router/list_nets.py board.kicad_pcb --component U2A --pads

# List only DATA nets on a component
python py_router/list_nets.py board.kicad_pcb --component U2A --pattern "*DATA*"
```

### Output Examples

Board summary (no options):
```
Board: board.kicad_pcb
Total nets: 174
Total components: 25

Top 10 most-connected nets:
  GND: 42 pads
  VCC: 23 pads
  ...
```

Differential pairs (`--diff-pairs`):
```
Differential Pairs (5 found):
  LVDS_CLK_P  /  LVDS_CLK_N
  LVDS_DATA0_P  /  LVDS_DATA0_N
  ...
```

Power/ground nets (`--power`):
```
Ground Nets (2 found):
  GND: 42 pads
  AGND: 8 pads

Power Nets (3 found):
  VCC: 23 pads
  +3.3V: 15 pads
  +1.8V: 6 pads
```

Component nets (`--component U2A`):
```
Nets on U2A (42 total):
  Net-(U2A-DATA_0)
  Net-(U2A-DATA_1)
  ...
  GND
  VCC
```

With `--pads`:
```
Pads on U2A (64 pads):
  A1: Net-(U2A-DATA_0)
  A2: Net-(U2A-DATA_1)
  A3: GND
  ...
```

## BGA Fanout Generator (`bga_fanout.py`)

Generates escape routing for BGA packages with support for differential pairs.

### Usage

```bash
python py_router/bga_fanout.py input.kicad_pcb --component REFERENCE --output output.kicad_pcb [OPTIONS]

Options:
  --component, -c     Component reference (auto-detect BGA if not specified)
  --output, -o        Output PCB file (default: kicad_files/fanout_test.kicad_pcb)
  --layers, -l        Routing layers (default: F.Cu B.Cu)
  --track-width, -w   Track width in mm (default: 0.3)
  --clearance         Track clearance in mm (default: 0.25)
  --via-size          Via outer diameter in mm (default: 0.5)
  --via-drill         Via drill size in mm (default: 0.3)
  --nets, -n          Net patterns to include
  --diff-pairs, -d    Differential pair patterns (e.g., "*lvds*")
  --diff-pair-gap     Gap between P/N traces in mm (default: 0.1)
  --exit-margin       Distance past the BGA boundary the escape stubs extend to, in mm
                      (default: 0.5)
  --primary-escape, -p  Primary escape direction: horizontal or vertical (default: horizontal).
                      Pairs try this direction first, then switch if channels are full
  --force-escape-direction  Only use the primary escape direction; do not fall back to the
                      secondary direction
  --rebalance-escape  Rebalance escape directions after initial assignment: pairs near the
                      secondary edge but far from the primary edge are reassigned to the
                      secondary direction for more even distribution
  --check-for-previous  Skip pads that already have fanouts (also avoids occupied channels)
  --no-inner-top-layer  Prevent inner pads from using F.Cu
  --grid-step         Routing grid step in mm (default: 0.1). Escape stub ends
                      are snapped to this grid so the router gets on-grid
                      terminals; MATCH the --grid-step you pass to route.py
  --escape-method     auto (default), channel, underpad, or dogbone.
                      "channel" is the 45-stub + channel router; "underpad"
                      vias each ball in its pad and routes under the pad
                      field (dense arrays), working outside-in: outer rings
                      first escape as deep as possible on the pad layer, and
                      only the balls that cannot reach the edge via out;
                      "dogbone" places the classic offset via on the channel
                      diagonal beside each ball (opt-in, human-style; never
                      chosen by auto); "auto" runs channel and, if it drops
                      any ball, retries with underpad and keeps whichever
                      escapes more
  --layer-costs       One value per --layers entry, matching route.py semantics:
                      negative = forbidden (no escape copper there, e.g. a
                      soon-to-be-plane inner layer), otherwise a weight in
                      [1.0, 1000] - cheaper layers are filled first and costly
                      layers keep only overflow escapes
  --plane-drop        auto (default) or off. After the signal escape (any
                      method), every SMD ball on a plane net - a net excluded
                      from --nets with >= 6 balls on the part, or an excluded
                      net that already owns a copper zone - gets a via
                      immediately: a dog-bone via in a free inter-ball gap,
                      else an exact-checked via-in-pad tap. The plane poured
                      later picks these vias up at fill, so the plane step
                      never has to push a tap through the finished ball field
                      (#360/#424). Per-net counts land in
                      JSON_SUMMARY.plane_drop; KICAD_FANOUT_PLANE_DROP=0/1
                      overrides the flag (the recorded-manifest A/B switch)
  --fab-tier {standard,advanced,auto}  JLC fab capability floor (default: auto)
  --fab-overrides FILE  Fab-floor override file overlaying the selected --fab-tier
                      (see [Fab Tier Options](configuration.md#fab-tier-options))
```

Escape stub ends are snapped to the `--grid-step` grid, and the decorative
end-jog is validated against the full obstacle map (foreign pads + tracks +
vias): a jog that would extend into a foreign obstacle is dropped, leaving an
on-grid stub end the router can launch from (issue #149).

### Example

```bash
# Generate fanout for LVDS differential pairs on IC1
python py_router/bga_fanout.py board.kicad_pcb --component IC1 --output fanout.kicad_pcb \
    --nets "*lvds*" --diff-pairs "*lvds*" --primary-escape vertical

# Generate fanout for data nets on U3 with 4 layers
python py_router/bga_fanout.py board.kicad_pcb --component U3 --output fanout.kicad_pcb \
    --nets "*DATA*" --layers F.Cu In1.Cu In2.Cu B.Cu
```

### What It Does

Creates escape routing from BGA pads through channels between pad rows:

```
Before:        After:
  o o o          o─┐ o─┐ o─┐
  o o o    =>    o─┘ o─┘ o─┘
  o o o          o── o── o──
```

See [bga_fanout/README.md](../py_router/bga_fanout/README.md) for detailed documentation.

## QFN/QFP Fanout Generator (`qfn_fanout.py`)

Generates stub tracks for QFN/QFP package fanout.

### Usage

```bash
python py_router/qfn_fanout.py input.kicad_pcb --output output.kicad_pcb --component REFERENCE [OPTIONS]

Options:
  --component, -c     Component reference (auto-detected if not specified)
  --output, -o        Output PCB file
  --layer, -l         Routing layer (default: the layer the component is mounted on)
  --width, -w         Track width in mm
  --extension         Extension past pad edge before bend (mm)
  --clearance         Min clearance to other-net pads in mm; stubs that would
                      graze a foreign pad are shortened or dropped
  --nets, -n          Net patterns to include
  --grid-step         Routing grid step in mm (default: 0.1). Fanned stub ends
                      are snapped to this grid so the router gets on-grid
                      terminals; MATCH the --grid-step you pass to route.py
  --escape-method     stub (default) = surface 45-degree fan; underpad = drop a
                      through-via just past each pad and escape on an inner layer
                      (issue #164), for crowded fine-pitch edges. With --nets,
                      underpad also escapes interior (under-body) pads; unscoped
                      runs never via-drop the exposed/thermal pad (issue #410)
  --via-size          Underpad escape via outer diameter in mm (default: 0.45)
  --via-drill         Underpad escape via drill diameter in mm (default: 0.25)
  --board-edge-clearance  Min clearance from stub/via copper to the Edge.Cuts
                      outline in mm (default 0 = use --clearance)
  --allow-via-in-pad  Underpad escape: let the escape via overlap its OWN pad
                      (via-in-pad), so a via boxed in on the outward side can
                      stagger inward instead of being dropped. It ALSO enables
                      an inward search along the escape axis that steps by the
                      inter-net stagger -- on a fine-pitch part its later rungs
                      land past the pad edge on the chip side -- and four extra
                      stagger configurations (#846). A via that overlaps its pad
                      is clamped to the pad edge (#202) and needs IPC-4761 Type
                      VII; JSON_SUMMARY reports via_in_pad / via_in_pad_clamped /
                      via_in_pad_offcentre / max_stub_mm.
  --fab-tier          JLC fab capability floor: standard (default) or advanced
  --fab-overrides FILE  Fab-floor override file overlaying the selected --fab-tier
```

Fanned stub ends are snapped to the `--grid-step` grid, and the end-jog is
validated against the full obstacle map (foreign pads + tracks + vias): if it
would extend into a foreign obstacle the fan is shortened instead (issue #149).

### Example

```bash
# Generate fanout stubs for all signal nets on U3
python py_router/qfn_fanout.py board.kicad_pcb fanout.kicad_pcb U3
```

## Decap Clearance Repair (`place_fanout_clearance.py`)

The routing chain's step after a BGA fanout: it nudges the decoupling caps
(and resistors and ferrite beads, `--cap-prefix`) near a ball field off the
fanout vias and escape copper they now graze, and pulls a cap pad onto a
same-net ball's via where one is within `--capture-radius`. It is the one
placement step that writes copper (a via nudge), so it writes the DRC floor
back to the project like a routing step.

```bash
python3 -X utf8 py_placer/place_fanout_clearance.py fanned.kicad_pcb cleared.kicad_pcb \
    --clearance 0.1 --max-displacement 2
python3 -X utf8 py_placer/place_fanout_clearance.py fanned.kicad_pcb cleared.kicad_pcb \
    --intent floorplan.json        # hold the intent's decap limits and cap rotations
```

- **Moves.** A cap moves at most `--max-displacement` (default 2 mm), grown
  by `--displacement-growth` up to `--max-displacement-cap` (3 mm) when it
  cannot clear; one that still cannot is reported `Unresolved` for a hand
  fix. Quarter-turn rotations are tried unless `--no-rotate`;
  `--beneath-only` moves only the passives under a BGA's package and keeps
  them there.
- **Clearance.** Given, `--clearance` is a ceiling: every net class is priced
  at `min(class, value)` and the output project is clamped to it. Omitted,
  each pair is priced at its own net-class clearance and the classes are kept.
- **`--intent`.** The intent's decap limits (`decaps.max_distance_mm`,
  `decaps.max_pin_distance_mm`, at error severity) are held: no move takes a
  claim past its limit and further than the board as it stands, unless no
  clear pose keeps it -- then the cap clears the copper anyway and the broken
  claim is named. A gated run that broke a claim or left a cap grazing is
  compared with the same pass without the gate, and the better kept (fewer
  unresolved grazes, then fewer claims made worse). The intent's declared cap
  rotations (`blocks[].rotation`, `rotation_candidates`) are held too. The run
  prints the decap grade before and after. Without `--intent` a cap can move
  past a decap limit silently.

Exit 0 written (unresolved caps are reported, not failed); 2 unreadable input,
or two intent blocks declaring one part at different angles.

## Build Script (`build_router.py`)

Builds the Rust router module.

### Usage

```bash
python build_router.py [OPTIONS]

Options:
  --clean        Remove all Rust build outputs and compiled library files
  --from-source  Skip the prebuilt download and build locally with cargo
  --tag TAG      Download from a specific release tag (e.g. v0.15.0) instead of
                 the latest release
```

### What It Does

By default it downloads a prebuilt binary for the current platform from the
GitHub Release and installs it, verifying the version. With `--from-source` it
skips the download and builds locally with `cargo` instead.

In a git checkout whose `rust_router/` differs from main (a branch carrying
crate changes, or uncommitted crate edits), it skips the download and builds
from source automatically: release binaries are built from main's crate, so a
prebuilt can match the version string yet carry a different ABI (#615). An
explicit `--tag` overrides this. Release-zip installs (no git) are unaffected.

Either way it copies the resulting library to the correct location:
   - Windows: `grid_router.pyd`
   - Linux/Mac: `grid_router.so`

### Requirements

- Rust toolchain installed (`rustup`)
- PyO3 and maturin for Python bindings

## PCB Geometry Extractor (`extract_pcb_geometry.py`)

Extracts PCB geometry data into a simple JSON format for analysis or debugging.

### Usage

```bash
python py_tools/extract_pcb_geometry.py input.kicad_pcb [output.json] [OPTIONS]

Options:
  --output FILE    Output JSON file
  --nets PATTERN   Filter nets by pattern
  --summary        Print summary statistics only
```

### Example

```bash
# Extract all geometry to JSON
python py_tools/extract_pcb_geometry.py board.kicad_pcb geometry.json

# Extract specific nets
python py_tools/extract_pcb_geometry.py board.kicad_pcb --nets "*lvds*" --output lvds.json

# Print summary without writing file
python py_tools/extract_pcb_geometry.py board.kicad_pcb --summary
```

### Output Format

```json
{
  "nets": {
    "1": {"name": "GND", "segments": 45, "vias": 12},
    "2": {"name": "VCC", "segments": 23, "vias": 8},
    ...
  },
  "components": [
    {"reference": "U2A", "footprint": "BGA-256", "pads": 256},
    ...
  ],
  "layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
  "board_outline": {"min_x": 0, "min_y": 0, "max_x": 100, "max_y": 80}
}
```

## Test Scripts

### Full Fanout and Route Test (`test_fanout_and_route.py`)

Runs a complete integration test: fanout generation followed by routing and verification.

#### Usage

```bash
# Run all stages
python tests/test_fanout_and_route.py --all

# Quick mode (reduced scope)
python tests/test_fanout_and_route.py --all --quick

# Run specific stages only
python tests/test_fanout_and_route.py --fanout --ftdi
python tests/test_fanout_and_route.py --ram --planes

# Run only checks (no routing)
python tests/test_fanout_and_route.py --onlychecks --ftdi --lvds --ram
```

#### Options

| Option | Description |
|--------|-------------|
| `--all` | Run all stages: fanout, ftdi, lvds, ram, planes, and checks |
| `--quick` | Run quick test with reduced routing (fewer nets per stage) |
| `--fanout` | Run fanout tests (QFN and BGA fanout) |
| `--ftdi` | Run FTDI single-ended routing tests |
| `--lvds` | Run LVDS differential pair routing tests |
| `--ram` | Run DDR RAM routing tests |
| `--planes` | Run power/ground plane routing tests |
| `--checks` | Run DRC and connectivity checks after routing |
| `--onlychecks` | Run only checks, skip all routing stages |
| `-u, --unbuffered` | Run python commands with unbuffered output |

Note: By default, no stages run. Use `--all` to run everything, or specify individual stages.

This script runs a sequence of fanout and routing operations on the test board, including:
1. QFN fanout for U2 (ADC interface)
2. BGA fanout for multiple components (FTDI, ADC, FPGA, DDR)
3. Single-ended routing (FTDI data lanes)
4. Differential pair routing (LVDS)
5. DDR routing (DQS/CK diff pairs + DQ data lanes)
6. DRC and connectivity verification

#### Cross-Platform

This Python script replaces the previous PowerShell script and works on any platform (Linux, macOS, Windows).

## Cycle Checker (`check_cycles.py`)

Robustly detects **redundant copper loops (cycles)** per net. A routed signal net
should be a **tree**; rip-reroute / failed-edge retry and overlapping fanout +
routed copper can close loops (the "crazy" wandering traces and via stacks seen
on dense boards). This reports the **cyclomatic number** (independent loops =
`E - V + components`) per net, plus same-net **overlapping via pads**.

The connectivity graph is geometry-correct (this is what makes it robust, and
what a naive shared-endpoint graph misses):

- tolerance endpoint matching (size-aware for vias/pads),
- **T-junction** detection — an endpoint landing on another segment's *interior*
  splits that segment, but only when the cluster actually has copper on that
  layer (no false cross-layer junctions),
- via / through-hole-pad **layer joins** (a via connects all copper at its spot).

Copper pour / zone nets (planes) are meshes by design and are hidden unless
`--all`. A non-zero loop count means redundant copper (a detour loop and/or
fanout/route overlapping on the same line). Overlapping via pads (centre distance
< sum of radii) are flagged as redundant via stacks.

### Usage

```bash
python3 py_tools/check_cycles.py board.kicad_pcb [OPTIONS]

Options:
  --net NAME       Check one net by exact name (always shown, even if clean)
  --nets PATTERN   Glob of net names to check (e.g. 'RAM_*')
  --all            Also list zoned/plane nets (meshes, normally hidden)
  --verbose        Also list the overlapping via-pad coordinates / per-net detail
```

Exits non-zero if any signal net has loops or overlapping vias (usable in CI).

### Examples

```bash
# Whole board: list every signal net with loops or overlapping vias
python3 py_tools/check_cycles.py routed.kicad_pcb

# Focus one net (the wandering loop you saw in the layout)
python3 py_tools/check_cycles.py routed.kicad_pcb --net RAM_D8

# All RAM data lines, with via-overlap coordinates
python3 py_tools/check_cycles.py routed.kicad_pcb --nets 'RAM_D*' --verbose
```

### Output

```
net                           segs  vias  loops  comp  ovlVias
--------------------------------------------------------------
RAM_LDM                         28     4     13     1        1  <== LOOPS  <== 1 OVERLAPPING VIA PAIR(S)
...
signal nets with loops: 116; total independent loops: 615; nets with overlapping vias: 11
```

`loops` is the cyclomatic number (0 = tree). `comp` is the number of connected
copper components for the net (should be 1 for a fully-routed net). The
`prune_redundant_cycles` finalization pass in `route.py` removes the simple
shared-endpoint loops; loops that remain here are typically **overlapping
collinear copper** (a fanout stub and the route on top of it) or **via stacks**,
which that pass does not yet handle.

## Copper Hygiene Checker (`check_weird.py`)

Read-only scan for **weird copper** a routed board should not have: dangling
trace ends and tails, near-open **soft joints** (same-net segments that overlap
by their end caps instead of meeting endpoint-to-endpoint, and meet nowhere
else -- two stubs leaving one vertex are not one, #984), redundant copper
**loops/cycles**, **removable** segments (copper that can be deleted without
disconnecting the net), **stacked** duplicate copper, and **floating** vias
(vias touching no copper on any layer). It never modifies the board — use it as
a triage pass before or after the other checkers.

**Removable** is graded by the same predicate route.py's post-route cleanup
removes by (`pcb_modification.StrictRemovalModel`, #1063): a segment, or an
unbranched run of them, whose removal keeps every pad connected and leaves no
new dangling end, soft joint, copper island or dangling via. On a layer where a
through-hole pad's `remove_unused_layers` mode leaves it unflashed, a track
joins that pad only by reaching its drill, as in KiCad, so a tail into the pad
centre there is not removable. So a plain
`route.py` output carries no removable segment on the nets it cleaned; one that
remains is copper that run did not own (a net outside its `--nets`, or input
copper kept by `--keep-input-copper`).

### Usage

```bash
python3 py_router/check_weird.py board.kicad_pcb [OPTIONS]

Options:
  --nets, -n PATTERN [PATTERN ...]  Net name patterns to check (fnmatch wildcards,
                                    e.g. "*lvds*"). Default: all nets
  --thorough            Run the removable-segment scan on nets with >500 segments too
                        (slow — off by default so big power/plane nets don't dominate)
  --tolerance FLOAT     Minimum finding size in mm (dangle/tail length, gap,
                        duplicated-copper length, via diameter); smaller findings are
                        dropped. Default: 0.1; use 0 to report everything
  --max-print INT       Max findings printed per category (<=0 prints all; default 20)
```

### Examples

```bash
# Whole board, default 0.1mm tolerance
python3 py_router/check_weird.py routed.kicad_pcb

# Only the LVDS nets, report every finding regardless of size
python3 py_router/check_weird.py routed.kicad_pcb --nets "*lvds*" --tolerance 0

# Include the slow removable-segment scan on large nets
python3 py_router/check_weird.py routed.kicad_pcb --thorough
```

## Non-Orthonormal Segment Checker (`check_orthonormal.py`)

Flags track segments that run at an angle other than a multiple of 45° **and**
are longer than about one grid cell. An on-grid router emits only 0/45/90°
segments; the one legitimate non-orthonormal segment is the **short** terminal
connector that joins an on-grid path to an off-grid pad/ball/fanout-stub end
(≤ ~1 grid cell). Any *longer* skewed segment is a routing defect — a terminal
connector or merged terminal that spanned many cells and can cut straight
across foreign copper (issues #157/#159). This finds them directly in the
output, independent of DRC.

### Usage

```bash
python3 py_tools/check_orthonormal.py board.kicad_pcb [OPTIONS]

Options:
  --grid-step FLOAT     Router grid step in mm (default: 0.05)
  --max-len FLOAT       Flag non-orthonormal segments longer than this (mm).
                        Default: 5 × grid-step (0.25mm at 0.05 grid) — clears the
                        legitimate ≤~0.15mm bga_fanout stub-end jogs
  --angle-tol FLOAT     Degrees off a 45° multiple still considered orthonormal
                        (default: 1.0, absorbs float noise)
  --max-list INT        Max segments to list (default: 40)
  --include-stub-pads   Also flag skewed segments that terminate at a pad (by default
                        these bga/qfn fanout stubs are excluded)
  --pad-tol FLOAT       Distance in mm for an endpoint to count as on a pad (default: 0.06)
```

### Examples

```bash
# Default (0.05mm grid)
python3 py_tools/check_orthonormal.py routed.kicad_pcb

# Match a 0.1mm routing grid
python3 py_tools/check_orthonormal.py routed.kicad_pcb --grid-step 0.1
```

## Parser-Parity Validator (`validate_pcb_data.py`)

Headless twin of the GUI About-tab "Validate PCB Data" button: loads the board
with pcbnew, builds `PCBData` from the live board objects (the GUI plugin's
path), text-parses the same file with `parse_kicad_pcb` (the CLI path), and
diffs the two models with `compare_pcb_data`. Any difference means one of the
two parsers mis-models the board (pad geometry, arc tracks, zones, bounds,
...) and the CLI and GUI would route against different worlds.

```bash
python3 py_tools/validate_pcb_data.py board.kicad_pcb [more.kicad_pcb ...]
```

Needs the pcbnew Python module; when run with a plain `python3` it re-execs
itself into KiCad's bundled interpreter automatically. Exit 0 = all boards
match, 1 = differences, 2 = error. The stress-test flows run it per board
(RUNBOOK rule 1b; `redo_stress_test.py` validates the final board after each
replay, `--skip-validate` to opt out).

## DRC Settings Fixer (`fix_kicad_drc_settings.py`)

Rewrites a board's **project file** (`.kicad_pro`) so that a manual DRC in the
KiCad GUI shows only the routing-relevant errors instead of stock-default noise,
making KiCad's enforced constraints consistent with the floors the board was
actually routed to (issue #160).

KiCad stores DRC *design rules* and *violation severities* in the `.kicad_pro`,
not in the `.kicad_pcb`. A freshly written board gets a project with KiCad's
defaults, which produce noise in two ways:

1. **Constraint floors stricter than the routed board** — stock `min_clearance`
   0.2 mm, `min_via_diameter` 0.45 mm, `min_track_width` 0.2 mm,
   `min_hole_clearance` 0.25 mm — fire on every track/via/drill the router placed
   below them (hundreds of markers). They are spurious at the real manufacturing
   floor; `check_drc.py` never reports them.
2. **Non-routing categories** — courtyard overlaps, solder-mask bridges and
   footprint/library markers (`annular_width`, `lib_footprint_*`) the router
   neither creates nor fixes — often dominate the report (e.g. ~200 annular +
   ~150 library markers on the orangecrab stress board).

The script sets the relevant **Constraints** to the per-object minima the board
uses — copper `min_clearance` (+ the Default net-class **clearance**),
`min_hole_to_hole`, `min_hole_clearance`, `min_copper_edge_clearance`, and the
min track / via / drill / annular sizes.

**The board floor and the net-class clearance are two different numbers**
(#900). `rules.min_clearance` is an *absolute* floor KiCad applies underneath
everything, including a pad's own `(clearance …)` override and a `.kicad_dru`
rule — so it is capped at the smallest copper-pad clearance override on the
board (#530); leaving it above one would flag copper routed correctly at that
value. The net classes carry the clearance the board was actually **routed** to
and are capped by **neither** that override nor a `.kicad_dru` rule: a class
lowered to one part's 2 mil library override would declare every pair in that
class legal at 0.05 mm, and the next chain step reads that class back as the
board's own floor and routes the whole board at it. Both writebacks — the file
one and the live-`pcbnew` one — record the two apart.

> A second cap, at the smallest `.kicad_dru` layer-rule clearance (#498),
> applies to `rules.min_clearance` in the writeback the ROUTING steps use
> (`fix_project_for_output`, and its live-board twin). This standalone CLI has
> never applied it: run it on a board with a relaxing rule and the recorded
> floor is your `--clearance`, not the rule. Pre-existing, and named here
> because the paragraph above would otherwise imply otherwise.

The net-class `track_width`,
`via_diameter`, `via_drill` and `diff_pair_*` values are **never written**: KiCad
loads them as draw defaults (`opt`), not DRC minimums, so lowering them prevents
no violation and rewrites the designer's intent — one 0.127 mm neck used to make
the Default class 0.127 mm and every later run then routed at it (#842).
Severities are **untouched unless you pass `--relax-severities`** (#856); with
it, the courtyard / solder-mask / footprint categories go to `ignore`,
`starved_thermal` and `courtyards_overlap` to `warning`, each change is printed,
and the previous value is recorded under `kicad_routing_tools.saved_severities`
so it can be restored.
For the **size** floors (track / via / drill) it uses the **smaller** of the
routing param you pass and the smallest such object actually on the board, so a
later coarse step (say a 0.3 mm repair pass) can't raise the floor above 0.127 mm
tracks that earlier steps left — the floor always sits at or below the smallest
object physically present. **Clearance / hole-to-hole / edge** can't be read back
from the board (a spacing isn't stored per object, and the *achieved* spacing may
dip below the intended value at real violations, which we must not mask), so they
come from the routing param; across a chain they accumulate the **minimum** used
in any step via the only-loosen rule as each step threads its `.kicad_pro` to the
next. Clearance defaults to the project's Default net-class clearance when not
passed. Pass the routing parameters (`--clearance`, `--hole-to-hole`,
`--edge-clearance`, `--track-width`, `--via-size`, `--via-drill` — the same values
you gave `route.py`) to pin them exactly.

If the project has **no** Default net class (a bare/stub project), the script
seeds a *complete* one before lowering its clearance — KiCad silently ignores a
sparse net class and falls back to the stock 0.2 mm clearance, so an incomplete
class would leave the board flagging 0.2 mm clearance everywhere.

**Only loosen, never tighten.** Every constraint is set to `min(current, target)`
— lowered toward the fab floor but never raised — so the fix can never introduce
a new violation or silently strengthen a rule you set looser. `clearance` shorts
and `unconnected_items` are left untouched (KiCad shows unconnected items in
their own tab).

**KiCad-version safe.** The script edits only the `.kicad_pro` (JSON); it never
touches the `.kicad_pcb`, so a KiCad-9 board (`version 20241229`) stays
KiCad-9 — unlike a `pcbnew`/GUI round-trip, which upgrades the board to the
running KiCad's format on save. (One caveat: copper **zones** are not refilled by
`kicad-cli pcb drc`, so a stale GND pour can still show "zone clearance" markers
on the command line; KiCad's *interactive* DRC refills zones first, so those
do not appear in the GUI.)

> **Close the board in KiCad before running.** KiCad keeps the project in memory
> and overwrites an externally-edited `.kicad_pro` on save/close, reverting the
> fix. Run the script with the board closed, then reopen.

### Usage

```bash
python3 py_router/fix_kicad_drc_settings.py board.kicad_pcb [OPTIONS]

Options:
  --clearance MM        Copper clearance floor (default: project Default net-class clearance)
  --hole-clearance MM   Hole/copper clearance floor (default: the copper clearance)
  --hole-to-hole MM     Hole-to-hole clearance floor (routing --hole-to-hole-clearance)
  --edge-clearance MM   Copper-to-edge clearance floor (routing --board-edge-clearance)
  --track-width MM      Min track width (default: smallest track on the board)
  --via-size MM         Min via diameter (default: smallest via on the board)
  --via-drill MM        Min hole/drill diameter (default: smallest drill on the board)
  --relax-severities    ALSO lower the non-routing DRC severities (off by default,
                        #856): courtyard / solder-mask / footprint categories ->
                        ignore, starved_thermal and courtyards_overlap -> warning.
                        Previous values are kept in kicad_routing_tools.saved_severities
  --keep-courtyards     With --relax-severities: do not ignore the courtyard categories
  --keep-mask           With --relax-severities: do not ignore solder_mask_bridge
  --keep-footprint      With --relax-severities: do not ignore footprint/library
                        categories (annular_width, lib_footprint_issues, lib_footprint_mismatch)
  --keep-thermal        With --relax-severities: keep starved_thermal an error
  --ignore CAT [CAT...] Additional severity categories to set to "ignore" (works
                        with or without --relax-severities)
  --ignore-warnings     Set EVERY category currently at "warning" severity to
                        "ignore" (hides all warning markers; errors untouched)
  --dry-run             Print what would change without writing
```

The board must already have a sibling `.kicad_pro` (any board opened or saved in
KiCad has one). If it is missing, open the board in KiCad once to generate it,
then re-run. The script is idempotent and accepts either the `.kicad_pcb` or the
`.kicad_pro` path.

### Runs automatically after routing

You normally don't run this by hand: **`route.py`, `route_diff.py`,
`route_planes.py`, and `repair_planes.py` invoke it as their final
step** (issue #160), pinning the floors to the clearances/sizes they just routed
with, so the written project is DRC-consistent by default. If the output is a new
file with no project yet, they copy the input board's `.kicad_pro` (or seed a
complete one when the input has none). Pass `--no-fix-drc-settings` to skip it, or
`--relax-drc-severities` to ALSO lower the non-routing severities (off by default,
#856; `--keep-thermal` is accepted as a deprecated no-op). Every routing step
that writes the project prints one `PROJECT_WRITES_JSON: {...}` line listing
what it changed, so a harness can see it without grepping prose.

When routing used `--clearance-ceiling` (#530; formerly the implicit meaning of
`--clearance`, #439), the writeback also **clamps** each NON-Default net class'
**clearance** DOWN to the ceiling, so KiCad grades the copper at what was
actually routed rather than at the (usually aspirational) stock class. Without
it the classes are preserved (each net routed at its own class; `--clearance`
alone sets only the Default class). In the GUI the **Clearance ceiling** checkbox
next to Min Clearance is the switch.

The **GUI plugin** does the equivalent on the live board via the pcbnew API
(`BOARD_DESIGN_SETTINGS` + the Default net class clearance) after routing, and
marks the board modified so your next save keeps it. A single **"Fix DRC settings
after routing"** checkbox on the **Route tab** controls this for every routing
action in the dialog — single-ended routing, differential pairs, and plane
create/repair all read that one shared toggle (it is on by default); a **"Relax
non-routing DRC severities in the project"** checkbox on the **Advanced options
tab** is the GUI counterpart of `--relax-drc-severities` (off by default). Both
front-ends share the same target-computing logic (`compute_targets` /
`severity_plan` in `fix_kicad_drc_settings.py`) and differ only in how they apply
it (`.kicad_pro` file vs. pcbnew API).

### Examples

```bash
# Default: derive floors from the board's own minima + project clearance;
# severities untouched
python3 py_router/fix_kicad_drc_settings.py routed.kicad_pcb

# Also silence the courtyard, solder-mask and footprint/library
# (annular_width, lib_footprint_*) categories -- explicit opt-in
python3 py_router/fix_kicad_drc_settings.py routed.kicad_pcb --relax-severities

# Pin every floor to the routing parameters you gave route.py (recommended)
python3 py_router/fix_kicad_drc_settings.py routed.kicad_pcb \
    --clearance 0.15 --track-width 0.15 --via-size 0.4 --via-drill 0.3 \
    --hole-to-hole 0.2 --edge-clearance 0.0

# Preview without writing
python3 py_router/fix_kicad_drc_settings.py routed.kicad_pcb --dry-run

# Pin the hole-clearance floor explicitly
python3 py_router/fix_kicad_drc_settings.py routed.kicad_pcb --hole-clearance 0.1

# Also silence every warning-severity marker (track_dangling, silk_*, etc.)
python3 py_router/fix_kicad_drc_settings.py routed.kicad_pcb --ignore-warnings

# Keep courtyard checks, ignore only the mask bridges plus one extra category
python3 py_router/fix_kicad_drc_settings.py routed.kicad_pcb --relax-severities --keep-courtyards --ignore starved_thermal
```

### Output

Prints each change (`min_hole_clearance: 0.25 -> 0.0889 mm`,
`severity[solder_mask_bridge]: error -> ignore`, …) and a reminder to reopen the
board. On a typical dense BGA board this turns a ~300-violation DRC into the few
dozen genuine routing errors (clearance + shorts + real sub-floor hole clearance).

## Fab Tier Floors (`fab_tiers.py`)

A shared library — not a stand-alone command — that defines the JLCPCB
manufacturing **floor ladder** every routing/DRC CLI shrinks tracks, vias and
clearances *down toward*. It is stdlib-only so the lightweight DRC-settings
script can import it without the PCB parser, and it exposes the two flags
(`--fab-tier` / `--fab-overrides`) that `route.py`, `route_diff.py`,
`route_planes.py`, `repair_planes.py`, `bga_fanout.py`,
`qfn_fanout.py`, `check_drc.py`, `fix_kicad_drc_settings.py` and `list_nets.py`
all add through its `add_fab_tier_args()` helper (so the flag is identical
everywhere).

- **`--fab-tier standard`** (default) — the cheap, no-extra-cost floor. A
  **hard** floor since #857.
- **`--fab-tier advanced`** — JLC's tighter, "more costly" floor (0.25 via /
  0.15 drill, 0.09–0.10 mm track/clearance). A **hard** floor.
- **`--fab-tier auto`** — `standard`, **escalating to `advanced`** (one warning
  per context, counted in the run summary) when a fine-pitch fan-out or a
  last-resort via genuinely cannot fit at the standard floor. The old default,
  now opt-in.
- **`--escalation off|board|fab`** (default `board`) — how far below a
  *requested* size a failing net may be retried; see
  [Fab Tier Options](configuration.md#fab-tier-options). `--strict-sizes` turns
  any such delivery into exit code 3.

`--fab-overrides FILE` overlays the selected tier with a plain, human-editable
`key = value` file — only the floor values listed change; the rest come from the
base tier. Supplying one **disables escalation** (the floor becomes exactly
*base tier + file*), since the file states your exact fab limits. Keys are
`track_width`, `clearance`, `via_diameter`, `via_drill`, `hole_to_hole`,
`pad_hole_to_hole`, `annular`; `#` starts a comment; values are in mm and must
be > 0 (unknown/invalid lines are warned and skipped). A fully-commented
template listing every key and the built-in tier values ships as
[`fab_overrides.example.txt`](../fab_overrides.example.txt) in the repo root.

```bash
# Route to the cheap floor (default, hard)
python3 py_router/route.py in.kicad_pcb out.kicad_pcb --nets "Net*"

# Let dense fan-outs escalate to the advanced via (warned + counted)
python3 py_router/route.py in.kicad_pcb out.kicad_pcb --nets "Net*" --fab-tier auto

# Opt the whole board into the tighter, more-costly floor
python3 py_router/route.py in.kicad_pcb out.kicad_pcb --nets "Net*" --fab-tier advanced

# Declare your own fab capability (pins the floor, no escalation)
python3 py_router/route.py in.kicad_pcb out.kicad_pcb --nets "Net*" --fab-overrides my_fab.txt
```

See [Fab Tier Options](configuration.md#fab-tier-options) for the full floor
tables and how the CLIs enforce these floors (they **error** if a size/clearance
param is set below the active floor).

## Fill for Delivery (`fill_for_delivery.py`)

A routed board ships zone **outlines** with no `(filled_polygon ...)`: the
writer emits the `(fill yes ...)` properties, and nothing in the plane path
ever writes a fill. Opened in KiCad before a refill — or graded by `kicad-cli
pcb drc` **without** `--refill-zones` — such a board reports plane-net opens
that are not real. Measured on `lvds_converter_dualclk_gnd` with its fills
stripped: **54 unconnected without the flag, 42 with the fill written** — 12
phantom opens that were never a routing defect.

This is the opt-in delivery step (issue #910). It runs KiCad's own
`ZONE_FILLER` through the bundled interpreter and saves with
`aSkipSettings=True`, so the sibling `.kicad_pro` — and every non-Default net
class in it — survives; the tool re-reads the classes afterwards and
**refuses**, deleting its own output, if any went missing.

### Usage

```bash
python3 py_tools/fill_for_delivery.py routed.kicad_pcb -o delivered.kicad_pcb
```

| Flag | Meaning |
|------|---------|
| `-o, --output` | Destination board. Its siblings (`.kicad_pro`, `.kicad_prl`, `.kicad_dru`, design brief) are copied first, via `copy_board`. |
| `--timeout N` | Seconds to allow the KiCad fill (default: the exact-fill budget). |
| `--exit-zero` | Report problems but exit 0. |

`route.py --write-fill` does the same thing in place, at the very end of a
run, after the DRC-floor writeback — so the fill is graded against the
project's real net classes.

### Output

```
Filled: delivered.kicad_pcb
  filled_polygon blocks: 1
  net classes preserved: 2
  unconnected (no --refill-zones): 54 -> 42
```

The before/after line is the record that the fill **revealed** connectivity
rather than changing it.

### Requirements and exit codes

Needs KiCad's bundled python (`KICAD_PYTHON` overrides the search). Without
it the step refuses with `no_kicad_python` and writes nothing — the copied
board is simply unfilled, and `kicad-cli pcb drc --refill-zones` (or **B** in
KiCad) still grades it correctly. Exit 0 when filled with classes intact,
1 when the fill did not run or a class went missing.

## Reach Metrics (`repo_metrics.py`)

Snapshots this repository's own GitHub reach — release asset downloads, daily
views and clones, referrers and popular paths — into a snapshot archive, and
renders `docs/site/` for GitHub Pages — a landing page at the root and the
metrics page at `/metrics`. Run daily — and on every published release — by
`.github/workflows/metrics.yml`.

**Where the archive lives:** the orphan **`metrics-data`** branch, not `main`.
It has to be committed (see below) but not to a branch anyone reads history on:
one bookkeeping commit per day plus one per release buries the project's real
log, and eight had landed within two days of the workflow going live. The
branch shares no history with `main` and holds nothing but the JSON. Locally
the default is still `metrics/data/`; `--data-dir` (or `$KRT_METRICS_DATA`)
points the collector anywhere, which is how CI aims it at the branch checkout.

    git fetch origin metrics-data
    git show origin/metrics-data:traffic_daily.json

Snapshots taken before 2026-09-16 remain in `main`'s history; the branch was
seeded from the last of them.

**Why it must be committed and run on a schedule:** GitHub's traffic API is a
**rolling 14-day window** that is never backfilled. Days older than that are
discarded by GitHub and cannot be recovered by anyone, so the committed archive
on `metrics-data` is the project's only history of its own reach. Actions
artifacts expire and the Actions cache is evicted, so neither can hold it — it
must be a branch. Release
counters do not expire, but they are **cumulative**, so "how many downloads
last week" exists only as the difference between two snapshots.

Each traffic call returns 14 daily buckets, so any cadence under a fortnight
observes every day — the margin is the point: weekly left one run of slack,
daily leaves thirteen. Merging is by date keeping the **max**, which makes the
heavy overlap between daily runs idempotent and lets a part-elapsed day be
corrected by the next run instead of being frozen low. Snapshot stores are
thinned to one per ISO week after 30 days, which cannot move a lifetime total
(counters only rise, so the last snapshot of a week holds its maximum).

**Two populations, never summed.** The PCM zip is what KiCad's Plugin and
Content Manager fetches on install/update, and it accumulates on whichever
release PCM currently points at — so a newer release showing few zip downloads
means PCM has not been pointed at it, not that interest fell. The
`grid_router-*` binaries are fetched by `build_router.py` and therefore count
from-source installs **including this project's own CI**: every Modal image
build downloads the Linux binary, which makes Linux an upper bound rather than
a user count.

**PCM listing history.** GitHub records when a release was published, not when
PCM began serving it, and PCM serves only its newest *listed* version, skipping
every release in between. So the collector also reads the merge history of our
package file in `gitlab.com/kicad/addons/metadata` (public API, no token) into
`pcm_listings.json`. That file holds one entry per upstream commit: the newest
version in the file at that commit, and the date its MR merged. This is
history, so each commit is fetched once and kept.

**How the downloads chart places counts in time.** From the first snapshot on,
the chart is measured: the difference between two snapshots of the counters,
spread over the hours between them. The collector records each snapshot's time
in `release_times.json`, because runs do not land a day apart. Scheduled runs
start anywhere from about 11:00 to 13:00 UTC, and a manual or release-triggered
run replaces the day's snapshot at any hour. When one whole interval was booked
to a single day, a manual run at 19:04 made one day read 30% high. The day the
last snapshot falls in is left out until it is over. Before the first snapshot,
the archive holds only each release's lifetime total,
so the chart spreads that total evenly over the release's *reign*. The reign
runs from when the release became the newest until its successor did. For the
PCM zip, both dates come from the listing history above. The chart used to
spread each total from publish to *today* instead. Every release then added a
layer to every later day, so flat interest drew a rising line. The snapshots
show why the reign is the right window: when PCM switched from v0.20.4 to
v0.22.1, v0.20.4 dropped from ~150 installs a day to ~3.

**PCM installs count only listed releases.** PCM cannot install a version its
catalogue does not list, so a zip downloaded from any other release is a
*direct* download. It comes from the release page, or from automation. The
chart draws these as a separate dashed line, and the PCM card and tables count
them apart. On 2026-09-27, v0.19.0 was never listed and had been superseded for
two months. It took 415 zip downloads in a day, and its Linux binary climbed
alongside while its other platforms did not move. Before the split, the PCM
line counted every one of those as an install. With no listing history
collected, every zip is counted as PCM and the page says so.

```bash
# Both stages (default): snapshot, then render
python3 py_tools/repo_metrics.py

# Re-render the page from the committed archive without calling the API
python3 py_tools/repo_metrics.py --only render
```

The traffic endpoints need a token with **push access**; releases are public
and need none. A failing endpoint is recorded in the archive's `meta.json` and
disclosed on the page, because a silently absent series looks exactly like a
quiet week.

## Common Workflows

### Route and Verify

```bash
# Route the nets
python py_router/route.py input.kicad_pcb routed.kicad_pcb "Net-*" --ordering mps

# Check for DRC violations
python py_router/check_drc.py routed.kicad_pcb

# Verify connectivity (all routed nets)
python py_router/check_connected.py routed.kicad_pcb

# Or verify connectivity for a specific component
python py_router/check_connected.py routed.kicad_pcb --component U1
```

### BGA Differential Pair Flow

```bash
# 1. Generate fanout stubs
python py_router/bga_fanout.py board.kicad_pcb --component U2A --output fanout.kicad_pcb \
    --nets "*lvds*" --diff-pairs "*lvds*"

# 2. Route differential pairs
python py_router/route.py fanout.kicad_pcb routed.kicad_pcb --nets "*lvds*" \
    --diff-pairs "*lvds*" \
    --ordering inside_out

# 3. Verify results
python py_router/check_drc.py routed.kicad_pcb
python py_router/check_connected.py routed.kicad_pcb --nets "*lvds*"
```

### Debug Routing Issues

```bash
# List nets on problematic component
python py_router/list_nets.py board.kicad_pcb U2A --pads

# Route with debug lines enabled
python py_router/route.py input.kicad_pcb debug.kicad_pcb "Net-(*)" --debug-lines

# Open debug.kicad_pcb in KiCad, check User.3/4/5/6/8/9 layers
```
