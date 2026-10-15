#!/usr/bin/env python3
"""`--sense-branches` keeps new copper off protected Kelvin sense copper.

Fixture tests/fixtures/sense_branch/sense_star.kicad_pcb: net VDD with join pad C1.1
at (3, 5), sense pad U1.1 at (27, 5) joined by one 0.2 mm F.Cu sense trace, and
two load pads U2.1 (15, 8) / U3.1 (20, 8) just below the trace.

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

Run:
    python3 -m pytest tests/test_538_sense_branches.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import sense_branch as sb  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402

BOARD = os.path.join(TESTS_DIR, 'fixtures', 'sense_branch', 'sense_star.kicad_pcb')
BRANCH = {'name': 'vos', 'sense': [('U1', '1')], 'join': ('C1', '1'), 'force': []}
GEOM = ['--layers', 'F.Cu', 'B.Cu', '--track-width', '0.3', '--clearance', '0.2']


def _sense_pads(board):
    _j, _s, items, why = sb.sense_component(parse_kicad_pcb(board), BRANCH)
    assert why is None, why
    return sorted(f"{i.component_ref}.{i.pad_number}" for k, i in items if k == 'pad')


def _route(flag_file=None):
    fd, out = tempfile.mkstemp(suffix='.kicad_pcb', prefix='sb538_')
    os.close(fd)
    cmd = [sys.executable, 'py_router/route.py', BOARD, out, '--nets', 'VDD'] + GEOM
    if flag_file:
        cmd += ['--sense-branches', flag_file]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
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
    # Already tapped: U2 declared as part of the branch's far side by moving the
    # join to U2 -- C1 then sits in the sense component.
    assert sb.isolate(p, [{**BRANCH, 'join': ('U2', '1')}]) == {}
    p = parse_kicad_pcb(BOARD)
    p.segments = [s for s in p.segments if s.net_id != p.pads_by_net[1][0].net_id]
    assert sb.isolate(p, [BRANCH]) == {}


def test_loader_rejects_bad_pad_keys():
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump([{'name': 'x', 'sense': ['U1'], 'join': 'C1.1'}], f)
    with pytest.raises(ValueError, match='REF.PAD'):
        sb.load_sense_branches(path)


@pytest.mark.integration
def test_router_keeps_loads_off_the_sense_trace():
    ctrl, _ = _route()
    assert _sense_pads(ctrl) != ['U1.1'], 'control: router must tap the sense trace unprotected'

    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump([{'name': 'vos', 'sense': ['U1.1'], 'join': 'C1.1'}], f)
    out, log = _route(path)
    assert 'vos: protected 1 segment(s)' in log
    assert _sense_pads(out) == ['U1.1'], 'a load reached the sense copper off the join pad'
    r = subprocess.run([sys.executable, 'py_router/check_connected.py', out, '--nets', 'VDD'],
                       cwd=ROOT, capture_output=True, text=True)
    assert 'ALL NETS FULLY CONNECTED' in r.stdout + r.stderr
    text = open(out).read()
    assert re.search(r'\(segment\s*\(start 3 5\)\s*\(end 27 5\)', text), 'sense trace was stripped'


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


@pytest.mark.integration
def test_gui_path_routes_to_join_and_hands_pcb_data_back():
    """The GUI's call shape: an in-memory PCBData, return_results=True. The
    engine protects the branch for the run and returns the PCBData on its
    real nets, so the dialog's next run starts from the board as it is."""
    import route as _route
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump([{'name': 'vos', 'sense': ['U1.1'], 'join': 'C1.1'}], f)
    p = parse_kicad_pcb(BOARD)
    vdd = next(i for i, n in p.nets.items() if n.name == 'VDD')
    nets_before = sorted(p.nets)
    _ok, _f, _t, data = _route.batch_route(
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
