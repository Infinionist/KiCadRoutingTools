# Placement

Perturbative placement optimization for KiCad PCB files. Starts from an
existing (hand- or AI-made) placement and improves it for routability —
it does not place boards from scratch *unaided* (`place_seed.py` below is
the aided path: a declared floorplan intent carries the constraints a
from-scratch run lacks). Background research and experiment results:
[docs/placement-optimization.md](../../docs/placement-optimization.md).

Four command-line tools sit on top of this module:

## place_optimize.py — greedy quench

Small nudges (capped by `--max-displacement`), 90° rotations (on each part's
own angular lattice, so a part placed at 45° rotates to 135/225/315 rather
than snapping to the axes), and same-footprint swaps (capped by
`--swap-max-displacement`, default: the same cap) that reduce airwire length
+ crossings + a whitespace (halo) penalty scaled by pin count + a soft
board-edge margin. Locked footprints never move.

```bash
# Conservative polish (recommended starting point)
python py_placer/place_optimize.py input.kicad_pcb optimized.kicad_pcb \
    --max-displacement 3 --length-weight 0.3 --crossing-penalty 30 \
    --halo-coef 0.15 --halo-weight 2 --edge-halo 2 \
    --ignore-nets GND "+3.3V" \
    --lock "J*" "P30*" "*PORT*"
```

Key options:

| Option | Default | Description |
|--------|---------|-------------|
| `--max-displacement` | 10 mm | Max distance a part may move from its seed position; applies to nudges and swaps alike (3 mm recommended; large values can destroy the placement's macro structure) |
| `--swap-max-displacement` | = max-displacement | Displacement cap for swap moves; must be ≤ `--max-displacement` |
| `--ignore-nets` | – | Net patterns excluded from airwire scoring (plane-routed power nets) |
| `--lock` | – | Reference patterns to pin in place (connectors, mounting-critical parts) |
| `--halo-coef` | 0.25 | Extra whitespace per √(pin count); keep modest (~0.15) on dense boards |
| `--intent` | – | Floorplan intent JSON. Its declared zones, keep-outs and exclusive zones become HARD per-move gates; its `must_lock` globs and `edge_connectors` edge claims are locked. MONOTONE: it prevents a part being walked out of a zone, it does not walk one back in. Omitted, the run is bit-identical to one built before the flag existed (#702) |
| `--no-rotate` / `--no-swap` | off | Disable rotation / swap moves. `--no-rotate` freezes every part's angle: nudges keep the current rotation, and same-footprint swaps are restricted to pairs that already share one, since a swap exchanges full poses and a mixed-angle pair would rotate both parts |

> **The declared design brief reaches these CLIs THROUGH `--intent`, not through a flag of their own (#711).** `<board>.design-brief.json` is read by `check_floorplan.py` (and reported by `board_brief.py`); `check_floorplan --emit-intent` COMPILES it into `edge_connectors` / `keepouts`, and the placement CLIs then consume that intent. One artifact, one flag: a second channel into the same engine is exactly the divergence this module's `cli_gates` exists to prevent. `--brief` / `--no-brief` therefore exist on `check_floorplan.py` and `board_brief.py` only. See [docs/design-brief.md](../../docs/design-brief.md). One brief key has a second reader by design: `keep_away` (#1253) compiles into the intent's `keep_away[]`, which `check_floorplan` grades on the pads (WARN by default), and the ROUTING engines read the same key off the brief to price it on the tracks. No placement search reads it (`floorplan.GRADE_ONLY_RULES`).

### How the quench prices a pose, and why it scales

A pass visits every movable part. Per visit, the quench tries each candidate
pose within `--max-displacement` at each rotation (about 120 on a 1 mm grid at
3 mm), keeps the ones `candidate_valid` admits (10-17%), and prices each by
the airwires of the nets the part touches plus its geometry terms (halo,
edge, alignment). A net's airwires are its pads' minimum spanning tree. The
price is their length (`--length-weight`), their crossings with every other
airwire on the board, and their crossings with each other (`--crossing-penalty`
each).

**The cost that did not scale was the big nets.** Take a capacitor with one
pad on GND, on glasgow_revC, whose GND has 293 pads. Priced the direct way,
every pose rebuilt GND's tree (Prim, 293² distances) and tested its 292 edges
against the board's other ~1,000 airwires: about 250,000 pair tests, 2.4 ms a
pose. A bigger board has a bigger ground net and more airwires, so the cost
of a pose grew with the board, and a pass grew with its square: glasgow's
polish took 68 s.

But a 1 mm move changes two or three of those 292 edges. These pieces of
`quench.py` price a pose by what it changes, and test it no more than they
must:

1. **The pose pricer** (`_PosePricer`, `_TreeJoin`), for a part on a net of
   `PRICER_MIN_POINTS` (24) pads or more:
   - Once per visit, it builds the tree of the net's OTHER pads (the fixed
     tree) and prices each fixed edge once: its crossings with the board's
     other airwires, which do not move during the visit, and with the other
     nets' fixed edges.
   - Per pose, it joins the part's pads to the fixed tree. A shortest tree of
     the whole net lies inside "fixed tree + edges from the moved pads" (any
     edge between two fixed pads that the fixed tree leaves out is the
     longest edge of a cycle among fixed pads, at every pose), so Kruskal
     over those edges alone finds it. A pad edge that is the strictly
     longest side of a triangle with a fixed edge is in no shortest tree and
     is dropped before the sort.
   - One moved pad (a passive's ground pin, the usual case) is joined without
     a loop over the tree, on the fixed tree's Kruskal reconstruction tree: a
     fixed edge drops out exactly when the pad reaches both of its sides by
     edges shorter than it, and a pad edge comes in exactly when no earlier
     pad edge reached the component its far end was in when Kruskal got to it.
     Both are range queries (below), O(c log m) for c candidate edges. Above
     `JOIN_ONE_MAX_CANDIDATES` (48) candidates -- a pad far outside the net,
     which sees much of its hull -- and for several moved pads, the Kruskal
     loop runs instead; the two give the same tree.
   - The price is then the fixed totals, minus the dropped edges' shares,
     plus the added edges' own counts (the only edges tested against the
     board).

   The counts are the full rebuild's own integers, and `tests/test_quench_pricer.py`
   holds the pricer to a recount of its trees and to Prim's length. One
   thing differs from the full rebuild: where pads sit on a regular grid,
   several trees are equally short, and Kruskal may pick another than Prim
   (49 of 234 glasgow poses), which can change a crossing count. Neither
   tree is more correct; graded on seven boards against the full rebuild,
   best candidates were 2 better, 1 worse and 4 identical.
2. **The crossing kernel** (`grid_router.crossing_pairs`, in
   `rust_router/src/placement_kernels.rs`). Every crossing count is a pair
   test: boxes that meet, then the orientation test, never two airwires of
   one net, never two that share an end. That loop runs in Rust and returns
   the crossing pairs (and each pair's net weight) in row-major order; numpy
   does the sums. Its float expressions, tie rules and pair order are the
   Python loop's own, so every count and weighted sum is bit-identical
   (`tests/test_quench_pricer.py` compares it with a Python reference on
   random segments, NaN and shared ends included).
3. **The tree.** Below `SPARSE_MST_MIN_POINTS` (2,500) pads a net's tree is
   dense Prim in Rust (`grid_router.mst_prim`): `compute_mst_edges`' own
   choice at every step (the first of equal keys, a key lowered only by a
   strictly shorter edge), computed row by row in O(n) memory instead of the
   n × n matrix. From 2,500 pads it is **sparse Prim** (`_mst_edges_sparse`):
   Prim over the Delaunay triangulation's edges, O(n log n), and the same
   tree, ties and coincident pads included. Every edge dense Prim can choose
   is a lightest edge leaving its tree, so it is in some minimum spanning
   tree, so no third pad is strictly nearer both its ends, so its closed
   diametral circle is empty, so it is a Delaunay edge; a heap keyed
   (distance, pad) with dense Prim's strict-less update makes the same
   choice at every step. Collinear pads, which have no triangulation, fall
   back to the dense form.
4. **Cached net arrays** (`airwires_excluding`): the board's other airwires
   are joined from per-net arrays kept until a move rebuilds that net, not
   re-read from every net's list at every visit.
5. **The swap phase** compares two identical parts with their poses exchanged
   against the poses as they are. A net whose pads on the two parts land
   exactly where the other's were (two decaps on one ground, the same pad on
   each) has the same tree both ways, so it adds the same to both sides of
   the comparison; it is priced as part of the board instead of being
   rebuilt twice (`_swap_fixed_nets`).
6. **The candidate gate's slow conjuncts** answer from a bound where the
   bound decides:
   - a rule-area keep-out (`RuleAreaKeepouts._near`) asks whether a pad can
     reach the area at all. The clearance at the centre of the pad's 1 mm
     cell (`NEAR_CELL_MM`), cached per cell, less the distance to that
     centre, is a floor on the clearance at the pad (a signed distance moves
     no faster than its point), so a pad clear of the area by that floor
     needs no walk of the area's rings;
   - a decap tether (`_tether_measure`) asks, for an IC moving under its
     caps, whether some cap on each supply pin's rail is within the limit.
     `nearest_rail_cap(..., stop_at=limit)` stops at the first that is, and
     measures every cap only when none is -- the case where the number
     itself is compared.
   Both answer exactly as the full measurement does
   (`tests/test_quench_gate_bounds.py`).

Measured, one pass of `place_optimize --max-displacement 3` from the human
placement (whole run, parse and write included):

| board | parts | ground pads | f6af54ae | now | peak memory now |
|---|---|---|---|---|---|
| glasgow_revC | 272 | 293 | 14.8 s | 3.0 s | 98 MB |
| ddr4_rdimm_tester | 691 | 833 | 255 s | 42 s | 587 MB |
| stm32h7_hdmi | 835 | 601 | 162 s | 27 s | 347 MB |
| kintex_410t_devboard | 1020 | 1136 | -- | 69 s | 719 MB |

(The two biggest files, 54 and 75 MB, spend 5-7 s per parse of that.)

What still grows with the ground net: the fixed tree is built once per visit
(O(m log m) at most), and a pose's join reads every fixed pad's distance once
(O(m) in numpy). The join itself is O(c log m) in the pad's c candidate
edges: the lowest common ancestor of two leaves of the reconstruction tree
is the split of largest index between them (nodes are made in Kruskal's
order, so an ancestor outnumbers its descendants), and a leaf's component at
length d is the run of leaves whose splits weigh at most d, both read off
sparse tables. So a pass costs about parts × ground pads × a small constant,
not parts² × ground pads.

## place_route_loop.py — router-in-the-loop repair

Routes the board with the real router, reads the failure diagnostics
(failed nets + the blocker nets named in the frontier analysis), and
micro-quenches only the small parts that could help those routes succeed,
weighting the failed nets so both their airwire length and any crossing they
take part in cost more. Re-routes and keeps the new placement only if
(failures, router effort) actually improves; otherwise reverts and widens the
search.

The tally counts the router's end-of-run reconciliation pass, so a net that
pass recovers is not treated as a failure in the next round. A rejected round
widens the **nudge** search 1.5×; the swap cap does not move. It stays at
`--swap-max-displacement` (default: the initial `--max-displacement`), so
widening the search can never turn into a long-range swap.
`--swap-max-displacement`, `--no-rotate` and `--no-swap` work here exactly as
they do in `place_optimize.py`, and `--verbose` surfaces each accepted quench
move plus the per-pass `swap-capped=N` count.

```bash
python py_placer/place_route_loop.py input.kicad_pcb repaired.kicad_pcb \
    --route-args '--nets "/*" "Net-*" --track-width 0.2 --clearance 0.2 ...' \
    --ignore-nets GND "+3.3V" --lock "J*" --swap-max-displacement 2
```

On the kit-dev-coldfire demo board this repaired the hand placement from
3 failed nets to 0 with 4.8× less router effort, moving only
resistors/caps/jumpers.

### Choosing the movers: `--target-select` (#553)

`--target-select pins` (the DEFAULT, unchanged) is the rule above: pad owners
of the failed and blocker nets, minus anything over `--max-target-pins`.

`--target-select diagnosis` ranks blocks and loose parts on three signals —
connectivity-centroid displacement, blocked cells owned, and legality defect
pairs — and takes the round-robin union of their top-k. It requires
`--group-by` (only the displacement signal needs blocks; the other two rank
loose parts, which is why the flag still does something on a board that
derives none). Each round is budgeted at the number of parts the pin filter
would have offered — so the two selectors are *asked* for the same size, but a
**block is added whole**, which makes the budget a floor rather than a cap.
Measured at budget 8, glasgow_revC selects 67 parts under `--group-by auto`,
because its one derivable block has 68 members. The overshoot is reported in
the run's own output; do not read "budgeted" as "bounded".

The budget is also how #553's own grading rule is answered. The issue asked for
a check that the diagnosis-selected set "differs from the pin-count set" — but
that is satisfied by *any* different function, including a random one, so on
its own it establishes nothing. Matching the two selectors' requested size
turns it into a comparison of *which* parts, not *how many*, and the per-round
`target_select_overlap` record (`{pins, diagnosis, overlap, fallback}`) is where
a run reports it. Equal numbers with `fallback: false` mean the diagnosis
independently agreed; equal numbers with `fallback: true` mean it was
discarded. Those are different facts and the flag distinguishes them.

```bash
python py_placer/place_route_loop.py input.kicad_pcb repaired.kicad_pcb \
    --route-args '--nets "/*" --track-width 0.2' \
    --target-select diagnosis --group-by auto,decap --diagnosis-top-k 3
```

**No measurement shows this routes better than `pins`, and the study built to
support it came back NULL.** `tests/stress/diagnosis_recall.py` displaces a
known block and asks whether the ranking concentrates on it, paired against the
same ranking on the UNDAMAGED board: evidence-arm median deltas **+0.135**
(4 cells up, 2 down) and **+0.000** (2 up, 3 down). The study, its rows and its
baseline are committed as a recorded negative rather than deleted; its
docstring explains the three ways an earlier reading of the same data looked
like a finding. The flag ships default-off, the run verdict carries the
disclaimer in `target_select_efficacy`, and
`target_select_rounds_diagnosis` beside `target_select_rounds_fallback` says
how often it steered anything at all. See `diagnosis.py`'s own docstring for
what backs each signal — which is not the same for the three — and for why
`foreign_crossings` is absent.

A run in this mode prints a group-source census before round 0, because
`--group-by auto` derives NO BLOCK on five of the six boards this repo grades
placement on.

### Moving the block: `--relocate` (#554)

The quench can translate a block rigidly (#538), but only while every member
stays within `--max-displacement` of its own seed and everybody else is frozen.
Measured with the neighbours frozen, a block travels a **median 0.00 mm toward
its connectivity target against a median want of 10.36 mm**, over the 24
measurable blocks on the 9 boards that have one — a shipped board has no vacancy to translate into. `--relocate`
(**default off**) lets the neighbours yield instead, with their relative order
as a hard constraint and their total displacement minimised, and writes the
result as its own board so the quench can refine the new pose. The round's
re-route accepts or reverts it, byte for byte.

```bash
python py_placer/place_route_loop.py in.kicad_pcb out.kicad_pcb \
    --route-args '--nets "*"' \
    --relocate --group-by auto,netprefix,decap --relocate-max-corridor 20
```

Every proposal reports the corridor (*who* yielded, and how far) and a
**binding chain** — the named parts and gaps that stopped the block, straight
off the constraint graph's own shortest path. Every refusal is a named reason,
never a count: `no_room_at_any_dose`, `block_member_locked:<ref>`,
`geometry_worsened:<ref>`, `declared_keepout_refused_a_shift:<ref>`,
`rule_area_keepout_refused_a_shift:<ref>` (a board rule-area band, #1031),
`corridor_over_budget`, and the rest are enumerated in `relocate.py`.

**The mechanism holds; the routed result does not support the feature.**
Letting neighbours yield bought ≥ 1 mm more travel than freezing them on 11 of
the 24 measurable blocks over 6 of the 9 boards that have one, max 16.66 mm
(`tests/stress/relocation_reach.py`; its frozen arm is the same solve with
everything else pinned, so the arms differ in exactly one thing — and
`reach >= frozen` is a theorem, so the evidence is the magnitude, never a win
rate).

The routed A/B (`tests/stress/block_relocation_study.py`) has been run, and it
is **UNDERPOWERED and lost to the incumbent**: the relocation recovered damage
on 3 of 3 evidence cells, median delta **+0.57** — but over only **2 boards**,
where the acceptance rule counts ≥ 3, and `place_route_loop` with the pin gate
lifted reached a **strictly better** routed result on **2 of those 3** cells
(esp_prog/swap 0.83 vs 0.33; splitflap/swap 0.71 vs 0.57; they tie at 1.0 on
esp_prog/wrong_side). So the flag stays default-off, and the run verdict carries
that whole sentence as `relocate_efficacy`. Under pre-registered rule 12 this is
not yet a withdrawal — the study has not met its own N — but it is the
opposite of support, and the next run of it decides.

Three limits worth knowing before reaching for it: it never fires on a board
that already routes (the loop stops at `failures == 0`, and `--target-nets` does
not lift that stop); `--group-by auto` derives no block on most tracked boards;
and the one board where every precondition holds, kit-dev-coldfire, is already
taken from 3 failed nets to 0 by the shipped loop (at a cap widened to 6.75 mm
by round 4). See `relocate.py`'s docstring for
why the constraint graph is *not* a conservative model of legality, and why the
exact re-check rather than the graph is what makes the pass safe.

## place_portfolio.py — K diverse candidates from one placement

The quench is deterministic by design (#457), so re-running it never produces
a different placement: every run walks into the same local minimum. When the
question is "what are my placement OPTIONS", this tool generates them: legal
seeded perturbations of the input placement (`jitter` disc offsets, `poses`
rotation variants pruned by `pair_order` inversions -- a part whose rotation
a block declares is turned only into that declaration (#1121; an
`arrays[].rotation` is not held) -- `swap`
block-interior
position exchanges), each quenched with the ordinary engine, scored **without
routing**, pruned to a diverse slate, probe-routed at the top, and presented
as per-candidate renders plus `portfolio.json`.

```bash
python py_placer/place_portfolio.py board.kicad_pcb --out-dir pf --seed 0 \
    --intent floorplan.json --ignore-nets GND VCC
```

What the contract guarantees:

- **Candidate 0 is the plain quench of the input** — "keep what I have" is a
  first-class outcome, and its numbers equal a `place_optimize.py` run with
  the same knobs (asserted by `tests/test_portfolio.py`).
- **Same `--seed` + same input ⇒ byte-identical portfolio**, across
  `PYTHONHASHSEED` values. Each candidate draws from its own
  `random.Random(f"{seed}:{i}:{strategy}")` stream, so `--only N`
  regenerates candidate N alone, byte-identically — that is the replay
  primitive the `--ledger` records (`converge.py replay` runs it).
- **Hard gates, then an ungameable rank.** A candidate is ranked only if it
  adds no courtyard overlap and no out-of-board parts beyond the baseline's,
  and (with `--intent`) grades error-free. Rank is lexicographic over
  numbers the repo already trusts: `(crossings, inversions, hpwl,
  health_penalty, displacement, index)` — no new magic weights.
- **Diversity is pose distance, not hpwl distance** (hpwl reads per-net
  extremes, so a mirrored arrangement can tie a clone). Kept candidates sit
  ≥ `--diversity-mm` apart, the baseline included; when fewer survive, the
  slate is backfilled by rank and SAYS so.
- **Probe tier (`--route-top 2` by default)**: baseline + top kept candidates
  are actually routed (one shared affected-net set, so verdicts compare like
  with like; the `_ratsnest_screen` veto skips a probe on a candidate whose
  ratsnest clearly regressed). `portfolio.json` carries TWO rankings —
  `ranking_static` and `ranking_routed` — because measured copper and a
  proxy must not interleave in one list.

A board that already carries copper is refused (exit 3): placement moves
footprints, not tracks. Run the portfolio on the placed pre-route board and
re-route the chosen candidate.

## place_seed.py — intent-driven initial placement

The aided from-scratch path. Given an UNPLACED board (netlist import, a
generator's pile) plus a floorplan intent, it emits a legal starting
placement: edge connectors on their declared edge inside their overhang band,
single-ref zones at the spec coordinate, multi-ref zones by each member's placed partners,
everything else at the nearest legal pose to where it adds the least wire --
each of its nets priced by its minimum spanning tree, so each net votes once,
at its nearest placed pad (`seeder._wiring_target`; this is also what lands a
decap next to its IC). The intent's `must_lock` and
seated `fixed_poses[]` refs are stamped `(locked yes)` into the output, a
quench polish tidies the free
parts, and the result is **graded against the same intent it was built
from** — a seed that fails its own intent exits 4, deliberately.

```bash
python py_placer/place_seed.py unplaced.kicad_pcb seed.kicad_pcb --intent floorplan.json
python py_placer/place_seed.py unplaced.kicad_pcb seed3.kicad_pcb --intent floorplan.json --seed 3
```

The stages, in the order `seeder.seed_from_intent` runs them (file-locked
parts, and parts outside `seed_refs`, count as placed before stage 0):

| stage | what it seats |
|---|---|
| 0 | `fixed_poses[]` (#1054): EXACTLY at the declared pose, a check and never a search -- courtyards may abut (KiCad's rule) but not overlap, pads and holes keep their clearance, and every declared pose is judged against every other one, so a clashing pair is refused BOTH; an illegal pose is refused with its measurement and the part held out of every later stage. Seated parts are stamped `(locked yes)` |
| 1 | edge connectors on their declared edge, inside the overhang band. The edge seat bypasses `pose_ok` (it overhangs by design), so `edge_seat_ok` carries its own conjuncts -- band, pads on the board, keep-outs, exclusive zones and, since #1044, the board's rule-area `(tracks not_allowed)` bands, ABSOLUTE: a band pose is refused by name and the connector left to the later stages rather than seated where no track can reach its pad. Its ANGLE (#1172): an undeclared connector whose input angle carries no information -- the board is a pile, the part a stacked suspect, or every pad off the outline -- is tried at each quarter turn, least depth into the board first; a STRONG mating face (#1155, `mating.py`) decides it outright, and turns a part facing away from its edge on any board. A rung must clear the courtyards stage 1 has already seated as well as their pads (`_placed_courtyard_hits`, the floorplan budget's currency); only when no rung does -- or when every rung that does leaves pad copper inside the board-edge floor and the pad-only seat does not, since copper at the edge outranks a courtyard overlap -- is the old pad-only seat kept, and the overlap named. Stage 1's own angles are walked in its order: the first that seats clean is kept, else the first that seats pad-clear (overlapping only a placed courtyard, named; a clean seat short of the board-edge floor does not beat one that clears it); when every one seats crowded, is refused after the turn, or does not fit the edge at all (wider than it, or outside its window), the part gets the attempt it had before any angle was chosen, from its input angle -- the attempt stage 1 always made, which may still crowd, as it did before #1172 -- and the note says what it did and when that angle points a strong face away. Stage 1 never keeps a pad-crowded seat at an angle it CHOSE. An angle is dropped as deeper than the board only past the band's `max` overhang. Packing an edge's claims by width when equal fractions would put them on each other, on a part already on the band, or outside a claim's zone is an opt-in, off by default (`seeder._stage1_packed_starts`): it packs around the placed parts that share a side with a claim and reach its depth, keeps each claim in its zones' span along the edge, orders zoned claims by where their zone lies, and lays them at the largest common gap that fits (#1244) |
| 1.5 | `must_lock` parts, at their current pose where it is legal |
| 2 | zoned blocks, each member at the nearest legal pose to its placed partners inside its zone (the zone centre when none is placed); a declared array whose members all sit in one zoned block is seated into it whole (stage 2.45) |
| 2.4 | declared non-zoned arrays: each row's served part, then the row (stage 2.45), each row at its members' rank. A row member is never seated alone here. The order is disclosed in the seeder's `early_order` |
| 2.45 | one declared array as ONE row (`_seat_array` -> `_seat_block`, #1051): the served part's pin order, one rotation, one pitch, a capped pose count (`ARRAY_SEAT_POSE_CAP`); a zoned row walks only anchors in its zone (plus tolerance) and sweeps the zone after the 1 mm ring, so a long zone is reached end to end. A row not seated whole goes to `array_unseated` and its members are seated one by one |
| 2.5 / 2.6 | the decap-governed caps, one per supply pin; what the pin stage declines is put back into its zone. `decap_stage` says what it claimed, and why when nothing -- on an unzoned seed, that no owner IC is seated before the stage (#1053) |
| 3 | everything else, at the nearest legal pose to its wiring target: the geometric median of each net's nearest placed pad, one vote per net (`_wiring_target`) |
| 3.5 | on by default when the INPUT board is a pile (`PlacementState.pile`), off otherwise (#1105; `--decap-claim-after-ics` / `--no-decap-claim-after-ics` force it): inside stage 3, at the first scoped cap after the queue's last owner IC, the pin claim again over the owner ICs stage 3 seated (never one 2.5 served). The pile default runs the within-limit variant, undoing a seat that lands past `decaps.max_distance_mm` as the grade measures it (`DECAP_PILE_WITHIN_LIMIT`); the explicit flag runs after-ics on any input, keeping every seat it finds (`DECAP_LATE_WITHIN_LIMIT`, off). It draws no RNG, so every part seated before it -- every IC -- is seated as without it; a cap it declines keeps its own turn. Stage 3's target jitter is drawn for every queue entry before the claim or the `after_queue` reorder, so the parts seated after it are aimed exactly as without it too. `decap_stage.late` says what it did, `late.armed_by` why it ran (`pile`, `not a pile`, or `set` by the flag) and `late.variant` which variant ran. The pile default is the maintainer's call on the `decap-pile-within-limit-*` A/B rows (tests/1105_pile_ab_prereg_v2.json): it pulls decaps in on every pile, at a cost in wire length or crossings on some, which the A/B's zero-tolerance guards alone would not accept. On placed boards the `decap-*` rows rejected every variant |
| 3c / 3b | the eviction rung (`--evict-depth`, below), then the gated anchor re-seat rounds |

`place_seed`'s `JSON_SUMMARY` carries what stages 0, 2.45 and 2.5 did, judged
at the WRITTEN poses: `fixed_seated` / `fixed_refused`, `arrays_formed` (the
grader's `array_formation` verdict) / `array_unseated`, and `decap_stage`.
Its final gate also names rule-area band pads (#1044): `keepout_copper_seeded`
is `[ref, mm]` for each part the seed MOVED whose pad copper went deeper into a
`(tracks not_allowed)` band than at its input pose, and exits 4 on it;
`oob_keepout_copper_count` is the written board's total, inherited ones
included (reported, not charged). `pad_copper_off_board_seeded` (#1241) is
`[ref, mm]` for each seated part whose pad copper the written seed leaves past
the outline (check_assembly's gating measure), and exits 4 on it;
`pad_copper_off_board_locked` is the same for a part `(locked yes)` in the
input, which the seed cannot move and still answers for: placement stamps its
own locks, so a lock would launder the last lap's overrun, and check_assembly
exempts none. An unlocked part outside a partially-unplaced run's scope is
the board's own, named and not charged -- at its input pose, or after a
polish move that took its copper no further past the outline.
`oob_pad_copper_gating_count` is the written board's total. `must_lock` alone
exempts nothing: stage 1.5 seats every unlocked, in-scope must_lock part.
`place_seed --repair` charges the same copper on any part it could move
(`pad_copper_off_board_charged`), and `--reseat` alone on a part it moved; a
pinned part's such copper fails it too (`pad_copper_off_board_pinned`: a lock
is no waiver, the one exception to #1193's pinned-part contract). No
edge seat keeps such copper in the first place (#1241): stage 1 and
`_seat_edge` move a rung whose pads would sit past the outline inward onto
the board, and name a band only copper past the outline could meet in
`edge_band_contradiction`, seated on its edge with its band error standing.
No interior seat does either (#1257): `seeder.pose_ok`, the predicate behind
every `_try_place` search (stages 1.5 to 3, the row seat, the pose census,
the post-polish re-seat and `--repair`'s seat), refuses a pose at which
check_assembly would gate the part's pad copper
(`legality.pad_copper_gating_mm`: per pad, zero margin, an edge-straddling
castellated pad exempt). It asks only a part whose pads reach past its
courtyard by more than the board-edge margin (`_pad_copper_overhang`),
since any other part's copper is on the board wherever its courtyard is.
A part no pose can hold is left
unseated, or under `--repair` unrepaired, by name. The polish asks the same
question (`QuenchState.candidate_valid`, through the same two functions):
absolute on its ordinary path, and on the off-board unfreeze branch a part
coming home may not end with its pad copper further past the outline than it
starts. A declared `fixed_poses[]` pose whose courtyard is inside but whose
pad copper is past the outline is REFUSED by stage 0 with its measurement,
as an overhanging pose with copper past the outline always was.
The seed's pad-pair and band channels read "moved" off the WRITTEN board, so
a polish or re-seat move counts, on the delta (#1241).
The gate also charges the body pairs check_assembly gates on the written
board (#1258), read off its own grader (`grade_body_overlap`):
`containment_seeded` (a drawn body inside another's, `[a, b, area_mm2,
contained_frac]`) and `courtyard_blocking_seeded` (a courtyard
interpenetration past the run-23 floors, `[a, b, area_mm2, depth_mm]`), each
exiting 4. Stage 1's crowding edge seat is where they come from: glasgow's
pile seeded J3 71% inside SW1 and used to exit 0. A pair is the seed's when
a member is a part it placed, moved or kept; `*_unseated` (against a part it
could not seat) and `*_inherited` (both members locked in the input, or out
of a partial run's scope and unmoved) are named and not charged -- a locked
pair keeps #1193's contract, unlike off-board copper. `--repair` /
`--reseat` charge the same pairs on the board they deliver
(`containment_charged`, `courtyard_blocking_charged`; a pinned pair is
`*_pinned`, reported).
`edge_angle_chosen` names every edge
connector stage 1 turned to an angle IT chose or reordered (#1172) --
`{ref: {from, to, why, candidates}}`: an undeclared one, or a declared
`rotation_candidates` set reordered with its facing members first. A turn to
an angle the author declared outright (#893, #1120) is not in it.

Every `JSON_SUMMARY` it prints (seed, `--repair`, `--reseat`) carries
`connector_requirements` (#974): the declared edge connectors' graded
evidence, the requirements that were NOT measured, declarations `--reseat`
dropped, and the connector errors split own / pinned exactly as the exit code
split them; a dry run carries only `{complete: false, reason: 'dry-run'}`. It
reports only -- it never withholds the board or moves the exit code.
`floorplan.connector_requirements` builds it, and its docstring is the
key-by-key reading.

Before anything is written, the PLAN is checked against itself and the board
(`floorplan.plan_check`, #959). An area bound no arrangement can meet within
a declared overlap budget, a part longer than its edge, two exact fixed poses
whose side-aware courtyard/pad/hole geometry conflicts, or a real reference
used as a glob that lands a part in two disjoint zones refuses at **exit 5**
with nothing written and `JSON_SUMMARY.refused: 'plan_check'`
(`floorplan.PLAN_SEED_REFUSES`). Every other plan finding is printed as
`PLAN [...]` and the seed proceeds, so the seeder can name the member it could
not seat. `--repair` and `--reseat` only report. Exit 5 is distinct from 3
(the BOARD cannot be seeded) and 4 (a seed was written and its grade failed).
`check_floorplan --intent PLAN --plan-only` runs the same check without
seeding.

Rotations: the input rotation is tried in full first and kept when it fits; a
part with no contained legal pose at it falls back to its 90° lattice (noted
in the output — measured: an LDO with 0 legal poses at rot 0 and 3 at rot 90
on a packed board). A part whose rotation is a *decision* (pin order, the U3
rot-180 case) DECLARES it: a block's `rotation` / `rotation_candidates` (#893,
honoured by the seat search -- `place_seed`'s post-polish re-seat included
since #1117, which names a part it cannot put back at its angle in
`reseat_declined` rather than turning it; stage 1's edge seat applies a
declared `rotation`, and a `rotation_candidates` set at a member that fits
the edge since #1120, walking on to the next member when that one's seat
only crowds what is placed since #1125 -- and held by the
quench, whose swaps no longer trade a declared angle away), an array's
`rotation`
(the row is seated at one angle and the quench only translates it), or a
`fixed_poses[]` entry's `rot` (seated exactly, then locked). Explore rotations
deliberately with `place_portfolio.py --strategy poses`, which with `--intent`
explores a part a block declares only within its declaration (#1121).
An undeclared EDGE connector is the exception to keeping the input angle:
stage 1 chooses its angle when the input carries none, or when its strong
mating face points away from its edge (#1172, above), and `_seat_edge`
(`--repair`) turns such a part to the facing quarter turn or refuses it by
name. Neither CHOOSES a reversed angle. What still leaves one is named and
graded: an author's declaration -- a single `rotation`, a
`rotation_candidates` set none of whose members faces the edge, or one whose
facing members, tried first, seat only worse than another member (a pad short
is worse than a facing error; each kept, with a note; `--repair` seats a
declared ladder as declared) -- and stage
1's fall-back to the input angle when no facing angle seats even pad-clear,
including the later stages' seat of a part that attempt leaves to them
(coldfire's pile, UARTCAN203). The facing grade reports every one.

### How a seat search refuses poses

`_try_place` seats a part at the FIRST legal pose in a fixed order: three
rings of offsets around its target (1 mm out to `SEARCH_RADIUS_MM`, then a
fine and a grid-step ring, about 27,000 offsets in all), then, when nothing
holds the part to a zone, a 2 mm sweep of the whole board nearest-first.
Each ring is walked at every angle of the part's ladder, and the whole search
at each rung of the clearance ladder (full, half, a 0.02 mm floor), then at
the diagonals as a fallback. A part that fits stops early. A part with no
seat anywhere walks all of it, and the eviction census (`count_legal_poses`)
walks its zone again once per neighbour it lifts: glasgow_revC's MK2, a
3.5 mm mounting hole with no room on the board, cost 18 s of a 22 s seed.

Most of those poses fail for a reason that is cheap to see for many at once,
so before the full `pose_ok` the search drops, 512 positions at a time in
numpy (`_pose_prefilter`):

- a pose whose grade rect overshoots the usable bbox. `rect_outside_amount`,
  `pose_ok`'s outline test, starts from exactly that overshoot and only adds
  to it;
- a pose that overlaps a seated part (`_overlap_rejector`). A grid of 0.25 mm
  cells marks the cells wholly inside every rect the neighbour loop tests the
  part's courtyard against on its own side: a same-side part's courtyard,
  and a far-side drilled part's hole boxes. A pose whose rect wholly covers a
  marked cell overlaps that rect by positive area, where `rect_gap` is
  negative, so the loop refuses it at any clearance. A drilled part being
  seated is read on its own side the same way, so the grid serves it too
  (it used to skip drilled parts, which is why MK2 paid in full).

Both refuse only what `pose_ok` refuses, and the survivors keep their order,
so the search seats exactly the pose it found before, and a census count up
to `CENSUS_CAP` is the same in any order. `tests/test_seed_prefilter.py`
asks `pose_ok` about 30,000 refused poses on two boards, drilled parts
included. Measured on glasgow_revC's one-pass seed: the seat searches went
from 20.9 s to 2.2 s (MK2's from 7.1 s to 0.2 s), with all 272 poses
identical.

### The eviction rung (`--evict-depth`, #630, #699)

A part with no legal pose is not necessarily a part with no **room**. Run 19
measured the difference: three sweeps returned a bare *"no legal pose anywhere
on the board"* for two switches, and when the question was finally asked in
scoped form the engine answered precisely: with D14 in place **0** poses,
with D14 lifted **46**; with D31, **0** then **32**. One eviction each and both
seated. That verdict was reachable the whole time and nothing asked for it.

So when a part cannot be seated, the seeder counts its poses with each nearby
seated incumbent lifted in turn. That census runs at every depth and is what
the JSON_SUMMARY's `no_pose_blockers` (`{ref: {blocker: poses_freed}}`)
reports, next to `unseated_refs` (names, not just a count). `--evict-depth 0`,
the default, stops there: *tell me what is in the way, move nothing*.

`--evict-depth 1` also trades: evict the incumbent that frees the most, seat
the blocked part **first** against the lifted board, then put the blocker back
(inside its own zone, searching out from its old pose) with the part in place.
That ordering is the point: `reseat_scope` re-seats its scope at their net
centroids, which is back into the pockets they block, and returned a null
three times on exactly this case. The trade is kept only if, in this order,
both seats were found, both are legal against **every** seated part (re-checked
with the seat predicate, not read off the search's return value; the two parts
are obstacles to each other), and the seated board's violation count and
overlap area did not rise. HPWL is recorded and never consulted: a part in the
pile has an artificially short HPWL, so any gate that ranks it refuses every
legal seat. A trade that fails puts both parts back and is recorded as
reverted (`evictions` counts kept trades, `evictions_reverted` the others).

**`--evict-depth 2` lifts a PAIR (#699).** A rung that only ever lifts one
neighbour records *"immovable"* for a part two neighbours jointly block, and
that verdict is true only of the basin the board happens to be in — the
reporting case censused 8 neighbours, none of which frees a pose alone, on a
board whose truth arrangement seats the part by moving two of them together.
Depth 2 asks the same question of pairs, and **only for a part no single lift
helped**: the pair sweep cannot be pruned by the single-lift counts, because
in the case it exists for every one of them is zero. The trade, the ordering
and the acceptance rule are the same code, not a second copy — the blockers
simply go back **hardest first** (descending courtyard extent), each with the
not-yet-returned ones still excluded, since a lifted part has not moved and
would otherwise veto from a pocket it is about to vacate. Nothing deeper is
defined: depth 3 is refused rather than silently meaning 2.

Bounded on every axis: **no recursion at either depth** (a blocker's own
blocker is not chased), at most 8 candidates per part (a geometric superset,
so a part outside the box frees zero poses by construction), at most
`EVICT_MAX_PAIRS = 16` of the C(8,2) pairs — ordered by `(i + j)` over the
nearest-first candidate list, so truncation drops the far-far pairs instead of
starving one candidate of partners — **one trade per part** (a single lift
that was useful but whose trade reverted does *not* fall back to a pair), and
the census counts to a cap. Every one of those is a **count**, never a clock:
a wall-clock budget would place the same board differently on a slow machine
and a fast one (#621). What a cap drops is reported, not silently omitted.
Locked parts and declared edge connectors are never candidates. The rung only
fires on a part that was going to be reported unseated, so a run that seats
everything is unaffected at any depth. It is opt-in until an A/B row on three
boards exists (`tests/test_placement_ab.py`).

**The verdict says WHY, not just who (#699).** `no_pose_blockers[ref] == {}`
used to mean two different things with opposite answers for the reader —
"nothing seated is near this part" and "everything near it is locked" — and at
depth 0 a census that freed nothing printed nothing at all. Every unseated
part the rung reaches now carries a `no_pose_verdict` and a `no_pose_census`,
in the JSON_SUMMARY and as a NOTE:

| verdict | what it means | what to do about it |
| --- | --- | --- |
| `keepout_blocks` | a **declared keep-out** is what refuses it — measured, not inferred: the poses are recounted with that keep-out lifted (#701) | move the keep-out, or add the part to its `allow` list if it owns it |
| `zone_exclusive_blocks` | a **declared exclusive zone** is what refuses it, and the part is not a member of the block that reserved it — measured the same way, by recounting with that zone lifted (#797) | add the part to the block that owns the zone, move the zone, or drop its `exclusive` flag. There is no `allow` list here — **membership is the allow list** |
| `frozen_blocks` | movable neighbours were censused and none frees a pose, but a **locked or declared-immovable neighbour** is what refuses it — measured: it frees poses when lifted, or ALONE refuses every open pose (#1213: rp2350's U6 against U8, the locked Teensy frame). With no movable neighbour at all the verdict stays `immovable_given_frozen`, whose note then carries the same counts | unlock it or relax the intent clause that froze it — or, for a file-locked frame, check how it is modelled: a pin ring is not a body |
| `no_movable_neighbour` | nothing seated is near enough to be in the way | the outline, the zone or the part's own size refuses it |
| `immovable_given_frozen` | the only neighbours in the way are locked or declared edge connectors, **named with the decision that froze each** | relax that lock, or accept the pose |
| `no_single_lift_frees` | movable neighbours censused; no single lift frees a pose | try `--evict-depth 2` |
| `no_pair_lift_frees` | ...and no pair of them frees one either | the room is not there |
| `blocker_available` | a lift *would* free a pose; the depth declined to move | raise `--evict-depth` |
| `trade_reverted` | a trade was attempted and put back | read the recorded conjunct |
| `seated_after_eviction` | not unseated after all | — |
| `no_target_recorded` | the rung never got to ask (no recorded seat context) | — |

`no_pose_census[ref]` carries the counts those verdicts came from — `boxed`,
`movable`, `censused`, `frozen`, `truncated`, `baseline`, `pairs_total`,
`pairs_censused`, `pairs_truncated`, `best_pair`, `keepouts_freeing`,
`keepouts_joint`, `zone_exclusive_freeing`, `zone_exclusive_joint`,
`frozen_lifted`, `frozen_alone`, `open_poses`, `frozen_truncated` — so a
capped sweep can never read as a complete one. `frozen_lifted` is `{frozen
neighbour: poses freed by lifting it}`, zeros included, for a part with no
pose at all, nearest first and capped at `EVICT_MAX_BLOCKERS`
(`frozen_truncated` says how many were not censused). `frozen_alone` is
`{frozen neighbour: poses legal with it as the ONLY neighbour present}`,
against `open_poses`, the poses legal with every neighbour lifted: 0 of a
non-zero `open_poses` means that part alone refuses every pose the outline and
zone allow. Both are needed (#1213): rp2350's U8 refused U6 everywhere, but by
U6's turn the other parts had filled the frame, so lifting U8 alone freed
nothing. `frozen_blocks` is derived from these two counts, and the rung still
never moves those parts. `keepouts_freeing` is `{keep-out name: poses freed by
lifting it}`, filled only for a part with no pose at all and only over the
keep-outs that bind it; it is the count `keepout_blocks` is derived from, so
the verdict cannot drift from a differently-computed claim.
`keepouts_joint` is the poses freed by lifting **every** bound keep-out at
once, and it exists because two that overlap over the part's feasible region
each free *nothing alone* — so `keepouts_freeing` is `{}` and, without this,
the verdict would fall back to `no_movable_neighbour` and blame the outline.
`zone_exclusive_freeing` and `zone_exclusive_joint` are the identical pair for
declared **exclusive zones** (#797), keyed by BLOCK name, and computed as a
sibling of the keep-out sweep rather than inside it — a stranger can be refused
by a reserved zone on a board that declares no keep-out anywhere. One gap is
disclosed rather than fixed: both joint sweeps are per-RULE, so a part refused
by a keep-out *and* an exclusive zone over the same pocket frees nothing under
either and still falls back to `no_movable_neighbour`.
`movable` and `censused` are deliberately separate:
the first is how many neighbours *could* have been censused, the second how
many were, and quoting the first as the second is the inversion the whole
disclosure exists to prevent.

**On the `--reseat` path** the same flag applies and the same keys are
carried. Depth ≥ 1 is the one exception to that pass's *"every other part is
held fixed"* contract: it may trade out a seated **non-scope** neighbour, and
those refs are named in `evicted`, in a NOTE, and in `moves`. They have to be
in `moves` — that list is the whole of what gets written, so an evicted part
left out of it is written at its **old** pose while the scope ref takes the
pocket it vacated, which is overlapping copper reported as success. `evicted`
names what reached the **written** board: a pass the gate refused wrote
nothing, so it reports none even though `evictions` records the attempt.

An evicted part is **exempt from the per-part prune sweep**, and that is what
makes the trade atomic rather than a nicety. `prune_assignment` reverts a
moved part whenever restoring its input pose strictly improves the gate tuple
— `evidenced` gates only the *equal* case — and `GATE_TERMS` ranks `hpwl`
above `overlap`, so putting an evicted blocker back into the pocket the trade
just gave away scores as an improvement. Measured: prune restored a blocker
inside the seated part's courtyard on hpwl 24.4 → 14.4, the licence then
correctly refused the board prune had damaged, and a legal pair trade that
would have taken `oob` 9.65 → 0 was thrown away whole. `exempt`'s own
rationale — *"an edge-class seat is hpwl-worse BY DESIGN; pruning it back
would undo the seat one stage later"* — is exactly this case.

The pass additionally refuses any eviction that raised the board's stack count
or overlap area (`eviction_licence_ok`): its ordinary gate compares `oob`
lexicographically first, and `oob` moves hugely in this pass's own favour, so
a new stack would sit below it unread. At depth 0 — the default, and what
`place_reconstruct`'s reseat rung uses — nothing outside the scope is touched
and the counts are 0.

**A scope edge claim is seated on its edge (#1242).** The seed never sees one
-- `reseat_scope` drops the scope's declarations from the intent it seeds
with, because stage 1 seats a claim with no legality gate -- so after the
seed each is seated the way `--repair` seats it, through `_seat_edge`: the
band, the declared angles or the strong mating face's (a reversed connector
is turned), pads on the board, aimed at its zone or from where it stands.
One that seats keeps its declaration in `intent_used`, the intent the result
is graded against, and is exempt from `prune` (an edge seat is hpwl-worse by
design); one that does not keeps the seed's answer and its band is dropped,
as before (`edge_bands_dropped`).

**An EXPLICIT scope is accepted on a different rule from the auto one (#698),
and the difference is structural rather than a preference.** On
`auto:damage_witnesses` the pass's win *is* `oob`, at index 3 of the gate tuple
— **above** `hpwl` — so the lexicographic compare already sees it, `prune`
cannot revert a genuine homecoming, and `after[oob] < before[oob]` is a complete
rule. On an explicit scope the win is a declared claim or the scope's own
wirelength, which the tuple cannot see **at all**; `after[oob] < before[oob]` is
then unsatisfiable for any part that is on the board, so an explicitly named
part could never be re-seated whatever the search found. So:

- **Safety is TERM-WISE, not lexicographic.** Every gate term must not worsen
  except `hpwl`, the one licensed term — a seat made for a declared reason is
  hpwl-worse *by construction*, for the same reason `exempt` gives above.
  Measured on #698's fixture, swept over 20 seeds: escaping the declared
  keep-out costs hpwl on **20 of 20** — 6.82 to 12.39 mm, a different value per
  seed, since the seat search is seeded — so a lexicographic `after <= before`
  refuses exactly the case the change exists for. `oob` stays hard but is no
  longer required to *improve*, and that one asymmetry is the whole bug.
- **A separate trigger.** At least one basis in `RESEAT_BASES` must strictly
  improve: the six hard gate terms, the scope's own HPWL, and the count of
  breached declared claims -- zone/keep-out AND, since #1068, the tether rules
  (`decap_distance`, `decap_pin_distance`, `proximity`), each claim counted
  once, and since #1242 the `edge_connector` errors of a scope edge claim the
  edge seat seated; `accept_basis.intent_rules` names which. All are reported in `accept_basis` whether they
  fired or not — a basis that measured nothing and a basis that measured no
  change must not look alike.
- **The intent VECTOR is the guard; the intent COUNT is only the trigger.** A
  bare count carries the trap `quench._IntentTerm` names — a part hopping from
  keep-out A into keep-out B reads `1 -> 1`, which a monotone rule admits — so
  `IntentProbe.licence` separately refuses any term that *rose*, termwise and
  never summed.
- **`prune_assignment` had to be taught the same thing**, because it reverts
  *first*. Its tuple has no intent term either, so a seat that cleared a
  keep-out reads as a pure hpwl loss and was undone before the gate ran. It now
  takes an `intent_probe` and refuses a revert that would re-break a
  declaration (a tether term enters as its excess over its limit, so a revert
  is refused only if it pushes a claim further past it) — a conjunct rather than an `exempt` entry, so the sweep still
  catches every mis-move it caught before and stays monotone, now on
  `(tuple, intent vector)` jointly. Kept moves are named in a `prune: KEPT …`
  note.
- **`--reseat-min-gain` (mm) gates the `scope_hpwl` basis only.** Count bases
  threshold at one whole defect; one number compared against both currencies
  would assert an exchange rate between half a millimetre of wire and half a
  keep-out violation. Every continuous basis is *also* floored at
  `MEASURE_QUANTUM`, the last digit `reconstruct.measure` keeps for the 4-dp
  legality terms: without it the old `gain > 1e-9` let an `overlap` improvement
  of 1e-4 mm² — a change in the last representable digit — accept a pass, and
  since `hpwl` is the licensed term such a pass may be arbitrarily worse on
  wirelength. The floor is also what makes the rule **monotone in
  `--reseat-min-gain`**: a threshold below the quantum falls through to it, so
  a stricter flag can never produce a looser gate.

  The default is **0.0**, from `tests/measure_698_min_gain.py` (16 explicit
  re-seats on four corpus boards, parts chosen by pad count so the sample cannot
  be fitted to the conclusion). **Read that script's `pre_prune` column before
  quoting it**: the gains look bimodal — 10 exactly 0.000, and 6 running 1.004
  to 19.875 mm — but 9 of the 10 zeros are seats `prune_assignment` REVERTED
  (the search relocated by tens of millimetres to an hpwl-worse pose, so `after`
  is the restored input pose), and only one is a genuine no-op. The bimodality
  is therefore partly structural: anything surviving prune has improved hpwl by
  construction. What the sample does support is narrower and still sufficient —
  every re-seat whose gain was **non-zero** gained at least ~1 mm, so a non-zero
  default would buy nothing here while risking a genuine small win.

Requires an Edge.Cuts outline (exit 3 without one — the outline is spec-owned
and will not be invented) and refuses a board that already looks placed
(use `place_portfolio.py` to explore around an existing placement, or
`--force` to discard it). A KiCad lock is a placement decision, so a board
whose UNLOCKED parts sit off the outline is a seeding target whatever its
locked parts do: place and lock the fixed parts first, then seed (#1165).
The gate and its scope are `PlacementState.seed_scope` — every unlocked
part on an unplaced or off-outline board (`off_outline`, which measures the
unlocked parts when some are locked), only the stacked suspects on a
partially-unplaced one, nothing on a placed one — and `rank_rotations.py`
and check_floorplan's seeder forecast read the same property. Different `--seed` values give genuinely different
legal seeds; the same seed reproduces byte for byte. The two compose:
`place_seed --seed N` → `place_portfolio` diversifies and ranks.

## plan_regions.py and place_groups.py — grouped placement

Placement by function (see
[docs/grouped-placement.md](../../docs/grouped-placement.md) and the
`/pcb-group-place` skill). An agent writes the groups file
(`placement/grouping.py` has the format, `py_tools/check_groups.py` checks it);
`plan_regions.py` gives each group a region. Then `place_seed --intent` fills
the regions in one pass, or `place_groups.py` seeds the groups into them one
at a time.

```bash
python py_placer/plan_regions.py fixed.kicad_pcb --groups groups.json --intent intent.json \
    --out regions_intent.json --png regions.png
python py_placer/place_seed.py fixed.kicad_pcb one_pass.kicad_pcb --intent regions_intent.json
python py_placer/place_groups.py fixed.kicad_pcb by_group.kicad_pcb \
    --groups groups.json --intent regions_intent.json --json by_group.json
```

- **`plan_regions.py`** (`placement/regions.py`):
  - free space per face on a grid: the inset outline minus the locked parts,
    with slivers under 1 mm removed;
  - slicing floorplans whose cuts split FREE area in proportion to demand,
    every one evaluated up to 6 groups and annealed beyond;
  - a cost of wiring, cluster spread, anchoring and the hints, with hard
    checks that a region holds its largest part and its parts' area;
  - a region per face a group has parts on (`_face_parts`): the main one
    on the face its parts cover more of (or the group's declared `side`,
    then one region holding every part), and its other-face parts as a
    second region, `<group>@B`, pulled to the main one (`MINOR_PULL` links
    per part) over up to `FACE_PASSES` plans, so every block's parts are on
    its block's face.

  It writes the zones as intent `blocks[]` and exempts from the decap rule the
  caps the groups file places away from the IC the rule would claim them for.
  It also writes each crystal's claims (`placement/crystals.py`): the crystal
  at its IC's oscillator pins, each load cap at the crystal. Those parts are
  left out of every zone, and the seeder's followers stage seats each one
  right after its partner, turning the IC when its oscillator pins face no
  room. It writes each indicator bank -- LEDs, each with its own series
  resistor, on one rail in one group's region -- as one `arrays[]` row that
  alternates LED and resistor (`placement/group_rows.py`; `--no-arrays`
  writes none) -- as the fewest rows side by side when one row does not fit
  the free space of its zone at the seeder's pitch (`fit_banks`,
  `regions.FitChecker`, `seeder.row_block`), and not at all when no split
  fits. On a pile with four mounting holes it gives each hole nobody
  locked the legal pose nearest a free board corner
  (`placement/mount_holes.py`), as a `fixed_poses[]` row of basis
  `derived_default`; the regions are planned around those poses, the hole is
  zoned in no block, and the tool says the hole was not locked
  (`--no-mount-holes` writes none). The board is never modified.
- **`place_groups.py`** runs `place_seed --decap-owners <the group>` once per
  group on a step board:
  - the groups not yet placed are deleted, and the placed ones are locked on
    the step board only;
  - the intent is trimmed to the parts present, less a row an earlier step
    formed, and each later group's zone is a keep-out on its face for this
    step's parts (`later_zone_keepouts`); a step that leaves one of its parts
    unseated so runs again without, and the run leaving fewer is kept;
  - a part with a fixed pose is seated in the FIRST step, whatever its group
    (and even pad-less, as an NPTH hole is), so no group takes its pose;
  - a cap is claimed only at its own group's IC, and one the pin stage
    declines is put back at that IC's supply pin, clamped into the zone.

  It copies each group's poses back, then grades the whole board:
  `floorplan.grade`, `check_assembly`, crossings and HPWL. An error on a part
  locked in the input is listed apart (`floorplan_errors_pinned`) and does not
  fail the run. `--final-polish MM` adds a bounded `place_optimize` pass, kept
  only when not worse. The output carries the input's own locks and no others.
- **`place_candidates.py`** places the board both ways over several seeds
  (default `--seeds auto`: seeds 0-2 of each flow, seed 0 alone above
  `AUTO_SEEDS_MAX_PARTS` = 300 movable parts), four candidates at a time (`--jobs`),
  grades each one as `place_groups.py` grades its own, ranks them by
  buildable, floorplan errors, crossings and HPWL, and copies the best to the
  output; every candidate stays in `<output>.candidates/` with its log.

## place_pose.py — the sanctioned pose setter (issue #892)

The engine's decision-makers all SEARCH; this is the one that takes an order.
`converge.py poses` ranks legal (x, y, rot) candidates and `grade_pad_legality`
grades a pose, but until #892 nothing applied one, so a model's own layout
decision reached the board through a hand script around `placement.writer`
(run 25's `pose_assist.py`) — the very thing the provenance regime refuses.

```bash
python py_placer/place_pose.py board.kicad_pcb out.kicad_pcb set U1 129.9 98.3 --rot 270
python py_placer/place_pose.py board.kicad_pcb out.kicad_pcb face U1 W USB1 lock U1
```

Verbs: `set` (exact, or `--near X Y` which implies `--snap`), `rotate`
(absolute, `--relative` for a delta), `face REF FACE PARTNER`, `lock`,
`unlock`, `replicate` (below). Several verbs in one call are ONE arrangement
— every op resolves against the input board and one `write_placed_output`
call writes them all.

**`replicate` (#1211) repeats one placed sheet channel onto its sibling
instances.** Instances of one hierarchical sheet carry the same symbol uuid
as the last component of every footprint's `(path ...)`, which is the
correspondence KiCad's own "Repeat Layout" uses. The engine half is
`placement/replicate.py`.

```bash
python py_placer/place_pose.py board.kicad_pcb out.kicad_pcb replicate --from CH1 --to CH2 --to CH3 --to CH4
python py_placer/place_pose.py board.kicad_pcb out.kicad_pcb replicate --from CH1 --to CH2 --anchor J1
```

It maps parts by the path below each instance, nested sub-sheets included,
and refuses (exit 4, nothing written) unless the instances are
interchangeable:

- the same parts on both sides;
- the same footprint and pad list for every pair;
- one-to-one nets over every mapped pad pair. A source net reaches one
  target net, a target net comes from one source net, and a net on pads of
  both instances (GND, a rail, a mix bus) maps to itself.

The refusal names the offending refs and pads, and spells the waiver for
each: `--allow-net-mismatch REF.PAD`, one pad per flag, leaves a pad wired
differently BY DESIGN out of the net rule -- it neither breaks the rule nor
makes a net look shared. REF on the `--from` sheet waives the pad in every
target; REF on a `--to` sheet waives it there only. glasgow_revC's two
IO_Buffer instances map 68 for 68 and differ only at their I2C address
straps; `--allow-net-mismatch U19.2 --allow-net-mismatch U20.6
--allow-net-mismatch U21.2` makes them correspond. `nets_waived` reports each
waived pad pair and whether the rule needed it; an unknown ref or pad is
refused at exit 2. Each target part takes its
source part's rotation and side (a side change goes through the writer's
#714 flip), at the source position plus one offset:

- by default, each target keeps its anchor where it is. The anchor is the
  source part with the most pads, or `--anchor REF`, a ref in any of the
  named sheets;
- `--offset DX DY` gives the offset directly (one `--to` only).

A sheet is named by its name (`CH1`, `/CH1/`: `Footprint.sheet_name`,
which both parse paths fill from the `(sheetname ...)` token or KiCad 6/7's
`Sheetname` property, so a GUI caller resolves the same sheets) or by its
path (`/<uuid>`, or a unique start of it, 4+ characters). An unknown or
ambiguous one is refused at exit 2, with the instances listed, each with its
sheet file.

The copy is graded like any other call. A target part that moves while
KiCad-locked is refused unless the call `unlock`s it, and a locked part
already at its copied pose is not moved and not refused. Each moved member
lists the new conflict pairs it is in (`ops[].new_conflicts`), and the
per-part findings it newly carries -- pad copper past the outline
(`oob_pad_copper REF`), in the edge band (`pad_edge REF.PAD`), in a keep-out
or graphic copper off the board -- so a refused copy says which members to
leave out. `--snap` does not apply: a copy is rigid. It is an
opt-in verb, and no seeder or quench default reads it.

**Declared in the intent (`replicate[]`, reader 11).** The same copy as a
design declaration: `{"from": "CH1", "to": ["CH2", "CH3", "CH4"], "anchor":
"J1"}` plus the verb's own settings (`allow_net_mismatch`, `only` / `skip`,
`search_mm`). `place_seed` applies each declaration after its polish and
re-seat (`replicate.apply_declared`), through `pose_ops.apply_poses` -- the
engine, guards and verdict of the verb -- and writes the copy only if it
grades no worse than the seed; a refused copy leaves the seed and names the
colliding members in `summary.replicate[]`. `place_pose replicate --intent
PATH` (no `--from`/`--to`) applies them by hand. check_floorplan grades each
target (`replicate`, WARN): parts matched as the verb matches them, offsets
fitted by their median, each part's residual from that fit. Measured on the
untracked multichannel_mixer: its CH1 laid out and locked as the designer
did, CH2-4 piled, `place_seed --seed 1` with the declaration copied 63 parts
(crossings 226 -> 105, hpwl 2608.9 -> 2076.3 mm, exit 4 -> 0: the OFF arm's
two blocking courtyard pairs were among the parts the copy moved) and every
target graded a copy at 0.00 mm; with CH2-4's panel parts locked at the
designer poses (seeded2) the copy grades 12 pad conflicts worse and is not
written, leaving the seed identical to the undeclared run. An undeclared
intent seeds identically to the code before this stage (esp_prog,
splitflap_driver and two mixer piles: every pose, lock and summary value).
The stage is declaration-driven and not in `tests/test_placement_ab.py`: the
only tracked board with repeated sheet instances is glasgow_revC, whose
IO_Buffer pair differs at its address straps and in layout.

**A copy that does not fit.** Two answers, both opt-in:

- a PARTIAL copy, `--only REF[,REF]` or `--skip REF[,REF]`. A `--from` ref
  names that symbol in every target, a `--to` ref names that one target
  part. The parts left out stay where they are, need no counterpart, may
  differ in footprint and cannot break the net rule; a net on any pad of
  either whole instance still counts as shared, so leaving a part out never
  turns a rail into a channel net. `to[].left_in_place` lists them. The
  default anchor is then the largest part copied into every target.
- an offset SEARCH, `--search MM` (`--search-step`, default 0.5 mm). Only
  when the copy as asked is refused: each target's offset is tried on a
  lattice within MM of its default, nearest first (ties by dy, then dx), and
  the first one the call's own verdict accepts is kept -- on a written,
  re-parsed, re-graded board, never a model. Targets are fitted in `--to`
  order; while one is fitted, the ones before it sit where they were
  fitted and the ones after it are left out of the grade on both sides
  (deleted from the parsed input and candidate alike), since their input
  poses are about to be vacated. The last target is graded with nothing
  left out, so the arrangement the search returns is the one the verdict
  passes. An offset that would move a locked part (the anchor, off its
  default) is skipped and counted. None found: exit 4, with
  `to[].search` -- candidates, tried, skipped, and the categories that
  refused them. On the 114-part multichannel_mixer each candidate costs
  about 0.27 s.

The engine half is `placement/pose_ops.py`, so the writer, the sibling carry
(#441), the grade and the snap are shared rather than living in a CLI `main()`
the GUI cannot reach. `seeder.stamp_unlocked` is the inverse of `stamp_locked`,
added here because the repo had a stamper and no un-stamper.

Legality is RELATIVE: `grade_pad_legality` on the candidate against the same
grade on the input, refusing (exit 4, nothing written) only a request that
makes a category worse. A pad stack (two parts' pad copper overlapping, any
net -- check_assembly's `pad_intersection`) is a category since #1064,
measured by `legality.pad_intersection_pairs`, the function check_assembly's
channel now is; so, since #1266, is a pad on another part's footprint
graphic copper (`graphic_pad_*`, check_drc's `footprint_graphic_pad_census`),
which the search's gate charges in `pair_shortfall` too, licensed by an
on-board seed only. An absolute gate would refuse poses no worse than
where the part already sits — and would refuse to arrange the unplaced pile
this tool exists for. `--strict-legal` is the absolute arm, `--force` the
waiver, and a KiCad `(locked yes)` is refused unless the same call `unlock`s
it. See `docs/utilities.md` for the full contract.

The SEARCH's own pad tests (`legality.pads_ok`) are measured on pad BOXES,
conservative by design: a box can falsely refuse a pose, never falsely admit
one. `legality.STACK_EXACT_CONFIRM` (#1127) is an OPT-IN switch, **off by
default**, that confirms the per-pad sweep's box hits on the pads' own
outlines before the gate refuses a pose for them. It covers both conjuncts
the boxes decide:

- the STACK (`PairShortfall.stack`): a box overlap counts only when
  `_exact_pad_stack`, the check check_assembly's pad_intersection channel
  makes, finds the outlines intersect;
- the PAD conjunct (`pad`, `pad_overlap`): a box clearance shortfall is
  re-measured by check_drc's pad-pad check (`check_pad_pad_overlap`, margin
  0) at the pair's requirement and charged check_drc's own millimetres, and
  a different-net overlap is a short only when the outlines intersect.

With it on, `pads_ok` refuses what check_drc and check_assembly grade rather
than what the boxes suggest (`tests/test_1127_stack_exact_confirm.py`). The
over-cap extent branch and the NPTH hole channel stay box answers, a part
that cannot be posed keeps the box's answer, the mode is read once when a
context is built, and each exact answer is memoised per posed pad pair. An
exact gate can leave a pair AT its clearance, and check_drc at margin 0 may
then read it a fraction of a nanometre short: the gate admits within its
`EPS`, and the board file rounds every pose to whole nanometres. KiCad's own
DRC does not flag such a pair -- it stores integer nanometres and tolerates
its DRC epsilon (0.0005 mm) -- so the gate's `EPS` stays as it is.

It is opt-in because the corpus A/B did not adopt it: the decisions it
changes move the placement A/B's signals in both directions across boards,
which is not the directional, at-least-three-board result CLAUDE.md asks of
a default. The measurement tools stay with it. `tests/1127_stack_ab_prereg.json`
pre-registered the look, which also asked whether box mode's seed baseline
LICENSES a real stack (a box-only near-touch at the seed recorded as a
stack); no committed input carries such a licence, and a pile has none:
a seed licenses nothing unless it is ON the board and out of a
`_degenerate_refs` bucket (`_seed_licensed`, for the pad pairs, the
graphic copper and the keep-out bands alike). A pile used to license what
its staging rows overlapped -- run-19 caught only parts stacked at one
point -- and on ulx3s's pile R30 and U10, staged with their pads stacked,
were seeded stacked again (same net, 0.25 mm short), which check_assembly
blocks.
`tests/measure_1127_licence_census.py` counts, on an OFF run inside the
engine call, every `pads_ok` decision the switch (both conjuncts) or a
licence fix would take differently; it does not observe
`relocate.exact_refusal`, `place_pose`, `reseat`, `portfolio` or `perturb`.

## place_fanout_clearance.py — decoupling-cap clearance repair (issue #130)

Run **after** `bga_fanout.py`. Nudges decoupling caps near a BGA so their
pads clear every foreign-net fanout via, every foreign track on the cap's
own copper side, and every foreign component pad (#130/#278/#275 — a graze
already present at the seed placement is a violation to fix, not a baseline
to preserve), and pulls each pad toward the nearest **same-net** ball — so
a power/GND via dropped at that ball later also lands on the cap pad (one
shared via connects ball + cap + plane). Caps move as little as possible
(90° rotations allowed), never overlap each other or a locked part, and a cap
that can't clear within the (auto-grown) displacement budget is reported
unresolved for a manual nudge.

The summary line reads `Moved N cap(s); resolved R/V initial violations;
K unresolved`. Since #746 both counts are graded from the **same** board state,
at the end of the pass: `resolved` means "was grazing at the seed and is clean
now" and therefore credits the #313 via-nudge as well as the cap move, with
`(F freed by via-nudge)` naming that share. `unresolved` means "grazing now",
which is not a subset of the seed violators — copper the pass itself drew can
break a cap that started clean, and when it does, a `Re-grazed by this pass's
own connector copper:` line names those caps. Before #746 `resolved` was
computed before the nudge and never refreshed, so a cap the nudge freed reached
neither list, and a cap the sweep had cleaned before the pass re-grazed it
reached both.

It reads each via's actual size from the board, so the only setting that
matters is `--clearance`, which must match the fanout / DRC floor:

```bash
# after: bga_fanout.py board.kicad_pcb -o fanned.kicad_pcb --clearance 0.1 ...
python py_placer/place_fanout_clearance.py fanned.kicad_pcb capclean.kicad_pcb --clearance 0.1
```

| Option | Default | Description |
|--------|---------|-------------|
| `--clearance` | the board's own Default net-class clearance, else 0.25 mm | Copper clearance **CEILING**, and the PRESENCE of the flag is the clamp switch -- the same two branches CLAUDE.md documents for `route.py` (#768). GIVEN: every class is priced at `min(class, this)` and the output `.kicad_pro` is clamped down to it. OMITTED: each pair is priced at its own net-class clearance and the classes are PRESERVED. Before #768 this step ran one branch for pricing and the other for the writeback in the same invocation, so `--clearance 0.1` on a board declaring 0.2 moved caps to satisfy 0.2 and then shipped a project saying 0.1 (measured on `glasgow_revC`, 19 pairs, and `ottercast_audio`, 79). Only the NETCLASS tier is capped: a `.kicad_dru` layer rule and a pad `local_clearance` still outrank the ceiling, because the writeback clamps neither and KiCad goes on enforcing both. `--clearance` is a copper-clearance ceiling only -- it no longer redefines `min_hole_clearance`, which the writeback now passes from the board's own declaration. A value below the layer-bucketed fab clearance floor (0.10 mm at <=2 copper layers, 0.09 mm at >=3) is DISCLOSED and honoured, not raised, because `check_drc` does not raise it either. |
| `--cap-prefix` | `C,R,FB` | Comma-separated reference prefix(es) for movable passives near a BGA (caps, resistors **and ferrite beads** by default, #252). Only 2-copper-pad parts move, so RN-style arrays are auto-excluded; paste-only apertures are ignored when counting pads. |
| `--capture-radius` | 2 mm | Max distance over which a same-net ball attracts a pad |
| `--max-displacement` / `--max-displacement-cap` | 2 / 3 mm | Initial and grown move budget per cap |
| `--default-via-size` | 0.3 mm | Fallback only, for vias with no readable size. Honoured by the grader **and** the via-nudge since #732; before that the nudge priced such a via at a hard-coded 0.5 and the two disagreed. |
| `--board-edge-clearance` | the board's own `min_copper_edge_clearance` when it asks for MORE, else 0.55 mm | Copper-to-Edge.Cuts margin for a moved cap **and** for a via the #313 nudge relocates -- one number since #733, where the nudger gated its own emitted copper at the bare `--clearance` and parked it 0.30 mm inside the band the cap mover reserves. Resolved by the shared engine, so the GUI plugin gets the same answer; TIGHTEN-only on an omitted flag, because `fix_project_for_output` pins this field up to the 0.20 fab floor on every board the chain writes. A given value is honoured as typed. |
| `--lock` | – | Extra reference patterns to pin in place |
| `--intent` | – | Floorplan intent JSON (#1067). Its decap limits (`decaps.max_distance_mm`, `decaps.max_pin_distance_mm`, each at error severity) are HELD no worse per claim, through the quench's own tether gate (`quench.TetherGateView`): a move may not take a claim past its limit and further than the board as it stands. A LADDER, not a wall: a cap whose every clear pose breaks a claim clears the foreign copper anyway (a short is worse than a far decap) and the claim is named (`Decap limit broken to clear foreign copper`, `decap.broken`). A hold made early can cost a claim later, or box another cap in, so a gated run that broke a claim or left a cap grazing is compared with the same pass without the gate, on a pristine copy, and the result with fewer unresolved grazes, then fewer decap claims made worse, is kept (`decap.compared`; measured on run 34's real board, which is not committed: the ungated one is kept, 1 claim worse against 2). Only the kept run's lines are printed. The decap grade before and after and a `JSON_SUMMARY` are printed after the summary line. Its declared rotations (`blocks[].rotation`, `rotation_candidates`) are held too (#1122): a cap declared at one angle is never turned away from it, and one with candidates turns only within them, on its own quarter-turn lattice -- in BOTH passes, because the one without the decap gate is often the one kept (`Declared rotations (intent)` names the caps, `declared_rotations` in the summary). Two blocks declaring one part (any part, as every intent gate refuses) at different angles exit 2 before anything is written or recorded, and a rotation block that resolves to no part is printed as an `INTENT WARN`. A hold has a price: a held cap can leave a graze the free pass would clear (it stays on the `Unresolved` line and in the `unresolved` key), and declaring one cap can change which other cap the run leaves grazing. Nothing else in the intent is read. An unreadable file exits 2 before anything is written or recorded. Omitted, the run is unchanged. The GUI fanout tab's `cap_intent_path` is the same parameter. |
| *(no flag)* track-scoped `.kicad_dru` rules | the board's own custom rules | What the #313 via-nudge charges between the connector copper it draws and a foreign **track**. KiCad stores a track-to-track requirement as a custom rule scoped to a net class (`A.Type=='track' && B.Type=='track' && A.NetClass=='X'`); netclasses cannot express it, and before #735 this pass could not read it, so on a declaring board it drew connectors closer to foreign copper than `check_drc` accepts -- an under-block, which ships the violation rather than refusing the landing. **RAISE-only** over the pair's already-resolved value, and **tracks only**: the cap-pad, board-pad and via arms are exempt by KiCad's own condition, not by omission. The board's value is a **PREFERENCE, not a gate**, the same shape as the drill floors above: the sweep runs every drill rung honouring the rule first and only falls back to the base requirement if nothing clears, saying so on stdout -- because a hard gate would abandon the via and leave the pad-via graze this pass exists to remove, which `check_drc` counts too. Resolved by the shared engine through the same `kicad_dru.track_pair_clearance` `check_drc` grades with, so the GUI plugin gets the same answer; an unsaved GUI board has no path to read a `.kicad_dru` from and keeps the netclass value. No board in this repo ships a `.kicad_dru`, so the channel is inert on the whole tracked corpus. |
| *(no flag)* drill-to-drill floors | the board's own `min_hole_to_hole` when it asks for MORE, else the fab tier's 0.20 (via-hole to via-hole) and 0.45 (via-hole to pad-hole) | What the #313 via-nudge charges when it relocates a barrel. Board-first and RAISE-only since #756; before that both were flat literals, so on a board declaring above 0.20 the pass parked a via at 0.20 while `check_drc` graded the same drill pair at the declared value and flagged it. Resolved by the shared engine off the board's sibling `.kicad_pro`, so the GUI plugin gets the same answer; an unsaved GUI board has no project to read and keeps the fab floors. The board's value is a PREFERENCE, not a gate: the nudge sweeps for a landing that clears it and falls back to the fab floor rather than abandoning the repair, so it can never place a via worse than it would have before. `--fab-tier` cannot move these floors (both tiers declare 0.20/0.45); a `--fab-overrides` file can, but note `place_fanout_clearance.py` accepts neither flag — the fab tier reaches this pass only as the process-wide value some other step set, which is how the GUI's Fanout tab supplies it. The pad-hole floor stays stricter than `check_drc`'s pad-drill arm (which grades at the single hole-to-hole value) by `max(d, 0.45) - max(0.20, d)` — 0.25 mm on a board declaring nothing, decaying to 0 at 0.45. Deliberate: 0.45 is the JLC fab minimum and nothing else in the repo enforces it. |

On ulx3s U1 (22×22, 0.8 mm) this took the fanned board from 4 PAD-VIA to
fully DRC-clean, tidying 19 caps toward same-net balls (all ≤1.9 mm). In the
GUI, the **"Optimize decoupling cap placement"** checkbox on the BGA fanout
tab runs the same engine automatically right after fanout (off by default).
The advanced knobs above (capture radius, near margin, search step, max
displacement / cap / growth, max passes, cap-ref prefix, allow-rotation) are
exposed in that tab's **"Cap Placement (advanced)"** box; `--clearance`,
`--grid-step`, and the via size come from the Basic tab.

## Testing

The placement tests are standalone scripts (no pytest needed), all in
`tests/run_all.py`'s `--fast` lane:

```bash
python3 tests/test_quench_swap_cap.py        # swap displacement cap (#430)
python3 tests/test_quench_neighbor_lists.py  # pruned-scan bit-exactness (#430)
python3 tests/test_458_loop_steering.py      # loop caps, tally, summary merge
python3 tests/test_458_quench_net_weights.py # weighted crossings
python3 tests/test_458_quench_rotations.py   # rotation lattice, --no-rotate
python3 tests/test_fanout_clearance.py       # cap clearance repair (#130)
python3 tests/test_456_courtyard_parser.py   # courtyard shapes + silk bleed (#456)
python3 tests/test_456_side_and_outline.py   # board side, real outline, graders (#456)
python3 tests/test_459_groups.py             # block sources + parsing (#459)
python3 tests/test_459_group_moves.py        # rigid block translation (#459)
python3 tests/test_554_order_graph.py        # the relocation constraint graph (#554)
python3 tests/test_554_relocate_solve.py     # min-perturbation + the exact re-check (#554)
python3 tests/test_554_loop_relocate.py      # --relocate wiring, mocked router (#554)
python3 tests/test_554_reach_regen.py        # the reach measurement, re-derived in full
python3 tests/mutate_554.py                  # does the #554 suite bite? (rewrites relocate.py)
python3 tests/test_portfolio_strategies.py   # perturbation strategy invariants
python3 tests/test_portfolio_determinism.py  # portfolio seed/replay contract (slow)
python3 tests/test_portfolio.py              # portfolio smoke + identity anchor (slow)
python3 tests/test_place_seed.py             # intent-driven seeding (slow)
```

Quench output is reproducible across processes — the same board and arguments
give the same placement, with no `PYTHONHASHSEED` pinning (#457). It used to
depend on the hash seed: `net_refs` was a set of reference strings, its iteration
order became the MST's point order, and Prim's tie-break is first-index-wins, so
equidistant pads (uniform-pitch GND arrays, decaps on a grid, symmetric
connectors) built a different tree per process. `interf_u_unrouted` scored 447 /
457 / 450 crossings under three seeds before a single move was made. `net_refs`
now holds sorted lists, and `tests/test_457_determinism.py` pins it.

When comparing two placements, `hpwl` is the metric to reach for first: it reads
only each net's pad-position extremes, so unlike the MST length and the crossing
count it is invariant to airwire order. If HPWL agrees and crossings do not, the
two runs differ in tie-breaks rather than in placement quality.

## Ratsnest metrics, and the pre-route screen (#504)

The quench cost function computes airwire length and crossings on every pass, and
`hpwl()` is pure pad geometry. Those numbers used to be printed and discarded.
They are now exported:

- `quench(..., metrics_out=d)` fills `d` with `{'before', 'after', 'legality'}` —
  an out-param rather than a changed return, since the return is consumed
  positionally by both CLIs and four test files.
- `place_optimize.py` emits them as a `JSON_SUMMARY:` line, so a chain or grader
  can gate on what a run achieved instead of scraping stdout.
- `place_route_loop.py` records `ratsnest_crossings` / `ratsnest_hpwl` /
  `ratsnest_length` in each round's metrics dict, report-only beside the
  `pad_pairs_*` keys. `better()` is deliberately untouched — reworking the
  comparator is #458.

**Which numbers compare.** `crossings` (a raw count by contract) and `hpwl` are
unweighted, so they are comparable across runs. `length` and `total` are scaled
by `net_weights`, so they only compare between the `before` and `after` of the
*same* call — which is why the screen thresholds on the first two.

**`--ratsnest-screen N`** (percent, `0` = disabled, the default) skips the routing
run when a candidate's crossings or HPWL regress by more than N% against the board
it came from. Routing is the honest judge but an expensive one, often minutes per
round; a candidate whose ratsnest clearly got worse is very unlikely to route
better. The baseline is free — quench is handed the current best board, so its own
`before` *is* that board's ratsnest. Every decision is logged with its numbers, so
it is auditable whether the screen ever skipped a placement that would have won.

## Alignment and orientation (`--align-weight`, `--orient-weight`, #548)

Two cost terms for the things a human does constantly and the quench does not:
put parts that belong together on a shared axis, and turn a part toward the net
it exists to serve. **Both default to 0 (off).**

```bash
python3 py_placer/place_optimize.py board.kicad_pcb out.kicad_pcb --max-displacement 3 \
    --align-weight 5 --orient-weight 1
```

### The premise in #548 is wrong in a way worth recording

The issue proposes to *"score airwires from the actual pad the net lands on"*.
The cost path **already does that** — `_net_points` emits one MST node per
connected pad from `pad_globals()`, full rotation applied, and there is no
centroid anywhere in the objective.

The gap is **numeric**, not geometric. Measured on the test fixture: the four
rotations of a part whose one connected pad faces away from its anchor differ by
**0.500 mm across a 19.8 mm net**. The directional signal is present and drowned.
So what was missing is a term that is directional at *part* scale with its own
weight, which is what `--orient-weight` is.

### Alignment

A pairwise penalty between **peers** — same `footprint_name`, the pairing the
swap phase already indexes — on the nearer shared axis:
`w * min(d, radius)²`.

- **Continuous** at the radius. The obvious charge-inside/zero-outside shape has
  a cliff there that pays a part to *flee* the row rather than join it.
- **Saturating** beyond it, so a distant peer contributes a constant that
  cancels between one part's candidate poses instead of dragging it across the
  board.
- **Zero** on a shared axis, so a tidy row is free.

Four caps seeded at y ∈ {99.8, 100.0, 100.2, 100.3} come out at four distinct y
today, and on one shared y with the term on.

The peer index is built in `__init__` and deliberately does **not** ride on the
pruned neighbour lists: that prune is a 2-D *box* overlap test, so a pair must be
near in both axes — but alignment is inherently long-range *along* the shared
axis (two caps 50 mm apart in x and 0.1 mm apart in y **are** aligned). It is a
*lossy* prune, unlike `_neighbors`' exact one: peers are fixed from seed
positions, so a pair that drifts within `--align-span` later is not picked up.

### Orientation

Sums `|r| − r·û` per connected pad, where `û` points from the pose origin toward
the centroid of that net's pads owned by *other* parts. Zero facing the anchor,
`2|r|` facing away — **bounded at part scale on purpose**: it breaks a rotation
tie, it does not outrank a real length win.

Two things it is not, worth knowing: it is not purely rotational (moving the part
changes `û` too, so a large weight also pulls the part toward its nets), and
through `part_geometry_cost` it reaches the group phase, so a rigid block
translate is steered by it as well.

### Measured, so the recommendation is not a guess

Sweep at `--max-displacement 3`, 3 passes. *tidy* is the mean share of distinct
axis positions within each same-footprint group — **lower is tidier**.

| board | align | orient | crossings | hpwl | tidy | overlap / oob |
|---|---|---|---|---|---|---|
| splitflap_driver | 0 | 0 | 194 | 2407.1 | 0.652 | 0.0 / 6 |
| | 5 | 0 | **187** | 2386.2 | 0.581 | 0.0 / 6 |
| | 15 | 0 | 187 | 2404.7 | **0.560** | 0.0 / 6 |
| | 0 | 3 | 185 | 2415.5 | 0.712 | 0.0 / 6 |
| | **5** | **1** | **185** | 2391.2 | 0.605 | 0.0 / 6 |
| interf_u_unrouted | 0 | 0 | 404 | 4175.0 | 1.000 | 0.0 / 2 |
| | 5 | 0 | 404 | 4174.5 | 1.000 | 0.0 / 2 |
| | **5** | **1** | **402** | **4162.3** | **0.889** | 0.0 / 2 |

**`--align-weight 5 --orient-weight 1`** is the recommended pair: best or
joint-best crossings on both boards, tidier, and **no legality regression
anywhere** — overlap stayed 0.0 and `oob_count` never moved.

Two honest caveats. **n = 2 boards, and no re-route** — crossings and HPWL are
proxies, and the router is the only judge that counts, so do not read the
crossing improvements as a routability claim. And **over-weighting trades
wirelength for tidiness**: `--align-weight 15` is the tidiest row in the table
and its HPWL is *worse* than at 5. `interf_u_unrouted` shows the other limit —
alignment does nothing at all on a board with few repeated footprints, because
there are no peers to align.

### `5` is not a transferable number

The two sweeps above make `--align-weight 5` look like a default. It is not. On a
third board — 42 footprints, 16 identical 0402 decouplers in two exactly-aligned
rows, so peers were not the limit — `5` destroyed both rows just as thoroughly as
switching the term off, at **either** crossing penalty. The mechanism was fine:
at `50` both rows returned to a `0.0000` y spread. It was the *weight* that did
not transfer.

The reason is in the shape of the term. The penalty saturates at `w · radius²`, so
the recommended pair is worth `5 × 0.5² = 1.25` per peer pair — against a halo term
of order 100s and tens per crossing. It is simply outvoted.

**Sweep `--align-weight` on the board in front of you and check a row's y spread;
do not carry `5` over.** And note what a weight large enough to win can cost: on
that board `50` bought aligned rows at +3 crossings and +3.2 mm HPWL, which the
Step 0c acceptance rule scores as a regression.

### Why off by default

The router, not a tidiness score, is the judge of a placement, and neither term
has been measured against routing *outcomes* — only against proxies, above.
On-by-default would silently change every user's board to buy legibility.

It would also corrupt the isolated fixtures in `test_458_*`, which zero every
geometry knob they know about so the objective is clean enough to assert
`total == 30.0`. A term with a nonzero default is invisible to those and breaks
the isolation — that is degrading a correctness test, not re-baselining a golden.

At `0.0` both hooks return before touching any geometry and the peer index is
empty, so a default run is **bit-identical**, verified on `interf_u_unrouted` and
`splitflap_driver` rather than argued.

## The board's placement lattice (`board_grid.py`, #708)

A board is laid out on a pitch. `splitflap_driver` puts 92% of its footprint
coordinates on 0.3175 mm (12.5 mil), `glasgow_revC` 97% on 0.05 mm. The quench
used to destroy that: `_candidate_positions` built candidates as
`seed + ix*step` and then snapped the **absolute** result to a lattice through
board origin, which discards the seed's residue. One pass took
`splitflap_driver` from 0.923 of its coordinates on its own lattice to 0.269.

Two things had to change, and the second is the one that does the work.

**Snap the offset, not the position.** `seed + ix*step` already carries the
board's phase; snapping the sum throws it away, because `seed_x` is not
generally a multiple of anything. This half is free: measured, the absolute
snap removed **zero** candidates at every step the tool ships (317/317 at
`--step 1.0`, 49/49 at 0.5, 81/81 at 0.2) — the two sets are a pure
translation of each other. `_group_offsets` had always snapped the offset,
which is why block moves never had this defect and is the existence proof for
the form.

**Snap it to the board's lattice, not the raster.** Seed-relative alone is not
enough, and this is worth stating because it is the obvious fix and it does not
work: the offsets are multiples of `--grid-step` 0.1, and 1.0 is not a multiple
of 0.3175, so only the *zero* offset lands back on an imperial lattice.
Snapping the offset to the board's own pitch gives `{0, ±0.9525, ±1.905,
±2.8575}`, and every candidate stays on the seed's coset of the lattice.
(Coset, not lattice: a part whose own seed is off-lattice keeps its residue
rather than acquiring the board's — the fix preserves phase, it does not impose
one. `splitflap_driver` has 8% of its coordinates off its own pitch.)

Reach is comparable but not uniformly better. At the shipped default
(`--max-displacement 10 --step 1.0`) the count goes 317 → 325; sweeping
`max_displacement` from 0.5 to 19.5 at `--step 1.0`, 12 values gain candidates,
17 tie and **10 lose** — worst 81 → 69 at 5.0 mm, because an offset the raster
admitted at exactly the cap can snap *up* past it and is then correctly
rejected. That is the price of making the displacement cap exact, and
`place_route_loop` widens `--max-displacement` ×1.5 per rejected round, so it
does reach those values.

Measured, one quench pass, fraction of footprint coordinates on the board's own
lattice:

| board | lattice | before | after (old) | after (now) | parts moved |
|---|---|---|---|---|---|
| `splitflap_driver` | 0.3175 | 0.923 | 0.269 | **0.923** | 43 |
| `flat_hierarchy` | 0.3175 | 0.828 | 0.273 | **0.828** | 40 |
| `sonde_u` | 0.3175 | 0.780 | 0.200 | **0.780** | 24 |

The `parts moved` column is the new arm. The quench keeps moving comparably
many parts — old vs new: 43/43 on splitflap, 45/40 on flat_hierarchy, 20/24 on
sonde_u — but it is **not the same set**: splitflap swaps `U6` for `R8`, and
flat_hierarchy drops six and gains one. That is expected, since the candidate
set is genuinely different; what does not change is that the search is still
free to move parts, and every move it makes is now a whole number of grid
units.

### There is no flag

`resolve_snap_lattice` returns the board's inferred pitch, or `--grid-step`
when the board declares none — which is exactly the granularity offsets have
always had. The fallback **is** the off state, and the board reaches it rather
than a flag. Eleven of the 22 tracked boards take it, each with a stated
reason,
and `metrics_out['board_grid']` records which branch ran so "no lattice" and
"never measured" cannot be confused.

### Two rules in the inference that are not the obvious ones

Both come from `tests/measure_708_lattice.py`, and both contradict the
mechanism #708 proposes.

**The tie-break is the finest rung within tolerance of the maximum, not the
argmax.** The ladder contains divisibility chains (0.3175 | 0.635 | 1.27 |
2.54), and occupancy is monotone along one — if `d` divides `s`, every on-`s`
point is on-`d`. Ties are therefore structural, not accidental:
`splitflap_driver` ties at 0.3175 *and* 0.635, `sonde_u` at 0.3175, 0.635 *and*
1.27. An argmax has no defined answer there, and taking the coarsest would
infer a 1.27 mm grid for `sonde_u` off a tie. A consequence worth knowing
before simplifying this: only 0.05 and 0.3175 lack a ladder divisor, so they
are the **only two values the function can return** — the issue's worry that a
2.54 mm inference would quantize the search into uselessness cannot happen, and
not because anything clamps it.

**A rate needs a denominator.** `cap_chain` (4 parts) and the `qfn_*` fixtures
(1–2) score 1.00 at six rungs, because hand-authored coordinates are round by
construction. Below `MIN_PARTS` there is no answer to give.

`OCCUPANCY_FLOOR` is 0.67 and not the rounder 0.70 for a reason worth keeping:
`interf_u_unrouted` scores **exactly** 0.700000, so a 0.70 floor would rest on
a corpus board where `>=` and `>` disagree on a float equality. 0.67 clears it
by 0.0276. It sits in a real gap (0.642424 → 0.700000) but **not the widest** —
that is 0.2235, between `tigard` 0.592 and `rp2350` 0.369, and putting the
floor there instead would admit `tigard` and `orangecrab_ext_pll` at 0.05. The
module docstring has the full gap table; the floor rests on the boundary
argument, not on a separation one.

### Where the lattice deliberately does NOT apply

`place_fanout_clearance` and `reseat.slot_pool` take the seed-relative half and
keep the **raster**. Both searches *are* sub-millimetre clearance repair, and
coarsening them starves exactly the search that must not be starved: at the cap
repair's `step=0.2` the candidate count falls 81 → 29 on a 0.3175 lattice and
81 → 9 on 0.635. `perturb` needed no change at all — it already snapped deltas,
so it never had the defect, notwithstanding #708 naming it.

### The portfolio jitter (#826)

The quench preserves the lattice it is *given*. `place_portfolio` used to give
it a broken one: `perturb_jitter` offsets a part by `r·cos(θ)` at 4 decimal
places — continuous, on no lattice — and `generate` quenches each candidate
from its jittered seed board. Candidate 0, by contrast, is quenched from the
input, so the **baseline kept the board's lattice and the candidates did not**:
the slate was ranked against a baseline with a privilege the candidates were
denied.

Measured over the 11 tracked boards that declare a lattice, at the default
radius: **10 lost it**, two of them to 0.000 occupancy
(`tests/measure_826_jitter_lattice.py`). It is not a tuning problem — the same
10 of 11 at radius 4.0, 1.0, 0.25 *and* 0.05, because a continuous offset is
off-lattice at every amplitude. `glasgow_revC` is the only survivor and not a
reprieve: of its 243 free parts only 59 pass the incumbent-legality guard and
58 then find a legal sample, so it survives by being dense rather than by being
right. Its post-jitter occupancy is 0.763 against a 0.67 floor — forcing parts
off-lattice one at a time, the inference still answers at 24 more and declines
at 25. And its 58 jittered parts still sit on a residue the quench then
preserves on the wrong coset, so even the survivor is on the wrong grid.

The jitter now snaps the **offset** to the board's lattice when `generate`
resolves one. The escape is unaffected — a radius-4 disc on 0.3175 holds **496** lattice
destinations and the sampler takes the first legal one; measured, the most
boxed-in part on any 0.3175 board still has **9** (`sonde_u` C4/C5,
`flat_hierarchy` R1/R2/R3/R6), and `splitflap_driver`'s worst is 11. Re-running
the identical rng stream snapped restores occupancy to *exactly* the input
value on all 11 boards, with the same parts perturbed on 9 of them.

Two details worth keeping:

- **The fallback is no snap, not `--grid-step`.** #708's "the fallback is the
  off state" worked because quench offsets were already multiples of `step`.
  The jitter is continuous, so snapping a no-lattice board to the 0.1 raster
  would change behaviour to buy nothing. `jitter_lattice` returns `None`.
- **A lattice coarser than the radius is refused**, because below it the disc
  holds no destination but the seed. Measured on `interf_u_unrouted`:
  `--radius 0.3175` perturbs 22 of 22, `--radius 0.3` perturbs 0 of 22 after
  440 draws and every candidate goes barren.

`perturb.py`'s `scatter` damage kind calls the same function and deliberately
does **not** pass a lattice — its own comment calls it "the POSITIVE CONTROL …
the arm that MUST recover", and a snapped offset would land the part exactly on
the coset the quench generates from, making the control easier to pass.

`portfolio.json` carries a run-level `jitter_lattice` and three
`board_grid_*` scalars per candidate, so a run can show which branch it took —
without them "the board kept its lattice" and "there was no lattice" read the
same.

## Placement blocks (`groups.py`, #459)

The per-part nudge cannot express "these parts need to travel together": an IC
and its decoupling caps fight each other one at a time, because moving either
alone worsens the pair. `--group-by` gives a block a **rigid translate** move —
the whole body shifts by one offset.

Sources, in precedence order, first match wins per part (a part is in at most one
block):

| source | what it is | corpus evidence |
|---|---|---|
| `kicad` | KiCad `(group ...)` blocks | **0 of 27** in-repo boards have one. Exact when present; verified against a synthetic fixture. |
| `sheet` | schematic sheet path | **12 of 22** boards with `(path ...)` have >1 sheet. `ulx3s`: 10 blocks sized 83/34/23/20/20/12. The workhorse. |
| `netprefix` | net-name prefix (`AUDIO_*`) | The **weakest** source. Raw prefixes are dominated by KiCad's auto-generated `Net-(U1-Pad)` names — one bogus bucket of up to 92 refs spanning 75mm — so those and power nets are excluded and a 20mm coherence gate applied. What survives is small but real: ulx3s 9 blocks, tigard 0. |
| `decap` | 2-pad caps tethered to their IC | Strong. A cap sits 0.0–2.6mm (median) from the nearest IC and **shares a net with it 93–100%** of the time; the net check rejects the rest (13 of ulx3s' 70). |

`--group-by auto` means `kicad,sheet`. Default is `none`: grouping is opt-in, and
with it off the group phase never runs and output is byte-identical to the
ungrouped engine — which is what lets every existing bit-identity test stand.

**The intent adds groups of its own (#1051, #1052, #1043)**, whatever
`--group-by` says, through the gate bundle `floorplan.resolve_intent_gate`
builds for every quenching CLI:

- **Rigid groups**: each declared `arrays[]` row (`array:<name>`) and each
  block with `rigid: true` (`block:<name>`). A member sits out the
  single-part nudge and every swap with a part outside its group (inside an
  array ordered `pin` or `declared`, a swap of two members the order
  positions); it leaves
  only when its own pose fails a clause no group offset clears, and that
  release is disclosed in `rigid_released` (a released member that is clean
  again and still in its slot rejoins). A group with a member that cannot
  move is `anchored`: its movable members are held still.
- **Tethers**: `decaps.max_distance_mm`, `decaps.max_pin_distance_mm` and
  `proximity[]`, each declared at error severity, gate every move by CALLING
  the grader's own measurement (`QuenchState._tether_terms`): a tether within
  its limit stays within, one past it gets no worse. With a decap limit armed,
  each IC and its caps (the `decap` grouper's blocks) also join the group
  phase as `tether:<IC>`, dropped when a rigid group already claims the IC.

A ref claimed by several groups keeps the FIRST (rigid, then tether, then
`--group-by`), each drop disclosed in `groups_deduped`. The summary keys
(`quench.DISCLOSURE_KEYS`: `rigid`, `rigid_released`, `groups_deduped`,
`tethers`) appear only when their channel is declared, and with none declared
the quench is bit-identical to before.

**What actually moves.** Small blocks move; large ones do not. On
`splitflap_driver` with `--group-by decap`, 6 blocks of 2–3 parts translate and
the objective improves (total 4781.6 → 4689.7, crossings 211 → 208, HPWL
2468.0 → 2436.3 against the ungrouped run). Sheet blocks of 16–83 parts moved on
none of the boards tried — at a few mm of displacement a rigid shift of that many
parts rarely finds a legal, improving offset. That is the expected shape: #459's
80mm block *relocation* is separate, unimplemented work.

**Why the cap is per-member.** Every member must land within `--max-displacement`
of **its own seed**. That single rule is what keeps three existing guarantees
true: `build_neighbor_lists`' pruning stays exact rather than lossy, the outline
gate's per-ref cached reach cannot be outrun, and the no-stranding invariant
holds unchanged. Lifting it is precisely what makes the 80mm case hard.

### Blocks are also a ROUTING scope

The same blocks drive `route.py`: `--group BLOCK` scopes a routing run to a
block's nets, `--preview` reports what a run would add without writing a board,
and `--undo` strips a block's copper back to unrouted. `--list-groups` prints
what is inferred, with both net counts, before you trust any of it.

Which nets a block "owns" has two honest answers, so `--group-scope` picks:

| scope | means | when it is the right one |
|---|---|---|
| `internal` | every pad of the net is inside the block | a schematic sheet — measured **60–70% internal** (glasgow's two 68-part sheets: 52 internal vs 23 boundary), so "route this sheet" is a real self-contained job |
| `touching` | any pad inside the block, interface nets included | matches what `--component` already means, and it is the **only** useful reading for a `decap` block: those are **0% internal** by construction, since a decoupling cap bridges VCC to GND and both span the board |

**The default depends on the operation**, because the same set of nets is right
for one and dangerous for the other. Routing defaults to `touching` — routing a
block's interface is the point. `--undo` defaults to `internal`, because a
block's touching set contains GND/VCC, and undoing *those* strips their copper
across the **whole board**: on the rp2350 fixture, `touching` removes 170
segments, 54 of them nowhere near the block, versus 75 for `internal`. Asking
for `--group-scope touching --undo` explicitly still works, and warns.

`--undo` refuses KiCad-**locked** copper — `locked` gets no override anywhere
else in the toolchain (#521) and an undo is not the place to invent one, in the
segment path or the arc path. Nets protected for a *reason* (length-matched,
diff-pair) are removed, because naming a net exactly is the deliberate targeting
those reasons exist to allow. It carries the sibling `.kicad_pro` across (#441):
an undo only removes copper, so the input's DRC floor is still the correct one
and must travel.

It removes `(segment ...)` **and** `(arc ...)` tracks. Arcs need their own pass:
the parser *linearizes* them into `pcb_data.segments`, so an undo counts them,
but the text writer only matches `(segment ...)` blocks — without the arc pass an
arc-routed (i.e. hand-routed) net kept its copper while the run reported success.

What `--undo` does **not** do, and says so at runtime:

- **Zone pours.** A filled plane is copper, so a net with a surviving pour is not
  back to "no copper". Deleting a pour is a plane decision, not an undo, so it
  reports and leaves it — re-routing such a net gives a track web beside the
  pour, not the original plane.
- **Pad/target swaps.** No inverse exists, and they can touch nets outside the
  scope. It returns the named nets to "no copper", not the board to a prior state.
- **Copper graphics.** Net-tagged artwork has no `(segment)` block to delete; it
  is counted and named rather than silently left behind.

It also **refuses to run unscoped** — for a routing run "no scope" sensibly means
the whole board, but for an undo that silently means erasing every track on it.

## Board-state gates (#431)

Both CLIs refuse, with **exit code 3**, two board states they cannot do anything
useful with. `0` ok, `1` crash, `2` argparse, `3` "the board is not in a state
this tool can work on" -- a distinct code so a caller branches on the number
rather than scraping text.

| state | why refusing beats trying | override |
|---|---|---|
| **unplaced** (parts stacked at one coordinate) | the quench REFINES a placement. On a pile every candidate pose is illegal, so the run prints "0 parts moved" plus a legality block that looks like a result, and a large `--max-displacement` yields a tiny scatter around the origin that looks like progress | `--allow-unplaced` |
| **already routed** | the quench models no copper at all: legality is courtyard + outline, cost is pad-to-pad airwires, and `writer.write_placed_output` rewrites footprint poses and — since #714, on request — their board SIDE, but never copper. Every track would be left behind, detached from its pad | `--allow-routed` |

`place_route_loop` gates BEFORE round 0, which routes the whole board -- refusing
there saves minutes-to-hours of A* that would fail everything and then quench a
pile. `render_placement.py` WARNS and renders instead: being able to SEE an
unplaced board is the point of having a renderer for one.

Detection is board-relative. Coincident positions is the only signal strong
enough to fire alone -- two parts at the *same* coordinate is physically
impossible in a real placement, however dense. Low spread never fires alone. The
outside-the-outline share fires at 0.9, not 0.5, because castellated boards
legitimately overhang; and with no usable outline it is **unavailable**, never
true, so a misparsed outline cannot condemn a placed board. Measured: none of
the 27 tracked boards trips any of it, `watchy` included -- and `watchy` is the
worst case, with 81 of 82 parts in courtyard violation. Density is not
unplacedness, which is why this does not live in `legality.py`.

## Floorplan intent (`floorplan.py`, #549)

Everything above judges a placement by `crossings` and `hpwl`, and both are
indifferent between a sensible layout and a scattered one with the same
wirelength. Nothing declares where parts *belong*, so nothing can check whether
they went there.

`check_floorplan.py` closes that: declare the floorplan, grade the board against
it, exit non-zero with the number that broke.

```bash
python3 py_tools/check_floorplan.py board.kicad_pcb --emit-intent floorplan.json   # start here
python3 py_tools/check_floorplan.py board.kicad_pcb --intent floorplan.json        # 0 clean, 4 violations
```

Full reference: [docs/floorplan-intent.md](../../docs/floorplan-intent.md). The
parts worth knowing from here:

- **The board outline is not editable by this toolchain.** `envelope` is READ
  from the board; a part outside it is a finding about the **part**. Board size,
  cutouts and slots are mechanical decisions the user owns.
- **Every rule reuses the geometry the optimizer gates on** — `QuenchState`'s
  `legality_metrics` and `edge_gate`, `GradedPart` rects and sides, and
  `groups.decap_tethers` for the cap→IC rule. A grader with its own idea of
  "legal" grades the reimplementation, so tests assert the two agree exactly.
- **A block that resolves to nothing is an error**, not an empty block. A typo'd
  `refs` grades clean while nothing was checked.
- **`rules_run` / `rules_skipped` are reported**, because "0 violations" and
  "0 rules ran" must not look the same to a machine.
- **A board with no trustworthy outline is refused (exit 3), not graded.** The
  fallback is invisible: no rings ⇒ `BoardOutlineGate.active` False ⇒ every
  containment test silently degrades to the bounding box.
- **`oob_area` cannot be budgeted.** It is measured against the bbox inset, so a
  part sitting entirely inside a cutout scores `0.0`. Refused at load time with
  that reason; use `oob_count` / `oob_amount`.
- **An unknown key is refused at every level of the intent** (#710), and so is a
  `severity` key that is not a rule name. Same reason as `block_unresolved`, one
  level down: a typo'd key that is silently dropped is a constraint the author
  believes they set and the grader never checks. `context` is the one exception,
  open by design at the top level and on every entry, because provenance with
  nowhere to go ends up in a key that IS graded.
- **An edge connector is graded by the way it OPENS (#1155).** The edge
  rule's facing conjunct fails a receptacle (declared, inferred by
  `part_class`, or any `edge_mount` / `through_edge` entry) whose body leaves
  its pad row away from its declared edge -- ERROR on a strong face, WARN on
  a weak one -- and `--emit-intent` never declares an edge a STRONG face
  points away from. A weak face that is itself wrong (a few stock jacks,
  `mating.py`) is not caught: a part seated by it reads pass. A footprint's
  own "PCB Edge" marker, where it draws one, confirms a face (strong) or
  vetoes it (abstains) (#1245); those jacks draw none.
- **An edge member's zone is open on its declared edge (#1174)** when that
  zone side lies on the outline and the claim bounds its overhang
  (`overhang_mm.max`): the edge rule grades how far past the edge it may go,
  and the zone no longer ejects it for going there. Its pads stay graded
  there, at zero. The grade, the quench term, feasibility, the plan checks
  and the seeder's lock note open the same side. A seat search does not need
  to: `seeder.zone_gate` keeps the closed side, but every search it serves
  also demands the courtyard on the board (`pose_ok`), so an open side would
  admit nothing. What keeps an edge member on its edge instead is the edge
  seat: `--repair` seats an edge claim through `_seat_edge`, and since #1240
  so does `place_seed`'s post-polish re-seat (`reseat_on_edge`), aimed at the
  zone's centre and kept only when the part grades no zone, keep-out,
  exclusive-zone or edge error there -- else the ordinary search runs, as
  before. Emitted zones are clamped
  inside the envelope as written, so an emitted intent's zones raise no
  `intent_zone_outside_envelope` or `zone_containment` error on the board
  it came from. That is an error-free grade, not a complete one: a channel
  the grade abstains on (an `overlap_area` budget it cannot measure) still
  exits `check_floorplan` 4 as NOT FULLY GRADED.
- **`schema` is the format number; `min_reader` is the field vocabulary.** An
  intent sets `min_reader` when a claim must not be silently ignored, and a
  build whose `READER_VERSION` is lower refuses the file instead of grading it
  without the claim.

One thing `--emit-intent` will not do: claim a `zone` for a sheet block. A
schematic sheet is a *functional* grouping, so its members scatter and all ten of
ulx3s's sheet bounding boxes mutually overlap (up to 4508 mm²). Zones are emitted
only where disjoint — the same spatial incoherence that makes sheet blocks
useless for movement, above.

## Lock advisor (#431)

`--suggest-locks` on either CLI reports which parts look position-critical, with
a reason each, and prints a paste-ready `--lock` list. It **never locks
anything**: a wrong auto-lock silently freezes a part that needed to move, and
the run just quietly does less.

Its strongest rule is a code fact rather than folklore. `quench.py:207` keeps
only `net_id > 0` pads, so a net-less NPTH mounting hole has `pin_count == 0`
and no airwires -- while the only skip in the part loop is `if not fp.pads`, and
a mounting hole HAS a pad. It is movable with **nothing opposing it but the halo
term**. `tigard` ships four unlocked M3 holes in exactly that state.

Geometric rules (NPTH, outline overhang, edge proximity) measure the board.
Lexical ones (footprint name, reference prefix, pin function) guess from names
and are asymmetric: near-exact on stock KiCad libraries, silent on a house
library like `interf_u:PGA120`. False negatives are common, false positives
rare, and the output says so -- treat a quiet result as "nothing detected", not
"nothing to lock". A ref matched by rules from two different evidence channels
promotes to high.

High-pin parts are an **advisory**, not a suggestion: `place_route_loop` guards
them with `--max-target-pins` but `place_optimize` does not, and "large" is not
"position-critical". Exact refs by default, never invented globs -- a `J*` you
did not inspect freezes parts you never looked at, which is the auto-lock
failure merely deferred.

```bash
python3 py_placer/place_optimize.py board.kicad_pcb --suggest-locks     --suggest-locks-json /tmp/locks.json     # writes NO board
# review the reasons, then:
python3 py_placer/place_optimize.py board.kicad_pcb out.kicad_pcb --lock H1 J1 J2 ...
# confirm you covered them -- unlocked_high must be 0:
python3 py_placer/place_optimize.py board.kicad_pcb --suggest-locks --lock H1 J1 J2 ...
```

## Reviewing a placement: render it (#431)

```bash
python3 py_tools/render_placement.py placed.kicad_pcb --before seed.kicad_pcb -o delta.png
python3 py_tools/render_placement.py board.kicad_pcb --list-groups --group-by sheet
python3 py_tools/render_placement.py board.kicad_pcb --zoom-group sheet:58d913ec --per-side -o out/
```

**The render is triage, not a verdict.** The verdict is the caption's numbers.
Do not judge a placement by how much moved -- "lots moved, looks broken" and
"barely moved, looks safe" are both wrong.

### What it renders

Every panel below is `render_placement.py` output, and every one carries a
metrics caption — the render is triage, the caption is the verdict.

**The placement diff, drawn.** `--before` turns on the dashed seed rects and the
displacement arrows, so "what moved and how far" is the picture rather than a
diff of two files. 22 parts here; `C4` travelled 95.65 mm.

```bash
python3 py_tools/render_placement.py placed.kicad_pcb --before seed.kicad_pcb -o delta.png
```

![placement delta](../docs/placement-delta.png)

**Per-side panels.** Placement lives on two sides, so `--per-side` emits one
panel per side instead of one flattened projection. Parts on the side you asked
for are bright; the far side's SMD pads are dimmed (still context — a back-side
part is why a front trace has to detour); **through-hole pads stay full
brightness on both**, because they are physically on both and a hole cannot move
on one side only.

```bash
python3 py_tools/render_placement.py board.kicad_pcb --before seed.kicad_pcb --per-side -o out/
```

| front | back |
|---|---|
| ![front](../docs/placement-side-F.png) | ![back](../docs/placement-side-B.png) |

**Zoom to one placement block.** `--zoom-group` takes the same block names as
`route.py --group` — `--list-groups` prints them with their part and net counts.

```bash
python3 py_tools/render_placement.py board.kicad_pcb --list-groups --group-by sheet
python3 py_tools/render_placement.py board.kicad_pcb --zoom-group 58d913ec --group-by sheet -o blk.png
```

![group zoom](../docs/placement-group-zoom.png)

**An unplaced board.** The placement CLIs refuse this one (exit 3); the renderer
warns and draws it anyway, framed to the PARTS rather than the outline — a pile
at the origin would otherwise render as a dot in the corner of an empty board.
13 footprints at 1 distinct position, and `overlap 792 mm²` gives it away.

![unplaced](../docs/placement-unplaced.png)

**Reference designators.** On by default, and sized to the PART rather than to
the image — scaling by image height alone gives an 8px label at any zoom:
drawn, technically present, and unreadable. A part too small to letter is
skipped, and its arrow carries the meaning instead.

![labels](../docs/placement-labels.png)

**Every toggle, on one board.** The same 13-part delta, one flag at a time:

![toggles](../docs/placement-toggles.png)

`--no-delta-first` looks unchanged there for a real reason: 12 of the 13 parts
moved, so there is no context left to dim. It matters on a board where the delta
is a genuine subset — which is also where `--ratsnest-all` earns its keep:

![ratsnest](../docs/placement-ratsnest.png)

By default only the moved and attributed nets are drawn. `--ratsnest-all` is the
deliberate hairball switch: on a dense board it reproduces exactly the
unreadable mess KiCad already shows, which is limitation #1 in the issue and the
reason delta-first is a default rather than polish.

**Just the nets you are chasing.** `--ratsnest-nets` takes the same globs as
`route.py --nets`, exclusions included, and draws only those airwires — in their
own colour, with the parts they run between labelled and un-dimmed. Between "the
nets that moved" and "every net on the board" this is the case that actually
comes up.

```bash
python3 py_tools/render_placement.py board.kicad_pcb --before seed.kicad_pcb     --ratsnest-nets '/CLK*' '/DATA*' '!*_N' -o clk.png
```

![named nets](../docs/placement-ratsnest-nets.png)

The matching goes through `net_queries.matches_net_filter` — the same helper
`route.py --nets` and the plan executor use — so a pattern means here exactly
what it means there. A pattern that matches nothing says so on stderr rather
than rendering an empty ratsnest, which would read as "this net has no airwires".

`--focus` adds one cropped panel per failed-net cluster.

## Module layout

| File | Purpose |
|------|---------|
| `quench.py` | The optimizer: cost terms, move generation, greedy quench |
| `simple.py` | The simple path's cost model (`../place_simple.py`): signal and rail trees, crossings, claims, decaps, group gap trees, halo, foreign-pad hits, congestion -- priced locally per move (`SpatialIndex`, cached breakdowns, Rust kernels) |
| `simple_anneal.py` | The simple path's optimiser: simulated annealing on `simple.py`'s exact cost from a random scatter, soft overlap rising to hard, seeded and deterministic |
| `portfolio.py` | K diverse candidates: perturbation strategies, gates, ranking, diversity selection |
| `seeder.py` | Intent-driven initial placement for unplaced boards, and `(locked yes)` stamping |
| `../group_routing.py` | Block → net scoping, and the undo, for `route.py` (#459) |
| `fanout_clearance.py` | Post-fanout decoupling-cap clearance repair (#130) |
| `groups.py` | Placement blocks: which parts move as one rigid body (#459) |
| `diagnosis.py` | Which parts to offer the quench, from routing evidence rather than pin count (#553). Ranks; never combines |
| `relocate.py` | Bounded block relocation as a constraint solve (#554): the relative-order graph, and how far a block travels when its neighbours yield |
| `routability.py` | Block displacement, corridors, escape lanes, net affinity — the geometry behind `floorplan.grade`'s health block |
| `reconstruct.py` | The structural ("puzzle") solver: tier classification, pattern fit, rigid vectors, the assignment ILP, and the minimal-move legalize sweep |
| `reseat.py` | Hungarian re-assignment of a proximity-tethered cluster onto rings around its anchor. NOT `seeder.reseat_scope`, which is a different mechanism for a different problem |
| `reachability.py` | Whether a pose is reachable at all, for the repair ladders |
| `labels.py` | Reference-designator silkscreen geometry (`beautify_labels`, #481) |
| `perturb.py` | Manufactures known-bad seeds by displacing a block (#411). Damage, never repair |
| `recovery.py` | Grades a repaired placement against the original it was damaged from (#411) |
| `floorplan.py` | Floorplan intent: declare where things are supposed to go, then grade it (#549). A checker; it authors no geometry |
| `escape.py` | Per-face escape-lane ledger: can the nets on this part's face get OUT? |
| `pair_order.py` | Pin-order inversions between connected part pairs. A LOWER BOUND, not a proxy |
| `part_class.py` | What determines a part's position — a pose-INDEPENDENT class |
| `roles.py` | What a part IS (regulator, crystal, transistor, ...), read off footprint name, pins and prefix: the table `board_context` prints and the decap owner predicate (`groups.supply_owners`, #1249) reads |
| `emc_roles.py` | Converters, crystals, oscillators and ESD arrays by pin function, each leg measured pad edge to pad edge, with a ready `proximity[]` row (`max_mm` null) -- `board_context --json`'s `emc_roles` (#1254). Also PROPOSES design-brief `keep_away` rules: each crystal's or oscillator's clock nets against the switch nodes (SW / LX, a discrete switch's drain; pin-less parts as `candidate`), at a 3 mm derived-default gap, measured by check_floorplan's own `keep_away` grade, with a `brief_row` to paste (#1253). Report only |
| `crystals.py` | Crystal claims: each passive crystal (`emc_roles`) within `LEG_MM` of its IC's oscillator pins and each load cap (a two-pad cap from a load net to ground) within `CAP_MM` of the crystal pad on its net, as `proximity[]` rows. The limits are the human corpus's 75th percentiles (`tests/stress/placement_survey.py`). `plan_regions.py` writes them; the seeder seats each claimed part right after its partner |
| `options.py` | When a board cannot hold its parts, say so WITH THE NUMBER and the options |
| `provenance.py` | Was every pose in this board produced by a registered engine lever? |
| `lock_advisor.py` | Which parts should not be moved, and why (#431). Advice only |
| `mount_holes.py` | A default pose for the mounting holes a pile leaves unlocked: on a board with exactly four holes (the human corpus puts all four within 8 mm of a corner on 86 of 125 such boards, `tests/stress/placement_survey.py`), each at the legal pose nearest a free corner, by the seeder's own seat search. `plan_regions.py` writes them as `fixed_poses[]` of basis `derived_default` |
| `placement_state.py` | Board-state gates: unplaced, and already-routed (#431) |
| `cli_gates.py` | argparse shared by both placement CLIs so they cannot drift |
| `../render_placement.py` | Headless PNG stills of placement status (#431) |
| `legality.py` | Hard constraints shared by both engines: board side, real Edge.Cuts containment, and the OO/OoB graders (#456) |
| `body.py` | THE body model (#896): courtyard -> fab -> silk U pads -> pad bbox, with the occupancy rect and the source that answered; a fab MARKING (under 2% of the pad box) is never the body (#1201) |
| `mating.py` | Which way a connector OPENS (#1155): its mating face, read off where the body sits relative to its pad row. The edge rule and the seeder both read it through `floorplan.edge_facing` |
| `connector_geometry.py` | The DRAWN connector envelope against a declared compass edge (#961): the number an `overhang_mm` band is graded on, at zero margin, and the legacy occupancy reading named as the basis wherever no body can be measured |
| `parser.py` | Courtyard, fab, silk and locked-footprint extraction |
| `writer.py` | Writes new positions/rotations (rotates pad angles with the footprint, as KiCad stores pad angle = footprint + pad rotation). Resolves blocks through `kicad_parser.iter_footprint_blocks`, so one placement moves ONE block even when two share a reference (#726) |
| `board_grid.py` | The pitch a board was laid out on, inferred from its footprint origins (#708). Pure; no engine imports |
| `utility.py` | Shared utilities (bbox from pads, grid snapping) |

## Body model (`body.py`, #896)

What a footprint's *body* is, decided once. Before this module the answer lived
in five places with three ladders, and the one most consumers reached through
went straight from courtyard to the **pad bounding box** -- it never looked at
`.Fab`. On a library that draws no courtyard, every placement instrument was
therefore grading pad boxes. Measured on `esp_prog` (OLIMEX): **0 of 21**
footprints draw a courtyard, and six of them -- CON1, CON2, U1, U2, Q1, Q2 --
draw no `.Fab` either. Those six are the connector housings, the SSOP and the
SOT89 whose collisions cost run 25 two laps, each found only by a reviewer
writing its own geometry.

**Two ladders, one reader**, because they answer different questions:

```
occupancy    courtyard -> fab -> silk U pad_bbox -> pad_bbox -> synthetic
drawn body   fab -> silk U pad_bbox -> (nothing)
```

`occupancy_local` is *what does this part occupy* and never shrinks; it is what
`part_local_bounds` publishes and what every consumer deciding whether
something may be seated somewhere reads. `drawn_local` is *what did the library
draw as this part's body*, and the courtyard is deliberately NOT on it -- a
courtyard is a body plus an assembly margin plus any shell overhang, which is
exactly why run 6 calibrated the courtyard channel and the fab channel apart. A
part can have `source == 'courtyard'` and `drawn_source == 'fab'` at once.

**Only the silk rung unions with the pads**, and that is measured rather than
assumed. Silk is not an outline: on a stock KiCad footprint it is a pair of
clipped side ticks. Over the 10 esp_prog footprints drawing both, the silk bbox
is narrower than the `.Fab` body along the pad axis on 10 of 10 (Y1: 0.508 mm
against a 3.200 mm body) and wider across it on 10 of 10, so no offset
reconciles them -- the sign of the error differs per axis. Taken bare it would
SHRINK parts (Q1/Q2 SOT23: a 3.610 x 2.902 pad box becomes 0.838 x 2.845),
which is the unsafe direction. Unioning the courtyard and fab rungs too would
be a different measurement: it moves lap-3 `U1<->Y1` from -0.1330 to -0.2830,
because Y1's pads overhang its fab body.

**A pad-less footprint gets no silk body.** Allowing it put 5 corpus pairs
above the run-23 blocking floors and all five were logos (`logo`, `oshw:oshw`,
`Glasgow:nono_hana_lines`).

**A `.Fab` rung under 2% of the pad box is a MARKING, not a body (#1201).**
The fab rung used to be taken unconditionally while silk had a tick-mark
guard, so the 0.06 mm pin-1 `fp_circle` -- the only F.Fab item an
EasyEDA-converted USB-C receptacle draws -- became that part's body, and its
edge seat was graded on a dot ("seated 8.90mm from the nearest edge" for a
part whose pads are 3.8 mm from it, CM5_MINIMA_3 J101). `fab_cannot_be_body`
(`FAB_MIN_PAD_AREA_FRAC = 0.02`) rejects it: the DRAWN body falls to silk,
else nothing, and `fab_rejected` records it. What the part OCCUPIES is
deliberately unchanged -- the marking united with its pads, as before, now
labelled `fab_marking` -- because a silk occupancy never gates (#896), so
silk may not answer that question. Measured: the markings reach
0.00466 of their pad box (all 204 on the fa10 designer boards), the least
real bodies 0.0537 (KiCad's stock library) and 0.3114 on the tracked corpus
(orangecrab J4), which loses none. A pad-less part keeps its fab body.

**A LOCKED part without a courtyard is an obstacle by its fab body.** The
seed and the polish space a courtyard-less part by its pad box -- the body
as their currency for every part (#1182) was A/B'd and rejected -- while
check_assembly grades each moved neighbour against its body. For a part
nothing moves, the body is the board's: `quench.locked_body_locals` gives
each locked part whose occupancy comes from a `.Fab` body (no silk outline,
no marking, no container) that occupancy as its seat rect. On ulx3s the
ESP32 module U9, locked, draws a fab body 4 mm past its pads at the antenna
end, and six parts seated there had made every grouped candidate NOT
BUILDABLE.

**A silk-sourced body never GATES**, structurally rather than by a census
coming out clean. On the shipped esp_prog, U2's OLIMEX SOT89 draws four corner
brackets at +/-2.5 mm plus a pin-1 dot -- a 5.2 x 5.2 mm assembly square
centred on an origin its pads are not centred on -- and R1, which clears U2's
real body by 2.1 mm, reads as 89% CONTAINED. The pair is reported, with its
source; it does not decide. The exclusion is scoped per channel (containment
reads `drawn_source`, the courtyard channel the occupancy source), because
scoping it to one set threw away four pre-existing findings silk had nothing to
do with. check_assembly says WHY each contained pair does not gate
(`gate_exempt`: `silk_drawn`, or the marker / container waiver that applies,
#1201) instead of one sentence that described neither.

**A mounting hole is decided by its ITEM name, not a substring (#1176).**
`part_class.is_mount_hole_name` reads `MountingHole` at the start of the item
or, singular, later in it (a house `M3_MountingHole_Pad`); the plural
`MountingHoles` is what a part that merely HAS holes says (KiCad's horizontal
D-subs' `MountingHolesOffset`, its modules' `WithMountingHoles`), and the
substring rule read those as markers. The item rather than `lib:item`, because
the pcbnew front drops the library prefix. A horizontal D-sub is therefore
graded as an edge receptacle, so a part inside its drawn housing is a
containment that gates check_assembly, where the marker waiver used to excuse
it.

**There is no offset constant, deliberately.** #896 proposes expanding silk by
"the silk-to-body offset the library uses, ~0.2 mm on OLIMEX"; that turns its
own acceptance numbers -0.12 / -0.09 / -0.133 into -0.52 / -0.49 / -0.53. Silk
at stroke centreline, unioned with the pads, reproduces all three exactly.

Corpus effect over the 22 tracked boards, symmetric diff, nothing lost:

| | before | after |
|---|---|---|
| `fab_unjudged` | 140 | **77** |
| `blocking` | 0 | 0 |
| `containment_blocking` | 0 | 0 |
| `courtyard_blocking` | 118 | 119 |
| `advisory` | 177 | 183 |

Source mix over 1349 corpus parts: 1267 courtyard, 27 fab, 6 silk, 49 pad bbox,
0 with no geometry at all; 2 silk boxes refused as tick marks.

`tightest_body_seam` reports the closest drawn-body pair on a board, signed
(negative is an overlap, the same convention as `rect_gap`) and naming the
source each side rests on. `body_overlap_pairs` reports a depth only for pairs
that already overlap, so a board a hair from a collision said nothing at all.

**A test point under another part's body (#1250).** The marker waiver
excuses a test point's courtyard pair and its containment, because a test
point has nothing above board level that could be in anyone's way. It never
asked whether something now sits over the test point. Glasgow's TP6, moved
39.7 mm into U19's TSSOP body, graded `buildable`. `grade_body_overlap`'s
`testpoint_covered` measures every test point's pad copper
(`bodyless_pad_shape`) against each other same-face drawn body
(`pads_under_body_frac`, at `CONTAINMENT_FRAC`, the #1106 measurement).
check_assembly gates a finding only when a member moved relative to
`--baseline` (`testpoint_covered_gating`, the courtyard gate's currency). It
also needs the body to be `.Fab`, since silk never gates, and the pair must not
be in the intent's `overlap_waivers`. Over the 22 tracked boards, 2 of 40 test
points read covered: orangecrab TP26/TP27 under J4, both the designer's own
poses, so they stay buildable. Fiducials are not graded here, because human
boards seat them under connectors on purpose. This is the grader's census only.
The movers' marker exemption (`body_exempt_refs`, `blocking_waived`) still
admits such a seat, and this gate then charges it.

**A body's distance to the board edge (#1255).** `legality.body_edge_band`
measures every pad-bearing part's body against the real outline at margin 0.
The body is the drawn one, else the courtyard, else the pad box, with the rung
named. Edge-class parts, markers and parts with an edge in the intent are
listed apart. The band is the intent's `assembly.body_edge_mm`; without it,
JLCPCB's 2.5 mm (`BODY_EDGE_DEFAULT_MM`, labelled `derived_default`, with its
citation). check_assembly and render_placement report the census, and it gates
only when the band is declared. All five human boards measured break 2.5 mm
(splitflap 10 of 58 parts, esp_prog 7 of 15, glasgow 4 of 232, watchy 10 of 70,
ulx3s 9 of 210), and JLC adds edge rails when the file has none. It is the
grader's census only: the quench's soft `edge_halo` (2.0 mm) does not read a
declared band.

## Mating face (`mating.py`, #1155)

The toolchain could say which edge a connector's courtyard was NEAREST, and
nothing else, so a DSUB edge-mount seated backwards -- shell over the board,
opening pointing inward -- passed every grader whenever the back of its shell
grazed its declared edge (sonde_xilinx J2). The fact that decides it is where
the BODY sits relative to the PAD ROW: a horizontal connector solders at the
back and extends forward to its opening. `mating_face_local` reads that
offset in the footprint's own frame from two votes, the courtyard and the
drawn body (fab after #1201's guard, else silk), and `facing_edge` maps it
through the pose.

It abstains, with a reason, on no pads, a board-copper plug (the board is the
mating body), a `Vertical` footprint name, a symmetric part (offset under
`FACE_MIN_OFFSET_MM` 0.57 mm, or under `FACE_DOMINANCE` times its cross-axis
offset), votes that disagree, and a pose more than 30 degrees off every
quarter turn.
A face is STRONG at `FACE_STRONG_OFFSET_MM` (2 mm): only a strong face fails
a declared edge at ERROR. The tiering is measured, not cautious -- KiCad's
stock library has three receptacles whose PAD layout sets a confidently
wrong face between 0.9 and 1.4 mm, among 60+ correct USB faces in the same
range, and no wrong face is known above 2 mm. A wrong WEAK face is not
caught: stage 1 also tries the angles that point a weak face at the edge
first when it chooses an undeclared connector's angle, so such a jack can
be seated opening into the board, and the facing conjunct then reads pass.

The footprint's "PCB Edge" marker is the third source (#1245,
`marker_face_local`, carried as `BodyGeometry.edge_marker` from
`parser.extract_edge_markers`). It reads the LINE the marker labels -- the
straight line parallel to the text's baseline (its stored angle less the
footprint's rotation), nearest it, on any layer but a courtyard or copper --
and the face is the side of it away from the pad row. The text's POSITION is
not the reading: sonde_u's DSUB texts sit 21.55 mm along the edge line, and
stock DIN41612s put theirs 2 mm off it with an arrow. It abstains on a
diagonal text, no parallel line, a line within `FACE_MIN_OFFSET_MM` of the
pad centre, a rival line on another face less than `MARKER_NEARER_RATIO`
times and `MARKER_NEARER_MM` farther, and two markers that disagree. It
never names a face alone: agreeing, it makes the body's face STRONG (basis
`+marker`); contradicting, it makes it abstain, never fail. Calibrated by
`tests/measure_1245_edge_marker.py` on KiCad 10's 154 marked stock
footprints: 73 of 73 strong body faces agree, 75 weak ones agree, one weak
one is vetoed (the RaspberryPi Pico W module's RF-edge marker), and no
reading moves when the footprints are saved at 90, 180 or 270. The three
wrong-faced stock jacks above draw no marker, so it does not reach them.

One predicate, every consumer: `floorplan.edge_facing` is what
`rule_edge_connector`'s facing conjunct grades, what `emit_intent` withholds
a reversed edge by, and the function object the seeder seats by, so the
seeder's angle choice and the grade read one answer for which way a part
faces. A reversed seat is still possible -- an author's declared angle is
kept, stage 1 falls back to the input angle when no facing angle seats, and
the wrong weak face above misleads both alike -- and the grade reports the
first two. `board_context`'s "Mating face" reads the same primitive. B-side parts need
no extra mirror: the board file stores their local coordinates mirrored
already. Census: `tests/measure_1155_mating_census.py` -- every decisive
receptacle on the tracked and fa10 designer boards faces the edge it sits on.

## Legality model (`legality.py`, #456)

What counts as a *legal* placement is decided in one place, so the optimizer and
any grader cannot disagree:

- **Board side.** A part occupies its own side with its full courtyard, and the
  opposite side only with the bounding box of its **drilled** pads. So a
  back-side decoupling cap may sit under a front-side BGA (they overlap in XY,
  not in copper), but not inside a front-side connector's pin field. Cross-side
  pairs also pay no halo penalty — spreading them apart buys no routing room.
  On a single-sided board every test reduces to plain courtyard-vs-courtyard.
  The far side is one box per CLUSTER of drilled pads (#1206,
  `legality.far_side_local`): holes merge single-link while their boxes are
  within `FAR_SIDE_CLUSTER_GAP_MM` (2.54) of each other, so a pin row or a
  2-row header stays one box and two mounting posts 48 mm apart are two.
  The returned `FarSide` is still the union 4-tuple, so a consumer that does
  not read `.boxes` gets the old (stricter) answer. CM5 human: 45 courtyard
  pairs -> 14 (`tests/measure_1206_far_side_clusters.py`).
- **Containers** (#1184, #1212, `legality.container_kinds`). A part is a
  container only by GEOMETRY: at least `CONTAINER_RATIO` (0.5) of the board,
  no drawn courtyard, and either an `outline` (no pads and no holes -- watchy's
  e-paper REF**) or a `pin_frame` (at least `FRAME_DRILLED_FRAC` of its pads
  drilled and none inside the box inset by `FRAME_BAND_FRAC` of its short
  side -- rp2350's Teensy U8). Never by area alone (One-Air-Max's SMD 18650
  holder BAT1 is a BODY) and never by a lock. Pose-independent: a turn grows a
  rect, never a part's own box. An outline's courtyard pairs are listed,
  waived `container_class`, locked or not. A pin frame's rect pairs leave the
  courtyard channel and its drilled HOLES are graded instead, kind
  `pin_in_courtyard` (KiCad's `pth_inside_courtyard`): check_assembly's
  eighth conjunct, absolute; `tests/measure_1212_kicad_pins.py` cross-checks
  it against kicad-cli. The search (`container_pin` veto), the seeder's
  declared-pose and eviction checks and the repair census all ask
  `CourtyardCensus.pin_hits`, the grader's own function.
- **Board containment** measures against the real Edge.Cuts rings, not an inset
  of the axis-aligned `board_bounds`, so parts are not nudged into an L-shaped
  board's notch or an interior cutout. Three levels of short-circuit keep it
  cheap: the gate self-disables when the outline *is* its bounding box (or when
  the parser found no usable ring, where the bbox is all there is), then a cached
  per-part reachable-disk prune, then the exact ring test.
- **Off-board seeds are not frozen.** Only candidate poses are validated, never
  the incumbent, so a part sitting outside the board had every alternative
  rejected and could never move — not even toward the board. A part whose only
  violation is board containment may now take a pose that moves it strictly back
  toward the board without overlapping anything.

  This is deliberately limited to the board term. An *overlapping* part keeps the
  original rule (it may move only to a fully legal pose), because the violation
  measure is a distance while the thing at stake is an area: trading one deep
  narrow overlap for a shallow wide one lowers the distance and raises the area.
  Measured on `watchy`, where 81 of 82 parts start in violation — its hand
  placement is tighter than the 0.25 mm courtyard clearance quench asks for — a
  permissive rule took total courtyard overlap from 9.1 mm² to 37.9 mm²
  (strict-decrease: 16.8) where the board-only rule gets 0.23 while also walking
  10 of the 13 off-board parts back on.
- **Graders.** `placement_overlap_area` (OO, mm²) and `placement_out_of_board`
  (OoB) report the same geometry the optimizer gates on;
  `QuenchState.legality_metrics()` returns both for a live placement. All zero
  means fully legal. Intended for the placement scorecard in #411/#110.

**Cost on two-sided boards.** Side-awareness removes a large number of false
collisions, so many candidate poses that used to be rejected outright now reach
the cost function. On `glasgow_revC` (172 front / 92 back parts) a bounded 40-part
pass makes **3.4× more airwire cost evaluations** than before (9.3k → 31.4k
`_count_crossings_np` calls) and takes correspondingly longer. Nothing per-call
got slower — the optimizer is searching the space it was previously, wrongly,
skipping. Small boards are unaffected or faster (`watchy`: 69 s against 109 s
before).

Courtyard extraction (`parser.py`) reads `fp_line`/`fp_rect`/`fp_arc`/
`fp_circle`/`fp_poly` per side. A footprint with no courtyard on any layer falls
back to its pad bounding box — which carries no courtyard margin at all, so the
part is modelled smaller than it is; that fallback now warns and names the refs.

Note: an earlier from-scratch constructive placer (`place.py` +
`rust_placer/`) was removed after experiments showed hand placements beat it
by ~500× in router effort; see git history and
docs/placement-optimization.md for details.

## Pad+drill legality layer, repair mode, and the reconstruct solver

Added after two evaluation runs on the #411 swap corpus measured the failure
modes directly (parts walked onto a locked connector's pad, parts left
off-board, parts on NPTH holes, 48/92-part churn, `place_seed --force`
re-seating 85/92 while leaving its zone targets unmoved):

- **`legality.PartPads` / `LegalityContext` / `grade_pad_legality`** — the
  pad+drill layer. Gate currency: rotation-inflated AABB pad rects (the
  `_Cap.pad_rects` pattern; conservative — can falsely reject, never falsely
  accept), NPTH drills held off foreign copper at a STANDOFF FROM THE HOLE
  WALL of `max(--clearance, NPTH_TO_TRACK_CLEARANCE, the board's declared
  min_hole_clearance, the hole pad's own local_clearance)` — `check_drc`'s own
  requirement (#730, #761). Resolved in ONE place, `PartPads.hole_keepouts`:
  the stored `holes_local` radius is the growth ABOVE `--clearance` and the
  consumer adds the clearance back, exactly as `fanout_clearance`'s cap gate
  and `labels.py`'s silk test each do — before #761 legality added nothing, so
  its modelled standoff collapsed to zero at and above the requirement.
  `copper_holes=False` opts a SILK caller out of both copper terms (the pad
  override and the board floor); the board floor arrives as a resolved FLOAT
  (`resolve_npth_floor`), never a board pointer, and only at the two of six
  `build_part_pads` call sites that read hole keep-outs. `holes_extent`
  carries the same holes WITHOUT either copper term, so an author's keep-clear
  can never push a part off the outline. Per-PAIR
  baselines from SEED poses ("never worse than the board you were handed"; a
  NEW different-net pad intersection is never admitted). Exact `check_drc`
  geometry runs once per CLI for reports, so summaries carry no AABB phantoms.
- **Quench integration (default ON)** — `candidate_valid`, the off-board
  unfreeze branch and the swap phase all carry the pad gate (and, since
  #1257, `candidate_valid` refuses pad copper past the outline for a part
  whose pads reach past its courtyard -- veto `pad_copper_outline`); the halo term no
  longer saturates on overlap (existing overlaps now have a repair gradient);
  zero-net parts (mounting holes) are frozen by the quench unless
  `--move-unconnected`; zero-pad footprints with a courtyard are static
  obstacles; `--min-gain-per-mm` (default 0.1) is a displacement-scaled
  acceptance threshold. `--courtyard-only` restores the old model bit-for-bit.
- **`place_seed --repair`** — violation-driven minimal-move repair: only
  violators move, worst first, escalating caps (0.5/1/2/5 mm), file-locked
  non-must_lock violators are reported, never moved. Zones smaller than a
  part's courtyard grade (and seat) on the anchor point — the spec-coordinate
  pattern is satisfiable by construction now. A violator is reported
  `repaired` only when every grade error it was charged for is gone
  (#1066): each charged ref, moved or not, is re-graded after the pass and
  one still carrying its claim is `unresolved`, named in `JSON_SUMMARY`
  (`repaired_refs`, `unresolved_refs`, `unresolved_by_rule`). So is a moved
  ref whose move CREATED a finding the input poses did not have, or made one
  WORSE (compared per finding -- rule, ref, pad, net -- not per claim; a
  finding naming no moved ref is charged by counterfactual, each moved ref
  restored alone), and a cap pushed past the
  decap search radius (its `decap_distance` became `decap_ungraded`, which is
  not a fix). A part charged by decap rules ALONE is not moved by this seat
  (#1150 b): the seat is the nearest legal pose to the part's own and has no
  decap target, so it could only leave the finding or make it worse (esp_prog
  C2 shipped 0.06-0.10 mm farther from U1 three ways) -- unless it is a
  member of a pad, body or baseline-gating courtyard pair the census charged
  to the OTHER member (#1259). Such a part waits until every charged member
  has had its seat; if its pair is still on the board (the charged seat out
  of proportion, absent, or taken without clearing it) it gets this seat,
  kept only when the move clears the pair -- its pad/hole/stack shortfall
  against the charged member falls with none of it growing, or the gating
  courtyard pair is gone with no new one -- and is otherwise put back. watchy
  C15/TP7 and splitflap C8/J7 shipped without it. A part that takes part
  in a decap finding -- its ref, or the cap / IC it measures -- is seated
  first where none of those findings grows, and ANY part a decap rule can
  measure (a graded cap, a cap serving a supply pin, an IC with supply pins
  or caps elected to it) first where it creates no decap finding -- a cap
  moved off a pad conflict used to be seated outside its limit, reported and
  shipped; by the plain seat only when no such pose exists within any cap,
  which a note says. An IC is never moved for a decap finding. `unresolved` sets no exit code of its own; exit 4 stays
  `unrepairable_failed_refs`' (a free violator no seat could place) and the
  final grade's. A locked violator (`(locked yes)`, `--lock`, a fixed pose)
  is `unrepairable_pinned_refs`: reported, not a gate failure, the
  pinned-part contract (#1193) -- except pad copper past the outline, where a
  lock is no waiver, as in the seed gate (#1241): such a ref is also in
  `unrepairable_pinned_off_board_refs` and exits 4. The
  off-board census grades each pad's copper and each hole ALONE
  (`legality.pad_copper_outside_amount`, the `oob_pad_copper_refs`
  measure), never the part's pad union, whose corner can sit in a notch no
  pad enters. `place_reconstruct`'s legalize
  stage reports the same `unresolved` list.
  `--repair-decaps` (#1066 b, opt-in) adds the missing actor, and it moves
  only CAPS (`DECAP_RUNG_RULES`). A `decap_distance` cap, or a cap a
  --decaps-from reference holds and the board strands past the search radius
  (`decap_ungraded` at error, #1150 a), is seated toward the IC pad on its
  rail nearest it. A `decap_pin_distance` pin may be served by ANY usable cap
  on its rail (#1188): every one is ranked, first those no other pin relies on
  (not the only cap within the limit of a passing pin, not the nearest cap of
  a failing one), nearest first, and the first one the grade keeps wins --
  the finding's `measured.cap` is only the nearest, and moving it when it is
  the nearest for several failing pins worsens the rest, and the grade
  reverts it. Each search asks only poses that clear the target and
  make no decap finding the cap takes part in new or worse
  (`_DecapView.accept`), and a pose nearest the target that is out of
  proportion is followed by a search within the budget of the cap's own pose.
  What makes it affordable changes no verdict: a search (the ordinary seat's
  too) asks `pose_ok` against only the parts within reach of its disc
  (`_search_scope`, a superset kept in board order), `accept` settles a check
  by a bound on the cap's pads where the bound decides it and poses the cap
  only otherwise, and a board the rung has graded is not graded again.
  A seat is KEPT only when that finding is gone, no finding anywhere is new or
  worse, the overlap / off-board numbers did not grow, and the move is
  proportionate (the repair's own `DISPROPORTION_RATIO` / `_FLOOR_MM`,
  against how far the cap is past its limit). A finding still open because
  every pose within the limit is taken is asked once more with the movable
  PASSIVES near its target set aside (a resistor, capacitor, inductor or
  ferrite with two copper pads, unlocked and undeclared -- never an IC, a
  diode, a connector or a marker): the cap takes a pose with at most two of
  them in its way, each of those moves to the nearest pose of its own within
  the repair's cap ladder that creates no decap finding and makes none
  worse (a decap set aside stays within its own limits), and the grade
  judges the cap and the moved passives as one move. A passive moved aside
  is in `moves` and in `decap_rung` (`moved_aside`, `aside_for`), not in
  `repaired`. An IC still carrying a pin
  finding is UNRESOLVED with a note naming the caps tried for each pin and
  their result. Its record is `JSON_SUMMARY.decap_rung`. It stays
  off by default: of `tests/test_placement_ab.py`'s five `repair-decaps-*`
  rows one improves, three regress on a guard while clearing their decap
  errors (watchy on crossings; esp_prog and glasgow on hpwl, from a passive
  set aside), and one is neutral, short of the N-1 rule.
  The repair also reads check_assembly's own channels (fa10 P1): a part
  over a pin frame's drilled pin is charged absolutely (`Pin census`,
  #1212), and the COURTYARD channel (`Courtyard census`, #1182) --
  `CourtyardCensus` with the intent's waivers -- is charged only under
  `--baseline BOARD`, because check_assembly gates a courtyard pair only
  when a member moved against one. The member that moved is charged
  (weight its depth, >= 1 mm), and the pairs are re-graded at the final
  poses: a charged pair that still gates, or a gating pair a moved part
  created, keeps its mover out of `repaired`, and the note says when the
  search spaced pad boxes. `JSON_SUMMARY`'s repair record carries
  `courtyard_gating_before` / `_after` (None without a baseline).
- **`--body-model`** (#1182, from #916) arms every search state a
  `place_seed` run builds -- seed, polish, the post-polish re-seat, repair,
  reseat and its inner seed -- with `placement.body`'s occupancy (courtyard,
  else the drawn .Fab body, else silk, each united with the pads). Only the
  NEIGHBOUR currency moves (`quench._Part.rect`): every intent, zone,
  keep-out, edge-claim and board question asks `_Part.grade_rect`, the
  courtyard-else-pad-box ladder the floorplan grade reads off a default
  state, so the armed search cannot refuse a seat the grade accepts and the
  PoseGrader grades an armed state exactly as an unarmed one. For a library
  that draws bodies and no courtyards (One-Air-Max: 197 of 204 parts) pad
  boxes let bodies overlap. Measured on One-Air-Max's s180_0p with
  `--repair --baseline`: armed, check_assembly's gating pairs went 6 -> 2;
  unarmed, 5 of the 6 charged movers ended UNRESOLVED. **Opt-in**: the
  `body-seed-*` rows of `test_placement_ab.py` mark REGRESS on all four
  boards (wirelength, crossings or an intent error), so they are kept as
  `rejected` rows.
- **`--reseat`'s gate** measures its last term, courtyard overlap, on
  check_assembly's geometry (`CourtyardGrade.overlap_exact` at the state's
  poses), not the search's rects -- One-Air-Max read `0.4323 -> 0.4323` on
  pad boxes while four pairs gated; it now reads 13.4736. hpwl still ranks
  above it (run 4's order), so a re-seat that clears a pair at an hpwl cost
  is refused, visibly.
- **`place_reconstruct.py`** (`placement/reconstruct.py`) — the structural
  ("puzzle") solver: tier classification (frame -> anchors -> smalls),
  corner-inset pattern fit (propose-only), rigid ±v vector detection, ONE
  simultaneous candidate assignment as an Assignment-Problem-with-Conflicts
  ILP (`scipy.optimize.milp`/HiGHS; breakout-weighted descent fallback), and
  a minimal-move legalize sweep. Every stage is gated on the lexicographic
  tuple (pad conflicts, hole shortfall, pad off-board, overlap, hpwl), so the
  count gate cannot be satisfied by pushing parts off the board. Acceptance
  measured on the swap corpus: bare-board PAD-PAD 68 -> 0 in one solve with
  zero evacuation; on the correct control board it proposes nothing and moves
  0 parts. Zero-net pattern parts (two M3 holes) carry no net-anchor cost, so
  slot assignments used to be exactly degenerate and the solver picked
  arbitrarily — run 3 shipped the two repaired holes CROSSED, ~40 mm from
  home each, "mechanically equivalent" and recovery-visible (worth ~0.16 of
  recovery on that board). Run-4 F1 deliberately REVERSES the earlier
  position that this was acceptable: a scale-free nearest-slot tiebreak
  (`DIST_TIEBREAK_PER_MM`) now makes each pattern part take the slot nearest
  its current pose — equivalent to the board is not equivalent to recovery,
  and nearest is the minimal-perturbation choice. The assign stage also
  requires ≥2 distinct supporting refs per rigid vector (R4's own "two or
  more agree" letter) and runs a per-part revert sweep (`prune_assignment`)
  after acceptance, because the board-wide gate tuple cannot see an
  individual mis-move smuggled inside a hugely-improving set (run 3's J7:
  31.6 mm from home, worse than its 15.8 mm input). Candidates are
  translations at the part's own angle, with one exception (#1242): an
  edge-less receptacle's seats face out of the edge they sit on
  (`facing_seats`) -- a seat its strong mating face points away from is
  dropped and the facing quarter turn there offered as `(x, y, rot)`, which
  the ILP, the descent and the pair rows read through `cand_pose`.
- **`render_placement --legality`** (default ON) draws the defects the caption
  used to only count: conflict rings/links, NPTH keepout circles (the real
  keep-out since #761 — it drew the bare drill at `--clearance >= 0.20`,
  agreeing with the model's own blind spot), dashed-red off-board pad
  extents; caption gains `pad-conflicts` / `hole-conflict`.

Design lineage (see the session literature survey): Abacus/minimum-
perturbation legalization (Spindler 2008; Brenner 2012; Kahng-Markov-Reda
2004), conflict-directed repair scoping (FLOORIST, Moffitt 2006), assignment
with conflicts (Oncan 2019), breakout weighting (Morris 1993), frame-first +
largest-margin-first ordering (Wolfson 1988; Paikin & Tal 2015; regret-k,
Ropke & Pisinger 2006).
