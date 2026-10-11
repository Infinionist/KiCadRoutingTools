#!/usr/bin/env python3
"""#530 decision 4 for multipoint nets: drawn at their own via where it fits.

A multipoint net is searched at the run's via; single_ended_routing.
_draw_at_own_via then draws each via it placed at the net's own class via
wherever the cell is legal at the net's own via-legality rung, and keeps the
run's via (disclosed as a descent) where it is not. A via another mechanism
already sized is left alone. The routed end-to-end row lives in
test_530_per_net_via_sizes (net P2); this pins the branch a roomy board never
reaches: a cell legal for the run's via but not for the class via.

Needs grid_router 0.22.0+ (SKIPs otherwise).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))

FAILS = []


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        FAILS.append(name)


def main():
    try:
        import grid_router
        if not hasattr(grid_router.GridObstacleMap, 'add_blocked_vias_rung_batch'):
            print('SKIP: grid_router without the via-rung API (needs 0.22.0+)')
            return 0
    except ImportError:
        print('SKIP: grid_router not importable')
        return 0
    import fab_tiers
    from kicad_parser import Via
    from routing_config import GridRouteConfig
    from obstacle_cache import rung_for_net
    from single_ended_routing import _draw_at_own_via
    P = 7
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], grid_step=0.1, via_size=0.3, via_drill=0.15,
                          net_via_sizes={P: (0.6, 0.3)})

    class _Board:                      # rung_for_net reads only the copper layer count
        class board_info:
            copper_layers = ['F.Cu', 'B.Cu']
    r = rung_for_net(cfg, _Board, P)
    check('the class via has a rung', r >= 1, f'rung {r}')
    m = grid_router.GridObstacleMap(2)
    m.add_blocked_via_rung(r, 50, 50)        # cramped: blocked for the class via only
    m.add_blocked_via_rung(r, 0, 0)          # (a populated rung, elsewhere)
    roomy = Via(x=2.0, y=2.0, size=0.3, drill=0.15, layers=['F.Cu', 'B.Cu'], net_id=P)
    cramped = Via(x=5.0, y=5.0, size=0.3, drill=0.15, layers=['F.Cu', 'B.Cu'], net_id=P)
    shrunk = Via(x=8.0, y=8.0, size=0.25, drill=0.15, layers=['F.Cu', 'B.Cu'], net_id=P)
    before = len(fab_tiers._LEDGER['narrowed'])
    _draw_at_own_via([roomy, cramped, shrunk], _Board, P, cfg, m)
    rows = fab_tiers._LEDGER['narrowed'][before:]
    check('a via the class via fits is drawn at it', (roomy.size, roomy.drill) == (0.6, 0.3),
          str((roomy.size, roomy.drill)))
    check('a via the class via does not fit keeps the run via', (cramped.size, cramped.drill) == (0.3, 0.15),
          str((cramped.size, cramped.drill)))
    check('a via another mechanism sized keeps its size', (shrunk.size, shrunk.drill) == (0.25, 0.15))
    check('the kept via is disclosed as a descent from the class via',
          len(rows) == 1 and rows[0]['requested'] == 0.6 and rows[0]['delivered'] == 0.3
          and rows[0]['count'] == 1, str(rows))
    other = Via(x=2.0, y=2.0, size=0.3, drill=0.15, layers=['F.Cu', 'B.Cu'], net_id=P)
    _draw_at_own_via([other], _Board, P,
                     GridRouteConfig(layers=['F.Cu', 'B.Cu'], grid_step=0.1, via_size=0.3,
                                     via_drill=0.15), m)
    check('a net with no own via is untouched', (other.size, other.drill) == (0.3, 0.15))
    if FAILS:
        print(f'\nFAIL: {len(FAILS)} check(s): ' + '; '.join(FAILS))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
