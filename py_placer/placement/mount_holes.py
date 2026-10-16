"""Mounting holes nobody placed: a default pose at the board's corners.

A mounting hole's position is a mechanical decision -- the enclosure's
standoffs say where it goes -- so `/pcb-group-place` places and locks every
hole before anything else, from a design brief, `mechanical.json`, the user,
or a believable input board. When none of them did, the seed has nothing to
keep: on a PILE (`placement_state.assess_placement`) the input pose is
staging, and the seeder treated a hole like any other part, packing it into
its group's region. On glasgow_revC's pile that put all four M3 holes in
the board's interior, or left two off the board when the region was full.

Humans do have a habit to fall back on, and only one. Measured over the
human-placed boards of stress sets 1-30
(`tests/stress/placement_survey.py report`, `MOUNTING HOLES`): on a board
with exactly four holes, all four sit within 8 mm of a board corner on 86 of
125 boards; with one to three holes, or five or more, no hole count reaches
30%. So `corner_hole_poses` acts on a pile with exactly four holes, gives
each unlocked one a free corner (one no locked hole already holds), and
finds there the legal pose nearest the corner with the seeder's own seat
search -- the state the seed itself builds, every unplaced part ignored, the
holes seated so far in the way. Locked connectors can fill a corner (on
glasgow_revC the nearest legal pose is beside them, 6-15 mm in, where the
human's oversized hole courtyards overlap theirs); a pose past the corner's
quarter of the board is refused. Any other board gets nothing.

`plan_regions.py` writes each pose as a `fixed_poses[]` entry of basis
`derived_default`, so the seeder's stage 0 seats and locks it before any
group, and says the hole was not locked.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

#: A corner is taken when a LOCKED hole's centre lies this close to it on
#: both axes -- the survey's corner test (holes within 8 mm), widened to the
#: 12 mm a large hole's centre can stand off.
CORNER_TAKEN_MM = 12.0

#: The hole count the default applies to (see the module docstring).
CORNER_HOLE_COUNT = 4


def _natural(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', ref)]


def mount_holes(pcb) -> List[str]:
    """Every mounting hole on the board, by `part_class`, in natural order."""
    from placement.part_class import classify_part
    out = []
    for ref, fp in (pcb.footprints or {}).items():
        try:
            if classify_part(fp, ref).name == 'mount_hole':
                out.append(ref)
        except Exception:                                  # noqa: BLE001
            continue
    return sorted(out, key=_natural)


def corner_hole_poses(pcb, pcb_file: str, claimed=(),
                      intent=None) -> Tuple[List[Dict], List[str]]:
    """`(poses, notes)`: `[{ref, x, y, rot, corner}]` for each unlocked hole
    this default places, and a note per hole it could not.

    Nothing unless the board is a pile with exactly `CORNER_HOLE_COUNT`
    holes. `claimed` refs are skipped: a hole the intent already places (a
    fixed pose, an edge connector, a `must_lock` match) is not this
    default's. Holes take the free corners in natural order, corners in
    the order top-left, top-right, bottom-right, bottom-left; a hole with no
    free corner, or no legal pose near one, is left to the ordinary stages
    and named in `notes`. `intent` (a loaded `floorplan.Intent`) binds the
    search as it binds the seed: its keep-outs, its exclusive zones and a
    rotation it declares for the hole."""
    holes = mount_holes(pcb)
    if len(holes) != CORNER_HOLE_COUNT:
        return [], []
    from placement.placement_state import assess_placement
    if not assess_placement(pcb, pcb_file).pile:
        return [], []
    bounds = pcb.board_info.board_bounds
    if not bounds:
        return [], []
    x0, y0, x1, y1 = bounds
    corners = [('top-left', x0, y0), ('top-right', x1, y0),
               ('bottom-right', x1, y1), ('bottom-left', x0, y1)]
    fps = pcb.footprints
    locked = [r for r in holes if getattr(fps[r], 'locked', False)]
    free = [c for c in corners
            if not any(abs(fps[r].x - c[1]) <= CORNER_TAKEN_MM
                       and abs(fps[r].y - c[2]) <= CORNER_TAKEN_MM for r in locked)]
    todo = [r for r in holes if r not in locked and r not in set(claimed)]
    if not todo:
        return [], []
    import pose_score
    from list_nets import board_floor_knobs
    from placement import floorplan
    from placement.groups import parse_sources
    from placement.seeder import _try_place
    clearance, edge, _ = board_floor_knobs(pcb_file)
    blocks, declared = {}, {}
    if intent is not None:
        blocks, _problems = floorplan.resolve_blocks(intent, pcb,
                                                     parse_sources('auto'))
        declared = floorplan.rotations_for_ref(intent, blocks)
    state = pose_score.make_state(
        pcb, pcb_file, clearance=clearance, board_edge_clearance=edge,
        grid_step=0.1, keepouts=intent.keepouts if intent else (),
        exclusive_zones=(floorplan.zone_entries(intent, blocks)
                         if intent else ()))
    unplaced = {r for r, p in state.parts.items() if not p.locked}
    poses, notes = [], []
    for ref in todo:
        if ref not in state.parts:
            continue
        if not free:
            notes.append(f"{ref}: mounting hole, no free board corner left")
            continue
        name, cx, cy = free.pop(0)
        start = (state.parts[ref].x, state.parts[ref].y, state.parts[ref].rot)
        if _try_place(state, ref, cx, cy, unplaced - {ref},
                      rotations=floorplan.declared_ladder(declared.get(ref))) is None:
            notes.append(f"{ref}: mounting hole, no legal pose near the "
                         f"{name} corner")
            continue
        p = state.parts[ref]
        # The nearest legal pose can be far from a corner that locked parts
        # fill; one past the corner's quarter of the board is no corner pose.
        if (abs(p.x - cx) > (x1 - x0) / 2.0 or abs(p.y - cy) > (y1 - y0) / 2.0):
            notes.append(f"{ref}: mounting hole, no legal pose in the {name} "
                         f"quarter of the board")
            state.apply_move(ref, *start)
            continue
        unplaced.discard(ref)
        poses.append({'ref': ref, 'x': round(p.x, 4), 'y': round(p.y, 4),
                      'rot': round(p.rot % 360.0, 4), 'corner': name})
    return poses, notes
