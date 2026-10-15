#!/usr/bin/env python3
"""#1227: the dead-end prune never removes copper from between a route's ends.

A route committed through add_route_to_pcb_data with a small hole of its own
(two consecutive segment ends 0.022mm apart -- rp2040_motorctrl's
/SPI1_SCK_2, a net_rescue commit at a 0.3 -> 0.0889mm width step) is STRICTLY
open at the hole, so the geometric prune unwinds the whole fine chain from the
hole back to its pad. The hole's bridge segment is rejected (dropping it
strands the far pad), but every 0.025mm staircase step behind it passed both
pad-count gates: the physical one on its neighbours' 0.0889mm cap overlap, the
strict one because its base count was already 1 and two pads cannot do worse.
Before #1227: 18 segments committed, 7 on the board, 10 unjoined stumps
(check_drc: segment-endpoint-gap).

    python3 tests/test_1227_prune_never_splits.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'oracle'))

from constraint_agreement import write_board
from kicad_parser import parse_kicad_pcb, Segment
from pcb_modification import add_route_to_pcb_data

FAILS = []
FINE = 0.0889


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ''))
    if not cond:
        FAILS.append(name)


def seg(a, b, w=FINE):
    return Segment(start_x=a[0], start_y=a[1], end_x=b[0], end_y=b[1],
                   width=w, layer='F.Cu', net_id=1)


def board():
    td = tempfile.mkdtemp(prefix='t1227_')
    path = os.path.join(td, 'p.kicad_pcb')
    write_board(path, footprints=[
        {'ref': 'U1', 'x': 10, 'y': 10, 'net_id': 1, 'net_name': 'A', 'layer': 'F.Cu'},
        {'ref': 'U2', 'x': 20, 'y': 10.2, 'net_id': 1, 'net_name': 'A', 'layer': 'F.Cu'}])
    return parse_kicad_pcb(path)


def route(hole, tail=((14.375, 10.2), (20, 10.2))):
    """The issue's route: a 0.3mm leg off U1, a ``hole``, then a fine
    staircase of 0.025mm steps and a tail into U2."""
    stair = [(12 + hole, 10), (14, 10), (14.025, 10.025), (14.05, 10.025),
             (14.075, 10.05), (14.1, 10.05), (14.125, 10.075), (14.15, 10.075),
             (14.175, 10.1), (14.2, 10.1), (14.225, 10.125), (14.25, 10.125),
             (14.275, 10.15), (14.3, 10.15), (14.325, 10.175), (14.35, 10.175),
             tail[0]]
    segs = [seg((10, 10), (12, 10), 0.3)]
    segs += [seg(a, b) for a, b in zip(stair, stair[1:])]
    segs += [seg(a, b) for a, b in zip(tail, tail[1:])]
    return segs


def commit(segs):
    pcb = board()
    add_route_to_pcb_data(pcb, {'new_segments': list(segs), 'new_vias': []})
    return [s for s in pcb.segments if s.net_id == 1]


def unjoined(segs):
    ends = {}
    for s in segs:
        for p in ((round(s.start_x, 4), round(s.start_y, 4)),
                  (round(s.end_x, 4), round(s.end_y, 4))):
            ends[p] = ends.get(p, 0) + 1
    return sorted(p for p, n in ends.items() if n == 1)


def row_input_exercises_the_bug():
    """The geometry must be one the pad counts cannot protect, or the rows
    below would pass on the old code too."""
    from check_connected import check_net_connectivity
    from pcb_modification import _strict_conn_graph
    pcb = board()
    pads = pcb.pads_by_net[1]
    segs = route(0.022)
    chipped = [s for i, s in enumerate(segs) if i != 2]   # one staircase step
    phys = len(check_net_connectivity(1, chipped, [], pads, [])['disconnected_pads'])
    base_strict = len(_strict_conn_graph(1, segs, [], pads, [])[0]['disconnected_pads'])
    strict = len(_strict_conn_graph(1, chipped, [], pads, [])[0]['disconnected_pads'])
    check('a chipped step stays physically connected and the strict count '
          'does not rise', phys == 0 and base_strict == 1 and strict == 1,
          f'physical {phys}, strict {base_strict} -> {strict}')


def row_hole_route_is_kept_whole():
    segs = route(0.022)
    kept = commit(segs)
    check('a route with a 0.022mm hole of its own is committed whole',
          len(kept) == len(segs), f'{len(segs)} committed, {len(kept)} on the board')
    check('the only unjoined ends are the pads and the hole itself',
          unjoined(kept) == [(10, 10), (12, 10), (12.022, 10), (20, 10.2)],
          f'{unjoined(kept)}')


def row_control_without_hole():
    segs = route(0.0)
    kept = commit(segs)
    check('control: the same route without a hole is committed whole',
          len(kept) == len(segs) and unjoined(kept) == [(10, 10), (20, 10.2)],
          f'{len(segs)} committed, {len(kept)} on the board')


def row_dead_spur_still_pruned():
    """The gate must not over-keep: a genuine dead spur off the same strictly
    split route still goes."""
    segs = route(0.022, tail=((14.375, 10.2), (17, 10.2), (20, 10.2)))
    spur = seg((17, 10.2), (17, 12))
    kept = commit(segs + [spur])
    ids = {id(s) for s in kept}
    check('a dead spur off a strictly split route is still pruned',
          id(spur) not in ids and all(id(s) in ids for s in segs),
          f'{len(segs) + 1} committed, {len(kept)} on the board, '
          f'spur {"kept" if id(spur) in ids else "removed"}')


def row_island_does_not_pay_for_a_split():
    """Removing a dead island lowers the copper-component count; a gate that
    compared that count against the BASE would then let one split through.
    The island's segments are round-1 candidates, the staircase steps
    behind the rejected hole bridge round 2+, so the island goes first."""
    segs = route(0.022)
    island = [seg((30, 30), (31, 30)), seg((31, 30), (31, 31))]
    kept = commit(segs + island)
    ids = {id(s) for s in kept}
    check('an island is removed and the staircase kept whole in the same commit',
          not any(id(s) in ids for s in island) and all(id(s) in ids for s in segs),
          f'{len(segs) + 2} committed, {len(kept)} on the board')


def main():
    print('#1227: the per-commit dead-end prune never splits copper')
    row_input_exercises_the_bug()
    row_hole_route_is_kept_whole()
    row_control_without_hole()
    row_dead_spur_still_pruned()
    row_island_does_not_pay_for_a_split()
    print('=' * 60)
    if FAILS:
        print(f'{len(FAILS)} failure(s)')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
