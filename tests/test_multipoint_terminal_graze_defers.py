#!/usr/bin/env python3
"""A multipoint main edge whose terminal copper overlaps foreign copper defers
that edge; it does not fail the whole net (#101's contract).

route_multipoint_main tries MST edges longest-first so one boxed pad cannot
zero a power net (#101). The terminal-bridge short gate (ux pf9) that runs
after Phase 1 FOUND a path returned failed for the whole net instead,
bypassing that loop: cm5-io's +3V3_CM5 (34 pads) routed nothing because its
longest edge's terminal grazed a foreign via-in-pad by 0.085 mm.

Fixture: net B with three pads in a row, so the MST has two edges and the
longest (P1-P2) is Phase 1's first pick. The terminal-overlap measurement is
stubbed to report an overlap on the FIRST Phase 1 conversion only (no small
board reproduces a real terminal overlap); the test pins the control flow:
Phase 1 must ship the other edge, keep the deferred one for Phase 3, and the
taps must then connect every pad.

    python3 tests/test_multipoint_terminal_graze_defers.py
"""
import contextlib
import io
import os
import re
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'oracle'))

FAILS = []


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        FAILS.append(name)


def main():
    import constraint_agreement as ca
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig
    from obstacle_map import build_base_obstacle_map
    from connectivity import get_multipoint_net_pads
    import single_ended_routing as ser

    with tempfile.TemporaryDirectory() as td:
        board = os.path.join(td, 'graze.kicad_pcb')
        fp = lambda ref, x, y: dict(ref=ref, x=x, y=y, net_id=2, net_name='B', size=0.6)
        ca.write_board(board, footprints=[fp('P1', 5, 20), fp('P2', 20, 20), fp('P3', 32, 20)])
        pcb = parse_kicad_pcb(board)
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=0.15, clearance=0.15,
                          grid_step=0.05, via_size=0.6, via_drill=0.3)
    obs = build_base_obstacle_map(pcb, cfg, [2], static_base=True)
    pad_info = get_multipoint_net_pads(pcb, 2, cfg)

    real = ser._neck_route_terminal_grazes
    calls = []

    def first_overlaps(segments, *a, **k):
        calls.append(len(segments))
        real(segments, *a, **k)
        if len(calls) == 1:
            return [(SimpleNamespace(layer='F.Cu'), 0.0, None)]
        return []

    ser._neck_route_terminal_grazes = first_overlaps
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            res = ser.route_multipoint_main(pcb, 2, cfg, obs, pad_info)
    finally:
        ser._neck_route_terminal_grazes = real
    log = out.getvalue()

    check('fixture: the overlap gate fired on Phase 1', 'would OVERLAP' in log)
    check('Phase 1 still routes a main edge', bool(res) and not res.get('failed'),
          str(res and {k: res[k] for k in ('failed', 'iterations') if k in res}))
    if not res or res.get('failed'):
        print(f'\nFAIL: {len(FAILS)} check(s): ' + '; '.join(FAILS))
        return 1
    edges = [tuple(sorted(e[:2])) for e in res['mst_edges']]
    m = re.search(r'routing longest MST edge \(pads (\d+) and (\d+)', log)
    deferred = tuple(sorted((int(m.group(1)), int(m.group(2))))) if m else None
    check('the shipped main edge is not the deferred longest edge',
          deferred is not None and edges[0] != deferred, f'{edges}, deferred {deferred}')
    check('the deferred edge is kept for Phase 3', deferred in edges[1:], str(edges))
    with contextlib.redirect_stdout(io.StringIO()):
        taps = ser.route_multipoint_taps(pcb, 2, cfg, obs, res) or {}
    check('the taps connect every pad', not (taps.get('failed_pads_info') or []),
          str(taps.get('failed_pads_info')))
    if FAILS:
        print(f'\nFAIL: {len(FAILS)} check(s): ' + '; '.join(FAILS))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
