# Grouped placement

A designer lays a board out by function. The USB connector gets its CC
resistors and series resistors right behind it. The regulator and its caps sit
between where power comes in and the parts it feeds. The MCU's crystal sits on
its oscillator pins, and the LEDs and the button go along an edge. Grouped
placement works the same way. The parts are sorted into **functional groups**,
each group gets a **region** of the board, and the groups are placed **one at a
time** or in one pass, each into its region.

The judgement and the geometry are split:

| step | who | tool |
|---|---|---|
| which parts belong together, and hints about where (an edge, a neighbour, a group to keep apart) | an agent, reading pin functions and datasheets | `groups.json`, checked by `py_tools/check_groups.py` |
| one region per group | computed | `py_placer/plan_regions.py` writes them as intent zones |
| the parts, each inside its group's region: in one pass, or group by group | computed | `place_seed --intent`, or `py_placer/place_groups.py`; `py_placer/place_candidates.py` runs both over several seeds at once and keeps the best |

The `/pcb-group-place` skill (`.claude/skills/pcb-group-place/SKILL.md`) runs
the three in order and has the agent look at each result.
`/pcb-free-agent` lists it as one of its levers.

## The groups file

```json
{"schema": 1,
 "groups": [
   {"name": "usb_in", "role": "interface", "refs": ["J1", "R2", "R3", "R4", "R5"],
    "anchor": "J1", "edge": "N", "near": ["core"],
    "why": "USB-C with its CC pull-downs and D+/D- series resistors"},
   {"name": "core", "role": "core", "refs": ["U3", "C3", "R1"], "anchor": "U3"}],
 "order": ["core", "usb_in"]}
```

- **`refs`**: every part the board does not lock must be in exactly one
  group. A locked part may be listed too: a connector placed first anchors its
  group's region.
- **`edge`**: one of `N`/`S`/`E`/`W`.
- **`near` / `apart`**: name other groups.
- **`order`**: optional. Without it, the group holding the core (`role:
  "core"`) goes first, then whichever group has the most signal links to what
  is already placed.

The format and its checks are in `py_placer/placement/grouping.py`.

`check_groups.py` refuses a file that cannot be placed:
- a part in no group, or in two;
- a ref the board does not have.

It warns about two things:
- **A part with more signal links to another group than to its own.** A hub
  (the anchor, or a part with 6+ pads) is exempt, and so are links to fixed
  connectors.
- **A cap the seeder's decap rule would claim at an IC of another group.** A
  crystal's load caps or an RC filter cap belong with their crystal or button,
  so `plan_regions.py` exempts such caps from the decap rule.

**Rails** are left out of every connectivity question here and in the seeder's
targeting: a ground net, or a net reaching more than a quarter of the board's
parts (`grouping.rail_net_ids`). On a small board GND and the main supply
reach most parts, and otherwise they make every part look like it belongs
everywhere.

## Regions

`plan_regions.py` works on the board's **free space**, per face, on a 0.25 mm
grid:
- what is left of the outline, inset by the edge clearance, after the locked
  parts;
- a locked part takes its own face by its courtyard and the other face by its
  drilled pads;
- slivers narrower than 1 mm are removed, because the gaps between a back-side
  header's pins hold no part.

It then searches **slicing floorplans** of that space: horizontal and vertical
cuts, one group per leaf.
- **Cuts:** each is placed so that the free area on each side is proportional
  to its groups' demand (their parts' courtyard area over `--util`, default
  0.55).
- **Search:** up to 6 groups, every distinct floorplan is evaluated (4, 36,
  528, 10,800 and 283,680 of them for 2-6 groups). Beyond that, a seeded
  annealing search over the same trees.

The cost:
- **wiring**: links between groups, and from each group to the pads of the
  fixed parts it wires to, priced by Manhattan distance between regions;
- **spread**: each group's internal links times the span its parts' cluster
  takes in that region shape, so a group is not stretched into a strip;
- **anchor**: a group stays touching its own fixed members;
- **hints**: `edge`, `near` and `apart` are priced as costs;
- **hard checks**: a region must hold its largest part at some quarter turn,
  and its parts' area, in its free cells.

Each region is written to the intent as a `blocks[]` entry: `zone`, `side`,
`tolerance_mm` (0.5) and a `context.grouped_placement` record. Neighbouring
zones are 0.6 mm apart (`--gutter`). `check_floorplan --plan-only` grades the
plan before any part moves, and `--png` draws it.

## Placing into the regions

Two flows fill the same regions.

**One pass.** `place_seed --intent regions_intent.json` seats every group at
once, each part held to its group's zone. It is the seeder as it always was;
the regions are the only new input.

**Group by group.** `place_groups.py` runs `place_seed` once per group, in
order. Each step runs on a **step board** built from the board so far:
- the groups not yet placed are **deleted**, so they are neither obstacles
  nor connectivity partners, and no pile pulls a seat off the board;
- the groups already placed are **locked**, on the step board only.

The intent is trimmed to the parts the step board has, and each step names
its own group as the decap owners (`place_seed --decap-owners`):
- **The decap pin stages claim a cap only at the group's own chips.**
  Otherwise a cap on a shared rail is claimed at whichever chip of an earlier
  group carries it. Measured on esp_prog: the step for the LDO's group had
  the CH340 already placed on +3.3V. Exempting the cap from the decap stage
  instead sent it through the zone pack, which does not see another
  footprint's graphic copper, and onto the LDO's SOT-89 tab.
- **A cap the pin stage declines is put back at the group chip's supply
  pin**, clamped into the zone, not at the zone's centre. The pin stage
  claims one cap per supply pin, so a regulator with more caps than pins
  declines the rest, and a group's zone is sized for the whole group.
  Measured on ch32v006_dev, seed 0: one of the LDO's five caps went from
  8.2 mm to 4.0 mm from it.

The poses are copied back, and the whole board is graded against the full
intent: `floorplan.grade`, `check_assembly`, and the airwire crossings and
HPWL the optimizer prices. An error on a part locked in the input board is
listed apart (`floorplan_errors_pinned`) and does not fail the run, as in
`place_seed`. `--final-polish MM` adds one bounded `place_optimize` pass,
kept only when it improves crossings without losing anything else.

## Results

Measured 2026-10-15 on three human-placed boards of the stress corpus, from a
pile with their connectors fixed at the human poses. The agent wrote each
board's groups, and `plan_regions.py` planned the regions. There were five
seeds per flow. Every placement was routed by the board's own recorded corpus
chain, ending in `route.py`, and scored by `board_score`. Each cell is the
mean `blocking` (unrouted, broken, DRC and assembly), then the mean via
count:

| board | all at once, no regions | regions, one pass | regions, group by group | the human placement |
|---|---|---|---|---|
| ch32v003_usb (25 parts) | 1.8 / 95.4 | 1.0 / 55.8 | 0 / 47.6 | 0 / 36 |
| esp_prog (21) | 1.0 / 41.0 | 0 / 35.0 | 0 / 37.2 | 0 / 38 |
| ch32v006_dev (26) | 1.0 / 78.4 | 0 / 58.4 | 0 / 65.4 | 0 / 82 |

The human column is the human's placement, routed by the same chain.

- **The regions are the gain.** On all three boards both grouped flows route
  with less blocking and 10-50% fewer vias than placing everything at once.
  On esp_prog they route with as few vias as the human placement, and on
  ch32v006_dev with fewer; on ch32v003_usb they still use more.
- **Neither flow wins everywhere.** Group by group is ahead on ch32v003_usb,
  where it is the only flow with no blocking in any seed. One pass is ahead
  on the other two. Both take seconds (the loop 2-9 s, one pass 0.5-4 s), so
  `/pcb-group-place` runs both and keeps the better.

## Crystals

A crystal and its load caps belong at the IC's oscillator pins, and nothing
in a netlist says so: a 3 mm crystal loop and a 30 mm one have the same
connections. `plan_regions.py` therefore writes, for each passive crystal
(`placement/crystals.py`, which reads the crystal and its IC off
`placement/emc_roles.py`), two kinds of `proximity[]` claim:
- **the crystal**: each signal pad within 8.1 mm of the IC pin on its net;
- **each load cap** (a two-pad cap from a load net to ground): its load-net
  pad within 2.2 mm of the crystal pad on that net.

The limits are the human corpus's 75th percentiles, measured the way the
claim grades them, pad edge to pad edge (`tests/stress/placement_survey.py`:
258 crystals, median crystal leg 5.4 mm, median load cap 0.8 mm). They guard
against drift. The placing is done by the seeder:
- **A claim's subject is seated right after its partner**, nearest the
  partner's declared pads (the followers stage of `seeder.seed_from_intent`):
  the crystal right after its IC, each cap right after the crystal.
- **The IC is turned for its crystal when it must be.** Among the rotations
  that seat the crystal within 6 mm of its pins (pad centres; the corpus
  median), the IC keeps the one its own connections prefer. Turning the IC
  for the crystal alone cost esp_prog 13 routed vias.
- **The claimed parts sit in no zone.** The regions are planned on the
  groups as written, and each claimed part stays inside its IC's zone, so it
  takes no room another group's region was planned for. Moving the crystal
  into its IC's group before planning re-planned the whole board, and cost
  ch32v003_usb 22 routed vias.

`check_groups.py` names each crystal it finds; `--no-crystals` on both tools
writes no claim. Measured as above (five seeds, routed by each board's
recorded chain), with the farther crystal leg pad centre to pad centre:

| board | crystal leg, before | with claims | human | blocking / vias, one pass, before -> with | group by group, before -> with |
|---|---|---|---|---|---|
| ch32v003_usb | 3.0-6.9 mm | 4.0-6.4 mm | 2.7 mm | 1.0 / 55.8 -> 0.2 / 64.4 | 0 / 47.6 -> 0.2 / 56.4 |
| esp_prog | 7.3-13.5 mm | 3.1-4.9 mm | 5.5 mm | 0 / 35.0 -> 0 / 32.4 | 0 / 37.2 -> 0 / 33.4 |
| ch32v006_dev | 6.7-12.7 mm | 3.9-9.5 mm | 4.5 mm | 0 / 58.4 -> 0 / 76.8 | 0 / 65.4 -> 0 / 70.2 |

The crystals now sit where a human puts them. The routing cost is mixed:
fewer vias on esp_prog, more on the other two, inside a seed-to-seed spread
of about 15 vias.

## Indicator banks

An LED and its series resistor belong side by side, and a row of indicators
belongs in a row. `plan_regions.py` writes each bank of them as one intent
`arrays[]` row (`placement/group_rows.py`; `--no-arrays` writes none):

- **a channel** is an LED and the one resistor it shares a net with that no
  other pad is on;
- **a bank** is two or more channels in one group's region, with one LED
  footprint, that share one outer net (the rail) while each has a signal of
  its own. A region holding banks on two rails gets two rows;
- **the row alternates LED and resistor**, in the LEDs' order, so each
  resistor stands beside its own LED (`allow_mixed`, order `declared`). The
  seeder seats the row whole and chooses its axis and angle; the polish moves
  it as one piece;
- **the row must fit its region** (`plan_regions.fit_banks`): at the seeder's
  own pitch, in the free space the plan planned on (its grid, the locked
  parts and the mounting holes taken out). A bank that does not fit is
  written as the fewest rows that do, side by side, its channels split in
  order and balanced, two or more per row (`group_rows.split_bank`); a bank
  no split fits is left out, with a note. Without this, such a row was
  declared, never seated -- every member seated alone after the search ran
  out -- and graded as an `array_formation` error. On glasgow_revC the
  connectors J3 and J4 cut into the IO_Banks region and leave no strip the
  16.5 mm row needs, so it is two rows of three and two LEDs, and all three
  of the board's rows now form; tigard's 2.45 mm UI strip holds no row of
  its bank at all. The free space is the seeder's own (a locked part's
  courtyard on its face, its drilled pads' boxes on the other, no
  container), on a 0.05 mm grid: a row can fill its zone to within 0.2 mm.
- **the seeder searches the whole zone** for a zoned row: only anchors inside
  the zone (plus tolerance), and the zone swept after the 1 mm ring, where
  an unzoned row sweeps the board. The rings reach 30 mm from the row's
  target, and before this a row whose only strip lay further along a long
  zone was never found: ch32v006_dev's bank ran out of poses, and now
  forms.

Rows of like VALUE are not taken. The array detector
(`check_floorplan --suggest-arrays`) groups parts by footprint and value, and
like value is not like function: ft2232h_jtag's `U4:1k` row is a reset
pull-up and four LED resistors, and a decoupling cap belongs at its own pin.

A zone's row is seated before the zone's other members, since it needs a
contiguous strip -- except a member at least as big as the whole row, which
is seated first. Otherwise a row of 0603s could take the strip a switch
needed: ch32v003_usb's SW1 was left off the board under its LED bank.

Measured from the same regions with and without banks, every candidate
(three seeds of each flow) routed by the board's recorded chain:

| board | bank | blocking, without -> with | vias (mean) | copper (mean) |
|---|---|---|---|---|
| ch32v003_usb | 2 LEDs | 0 -> 0 | 60.7 -> 60.5 | 379 -> 384 mm |
| ch32v006_dev | 2 LEDs | 0 -> 0 | 72.8 -> 70.8 | 684 -> 699 mm |
| ft2232h_jtag | 5 LEDs | 0 -> 0 | 92.7 -> 81.7 | 953 -> 843 mm |

ft2232h_jtag's bank was better on all six candidates; ch32v003_usb's is
within the seed-to-seed spread. ch32v006_dev's bank never formed as a row
in that measurement (its members were seated one by one), so its row
measured the spread too.

## Mounting holes

A mounting hole's position is a mechanical decision, so `/pcb-group-place`
locks every hole before it plans. When a hole is left unlocked on a pile,
its input pose means nothing, and the seeder used to place it like any other
part: inside its group's region, often in the middle of the board, or off the
board when the region had filled.

Humans have one strong habit here. On the human-placed boards of stress sets
1-30 (`tests/stress/placement_survey.py report`), all four holes sit within
8 mm of a corner on 86 of the 125 boards with exactly four holes. On boards
with one to three holes, or five or more, no hole count reaches 30%. So
`plan_regions.py` acts only on a pile with exactly four holes
(`placement/mount_holes.py`):

- each unlocked hole gets a free corner (one no locked hole already holds)
  and the legal pose nearest it, found by the seeder's own seat search. A
  pose outside the corner's quarter of the board is refused;
- the pose is written as a `fixed_poses[]` row of basis `derived_default`, so
  the seeder seats and locks the hole before any group. The group-by-group
  flow seats it in its first step;
- the regions are planned around the holes, and no hole is zoned in a block;
- a WARN line names the holes, with the `place_pose` command to lock them
  where the enclosure really needs them. `--no-mount-holes` writes none.

On glasgow_revC (groups by schematic sheet), from the same regions with and
without the holes, six candidates each:

| | holes unhandled | holes at the corners |
|---|---|---|
| one-pass candidates buildable | 0 of 3 | 3 of 3 |
| group-by-group candidates buildable | 3 of 3 | 3 of 3 |
| floorplan errors, one-pass / group-by-group | 107-114 / 120-128 | 105-107 / 100-107 |
| kept candidate: errors, crossings, HPWL | 120, 4303, 6470 mm | 100, 4503, 6276 mm |

Before, all four holes ended up in the board's interior. Now each stands
6-16 mm in from its corner, beside the locked connectors that fill the
corners there. The human put them 4 mm in, where their 10 mm hole courtyards
overlap the connectors' courtyards.

## Time

Every tool step `/pcb-group-place` runs, excluding the agent's own time, on
one laptop (an M3 MacBook Air): reading the board, emitting the intent,
checking the groups, planning the regions, the candidates through
`place_candidates.py` (four at a time), the render and `check_assembly`:

| board | parts | candidates | region plan | placing | total | before (f6af54ae) |
|---|---|---|---|---|---|---|
| soldercad_1 | 10 | 6 | 0.2 s | 1.8 s | 3.3 s | -- |
| esp_prog | 21 | 6 | 0.2 s | 2.4 s | 3.8 s | 4.1 s |
| ch32v003_usb | 25 | 6 | 0.3 s | 3.5 s | 5.3 s | 10.4 s |
| ch32v006_dev | 26 | 6 | 4.4 s | 5.4 s | 11.5 s | 14.9 s |
| ft2232h_jtag | 41 | 6 | 0.4 s | 7.4 s | 9.5 s | 9.5 s |
| tigard | 92 | 6 | 1.3 s | 13.2 s | 17.9 s | 28.8 s |
| glasgow_revC | 272 | 6 | 0.7 s | 56.5 s | 66.4 s | 114.8 s with 2 |

`place_candidates.py` places seeds 0-2 of each flow, and seed 0 alone on a
board of more than 300 movable parts. glasgow_revC's six candidates take 57 s
(two took 97 s at f6af54ae), and its best came from the second seed of the
group-by-group flow. (Its groups are its schematic sheets, standing in for an
agent's.)

### Where the time goes, and how it scales

A candidate seeds every part, then polishes. On glasgow_revC's one-pass seed
(seed 0) that was 85 s: 68 s of polish and 14 s of seating. It is now 16 s:
10 s of polish, 3 s of seating, 1 s of parsing, the rest grading and writing.
Each stage, and what it now grows with:

- **The polish** prices about 120 poses per part per pass by the airwires of
  the part's nets. A ground net made every pose cost as much as the board:
  its whole tree rebuilt, every edge tested against every other airwire. Now
  a pose pays for the few tree edges it changes, the crossing test and the
  tree run in Rust (the Python loops' own arithmetic, so the same counts),
  and a very big net's tree is built over its Delaunay edges (see "How the
  quench prices a pose" in `py_placer/placement/README.md`). A pass costs
  about parts x ground pads x a small constant. One pass from the human
  placement:

  | board | parts | ground pads | f6af54ae | now | peak memory now |
  |---|---|---|---|---|---|
  | glasgow_revC | 272 | 293 | 14.8 s | 3.0 s | 98 MB |
  | ddr4_rdimm_tester | 691 | 833 | 255 s | 42 s | 587 MB |
  | stm32h7_hdmi | 835 | 601 | 162 s | 27 s | 347 MB |
  | thunderscope | 1022 | -- | -- | 23 s | 170 MB |
  | kintex_410t_devboard | 1020 | 1136 | -- | 69 s | 719 MB |
  | jetson_agx_thor_bb | 1172 | -- | -- | 54 s | 677 MB |

  Whole runs, parse and write included: ddr4_rdimm_tester's file is 54 MB
  and kintex's 75 MB, and reading one costs 5-7 s (8-11 s before each
  footprint's blocks and shapes were found once per parse). Measured with
  `place_optimize --max-passes 1 --max-displacement 3`; peak memory is
  `/usr/bin/time`'s maximum resident set.
- **Seating** walks candidate poses nearest-first and keeps the first legal
  one; a part with no room anywhere walks them all, and its blocker census
  walks its zone again per neighbour. Most of those poses overlap a seated
  part or leave the board, which a grid and the board's box decide for 512
  positions at once before the full legality test. glasgow_revC's MK2, a
  mounting hole with no room, cost 18 s and now costs under 1 s; the poses
  seated are the same. Seating is about linear in the parts.
- **The region plan** tries every floorplan up to six groups on a face and
  anneals for a fixed 20,000 evaluations above that, so it is bounded:
  ch32v006_dev's six groups are its slowest (283,680 floorplans in 4.4 s;
  each split is laid out once per box, and each group's own cost terms once
  per cell rectangle).
- **Group by group** runs `place_seed` once per group on the whole board so
  far, so it pays a whole-board setup per group (parse, the plan check, the
  seeder's and the polish's states, the grade): about 2.4 s at 264 parts,
  about groups x parts. glasgow_revC by its four sheets takes 33 s (82 s
  before), split into 16 groups 65 s (122 s).
- **Grading.** `check_assembly --baseline` used to grade the starting pile's
  overlaps too, to tag each pair of the placed board new or inherited. A
  pile's overlaps are staging (#1103), so none is inherited; skipping them
  took the check on glasgow_revC from 9.4 s to 1.1 s. Each candidate is
  graded once.

What the speed did to the placements: the shortcuts in seating, grading,
parsing and writing are exact (the same boards, byte for byte where
checked). The polish's pose pricer is exact but for ties: where pads sit on a
regular grid, several trees are equally short, and it may pick another than
the full rebuild did. Graded on the seven boards above against f6af54ae, the
best candidate was better on 2, worse on 1 (ft2232h_jtag: the same crossings
and errors, 1.3 mm more HPWL) and the same on 4. In the quench A/B table
(`tests/test_placement_ab.py`) 7 of 79 rows moved by a few crossings or
inversions either way; two marks moved with them -- intent-rp2350 on its
crossings guard, and the rejected body-watchy row, whose two arms now make the
same moves -- and the baseline was re-recorded.

## Where humans put each group

The priors the agent's hints follow come from a survey of the 437
human-placed boards of the stress corpus (`tests/stress/placement_survey.py`;
the numbers are in `.claude/skills/pcb-free-agent/references/part-roles.md`).
For example:
- A power conditioner is no closer to the board edge than an average part. It
  sits by its power entry: on the median board only 16% of parts are nearer
  that connector than the regulator is.
- ESD parts sit at their connector, and the main IC is central.
