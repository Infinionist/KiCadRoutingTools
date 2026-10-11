#!/usr/bin/env python3
"""#1229: the #907 seal hint answers about the map the route SEARCHED, and
leaves every via map as it found it.

`same_net_pad_seal_hint` asks whether --same-net-pad-clearance closed the last
via site around a pad: it removes the flag's keep-out cells for that pad, looks
for a legal site, and re-adds them. Every via map saturates at zero, so that
remove/re-add is balanced only on a map that CARRIES the flag's stamp.
single_ended_loop asked after restoring the working map -- the net's own
cached copper back as an obstacle, its #581 keep-out gone -- so:

  * the removal took counts the net's own copper held, and the hint named the
    flag for a seal it did not make (on a net that failed at its OTHER pad);
  * removing cells the map did not hold removed nothing, and the re-add then
    stamped them: rung 0 and the #568 small rung kept cells no cache accounts
    for, for the rest of the run;
  * the probe also removed from, and re-added to, the small rung it never
    reads -- on a clone whose small rung was empty, populating it with the
    keep-out alone.

It also asked every net about rung 0 at the run's via. A net at its own #530
via searches its own rung, where prepare stamps the keep-out at that via
(#1226), so for such a net the hint answered a question the route never asked.

Rows:
  - the probe on a PREPARED map (the issue's repro, net B's pad boxed in by
    other copper): fires at 0.3, silent at 0.01, and every via rung holds the
    same cells after it as before;
  - the probe on a clone whose small rung is EMPTY leaves it empty;
  - a net at its own #530 via: the probe reads that net's rung at that
    rung's via, removes exactly what prepare stamped there, and quotes that
    via's surround; when the net's rung is empty it reads rung 0 at the RUN's
    via, which is where a search at that rung falls back to;
  - end to end through route.py with KICAD_OBSTACLE_AUDIT=1. Net B: P1 on F.Cu
    inside a locked net-A F.Cu ring, P2 on B.Cu (optionally walled in too):
      * P1 sealed by the flag, P2 open (ring 1.0, 0.3): the hint runs in the
        main pass and the audit is BALANCED;
      * P1 sealed by the flag, P2 walled in (ring 1.0, 0.3): B fails and
        JSON_SUMMARY.sealed_by_snpc names P1.1 -- the positive control;
      * P1 open on the searched map, P2 walled in (ring 1.25, 0.01): B fails
        at P2, and sealed_by_snpc is absent.
      * B at its own net-class via, 0.9 against the run's 0.6, P2 walled
        in (ring 1.5, 0.3): sealed at 0.9, open at 0.6 -- sealed_by_snpc
        names P1.1, because the hint asks about the rung B searched.

    python3 tests/test_1229_seal_hint_searched_map.py
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'rust_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'oracle'))

FAILS = []
SEAL_LINE = 'Hint: pad P1.1 is sealed by --same-net-pad-clearance'


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        FAILS.append(name)


def _fp(ref, x, y, net_id, net_name, **k):
    return dict(ref=ref, x=x, y=y, net_id=net_id, net_name=net_name, **k)


# ---------------------------------------------------------------------------
# the probe itself
# ---------------------------------------------------------------------------

def _rungs(obs):
    return [obs.blocked_via_cells_at_rung(r) for r in range(obs.rung_count())]


def _prepared_map(td, snpc):
    """The issue's repro: net B (P1, P2) with every via site around P1 that
    B's own cache does not cover blocked by other copper, prepared for B the
    way single_ended_loop prepares it -- and NOT restored."""
    import numpy as np
    import constraint_agreement as ca
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig, GridCoord
    from obstacle_map import build_base_obstacle_map, build_layer_map
    from obstacle_cache import (precompute_all_net_obstacles,
                                add_net_obstacles_from_cache, expand_via_spans)
    from routing_context import prepare_obstacles_inplace
    board = os.path.join(td, f'probe_{snpc:g}.kicad_pcb')
    ca.write_board(board, footprints=[
        _fp('P1', 10, 10, 2, 'B', size=0.6), _fp('P2', 30, 30, 2, 'B', size=0.6),
        _fp('U1', 30, 5, 1, 'A'), _fp('U5', 20, 30, 3, 'C')])
    pcb = parse_kicad_pcb(board)
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=0.15,
                          clearance=0.15, grid_step=0.1, via_size=0.6,
                          via_drill=0.3, same_net_pad_clearance=snpc)
    base = build_base_obstacle_map(pcb, cfg, [1, 2, 3], static_base=True)
    cache = precompute_all_net_obstacles(pcb, [1, 2, 3], cfg)
    work = base.clone_fresh()
    for nid in (1, 2, 3):
        add_net_obstacles_from_cache(work, cache[nid])
    gx, gy = GridCoord(cfg.grid_step).to_grid(10.0, 10.0)
    own = {tuple(c) for c in cache[2].blocked_vias.tolist()}
    if len(cache[2].blocked_via_spans):
        own |= {tuple(c) for c in
                expand_via_spans(cache[2].blocked_via_spans).tolist()}
    span = int(round(1.3 / cfg.grid_step)) + 2
    work.add_blocked_vias_batch(np.array(
        [(gx + dx, gy + dy) for dx in range(-span, span + 1)
         for dy in range(-span, span + 1) if (gx + dx, gy + dy) not in own],
        dtype=np.int32))
    with contextlib.redirect_stdout(io.StringIO()):
        prepare_obstacles_inplace(work, pcb, cfg, 2, [1, 2, 3], [], {},
                                  build_layer_map(cfg.layers), cache)
    return pcb, cfg, work


def row_prepared_map(td):
    from routing_diagnostics import same_net_pad_seal_hint
    for snpc, fires in ((0.3, True), (0.01, False)):
        pcb, cfg, work = _prepared_map(td, snpc)
        before = _rungs(work)
        hint, verdict = same_net_pad_seal_hint(pcb, cfg, 2, 'B', obstacles=work,
                                               return_verdict=True)
        after = _rungs(work)
        check(f'snpc {snpc:g}: the hint {"fires" if fires else "is silent"} '
              f'on the searched map', bool(hint) == fires,
              (verdict or {}).get('pad', 'silent'))
        check(f'snpc {snpc:g}: every via rung holds the same cells after the '
              f'probe ({[len(r) for r in before]})', before == after,
              f'{[len(r) for r in before]} -> {[len(r) for r in after]}')


def row_empty_small_rung(td):
    """A clone whose small rung holds nothing (add_same_net_via_clearance
    stamps a rung only when it is populated): the probe reads rung 0 and must
    not populate rung 1 with the keep-out alone."""
    import numpy as np
    from obstacle_map import (GridObstacleMap, add_same_net_via_clearance,
                              same_net_pad_via_keepout_cells, _rung_small_armed)
    from routing_config import GridCoord
    from routing_diagnostics import same_net_pad_seal_hint
    pcb, cfg, _ = _prepared_map(td, 0.3)
    obs = GridObstacleMap(2)
    add_same_net_via_clearance(obs, pcb, 2, cfg)
    keep = {tuple(c) for c in
            same_net_pad_via_keepout_cells(pcb, 2, cfg).tolist()}
    gx, gy = GridCoord(cfg.grid_step).to_grid(10.0, 10.0)
    span = int(round(1.3 / cfg.grid_step)) + 2
    obs.add_blocked_vias_batch(np.array(
        [(gx + dx, gy + dy) for dx in range(-span, span + 1)
         for dy in range(-span, span + 1) if (gx + dx, gy + dy) not in keep],
        dtype=np.int32))
    print(f'    (#568 small rung armed: {_rung_small_armed()})')
    before = obs.rung_len(1)
    hint = same_net_pad_seal_hint(pcb, cfg, 2, 'B', obstacles=obs)
    check('the probe ran (the flag seals P1 on this clone)', bool(hint))
    check('an empty small rung stays empty', before == 0 and obs.rung_len(1) == 0,
          f'{before} -> {obs.rung_len(1)}')


RUN_VIA = (0.3, 0.15)
NET_VIA = (0.6, 0.3)        # net B's own #530 via: LARGER than the run's


def _surround(via, snpc=0.3, grid=0.1):
    return round(via[0] / 2 + snpc + grid / 2, 4)


def _walls_around_p1(pcb, cfg, via):
    """Every via cell around P1 except the flag's keep-out at `via`."""
    import numpy as np
    from dataclasses import replace
    from obstacle_map import same_net_pad_via_keepout_cells
    from routing_config import GridCoord
    keep = {tuple(c) for c in same_net_pad_via_keepout_cells(
        pcb, 2, replace(cfg, via_size=via[0], via_drill=via[1])).tolist()}
    gx, gy = GridCoord(cfg.grid_step).to_grid(10.0, 10.0)
    span = int(round(1.3 / cfg.grid_step)) + 2
    return np.array([(gx + dx, gy + dy) for dx in range(-span, span + 1)
                     for dy in range(-span, span + 1)
                     if (gx + dx, gy + dy) not in keep], dtype=np.int32)


def _per_net_board(td):
    import constraint_agreement as ca
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig
    board = os.path.join(td, 'per_net.kicad_pcb')
    ca.write_board(board, footprints=[
        _fp('P1', 10, 10, 2, 'B', size=0.6), _fp('P2', 30, 30, 2, 'B', size=0.6),
        _fp('U1', 30, 5, 1, 'A'), _fp('U5', 20, 30, 3, 'C')])
    pcb = parse_kicad_pcb(board)
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=0.15,
                          clearance=0.15, grid_step=0.1, via_size=RUN_VIA[0],
                          via_drill=RUN_VIA[1], same_net_pad_clearance=0.3,
                          net_via_sizes={2: NET_VIA})
    return pcb, cfg


def row_per_net_rung(td):
    """Net B searches at its own via's rung. Foreign copper -- the same cells
    on rung 0 and on B's rung -- leaves P1 only the sites inside the flag's
    keep-out at B's via: B's via has none, a run-size via (whose keep-out is
    smaller) still has one. The probe must answer for B's rung at B's via,
    and remove from that rung exactly the cells prepare stamped there."""
    from obstacle_map import (build_base_obstacle_map, build_layer_map,
                              _per_net_rungs)
    from obstacle_cache import (precompute_all_net_obstacles,
                                add_net_obstacles_from_cache, rung_for_net)
    from routing_context import prepare_obstacles_inplace
    from routing_diagnostics import same_net_pad_seal_hint
    from single_ended_routing import _per_net_via_config
    pcb, cfg = _per_net_board(td)
    base = build_base_obstacle_map(pcb, cfg, [1, 2, 3], static_base=True)
    cache = precompute_all_net_obstacles(pcb, [1, 2, 3], cfg)
    work = base.clone_fresh()
    for nid in (1, 2, 3):
        add_net_obstacles_from_cache(work, cache[nid])
    with contextlib.redirect_stdout(io.StringIO()):
        prepare_obstacles_inplace(work, pcb, cfg, 2, [1, 2, 3], [], {},
                                  build_layer_map(cfg.layers), cache)
    r = rung_for_net(cfg, pcb, 2)
    net_cfg = _per_net_via_config(cfg, pcb, 2)
    check('fixture: B searches at a populated per-net rung',
          r >= 1 and net_cfg.via_rung == r and r in _per_net_rungs(work)
          and work.rung_len(r) > 0, f'rung {r}')
    walls = _walls_around_p1(pcb, cfg, NET_VIA)
    work.add_blocked_vias_batch(walls)
    work.add_blocked_vias_rung_batch(r, walls)

    before = _rungs(work)
    hint, verdict = same_net_pad_seal_hint(pcb, net_cfg, 2, 'B', obstacles=work,
                                           return_verdict=True)
    after = _rungs(work)
    check("B's config: the hint fires for P1 on B's rung",
          bool(hint) and (verdict or {}).get('pad') == 'P1.1',
          (verdict or {}).get('pad', 'silent'))
    check(f"B's config: the surround quoted is B's via's "
          f"({_surround(NET_VIA)} mm)",
          (verdict or {}).get('required_surround_mm') == _surround(NET_VIA),
          str((verdict or {}).get('required_surround_mm')))
    check('every via rung holds the same cells after the probe',
          before == after,
          f'{[len(x) for x in before]} -> {[len(x) for x in after]}')
    # The question the run config asks -- rung 0, at the run's via -- has a
    # different answer on this map, which is why the loop passes B's config.
    check("the run's config (rung 0, run via) answers otherwise: silent",
          same_net_pad_seal_hint(pcb, cfg, 2, 'B', obstacles=work) == '')
    check('...and leaves every rung as it found it', _rungs(work) == before)


def row_per_net_rung_empty(td):
    """B's rung holds nothing on this map, so a search there reads rung 0
    (is_via_blocked_rung's fallback), whose keep-out was stamped at the RUN's
    via. The probe must read rung 0 and size the keep-out at the run's via."""
    from obstacle_map import GridObstacleMap, add_same_net_via_clearance
    from routing_diagnostics import same_net_pad_seal_hint
    from single_ended_routing import _per_net_via_config
    pcb, cfg = _per_net_board(td)
    net_cfg = _per_net_via_config(cfg, pcb, 2)
    obs = GridObstacleMap(2)
    add_same_net_via_clearance(obs, pcb, 2, cfg)
    obs.add_blocked_vias_batch(_walls_around_p1(pcb, cfg, RUN_VIA))
    check('fixture: B has a rung, and it is empty on this map',
          net_cfg.via_rung >= 1 and obs.rung_len(net_cfg.via_rung) == 0,
          f'rung {net_cfg.via_rung}')
    before = _rungs(obs)
    hint, verdict = same_net_pad_seal_hint(pcb, net_cfg, 2, 'B', obstacles=obs,
                                           return_verdict=True)
    check('the hint fires for P1 on rung 0', bool(hint),
          (verdict or {}).get('pad', 'silent'))
    check(f"the surround quoted is the run's via's ({_surround(RUN_VIA)} mm)",
          (verdict or {}).get('required_surround_mm') == _surround(RUN_VIA),
          str((verdict or {}).get('required_surround_mm')))
    check('every via rung holds the same cells after the probe',
          _rungs(obs) == before)


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------

def _ring(cx, cy, r, layer):
    pts = [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
    return [(*pts[i], *pts[(i + 1) % 4], 0.2, layer, 1) for i in range(4)]


def _e2e_board(path, ring, wall_p2, b_via=None):
    """Net B: P1 (F.Cu) inside a closed net-A F.Cu ring of half-size `ring`,
    tied to net-A pad U1 on its corner -- so P1 leaves only through a via
    inside it. P2 on B.Cu. `wall_p2` walls P2 in: a net-A B.Cu ring around it
    and a net-A F.Cu pad over it, so no track and no via reaches it. Net A is
    locked: no rung may rip the walls."""
    import constraint_agreement as ca
    fps = [_fp('P1', 10, 10, 2, 'B', size=0.6),
           _fp('P2', 30, 30, 2, 'B', size=0.6, layer='B.Cu'),
           _fp('U1', 10 + ring, 10 - ring, 1, 'A')]
    segs = _ring(10, 10, ring, 'F.Cu')
    if wall_p2:
        segs += _ring(30, 30, 0.75, 'B.Cu')
        fps.append(_fp('U2', 30, 30, 1, 'A', size=2.6))
    # b_via: net B's own net class via (#530); the run's via is Default's.
    classes = patterns = ()
    if b_via:
        classes = [dict(ca.DEFAULT_CLASS, name='Big', via_diameter=b_via[0],
                        via_drill=b_via[1], priority=0)]
        patterns = [('B', 'Big')]
    ca.write_board(path, segments=segs, footprints=fps, classes=classes,
                   patterns=patterns)
    text = open(path, encoding='utf-8').read()
    open(path, 'w', encoding='utf-8').write(
        text.replace('") (net 1))', '") (locked yes) (net 1))'))


def _route(td, tag, ring, wall_p2, snpc, b_via=None):
    board = os.path.join(td, f'{tag}.kicad_pcb')
    _e2e_board(board, ring, wall_p2, b_via)
    env = dict(os.environ, KICAD_OBSTACLE_AUDIT='1', PYTHONUTF8='1')
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'py_router', 'route.py'), board,
                        '--output', os.path.join(td, f'{tag}_out.kicad_pcb'),
                        '--nets', 'B', '--same-net-pad-clearance', str(snpc)],
                       capture_output=True, text=True, env=env, cwd=ROOT,
                       timeout=600)
    log = r.stdout + r.stderr
    summaries = [json.loads(s) for s in
                 re.findall(r'^JSON_SUMMARY: (.*)$', log, re.M)]
    audits = re.findall(r'^\s+(BALANCED|LEAK/DESYNC)\b', log, re.M)
    leaks = re.findall(r'^\s+(\w+: [+-]\d+ cells unaccounted.*)$', log, re.M)
    sealed = [s.get('pad') for summ in summaries
              for s in (summ.get('sealed_by_snpc') or [])]
    failed = summaries[0].get('failed_single') if summaries else None
    ok = r.returncode == 0 and bool(summaries) and bool(audits)
    check(f'{tag}: route.py ran, summarised and audited', ok,
          f'rc {r.returncode}, {len(summaries)} summaries, audits {audits}'
          + ('' if ok else '\n' + log[-3000:]))
    return log, audits, leaks, sealed, failed


def row_e2e(td):
    log, audits, leaks, sealed, failed = _route(td, 'seal_open', 1.0, False, 0.3)
    check('flag seal, P2 open: the hint ran in the main pass', SEAL_LINE in log)
    check('flag seal, P2 open: the obstacle audit is BALANCED',
          audits and set(audits) == {'BALANCED'}, '; '.join(leaks) or str(audits))

    log, audits, leaks, sealed, failed = _route(td, 'seal_walled', 1.0, True, 0.3)
    check('flag seal, P2 walled: B fails', failed == ['B'], str(failed))
    check('flag seal, P2 walled: sealed_by_snpc names P1.1', 'P1.1' in sealed,
          str(sealed))
    check('flag seal, P2 walled: the obstacle audit is BALANCED',
          audits and set(audits) == {'BALANCED'}, '; '.join(leaks) or str(audits))

    log, audits, leaks, sealed, failed = _route(td, 'open_walled', 1.25, True, 0.01)
    check('P1 open, P2 walled: B fails', failed == ['B'], str(failed))
    check('P1 open, P2 walled: no sealed_by_snpc (B failed at P2, not at '
          'the flag)', not sealed, str(sealed))

    # Net B at its own net-class via, 0.9 (the run's is 0.6): at 0.9 the ring
    # and the flag leave P1 no site, at 0.6 they leave one. B searched at 0.9.
    log, audits, leaks, sealed, failed = _route(td, 'per_net_via', 1.5, True, 0.3,
                                                b_via=(0.9, 0.45))
    check('own via: B routes at its net-class via',
          'Per-net via sizes for 1 net(s) from their net class / rules: '
          '0.9/0.45 (run via 0.6/0.3)' in log)
    check('own via: B fails', failed == ['B'], str(failed))
    check("own via: sealed_by_snpc names P1.1 (sealed at B's via, not at "
          "the run's)", 'P1.1' in sealed, str(sealed))
    check('own via: the obstacle audit is BALANCED',
          audits and set(audits) == {'BALANCED'}, '; '.join(leaks) or str(audits))


def main():
    try:
        import grid_router  # noqa: F401
    except ImportError:
        print('SKIP: grid_router not importable')
        return 0
    with tempfile.TemporaryDirectory() as td:
        print('-- the probe on the map the route searched')
        row_prepared_map(td)
        print('-- the probe on a clone with an empty small rung')
        row_empty_small_rung(td)
        print("-- a net at its own #530 via: the probe reads the net's rung")
        row_per_net_rung(td)
        print('-- ...and rung 0, at the run\'s via, when that rung is empty')
        row_per_net_rung_empty(td)
        print('-- end to end (route.py, KICAD_OBSTACLE_AUDIT=1)')
        row_e2e(td)
    if FAILS:
        print(f'\nFAIL: {len(FAILS)} check(s): ' + '; '.join(FAILS))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
