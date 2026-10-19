# Placement A/B: route placed boards by their recorded chain

A placement is judged by routing it (CLAUDE.md, "What a placement run is
FOR"): place each arm over seeds, route every placed board the way the stress
corpus routed the original, and compare the routed results paired per board
and seed, with blocking first, then vias, copper_mm and segments.

- **`route_placed.py`** routes one placed board on this machine. It replays
  the corpus board's recorded chain (`runs_set<SET>/<BOARD>/redo_commands.sh`)
  on the placed board and scores the final board with `board_score`.
  `--quick` adds route.py's fast judge to the chain's whole-board route
  steps. `--quick 2`, the seed judge, also passes it to every
  `route_diff.py` step and scores from the route step's own tally and the
  final file instead of `board_score` (blocking from its last
  JSON_SUMMARY_MIN, vias / copper / segments from the file); see below.
- **`route_modal.py`** routes a whole arm on Modal with `route_placed.py
  --quick` (`--quick 2` for the seed judge, `--full` for the whole chain,
  the final route of a chosen placement), one container per board and seed.
  Every route runs at once, so an arm takes about as long as its slowest
  route.

```bash
# placements in arms/base/<board>/s<seed>.kicad_pcb and arms/new/<board>/s<seed>.kicad_pcb
python3 tests/stress/placement_ab/route_modal.py arms/base ch32,noahfc,tig 0-7 --out routed
python3 tests/stress/placement_ab/route_modal.py arms/new  ch32,noahfc,tig 0-7 --out routed
# routed/base_ch32_s0/score.json, ... routed/new_tig_s7/score.json
```

Rules that keep the comparison honest:

- **Compare Modal routes only with Modal routes.** Modal runs Linux, the
  KiCad image's Python and its KiCad (`kicad/kicad:10.0.0`; set
  `KRT_PAB_KICAD_IMAGE` for another), so route the baseline arm there too.
- **The tree that routes is `--src`**: this checkout by default, *as it
  stands*. Uncommitted and untracked-but-not-ignored files are shipped;
  ignored ones (work dirs, built libraries) are not. Pass a worktree to route
  with its router. The deployed app is named after a hash of everything it
  ships, so runs over the same tree share one app and a changed tree gets its
  own.
- The app is deployed when it is missing and **stopped automatically** once
  nothing spawned on it is pending (`~/.cache/krt_placement_ab/apps.json`
  records who waits on which app).
- **Rerunning a command resumes it.** Finished routes are skipped, and calls
  still running are collected rather than respawned. A call outlives the
  client, so a dropped link loses nothing.
- Placements are not made here. Place first, then route.
