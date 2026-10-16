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
| the parts, each inside its group's region: in one pass, or group by group | computed | `place_seed --intent`, or `py_placer/place_groups.py` |

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
- **A crystal is not yet seated on its IC's oscillator pins.** It lands
  3-14 mm from them, where the human put it 2.7-5.5 mm away.

## Where humans put each group

The priors the agent's hints follow come from a survey of the 437
human-placed boards of the stress corpus (`tests/stress/placement_survey.py`;
the numbers are in `.claude/skills/pcb-free-agent/references/part-roles.md`).
For example:
- A power conditioner is no closer to the board edge than an average part. It
  sits by its power entry: on the median board only 16% of parts are nearer
  that connector than the regulator is.
- ESD parts sit at their connector, and the main IC is central.
