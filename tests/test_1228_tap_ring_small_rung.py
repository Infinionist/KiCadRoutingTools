#!/usr/bin/env python3
"""#1228: Phase 3 taps never place a via on routed copper through an EMPTY
#568 small rung.

On a map from build_single_ended_obstacles with no net cache, the small rung
(rung 1) is allocated but empty -- and an empty rung falls back to rung 0,
which is safe. _register_inprogress_via stamped the in-progress via rings
into it unconditionally, leaving them its ONLY entries; the boxed-endpoint
rung-1 retry (gated on the SUM of every rung, so a populated per-net rung
waved it through) then searched a map that held no foreign copper and put
a small via on another net's track. Now the rings reach only rungs that
already hold copper, and the retry runs only when rung 1 does.

Fixture (the issue's reproducer, kept): net B's pad P3 sits inside a square
of net A's F.Cu track; a per-net via on net C allocates the rungs. Every tap
via must clear net A by the clearance -- or the pad stays unconnected (with
a same-net pad clearance on there is no legal site, and the small-rung-off
control agrees). The old code placed a 0.45 via 0.000 mm from net A.

Needs grid_router 0.22.0+ (SKIPs otherwise).
"""
import contextlib
import io
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'oracle'))

FAILS = []
TAP, HALF, CLR, TW = (2.0, 24.0), 0.40, 0.15, 0.15


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        FAILS.append(name)


def _seg_d(p, a, b):
    (ax, ay), (bx, by) = a, b
    t = max(0, min(1, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay))
                   / ((bx - ax) ** 2 + (by - ay) ** 2)))
    return math.hypot(p[0] - ax - t * (bx - ax), p[1] - ay - t * (by - ay))


def _run(td, snpc):
    import constraint_agreement as ca
    from kicad_parser import parse_kicad_pcb, Segment
    from routing_config import GridRouteConfig
    from obstacle_map import build_base_obstacle_map, build_layer_map
    from routing_context import build_single_ended_obstacles
    from connectivity import get_multipoint_net_pads
    import single_ended_routing as ser
    board = os.path.join(td, f'r{snpc}.kicad_pcb')
    ca.NETS[4] = 'D'
    try:
        fp = lambda ref, x, y, n, name, **k: dict(ref=ref, x=x, y=y, net_id=n, net_name=name, **k)
        ca.write_board(board, footprints=[
            fp('U1', 30, 5, 1, 'A'), fp('U2', 30, 35, 1, 'A'),
            fp('P1', 5, 20, 2, 'B', size=0.6), fp('P2', 35, 20, 2, 'B', size=0.6, layer='B.Cu'),
            fp('P3', TAP[0], TAP[1], 2, 'B', size=0.3),
            fp('U5', 20, 10, 3, 'C'), fp('U6', 38, 38, 4, 'D')])
    finally:
        del ca.NETS[4]
    pcb = parse_kicad_pcb(board)
    # run via 0.6/0.3; net C (not the net under test) has its own 0.8/0.4 via
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=TW, clearance=CLR,
                          grid_step=0.05, via_size=0.6, via_drill=0.3,
                          same_net_pad_clearance=snpc, net_via_sizes={3: (0.8, 0.4)})
    base = build_base_obstacle_map(pcb, cfg, [1, 2, 3], static_base=True)
    c = [(TAP[0] - HALF, TAP[1] - HALF), (TAP[0] + HALF, TAP[1] - HALF),
         (TAP[0] + HALF, TAP[1] + HALF), (TAP[0] - HALF, TAP[1] + HALF)]
    fence = list(zip(c, c[1:] + c[:1]))
    for a, b in fence:      # net A is routed: a square of F.Cu track around P3
        pcb.segments.append(Segment(start_x=a[0], start_y=a[1], end_x=b[0], end_y=b[1],
                                    width=TW, layer='F.Cu', net_id=1))
    obs, _ = build_single_ended_obstacles(
        base, pcb, cfg, routed_net_ids=[1], remaining_net_ids=[2, 3],
        all_unrouted_net_ids=[2, 3], net_id=2, gnd_net_id=None,
        track_proximity_cache={}, layer_map=build_layer_map(cfg.layers))
    rungs_before = [obs.rung_len(r) for r in range(obs.rung_count())]
    stats_before = tuple(obs.get_stats())
    with contextlib.redirect_stdout(io.StringIO()):
        main = ser.route_multipoint_main(pcb, 2, cfg, obs, get_multipoint_net_pads(pcb, 2, cfg))
        taps = ser.route_multipoint_taps(pcb, 2, cfg, obs, main) or {}
    gaps = [(v.size, round(min(_seg_d((v.x, v.y), a, b) for a, b in fence) - v.size / 2 - TW / 2, 3))
            for v in taps.get('new_vias', [])]
    return rungs_before, gaps, len(taps.get('failed_pads_info') or []), \
        [obs.rung_len(r) for r in range(obs.rung_count())], stats_before, tuple(obs.get_stats())


def main():
    try:
        import grid_router
        if not hasattr(grid_router.GridObstacleMap, 'add_blocked_vias_rung_batch'):
            print('SKIP: grid_router without the via-rung API (needs 0.22.0+)')
            return 0
    except ImportError:
        print('SKIP: grid_router not importable')
        return 0
    from obstacle_cache import rung_small_armed
    if not rung_small_armed():
        print('SKIP: the #568 small rung is not armed here (KICAD_VIA_RUNG != 2)')
        return 0
    with tempfile.TemporaryDirectory() as td:
        for snpc in (-1.0, 0.1):
            print(f'-- same_net_pad_clearance {snpc}')
            before, gaps, unconnected, after, st0, st1 = _run(td, snpc)
            check('fixture: rung 1 allocated but EMPTY before the taps',
                  len(before) >= 3 and before[1] == 0, str(before))
            check('no tap via within clearance of net A',
                  all(g >= CLR - 1e-6 for _, g in gaps), str(gaps))
            check('P3 is either reached by a legal via or reported unconnected',
                  any(g >= CLR - 1e-6 for _, g in gaps) or unconnected >= 1,
                  f'vias {gaps}, unconnected {unconnected}')
            check('the in-progress rings are removed from every rung they reached',
                  after == before and st1[:2] == st0[:2] and st1[7] == st0[7],
                  f'{before} -> {after}')
    if FAILS:
        print(f'\nFAIL: {len(FAILS)} check(s): ' + '; '.join(FAILS))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
