#!/usr/bin/env python3
"""`--sense-branches` keeps new copper off protected Kelvin sense copper (#1279).

Fixture tests/fixtures/sense_branch/sense_star.kicad_pcb (+ its .kicad_pro):
net VDD with join pad C1.1 at (3, 5), sense pad U1.1 at (27, 5) joined by one
0.2 mm F.Cu sense trace, and two load pads U2.1 (15, 8) / U3.1 (20, 8) just
below the trace.

Invariants:
1. Unit: `isolate` moves the sense trace and U1.1 onto a private net (locked,
   aliased back to VDD) and leaves C1.1 on VDD; it refuses to protect a
   branch the input already taps, and one whose copper does not reach the
   join pad yet.
2. End to end (route.py):
   - control, no flag: the router taps U2/U3 onto the sense trace (the
     defect) -- the sense component holds U2.1/U3.1;
   - with the flag: VDD is fully connected, the sense component holds only
     U1.1, and the sense trace is still in the written file.
3. The GUI call shape (in-memory PCBData, return_results=True) hands the
   PCBData back on its real nets.

Run:
    python3 tests/test_1279_sense_branches.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import sense_branch as sb  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402

BOARD = os.path.join(TESTS_DIR, 'fixtures', 'sense_branch', 'sense_star.kicad_pcb')
BRANCH = {'name': 'vos', 'sense': [('U1', '1')], 'join': ('C1', '1')}
GEOM = ['--layers', 'F.Cu', 'B.Cu', '--track-width', '0.3', '--clearance', '0.2']
SENSE_FILE = [{'name': 'vos', 'sense': ['U1.1'], 'join': 'C1.1'}]

_TMP = []


def _tmpdir():
    d = tempfile.mkdtemp(prefix='t1279_')
    _TMP.append(d)
    return d


def _sense_file(doc=None):
    path = os.path.join(_tmpdir(), 'sense.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(SENSE_FILE if doc is None else doc, f)
    return path


def _sense_pads(board):
    _j, _s, items, why = sb.sense_component(parse_kicad_pcb(board), BRANCH)
    assert why is None, why
    return sorted(f"{i.component_ref}.{i.pad_number}" for k, i in items if k == 'pad')


def _route(flag_file=None):
    """route.py to a FRESH output path (route.py reads a sibling .kicad_pro
    back), returning (output board, console)."""
    out = os.path.join(_tmpdir(), 'out.kicad_pcb')
    cmd = [sys.executable, 'py_router/route.py', BOARD, out, '--nets', 'VDD'] + GEOM
    if flag_file:
        cmd += ['--sense-branches', flag_file]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert os.path.isfile(out) and os.path.getsize(out) > 0, 'route.py wrote no board'
    return out, r.stdout


def test_isolate_moves_sense_copper_to_a_private_net():
    p = parse_kicad_pcb(BOARD)
    vdd = next(i for i, n in p.nets.items() if n.name == 'VDD')
    done = sb.isolate(p, [BRANCH])
    pid = done['vos']
    assert p._net_alias == {pid: vdd}
    trace = [s for s in p.segments if s.net_id == pid]
    assert len(trace) == 1 and trace[0].locked
    assert [(q.component_ref) for q in p.pads_by_net[pid]] == ['U1']
    assert sorted(q.component_ref for q in p.pads_by_net[vdd]) == ['C1', 'U2', 'U3']


def test_isolate_refuses_tapped_or_unjoined_branches():
    p = parse_kicad_pcb(BOARD)
    # Already tapped: moving the join to U2 puts C1 in the sense component.
    assert sb.isolate(p, [{**BRANCH, 'join': ('U2', '1')}]) == {}
    p = parse_kicad_pcb(BOARD)
    p.segments = [s for s in p.segments if s.net_id != p.pads_by_net[1][0].net_id]
    assert sb.isolate(p, [BRANCH]) == {}


def test_loader_rejects_bad_pad_keys():
    path = _sense_file([{'name': 'x', 'sense': ['U1'], 'join': 'C1.1'}])
    try:
        sb.load_sense_branches(path)
    except ValueError as e:
        assert 'REF.PAD' in str(e), str(e)
    else:
        raise AssertionError('a pad key without REF.PAD was accepted')


def test_router_keeps_loads_off_the_sense_trace():
    ctrl, _ = _route()
    assert _sense_pads(ctrl) != ['U1.1'], 'control: router must tap the sense trace unprotected'

    out, log = _route(_sense_file())
    assert 'vos: protected 1 segment(s)' in log, log[-2000:]
    assert _sense_pads(out) == ['U1.1'], 'a load reached the sense copper off the join pad'
    r = subprocess.run([sys.executable, 'py_router/check_connected.py', out, '--nets', 'VDD'],
                       cwd=ROOT, capture_output=True, text=True)
    assert 'ALL NETS FULLY CONNECTED' in r.stdout + r.stderr, r.stdout[-2000:]
    with open(out, encoding='utf-8') as f:
        text = f.read()
    assert re.search(r'\(segment\s*\(start 3 5\)\s*\(end 27 5\)', text), 'sense trace was stripped'
    assert '~sense' not in text, 'the private net name reached the written board'


def test_restore_undoes_isolate():
    p = parse_kicad_pcb(BOARD)
    vdd = next(i for i, n in p.nets.items() if n.name == 'VDD')
    nets_before = sorted(p.nets)
    pid = sb.isolate(p, [BRANCH])['vos']
    assert sb.isolate(p, [BRANCH]) == {}, 'a nested call must keep the outer isolation'
    sb.restore(p)
    assert sorted(p.nets) == nets_before and pid not in p.pads_by_net
    assert all(s.net_id == vdd and not s.locked for s in p.segments)
    assert sorted(q.component_ref for q in p.pads_by_net[vdd]) == ['C1', 'U1', 'U2', 'U3']
    assert not p._net_alias


def test_gui_path_routes_to_join_and_hands_pcb_data_back():
    """The GUI's call shape: an in-memory PCBData, return_results=True. The
    engine protects the branch for the run and returns the PCBData on its
    real nets, so the dialog's next run starts from the board as it is."""
    import route as _route_mod
    path = _sense_file()
    p = parse_kicad_pcb(BOARD)
    vdd = next(i for i, n in p.nets.items() if n.name == 'VDD')
    nets_before = sorted(p.nets)
    _ok, _f, _t, data = _route_mod.batch_route(
        BOARD, '', ['VDD'], return_results=True, pcb_data=p,
        layers=['F.Cu', 'B.Cu'], track_width=0.3, clearance=0.2,
        sense_branches=path)
    assert sorted(p.nets) == nets_before, 'private sense net left on the PCBData'
    trace = [s for s in p.segments if (s.start_x, s.end_x) == (3, 27)]
    assert trace and all(s.net_id == vdd and not s.locked for s in trace)
    assert sorted(q.component_ref for q in p.pads_by_net[vdd]) == ['C1', 'U1', 'U2', 'U3']
    new = [s for r in data.get('results', []) for s in (r.get('new_segments') or [])]
    assert new and all(s.net_id == vdd for s in new)
    # No new copper may end on the sense trace (y == 5) away from C1.1.
    for s in new:
        for x, y in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            assert not (abs(y - 5) <= s.width / 2 + 0.11 and x > 3.6), \
                f'new copper ends on the sense trace at ({x}, {y})'


TESTS = [
    test_isolate_moves_sense_copper_to_a_private_net,
    test_isolate_refuses_tapped_or_unjoined_branches,
    test_loader_rejects_bad_pad_keys,
    test_restore_undoes_isolate,
    test_router_keeps_loads_off_the_sense_trace,
    test_gui_path_routes_to_join_and_hands_pcb_data_back,
]


def main():
    if not os.path.isfile(BOARD) or not os.path.isfile(BOARD[:-len('.kicad_pcb')] + '.kicad_pro'):
        print(f'BROKEN TEST: fixture {BOARD} (or its .kicad_pro) is missing')
        return 2
    failed = []
    try:
        for t in TESTS:
            try:
                t()
                print(f'  PASS  {t.__name__}')
            except Exception:  # noqa: BLE001
                failed.append(t.__name__)
                print(f'  FAIL  {t.__name__}')
                traceback.print_exc()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)
    print(f'{len(TESTS) - len(failed)}/{len(TESTS)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
