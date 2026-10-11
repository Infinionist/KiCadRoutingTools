#!/usr/bin/env python3
"""A per-net via rung in slot 1 carries the #581 keep-out at ITS via.

The #568 small rung is armed by an env default, but a board only HAS a small
slot when the fab ladder holds a via smaller than the run's. Where it does
not -- the run's via is already the ladder bottom (0.25/0.15 on the 2- and
4-layer ladders), or --escalation off -- a net at its own #530 via sits in
slot 1. prepare_obstacles_inplace took "armed" to mean "slot 1 is the small
rung": its per-net mirror skipped slot 1, and the small mirror stamped it
with the --same-net-pad-clearance keep-out at the RUN's via. A net whose own
via is larger was then held to the run via's keep-out, and route.py shipped
a 0.8 via 0.166 mm from its own SMD pad under --same-net-pad-clearance 0.3.

Rows:
  - no small slot (run via 0.25/0.15, net B at 0.6/0.3 in slot 1): after
    prepare, B's slot-1 rung blocks every cell of the keep-out at B's via;
    after restore, every rung is back to base + caches;
  - with a small slot (run via 0.3/0.15): B's rung is slot 2 and blocks the
    keep-out at B's via, the small rung carries it at the run's via, and the
    restore balances;
  - the #907 seal hint on the slot-1 map reads B's rung at B's via and leaves
    every rung as it found it;
  - end to end: route.py, run via 0.25/0.15, B at a 0.8/0.4 class via, P1
    inside a locked foreign ring whose only open via sites lie within the
    keep-out at 0.8 but outside it at 0.25: every via B ships keeps
    --same-net-pad-clearance from B's own pads, and the obstacle audit is
    balanced.

    python3 tests/test_same_net_keepout_slot1_rung.py
"""
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import replace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'oracle'))

FAILS = []
SNPC = 0.3
NET_VIA = (0.6, 0.3)


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        FAILS.append(name)


def _fp(ref, x, y, net_id, net_name, **k):
    return dict(ref=ref, x=x, y=y, net_id=net_id, net_name=net_name, **k)


def _rungs(obs):
    return [obs.blocked_via_cells_at_rung(r) for r in range(obs.rung_count())]


def _board(td):
    import constraint_agreement as ca
    from kicad_parser import parse_kicad_pcb
    path = os.path.join(td, 'map.kicad_pcb')
    ca.write_board(path, footprints=[
        _fp('P1', 10, 10, 2, 'B', size=0.6), _fp('P2', 30, 30, 2, 'B', size=0.6),
        _fp('U1', 30, 5, 1, 'A'), _fp('U5', 20, 30, 3, 'C')])
    return parse_kicad_pcb(path)


def _cfg(run_via):
    from routing_config import GridRouteConfig
    return GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=0.15,
                           clearance=0.15, grid_step=0.1, via_size=run_via[0],
                           via_drill=run_via[1], same_net_pad_clearance=SNPC,
                           net_via_sizes={2: NET_VIA})


def _keepout(pcb, cfg, via):
    from obstacle_map import same_net_pad_via_keepout_cells
    return {tuple(c) for c in same_net_pad_via_keepout_cells(
        pcb, 2, replace(cfg, via_size=via[0], via_drill=via[1])).tolist()}


def _working(pcb, cfg):
    from obstacle_map import build_base_obstacle_map
    from obstacle_cache import (precompute_all_net_obstacles,
                                add_net_obstacles_from_cache)
    base = build_base_obstacle_map(pcb, cfg, [1, 2, 3], static_base=True)
    cache = precompute_all_net_obstacles(pcb, [1, 2, 3], cfg)
    work = base.clone_fresh()
    for nid in (1, 2, 3):
        add_net_obstacles_from_cache(work, cache[nid])
    return base, cache, work


def _prepare(pcb, cfg, work, cache):
    from obstacle_map import build_layer_map
    from routing_context import prepare_obstacles_inplace
    with contextlib.redirect_stdout(io.StringIO()):
        _, cells = prepare_obstacles_inplace(work, pcb, cfg, 2, [1, 2, 3], [], {},
                                             build_layer_map(cfg.layers), cache)
    return cells


def _residual(base, work, cache):
    """Per-rung cells left once every net's cache is taken back off a copy
    of the working map -- base's own cells when the map is balanced."""
    from obstacle_cache import remove_net_obstacles_from_cache
    probe = work.clone_fresh()
    for cd in cache.values():
        remove_net_obstacles_from_cache(probe, cd)
    return _rungs(probe), _rungs(base)


def _open(work, cells, rung):
    return sum(1 for c in cells if not work.is_via_blocked_rung(c[0], c[1], rung))


def row_slot1(td):
    from obstacle_cache import rung_for_net, via_rungs, rung_small_armed
    from obstacle_map import _per_net_rung_geometry
    from routing_context import restore_obstacles_inplace
    pcb = _board(td)
    cfg = _cfg((0.25, 0.15))
    if not rung_small_armed():
        print('  (the #568 small rung is not armed in this environment: the '
              'layout under test does not arise; rows skipped)')
        return
    r = rung_for_net(cfg, pcb, 2)
    base, cache, work = _working(pcb, cfg)
    check('fixture: no small slot, B sits in slot 1 as a per-net rung',
          r == 1 and via_rungs(cfg, pcb) == [NET_VIA]
          and (1, *NET_VIA) in _per_net_rung_geometry(work, cfg),
          f'rung {r}, rungs {via_rungs(cfg, pcb)}')
    cells = _prepare(pcb, cfg, work, cache)
    k_net, k_run = _keepout(pcb, cfg, NET_VIA), _keepout(pcb, cfg, (0.25, 0.15))
    band = k_net - k_run
    check(f"slot 1 blocks the keep-out at B's via ({len(k_net)} cells)",
          _open(work, k_net, r) == 0,
          f'{_open(work, band, r)}/{len(band)} cells only a 0.6 via covers open')
    restore_obstacles_inplace(work, 2, cache, cells)
    got, want = _residual(base, work, cache)
    check('restore: every rung back to base + caches', got == want,
          f'{[len(x) for x in got]} vs {[len(x) for x in want]}')


def row_small_slot(td):
    from obstacle_cache import rung_for_net, via_rungs, rung_small_armed
    from routing_context import restore_obstacles_inplace
    pcb = _board(td)
    run = (0.3, 0.15)
    cfg = _cfg(run)
    if not rung_small_armed():
        return
    r = rung_for_net(cfg, pcb, 2)
    base, cache, work = _working(pcb, cfg)
    check('fixture: a small slot exists, B sits in slot 2',
          r == 2 and len(via_rungs(cfg, pcb)) == 2, f'rung {r}, rungs {via_rungs(cfg, pcb)}')
    cells = _prepare(pcb, cfg, work, cache)
    k_net, k_run = _keepout(pcb, cfg, NET_VIA), _keepout(pcb, cfg, run)
    check("B's rung blocks the keep-out at B's via", _open(work, k_net, r) == 0,
          f'{_open(work, k_net, r)} open')
    check("the small rung carries it at the run's via", _open(work, k_run, 1) == 0,
          f'{_open(work, k_run, 1)} open')
    restore_obstacles_inplace(work, 2, cache, cells)
    got, want = _residual(base, work, cache)
    check('restore: every rung back to base + caches', got == want,
          f'{[len(x) for x in got]} vs {[len(x) for x in want]}')


def row_seal_hint(td):
    import numpy as np
    from obstacle_cache import rung_small_armed
    from routing_config import GridCoord
    from routing_diagnostics import same_net_pad_seal_hint
    from single_ended_routing import _per_net_via_config
    if not rung_small_armed():
        return
    pcb = _board(td)
    cfg = _cfg((0.25, 0.15))
    base, cache, work = _working(pcb, cfg)
    _prepare(pcb, cfg, work, cache)
    net_cfg = _per_net_via_config(cfg, pcb, 2)
    # foreign copper on both rungs: every site around P1 but the keep-out at B's via
    keep = _keepout(pcb, cfg, NET_VIA)
    gx, gy = GridCoord(cfg.grid_step).to_grid(10.0, 10.0)
    span = int(round(1.3 / cfg.grid_step)) + 2
    walls = np.array([(gx + dx, gy + dy) for dx in range(-span, span + 1)
                      for dy in range(-span, span + 1)
                      if (gx + dx, gy + dy) not in keep], dtype=np.int32)
    work.add_blocked_vias_batch(walls)
    work.add_blocked_vias_rung_batch(net_cfg.via_rung, walls)
    before = _rungs(work)
    hint, verdict = same_net_pad_seal_hint(pcb, net_cfg, 2, 'B', obstacles=work,
                                           return_verdict=True)
    need = round(NET_VIA[0] / 2 + SNPC + cfg.grid_step / 2, 4)
    check("the seal hint names P1 at B's via on slot 1",
          bool(hint) and (verdict or {}).get('required_surround_mm') == need,
          str(verdict and verdict.get('required_surround_mm')))
    check('...and leaves every rung as it found it', _rungs(work) == before)


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

def _e2e_board(path, run_via, b_via, ring=1.45):
    """Net B: P1 (F.Cu) inside a locked net-A ring tied to U1, P2 on B.Cu.
    The ring leaves via sites only in P1's diagonal corners, which lie inside
    the keep-out at B's 0.8 via and outside it at the run's 0.25."""
    import constraint_agreement as ca
    pts = [(10 - ring, 10 - ring), (10 + ring, 10 - ring),
           (10 + ring, 10 + ring), (10 - ring, 10 + ring)]
    segs = [(*pts[i], *pts[(i + 1) % 4], 0.2, 'F.Cu', 1) for i in range(4)]
    ca.write_board(path, segments=segs, footprints=[
        _fp('P1', 10, 10, 2, 'B', size=0.6),
        _fp('P2', 30, 30, 2, 'B', size=0.6, layer='B.Cu'),
        _fp('U1', 10 + ring, 10 - ring, 1, 'A')],
        classes=[dict(ca.DEFAULT_CLASS, name='Big', via_diameter=b_via[0],
                      via_drill=b_via[1], priority=0)],
        patterns=[('B', 'Big')])
    text = open(path, encoding='utf-8').read()
    open(path, 'w', encoding='utf-8').write(
        text.replace('") (net 1))', '") (locked yes) (net 1))'))
    pro = os.path.splitext(path)[0] + '.kicad_pro'
    with open(pro, encoding='utf-8') as f:
        proj = json.load(f)
    proj['net_settings']['classes'][0].update(via_diameter=run_via[0],
                                              via_drill=run_via[1])
    with open(pro, 'w', encoding='utf-8') as f:
        json.dump(proj, f, indent=2)


def _edge_gap(vx, vy, size, pads):
    """Via edge to the nearest own-pad edge (square pads: (x, y, half))."""
    return min(math.hypot(max(abs(vx - px) - h, 0.0), max(abs(vy - py) - h, 0.0))
               for px, py, h in pads) - size / 2


def row_e2e(td):
    from kicad_parser import parse_kicad_pcb
    from obstacle_cache import rung_for_net, via_rungs, rung_small_armed
    from routing_config import GridRouteConfig
    if not rung_small_armed():
        return
    run_via, b_via = (0.25, 0.15), (0.8, 0.4)
    board = os.path.join(td, 'e2e.kicad_pcb')
    out = os.path.join(td, 'e2e_out.kicad_pcb')
    _e2e_board(board, run_via, b_via)
    pcb = parse_kicad_pcb(board)
    probe = GridRouteConfig(layers=['F.Cu', 'B.Cu'], via_size=run_via[0],
                            via_drill=run_via[1], net_via_sizes={2: b_via})
    check('fixture: B sits in slot 1 on this board',
          rung_for_net(probe, pcb, 2) == 1 and via_rungs(probe, pcb)[0] == b_via,
          str(via_rungs(probe, pcb)))
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'py_router', 'route.py'), board,
                        '--output', out, '--nets', 'B',
                        '--same-net-pad-clearance', str(SNPC)],
                       capture_output=True, text=True, cwd=ROOT, timeout=600,
                       env=dict(os.environ, KICAD_OBSTACLE_AUDIT='1', PYTHONUTF8='1'))
    log = r.stdout + r.stderr
    audits = re.findall(r'^\s+(BALANCED|LEAK/DESYNC)\b', log, re.M)
    ok = r.returncode == 0 and os.path.exists(out) and bool(audits)
    check('route.py ran, wrote the board and audited', ok,
          f'rc {r.returncode}' + ('' if ok else '\n' + log[-3000:]))
    check('B routes at its own class via', 'Per-net via sizes for' in log
          and f'{b_via[0]:g}/{b_via[1]:g}' in log)
    if not ok:
        return
    text = open(out, encoding='utf-8').read()
    vias = [tuple(map(float, m.groups())) for m in re.finditer(
        r'\(via\s+\(at ([\d.]+) ([\d.]+)\)\s+\(size ([\d.]+)\)[^()]*'
        r'\(drill [\d.]+\)\s+\(layers[^)]*\)\s+\(net 2\)', text)]
    pads = [(10.0, 10.0, 0.3), (30.0, 30.0, 0.3)]
    gaps = [round(_edge_gap(x, y, s, pads), 3) for x, y, s in vias]
    check(f'every via B ships keeps {SNPC} mm from its own pads',
          all(g >= SNPC - 1e-6 for g in gaps), f'vias {vias}, gaps {gaps}')
    check('the obstacle audit is BALANCED', set(audits) == {'BALANCED'},
          str(audits))


def main():
    try:
        import grid_router  # noqa: F401
    except ImportError:
        print('SKIP: grid_router not importable')
        return 0
    with tempfile.TemporaryDirectory() as td:
        print('-- no small slot: a per-net rung in slot 1')
        row_slot1(td)
        print('-- a small slot: the per-net rung in slot 2')
        row_small_slot(td)
        print('-- the #907 seal hint on the slot-1 map')
        row_seal_hint(td)
        print('-- end to end (route.py, KICAD_OBSTACLE_AUDIT=1)')
        row_e2e(td)
    if FAILS:
        print(f'\nFAIL: {len(FAILS)} check(s): ' + '; '.join(FAILS))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
