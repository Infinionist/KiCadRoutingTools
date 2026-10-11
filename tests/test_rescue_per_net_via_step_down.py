#!/usr/bin/env python3
"""The rescue's via step-down reaches a net with its own via size (#530).

route_net_with_obstacles re-points a net listed in config.net_via_sizes at
its own via before every search, so a rescue rung that only lowered
via_size was searched -- and emitted -- at the class via again: every
step-down rung repeated the rung above it, map rebuild included, and the
ladder itself was cut against the RUN's via rather than the net's. Both
ladders (the windowed rescue's _rescue_rungs and terminal escalation's
_escalation_ladder) now cut against the net's own via and take the net out
of net_via_sizes on a step-down rung.

Fixture (2 layers): net P (class via 0.8/0.4, run via 0.45/0.2) runs from an
F.Cu pad on the left to a B.Cu pad on the right. A foreign F.Cu wall at
x=20 and a foreign B.Cu wall at x=19 leave a 1 mm strip as the only place P
can change layers: a 0.8 via cannot fit there, a stepped-down via can. The
functional row routes the first step-down rung both ways -- the old rung
(P still in net_via_sizes) must FAIL, or this test cannot tell the two apart.
"""
import os
import sys
import tempfile
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'oracle'))

FAILS = []
RUN_VIA = (0.45, 0.2)
CLASS_VIA = (0.8, 0.4)
CLR = 0.15
P, F = 1, 2


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        FAILS.append(name)


def _board(td):
    from constraint_agreement import write_board
    path = os.path.join(td, 'r.kicad_pcb')
    # Walls run past the outline at both ends, so nothing routes around them.
    write_board(path, segments=[(20.0, -1.0, 20.0, 41.0, 0.2, 'F.Cu', F),
                                (19.0, -1.0, 19.0, 41.0, 0.2, 'B.Cu', F)],
                footprints=[
                    {'ref': 'U1', 'x': 5, 'y': 20, 'net_id': P, 'net_name': 'A', 'layer': 'F.Cu'},
                    {'ref': 'U2', 'x': 35, 'y': 20, 'net_id': P, 'net_name': 'A', 'layer': 'B.Cu'},
                    {'ref': 'U3', 'x': 30, 'y': 3, 'net_id': F, 'net_name': 'B', 'layer': 'F.Cu'},
                    {'ref': 'U4', 'x': 30, 'y': 37, 'net_id': F, 'net_name': 'B', 'layer': 'F.Cu'}])
    return path


def _cfg():
    from routing_config import GridRouteConfig
    return GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=0.2, clearance=CLR,
                           grid_step=0.1, via_size=RUN_VIA[0], via_drill=RUN_VIA[1],
                           net_via_sizes={P: CLASS_VIA}, max_iterations=200000)


def _route(pcb, cfg):
    import contextlib
    import io
    from obstacle_map import build_base_obstacle_map
    from single_ended_routing import route_net_with_obstacles
    obs = build_base_obstacle_map(pcb, cfg, [P])
    with contextlib.redirect_stdout(io.StringIO()):
        res = route_net_with_obstacles(pcb, P, cfg, obs)
    # A failed search returns a dict with 'failed' set, not None.
    return None if (not res or res.get('failed')) else res


def _clear_of_walls(res):
    """Every new via clears both walls (0.2 wide) by the clearance."""
    bad = []
    for v in (res or {}).get('new_vias', []):
        for wx in (19.0, 20.0):
            gap = abs(v.x - wx) - v.size / 2 - 0.1
            if gap < CLR - 1e-6:
                bad.append((round(v.x, 3), round(v.y, 3), v.size, round(gap, 3)))
    return bad


def main():
    try:
        import grid_router
        if not hasattr(grid_router.GridObstacleMap, 'add_blocked_vias_rung_batch'):
            print('SKIP: grid_router without the via-rung API (needs 0.22.0+)')
            return 0
    except ImportError:
        print('SKIP: grid_router not importable')
        return 0
    from kicad_parser import parse_kicad_pcb
    from fab_tiers import may_narrow
    import net_rescue
    if not may_narrow():
        print('SKIP: escalation is off in this environment (no step-down rungs exist)')
        return 0
    with tempfile.TemporaryDirectory() as td:
        pcb = parse_kicad_pcb(_board(td))
        cfg = _cfg()

        print('-- the windowed rescue ladder')
        rungs = net_rescue._rescue_rungs(cfg, 0.05, pcb, P)
        # A rung is a step-down when the via the NET draws on it is smaller
        # than its class via (the clearance rungs carry the run's via_size
        # but still draw the class via).
        steps = [r for r in rungs if r.net_via(P)[0] < CLASS_VIA[0] - 1e-9]
        from fab_tiers import escalation_rungs
        below = sorted({f['via_diameter'] for f in escalation_rungs(2)
                        if f['via_diameter'] < CLASS_VIA[0] - 1e-9}, reverse=True)
        check('step-down rungs exist', bool(steps), f'{len(rungs)} rungs')
        check('the ladder is cut against the NET\'s via: every fab via below it is offered',
              sorted({r.net_via(P)[0] for r in steps}, reverse=True) == below,
              f'{sorted({r.net_via(P)[0] for r in steps}, reverse=True)} vs {below}')
        check('a step-down rung draws the via it sets',
              all(r.net_via(P) == (r.via_size, r.via_drill) for r in steps))
        check('every other rung still draws the class via',
              all(r.net_via(P) == CLASS_VIA for r in rungs if r not in steps))

        print('-- the terminal escalation ladder')
        esc = net_rescue._escalation_ladder(cfg, pcb, P)
        check('terminal escalation offers via step-downs below the class via',
              bool(esc) and all(r.net_via(P)[0] < CLASS_VIA[0] - 1e-9 for r in esc),
              str([r.net_via(P) for r in esc]))

        print('-- routing the smallest step-down rung')
        if not steps:
            print('  (no step-down rung for the net: routing rows not run -- the '
                  'ladder rows above already failed)')
            return _finish()
        first = steps[-1]
        res = _route(pcb, first)
        print('     new rung vias:', [(round(v.x, 2), round(v.y, 2), v.size)
                                     for v in (res or {}).get('new_vias', [])])
        vias = (res or {}).get('new_vias', [])
        check('new rung: routes with a via', bool(vias))
        check('new rung: the via is the step-down size, not the class via',
              bool(vias) and all(abs(v.size - first.via_size) < 1e-9 for v in vias),
              str([v.size for v in vias]))
        check('new rung: every via clears both walls', not _clear_of_walls(res),
              str(_clear_of_walls(res)))
        old = replace(first, net_via_sizes=dict(cfg.net_via_sizes))   # P still listed
        res0 = _route(pcb, old)
        print('     old rung vias:', [(round(v.x, 2), round(v.y, 2), v.size)
                                     for v in (res0 or {}).get('new_vias', [])])
        check('negative control (old rung, P still in net_via_sizes): no route', not res0)
    return _finish()


def _finish():
    if FAILS:
        print(f'\nFAIL: {len(FAILS)} check(s): ' + '; '.join(FAILS))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
