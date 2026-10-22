#!/usr/bin/env python3
"""A dropped #189/#535 unblock via leaves no free via behind on the map.

A free via tells the A* "a barrel of this net is here": it changes layer on
that cell at zero cost and through the via-blocked veto and the path's own
via spacing (router.rs). _register_unblock_via used to register one for
every placed unblock via, and nothing took it back when the via was
dropped -- the retry failed, the route did not use it, or the tap's terminal
short gate refused the edge (which uncommits the copper but kept the claim).
A later search on that map then drilled a NEW via on a cell the via map
vetoes. Measured on a GND tap edge into U1.4: the #535 escape via
was placed legally at (147.65, 111.35) and dropped with its edge; the orphan
fallback changed layer on its cell (147.6, 111.4), vetoed at every rung, and
shipped a 0.3/0.15 via 8.5 um inside U1.5's clearance (the #339 refit
tolerates 50 um, and the nudge found no move). The retry never needed the
claim -- it ends ON the via cell, on any layer -- and a kept via gets its
free via from its caller, so the registration is gone.

Rows:
  1. hand-built map (the pad's pocket sealed on F.Cu and via-blocked): the
     unblock places its via, the retry fails; afterwards no free via is left
     at the via cell, and a later search out of the pocket finds no way to
     change layer on a vetoed cell;
  2. negative control: put back the claim the old registration left, and
     that later search drills on the vetoed cell (the defect) -- so row 1 is
     not passing for want of a route;
  3. the real tap loop: the tap gate refuses the edge into a boxed pad after
     its unblock via was placed; every later search on that map sees no
     free via on the dropped via's cell;
  4. _register_unblock_via adds no free via (source shape).

    python3 tests/test_unblock_via_no_stale_free_via.py
"""
import ast
import contextlib
import io
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'oracle'))

import constraint_agreement as ca                        # noqa: E402
import fab_tiers                                          # noqa: E402
import single_ended_routing as ser                        # noqa: E402
from connectivity import get_multipoint_net_pads          # noqa: E402
from grid_router import GridObstacleMap, GridRouter       # noqa: E402
from kicad_parser import BoardInfo, Via, parse_kicad_pcb  # noqa: E402
from obstacle_map import build_base_obstacle_map          # noqa: E402
from routing_config import GridRouteConfig                # noqa: E402
from synth import make_net, make_pcb                      # noqa: E402

FAILS = []
STEP = 0.05
T = (200, 200)          # the boxed pad's cell, (10.0, 10.0) mm
S = (100, 200)          # source, 5 mm west
WALL = 3
CFG = dict(layers=['F.Cu', 'B.Cu'], grid_step=STEP, clearance=0.15,
           track_width=0.2, via_size=0.45, via_drill=0.3,
           hole_to_hole_clearance=0.25)
UNBLOCK = Via(x=10.0, y=10.0, size=0.25, drill=0.15,
              layers=['F.Cu', 'B.Cu'], net_id=1)


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        FAILS.append(name)


def pocket_map():
    """T in a pocket: F.Cu wall 3 cells out, every pocket cell via-blocked,
    B.Cu open only inside the pocket -- so no retry from S can reach it."""
    obs = GridObstacleMap(2)
    for dx in range(-WALL, WALL + 1):
        for dy in range(-WALL, WALL + 1):
            obs.add_blocked_via(T[0] + dx, T[1] + dy)
            if max(abs(dx), abs(dy)) == WALL:
                obs.add_blocked_cell(T[0] + dx, T[1] + dy, 0)
                obs.add_blocked_cell(T[0] + dx, T[1] + dy, 1)
    for gx in range(S[0] - 6, T[0] + 31):
        for gy in range(T[1] - 31, T[1] + 31):
            if max(abs(gx - T[0]), abs(gy - T[1])) > WALL:
                obs.add_blocked_cell(gx, gy, 1)
            if gx in (S[0] - 6, T[0] + 30) or gy in (T[1] - 31, T[1] + 30):
                obs.add_blocked_cell(gx, gy, 0)
    return obs


def run_unblock(obs):
    cfg = GridRouteConfig(**CFG)
    pcb = make_pcb(nets={1: make_net(1, 'N')},
                   board_info=BoardInfo(layers={0: 'F.Cu', 2: 'B.Cu'},
                                        copper_layers=['F.Cu', 'B.Cu']))
    pcb._via_unblock_failed = set()   # the real placement creates it
    pad = SimpleNamespace(component_ref='U1', pad_number='1', global_x=10.0,
                          global_y=10.0, size_x=0.3, size_y=0.3, drill=0,
                          layers=['F.Cu'])
    placed = []
    saved = {n: getattr(ser, n) for n in
             ('_net_pad_near', '_net_pads_near', '_place_shrunk_via_in_pad')}
    ser._net_pad_near = lambda *a, **k: pad
    ser._net_pads_near = lambda *a, **k: []
    ser._place_shrunk_via_in_pad = lambda *a, **k: (
        placed.append(1) or (UNBLOCK, T, 0, []))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            res = ser._route_with_via_unblock(
                GridRouter(via_cost=50, h_weight=1.5, turn_cost=100), obs, cfg,
                [(S[0], S[1], 0)], [(T[0], T[1], 0)], 0, pcb, 1)
    finally:
        for n, f in saved.items():
            setattr(ser, n, f)
    return res, len(placed)


def later_search(obs):
    """Out of the pocket: from the pad on F.Cu to a pocket cell on B.Cu.
    Every pocket cell is via-blocked, so only a free via lets it through."""
    r = GridRouter(via_cost=50, h_weight=1.5, turn_cost=100)
    path, _it, _bl = r.route_with_frontier(obs, [(T[0], T[1], 0)],
                                           [(T[0] + 1, T[1], 1)], 5000)
    return path


def vetoed_layer_changes(obs, path):
    return [p[:2] for a, p in zip(path or [], (path or [])[1:])
            if a[:2] == p[:2] and a[2] != p[2] and obs.is_via_blocked(p[0], p[1])]


def rows_hand_built():
    print('1. hand-built pocket: the dropped via leaves no claim')
    obs = pocket_map()
    free0 = obs.get_stats()[6]
    res, n_placed = run_unblock(obs)
    check('precondition: the unblock placed its via and the retry failed '
          '(the via is dropped)', n_placed == 1 and res[0] is None,
          f'placed {n_placed}, path {res[0] is not None}')
    check('no free via is left at the dropped via cell',
          not obs.is_free_via(*T) and obs.get_stats()[6] == free0,
          f'free_via({T})={obs.is_free_via(*T)}, count {free0} -> {obs.get_stats()[6]}')
    path = later_search(obs)
    bad = vetoed_layer_changes(obs, path)
    check('a later search changes layer on no vetoed cell', not bad,
          f'path {path is not None}, vetoed layer changes {bad}')

    print('2. negative control: the claim the old registration left')
    obs = pocket_map()
    run_unblock(obs)
    obs.add_free_via(*T)
    path = later_search(obs)
    bad = vetoed_layer_changes(obs, path)
    check('with the stale claim the later search drills on a vetoed cell '
          '(the defect)', path is not None and list(T) in [list(c) for c in bad],
          f'vetoed layer changes {bad}')


def rows_tap_loop():
    print('3. the real tap loop: a refused edge leaves no claim')
    fab_tiers.set_escalation_policy('fab')
    p1 = (26, 20)
    h = 0.5
    box = [(p1[0] - h, p1[1] - h, p1[0] + h, p1[1] - h),
           (p1[0] + h, p1[1] - h, p1[0] + h, p1[1] + h),
           (p1[0] + h, p1[1] + h, p1[0] - h, p1[1] + h),
           (p1[0] - h, p1[1] + h, p1[0] - h, p1[1] - h)]
    with tempfile.TemporaryDirectory() as td:
        board = os.path.join(td, 'boxed.kicad_pcb')
        ca.write_board(board, segments=[(a, b, c, d, 0.2, 'F.Cu', 1) for a, b, c, d in box],
                       footprints=[dict(ref=r, x=x, y=y, net_id=2, net_name='B', size=s)
                                   for r, x, y, s in (('P2', 5, 20, 0.6), ('P3', 20, 20, 0.6),
                                                      ('P1', p1[0], p1[1], 0.3))])
        pcb = parse_kicad_pcb(board)
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=0.4, clearance=0.15,
                          grid_step=0.05, via_size=0.6, via_drill=0.3)
    obs = build_base_obstacle_map(pcb, cfg, [2], static_base=True)
    pad_info = get_multipoint_net_pads(pcb, 2, cfg)
    dropped, seen = [], []
    real_unc, real_neck = ser._uncommit_unblock_copper, ser._neck_route_terminal_grazes
    real_rmc = ser._route_main_connection
    calls = []

    def unc(pcb_data, vias, segments):
        dropped.extend(cfg_cell(v) for v in vias or ())
        return real_unc(pcb_data, vias, segments)

    def neck(segments, *a, **k):          # the tap gate refuses conversion 2
        calls.append(1)
        got = real_neck(segments, *a, **k)
        return ([(SimpleNamespace(layer='F.Cu'), 0.0, None)]
                if len(calls) == 2 else got)

    def rmc(router, obstacles, *a, **k):
        for c in dropped:
            seen.append(obstacles.is_free_via(*c))
        return real_rmc(router, obstacles, *a, **k)

    def cfg_cell(v):
        return (int(round(v.x / cfg.grid_step)), int(round(v.y / cfg.grid_step)))

    ser._uncommit_unblock_copper = unc
    ser._neck_route_terminal_grazes = neck
    ser._route_main_connection = rmc
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            main = ser.route_multipoint_main(pcb, 2, cfg, obs, pad_info)
            if main and not main.get('failed'):
                ser.route_multipoint_taps(pcb, 2, cfg, obs, main)
    finally:
        ser._uncommit_unblock_copper = real_unc
        ser._neck_route_terminal_grazes = real_neck
        ser._route_main_connection = real_rmc
    log = out.getvalue()
    check('precondition: the tap into P1 dropped its via, the gate refused '
          'the edge, and a later search ran on that map',
          'Via-in-pad unblock: dropped' in log and 'failing the MST edge rather than' in log
          and bool(dropped) and bool(seen),
          f'dropped {dropped}, later searches {len(seen)}')
    check('no later search sees a free via on the dropped via cell',
          bool(seen) and not any(seen), f'{sum(seen)} of {len(seen)} saw one')


def row_source():
    print('4. source')
    tree = ast.parse(open(os.path.join(ROOT, 'py_router', 'single_ended_routing.py'),
                          encoding='utf-8').read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == '_register_unblock_via')
    adds = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute) and 'free_via' in c.func.attr]
    check('_register_unblock_via registers no free via', not adds,
          ', '.join(ast.unparse(c) for c in adds))


def main():
    rows_hand_built()
    rows_tap_loop()
    row_source()
    print('-' * 60)
    if FAILS:
        print(f'{len(FAILS)} FAILED: ' + '; '.join(FAILS))
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
