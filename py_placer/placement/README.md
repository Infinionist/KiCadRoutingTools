# Placement

Places a KiCad board by function: the parts sorted into groups (a judgement,
usually an agent's), one objective lowered by simulated annealing, and exact
legality at the end. `place_simple.py` is the placer; the rest of this module
is the state it works on (`quench.QuenchState`), the legality model every
tool shares, the floorplan intent and its grade, the pose tools, and the
fanout-time decoupling-cap repair the routing chain runs. The workflow --
group, place several seeds, fast-route each, keep the best, route; with
`--rounds N` also review, change the inputs and place again -- is the
`plan-pcb-placement` skill (`.claude/skills/plan-pcb-placement/`).

## place_simple.py — groups, one objective, annealed

```bash
python3 -X utf8 py_placer/place_simple.py board.kicad_pcb placed.kicad_pcb \
    --groups groups.json --intent intent.json [--seed 0] [--json report.json]
```

- **Inputs.** The groups file (`py_tools/check_groups.py` checks it;
  `grouping.load_groups` reads it): `{"schema": 1, "groups": [...]}`, each
  group a `name` and its `refs` (fixed parts may be listed), with optional
  `role`, `anchor`, `why`, `side` (`F`/`B`), `edge` (`N`/`S`/`E`/`W`) and
  `near` / `apart` (lists of other groups' names); every movable part in
  exactly one group. And the emitted intent
  (`check_floorplan --emit-intent`): its `proximity[]` claims, keep-outs,
  zones, declared faces and `fixed_poses[]`.
- **What stays put.** Parts the board locks (`(locked yes)`) and parts the
  intent seats at a declared pose. Faces the intent or a group's `side`
  declares are applied first, by the writer's flip; a contradiction, or a
  locked part declared elsewhere, is refused with nothing written (exit 2).
- **Groups, not regions.** A group gets no region and no shape. Its gap tree
  (the spanning tree over its members' courtyards, each edge priced by the
  gap between two courtyards) is a cost term, so a group stays in one piece,
  whatever shape the free space gives it. A crystal, its load caps and the
  clock group around them are folded into their IC's group
  (`grouping.fold_followers`), and claimed at its oscillator pins
  (`crystals.py`).
- **The objective** (`simple.py`): each signal net's Manhattan spanning tree
  over its pads; rails at 0.3 and ground at 0.02 of a signal per mm;
  crossings among the signal trees (x2); claims; each decap's distance to the
  supply-pin site of its own group's IC it is matched to, a rail's decaps
  spread one to a site (x2); group gap trees (x5); the groups file's `near` /
  `apart` / `edge` hints; a halo in front of a fine-pitch part's pads (8 or
  more connected pads at a median pitch of 1.0 mm or less: a QFN, QFP, BGA
  or fine-pitch connector), a strip 0.25 mm deep per non-ground net leaving
  each side its pads face, which a part on the same face pays to enter
  unless it is a small part (4 pads or fewer) wired to the chip; a chip's
  corners stay free; tree edges over foreign pads (x2, a through-hole pad
  twice); RUDY congestion on a 0.5 mm grid (x5). Every weight is a flag.
- **The anneal** (`simple_anneal.py`): every movable part starts at a random
  place on the board; moves shift, turn, swap, jump to a wiring target, dock
  against a partner, shift, turn or swap whole groups, and escape (an
  overlapping part to the least-overlap of 48 random poses); a move never
  leaves the board. An overlap penalty (courtyards, the outline, the intent's
  keep-outs and zones) rises from soft to hard over the run, only an
  overlap-free state can become the best, and the best is held to the exact
  pad legality (`seeder.pose_ok`), a part failing it moved to the nearest
  legal pose. A run that still leaves a part with no legal pose is annealed
  again from a fresh start, up to two more times (`RESTARTS`); the first
  legal placement is kept, else the run that left the fewest parts illegal
  (the report's `runs` counts the anneals), and exit 4 names those parts.
- **Local pricing.** A move re-prices only the moved parts and the nets they
  are on, against the committed state near them (`simple.SpatialIndex`),
  with the "before" read from cached breakdowns; the inner loops are Rust
  kernels (`grid_router` 0.23.3: `mst_l1`, `seg_rect_hits`, `seg_crossings`,
  `seg_cross_pairs`), each the Python reference in `simple.py` operation for
  operation. 500 moves per movable part (`--moves`): ch32v003_usb's 22
  movable parts in about 4 s.
- **Deterministic.** The same board, inputs and `--seed` give the same
  placement. Seeds differ more than most input changes: place several and
  judge them by routing.

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
opt-in verb, and nothing reads it by default.

**Declared in the intent (`replicate[]`, reader 11).** The same copy as a
design declaration: `{"from": "CH1", "to": ["CH2", "CH3", "CH4"], "anchor":
"J1"}` plus the verb's own settings (`allow_net_mismatch`, `only` / `skip`,
`search_mm`). `place_pose replicate --intent PATH` (no `--from`/`--to`)
applies them; `place_simple` does not, so arrange and lock the source
instance, apply the copies, lock them, and place the rest. check_floorplan
grades each target (`replicate`, WARN): parts matched as the verb matches
them, offsets fitted by their median, each part's residual from that fit.

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

It is opt-in.

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

## Placement blocks (`groups.py`, #459)

Which parts belong together by their SOURCE -- KiCad groups, schematic sheets,
net-name prefixes, decap tethers. `route.py --group BLOCK` scopes a routing run
to a block's nets (below), and `render_placement --group-by / --zoom-group`
draws them. (The placer's own groups are the groups file, `grouping.py`.)

Sources, in precedence order, first match wins per part (a part is in at most one
block):

| source | what it is | corpus evidence |
|---|---|---|
| `kicad` | KiCad `(group ...)` blocks | **0 of 27** in-repo boards have one. Exact when present; verified against a synthetic fixture. |
| `sheet` | schematic sheet path | **12 of 22** boards with `(path ...)` have >1 sheet. `ulx3s`: 10 blocks sized 83/34/23/20/20/12. The workhorse. |
| `netprefix` | net-name prefix (`AUDIO_*`) | The **weakest** source. Raw prefixes are dominated by KiCad's auto-generated `Net-(U1-Pad)` names — one bogus bucket of up to 92 refs spanning 75mm — so those and power nets are excluded and a 20mm coherence gate applied. What survives is small but real: ulx3s 9 blocks, tigard 0. |
| `decap` | 2-pad caps tethered to their IC | Strong. A cap sits 0.0–2.6mm (median) from the nearest IC and **shares a net with it 93–100%** of the time; the net check rejects the rest (13 of ulx3s' 70). |

`--group-by auto` means `kicad,sheet`. Default is `none`.

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

`placement_state.assess_placement` measures two board states a placement tool
must know about. `board_brief` reports both (`pile`, `has_copper`);
`check_floorplan` refuses each with **exit code 3** unless told
(`--allow-unplaced`, `--allow-routed`) -- `0` ok, `1` crash, `2` argparse, `3`
"the board is not in a state this tool can work on", a distinct code so a
caller branches on the number rather than scraping text -- and `place_pose`
refuses a routed board the same way.

| state | why it matters | override |
|---|---|---|
| **unplaced** (parts stacked at one coordinate, or piled off the outline) | nothing read off the parts' poses means anything: an intent emitted from a pile records the pile as the spec | `--allow-unplaced` |
| **already routed** | `writer.write_placed_output` rewrites footprint poses and -- since #714, on request -- their board SIDE, but never copper. Every track would be left behind, detached from its pad | `--allow-routed` |

`render_placement.py` WARNS and renders instead: being able to SEE an
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

A placement is judged by routing it. Before it is routed, its own numbers
(`crossings`, `hpwl`, the placer's cost) are indifferent between a sensible
layout and a scattered one with the same wirelength. Nothing declares where
parts *belong*, so nothing can check whether they went there.

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
  there, at zero. The grade, feasibility and the plan checks open the same
  side. Emitted zones are clamped
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

`py_tools/board_brief.py` reports which parts look position-critical, with a
reason each (its `locks` line; `placement.lock_advisor.advise_locks`). It
**never locks anything**: a wrong auto-lock silently freezes a part that
needed to move, and the run just quietly does less. Lock a part with
`place_pose ... lock REF`.

Geometric rules (NPTH, outline overhang, edge proximity) measure the board.
Lexical ones (footprint name, reference prefix, pin function) guess from names
and are asymmetric: near-exact on stock KiCad libraries, silent on a house
library like `interf_u:PGA120`. False negatives are common, false positives
rare, and the output says so -- treat a quiet result as "nothing detected", not
"nothing to lock". A ref matched by rules from two different evidence channels
promotes to high. High-pin parts are an advisory: "large" is not
"position-critical".

## Reviewing a placement: render it (#431)

```bash
python3 py_tools/render_placement.py placed.kicad_pcb --before seed.kicad_pcb -o delta.png
python3 py_tools/render_placement.py board.kicad_pcb --list-groups --group-by sheet
python3 py_tools/render_placement.py board.kicad_pcb --zoom-group sheet:58d913ec --per-side -o out/
```

**The render is triage, not a verdict.** The caption's numbers say what is
illegal; a placement is judged by routing it. Do not judge a placement by how
much moved -- "lots moved, looks broken" and "barely moved, looks safe" are
both wrong.

### What it renders

Every panel below is `render_placement.py` output, and every one carries a
metrics caption — the render is triage, the caption carries the numbers.

**The placement diff, drawn.** `--before` turns on the dashed seed rects and the
displacement arrows, so "what moved and how far" is the picture rather than a
diff of two files. 22 parts here; `C4` travelled 95.65 mm.

```bash
python3 py_tools/render_placement.py placed.kicad_pcb --before seed.kicad_pcb -o delta.png
```

![placement delta](../../docs/placement-delta.png)

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
| ![front](../../docs/placement-side-F.png) | ![back](../../docs/placement-side-B.png) |

**Zoom to one placement block.** `--zoom-group` takes the same block names as
`route.py --group` — `--list-groups` prints them with their part and net counts.

```bash
python3 py_tools/render_placement.py board.kicad_pcb --list-groups --group-by sheet
python3 py_tools/render_placement.py board.kicad_pcb --zoom-group 58d913ec --group-by sheet -o blk.png
```

![group zoom](../../docs/placement-group-zoom.png)

**An unplaced board.** The placement CLIs refuse this one (exit 3); the renderer
warns and draws it anyway, framed to the PARTS rather than the outline — a pile
at the origin would otherwise render as a dot in the corner of an empty board.
13 footprints at 1 distinct position, and `overlap 792 mm²` gives it away.

![unplaced](../../docs/placement-unplaced.png)

**Reference designators.** On by default, and sized to the PART rather than to
the image — scaling by image height alone gives an 8px label at any zoom:
drawn, technically present, and unreadable. A part too small to letter is
skipped, and its arrow carries the meaning instead.

![labels](../../docs/placement-labels.png)

**Every toggle, on one board.** The same 13-part delta, one flag at a time:

![toggles](../../docs/placement-toggles.png)

`--no-delta-first` looks unchanged there for a real reason: 12 of the 13 parts
moved, so there is no context left to dim. It matters on a board where the delta
is a genuine subset — which is also where `--ratsnest-all` earns its keep:

![ratsnest](../../docs/placement-ratsnest.png)

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

![named nets](../../docs/placement-ratsnest-nets.png)

The matching goes through `net_queries.matches_net_filter` — the same helper
`route.py --nets` and the plan executor use — so a pattern means here exactly
what it means there. A pattern that matches nothing says so on stderr rather
than rendering an empty ratsnest, which would read as "this net has no airwires".

`--focus` adds one cropped panel per failed-net cluster.

## Module layout

| File | Purpose |
|------|---------|
| `simple.py` | The placer's cost model (`../place_simple.py`): signal and rail trees, crossings, claims, decaps, group gap trees, the groups file's hints, halo, foreign-pad hits, congestion -- priced locally per move (`SpatialIndex`, cached breakdowns, Rust kernels) |
| `simple_anneal.py` | The placer's optimiser: simulated annealing on `simple.py`'s exact cost from a random scatter, soft overlap rising to hard, seeded and deterministic |
| `quench.py` | `QuenchState`: the parts, their poses and geometry at every quarter turn, the board, the intent's keep-outs and zones, and the airwire, halo and edge pricing helpers every placement tool builds on |
| `grouping.py` | The groups file: its schema, the board's rails, cohesion and decap checks, and folding a crystal into its IC's group (`check_groups.py`) |
| `crystals.py` | Crystal claims: each passive crystal (`emc_roles`) within `LEG_MM` of its IC's oscillator pins and each load cap within `CAP_MM` of the crystal pad on its net, as `proximity[]` rows -- the human corpus's 75th percentiles (`tests/stress/placement_survey.py`) |
| `seeder.py` | The seat predicate `pose_ok`, the wiring target, the fixed-pose checks and the `(locked yes)` stamps |
| `pose_ops.py` | The engine half of `place_pose.py` (#892): resolve, write, re-grade and snap one arrangement |
| `replicate.py` | The engine half of `place_pose replicate` (#1211): one sheet channel copied onto its sibling instances |
| `portfolio.py` | `copy_siblings` (a written board's sibling files) and `ignore_net_ids` |
| `../../py_router/group_routing.py` | Block → net scoping, and the undo, for `route.py` (#459) |
| `fanout_clearance.py` | Post-fanout decoupling-cap clearance repair (#130) |
| `groups.py` | Placement blocks: which parts belong together by source (#459), and decap owners |
| `routability.py` | Block displacement, corridors, net affinity — the geometry behind `floorplan.grade`'s health block |
| `reconstruct.py` | Tier classification and mounting-hole pattern fits, for `floorplan.emit_intent` and the lock advisor |
| `reachability.py` | Whether a pose is reachable at all (`check_reachability.py`) |
| `labels.py` | Reference-designator silkscreen geometry (`beautify_labels`, #481) |
| `floorplan.py` | Floorplan intent: declare where things are supposed to go, then grade it (#549). A checker; it authors no geometry |
| `design_brief.py` | The design brief (#711): what a board is for, compiled into the intent (`docs/design-brief.md`) |
| `reconcile.py` | Reconciles every ref two channels declare, and reads `mechanical.json` into grade-only anchors (#959) |
| `arrays.py` | Declared `arrays[]`: the formation predicate and the pin order (#1051) |
| `edge_facing.py` | Connected pads on a row facing the outline with nothing beyond it (`placement_score`'s `edge_facing`) |
| `pin_rows.py` | Whether a track can cross a through-hole pin row (#1196) |
| `escape.py` | Per-face escape-lane ledger: can the nets on this part's face get OUT? |
| `pair_order.py` | Pin-order inversions between connected part pairs. A LOWER BOUND, not a proxy |
| `part_class.py` | What determines a part's position — a pose-INDEPENDENT class |
| `roles.py` | What a part IS (regulator, crystal, transistor, ...), read off footprint name, pins and prefix: the table `board_context` prints and the decap owner predicate (`groups.supply_owners`, #1249) reads |
| `emc_roles.py` | Converters, crystals, oscillators and ESD arrays by pin function, each leg measured pad edge to pad edge, with a ready `proximity[]` row (`max_mm` null) -- `board_context --json`'s `emc_roles` (#1254). Also PROPOSES design-brief `keep_away` rules: each crystal's or oscillator's clock nets against the switch nodes, at a 3 mm derived-default gap, with a `brief_row` to paste (#1253). Report only |
| `options.py` | When a board cannot hold its parts, say so WITH THE NUMBER and the options |
| `provenance.py` | Which registered tool wrote each pose (`declare_lever`) |
| `lock_advisor.py` | Which parts should not be moved, and why (#431). Advice only |
| `placement_state.py` | Board-state gates: unplaced, and already-routed (#431) |
| `cli_gates.py` | argparse shared by the placement CLIs so they cannot drift |
| `../../py_tools/render_placement.py` | Headless PNG stills of placement status (#431) |
| `legality.py` | Hard constraints every tool shares: board side, real Edge.Cuts containment, pad and drill legality, and the graders (#456) |
| `body.py` | THE body model (#896): courtyard -> fab -> silk U pads -> pad bbox, with the occupancy rect and the source that answered; a fab MARKING (under 2% of the pad box) is never the body (#1201) |
| `mating.py` | Which way a connector OPENS (#1155): its mating face, read off where the body sits relative to its pad row. The edge rule reads it through `floorplan.edge_facing` |
| `connector_geometry.py` | The DRAWN connector envelope against a declared compass edge (#961): the number an `overhang_mm` band is graded on |
| `parser.py` | Courtyard, fab, silk and locked-footprint extraction |
| `writer.py` | Writes new positions/rotations (rotates pad angles with the footprint, as KiCad stores pad angle = footprint + pad rotation), and flips a part between faces (#714). Resolves blocks through `kicad_parser.iter_footprint_blocks`, so one placement moves ONE block even when two share a reference (#726) |
| `utility.py` | Shared utilities (bbox from pads) |

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
placement state spaces a courtyard-less part by its pad box, while
check_assembly grades each moved neighbour against its body. For a part
nothing moves, the body is the board's: `quench.locked_body_locals` gives
each locked part whose occupancy comes from a `.Fab` body (no silk outline,
no marking, no container) that occupancy as its seat rect. On ulx3s the
ESP32 module U9, locked, draws a fab body 4 mm past its pads at the antenna
end, and six parts seated there had made the placement NOT BUILDABLE.

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
caught: such a jack can be seated opening into the board, and the facing
conjunct then reads pass.

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
`+marker`); contradicting, it makes it abstain, never fail. Calibrated on
KiCad 10's 154 marked stock
footprints: 73 of 73 strong body faces agree, 75 weak ones agree, one weak
one is vetoed (the RaspberryPi Pico W module's RF-edge marker), and no
reading moves when the footprints are saved at 90, 180 or 270. The three
wrong-faced stock jacks above draw no marker, so it does not reach them.

One predicate, every consumer: `floorplan.edge_facing` is what
`rule_edge_connector`'s facing conjunct grades, what `emit_intent` withholds
a reversed edge by. A reversed seat is still possible -- a declared angle is
kept, and the wrong weak face above misleads the grade too -- and the grade
reports the
first two. `board_context`'s "Mating face" reads the same primitive. B-side parts need
no extra mirror: the board file stores their local coordinates mirrored
already. Census: every decisive receptacle on the tracked and fa10 designer
boards faces the edge it sits on.

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
  eighth conjunct, absolute. The seat predicate (`container_pin` veto) and the
  fixed-pose checks ask `CourtyardCensus.pin_hits`, the grader's own function.
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

Courtyard extraction (`parser.py`) reads `fp_line`/`fp_rect`/`fp_arc`/
`fp_circle`/`fp_poly` per side. A footprint with no courtyard on any layer falls
back to its pad bounding box — which carries no courtyard margin at all, so the
part is modelled smaller than it is; that fallback now warns and names the refs.

## Pad+drill legality layer

Added after two evaluation runs measured the failure modes directly (parts
walked onto a locked connector's pad, parts left off-board, parts on NPTH
holes):

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
- **The seat gate** -- `candidate_valid` carries the pad gate (and, since
  #1257, refuses pad copper past the outline for a part whose pads reach past
  its courtyard -- veto `pad_copper_outline`); zero-pad footprints with a
  courtyard are static obstacles.

- **`render_placement --legality`** (default ON) draws the defects the caption
  used to only count: conflict rings/links, NPTH keepout circles (the real
  keep-out since #761 — it drew the bare drill at `--clearance >= 0.20`,
  agreeing with the model's own blind spot), dashed-red off-board pad
  extents; caption gains `pad-conflicts` / `hole-conflict`.
