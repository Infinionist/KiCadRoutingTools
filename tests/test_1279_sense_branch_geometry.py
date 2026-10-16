#!/usr/bin/env python3
"""The geometry behind a `sense:<REF>.<PAD>` branch is the board's real copper (#1279).

tests/test_1279_sense_branches.py proves the rule on the sense_star fixture
(one straight 0.2 mm trace into the middle of a 1 x 1 mm rect join pad).
This file holds the rule to KiCad's own geometry where the fixture does not
reach, each case built in-test from the fixture's text:

- a load track crossing the sense trace in an X is a tap (KiCad's
  connectivity joins crossing copper, not only endpoints);
- a same-net pad the sense trace runs across off-centre is a sense pad, so
  no load is routed through it onto the trace;

Run:
    python3 tests/test_1279_sense_branch_geometry.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import sense_branch as sb  # noqa: E402
from kicad_parser import parse_kicad_pcb, Segment  # noqa: E402
from run_utils import evidence, tool  # noqa: E402

#: A handful of route.py runs on a 30 x 20 mm board, ~5 s each here.
RUN_ALL_TIMEOUT = 900

BOARD = os.path.join(TESTS_DIR, 'fixtures', 'sense_branch', 'sense_star.kicad_pcb')
PRO = BOARD[:-len('.kicad_pcb')] + '.kicad_pro'
MEMBER = '11111111-0000-0000-0000-000000000001'
#: The fixture's member track, the anchor every variant inserts before.
ANCHOR = '  (segment (start 3 5)'

_TMP = []


def _tmpdir():
    d = tempfile.mkdtemp(prefix='t1279g_')
    _TMP.append(d)
    return d


def _src():
    with open(BOARD, encoding='utf-8') as f:
        text = f.read()
    assert text.count(ANCHOR) == 1, 'fixture member track moved'
    return text


def _variant(text, name='v'):
    """`text` written as a board in a fresh dir, with the fixture's .kicad_pro."""
    d = _tmpdir()
    path = os.path.join(d, f'{name}.kicad_pcb')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    shutil.copy(PRO, os.path.join(d, f'{name}.kicad_pro'))
    return path


def _footprint(ref, x, y, pad='(pad "1" smd rect (at 0 0) (size 1 1) '
                              '(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VDD"))'):
    return (f'  (footprint "T:P" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS") '
            f'(effects (font (size 1 1))))\n    {pad})\n')


def _track(x1, y1, x2, y2, uuid, width=0.3, layer='F.Cu'):
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {width}) '
            f'(layer "{layer}") (net 1) (uuid "{uuid}"))\n')


def _branch(board):
    p = parse_kicad_pcb(board)
    assert len(p.sense_branches) == 1, p.sense_branches
    return p, p.sense_branches[0]


def _run(script, args, timeout=600):
    r = subprocess.run([sys.executable, tool(script)] + args, cwd=ROOT,
                       capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, f'{script} exit {r.returncode}:\n' + r.stdout[-3000:] + r.stderr[-3000:]
    return r.stdout


def _route(board, extra=()):
    """route.py VDD to a FRESH output path; (output board, console)."""
    out = os.path.join(_tmpdir(), 'out.kicad_pcb')
    log = _run('route.py', [board, out, '--nets', 'VDD', '--layers', 'F.Cu', 'B.Cu',
                            '--track-width', '0.3', '--clearance', '0.2'] + list(extra))
    evidence(out, 'route.py output')
    return out, log


def _tapped(board):
    """Pads of VDD reached from the member trace without crossing C1.1
    (the test_1279_sense_branches grader, on sense_branch._touch)."""
    p = parse_kicad_pcb(board)
    join = next(q for q in p.footprints['C1'].pads if q.pad_number == '1')
    items = ([('seg', s) for s in p.segments if s.net_id == join.net_id]
             + [('via', v) for v in p.vias if v.net_id == join.net_id]
             + [('pad', q) for q in p.pads_by_net[join.net_id] if q is not join])
    seen = {i for i, (k, it) in enumerate(items) if k == 'seg' and it.uuid == MEMBER}
    assert seen, 'member trace missing'
    todo = list(seen)
    while todo:
        i = todo.pop()
        for j, other in enumerate(items):
            if j not in seen and sb._touch(items[i], other, join):
                seen.add(j)
                todo.append(j)
    return sorted(f'{it.component_ref}.{it.pad_number}' for i, (k, it) in enumerate(items)
                  if i in seen and k == 'pad')


def _seg(x1, y1, x2, y2, width=0.2, layer='F.Cu'):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2, width=width,
                   layer=layer, net_id=1)


# --- contact ----------------------------------------------------------------

def test_x_crossing_is_a_tap():
    """A U2.1-U4.1 load track crossing the sense trace at (15, 5), both its
    ends 3 mm away: KiCad joins it to the trace (measured on pcbnew 10.0.3,
    GetConnectedItems of the member track lists U2.1 and U4.1), so the input
    already taps the branch. The same crossing ON the join pad is the join."""
    p, b = _branch(_variant(_src().replace(
        ANCHOR, _footprint('U4', 15, 2) + _track(15, 8, 15, 2, '44444444-0000-0000-0000-000000000001')
        + ANCHOR), 'xcross'))
    assert not b.protected and 'input copper already taps it (track (15, 8)-(15, 2)' in b.reason, \
        b.reason
    member = next(s for s in p.segments if s.uuid == MEMBER)
    join = b.join
    assert sb._touch(('seg', _seg(15, 0, 15, 10)), ('seg', member), join)
    # Crossing the member where it lies on C1.1 (3, 5): that contact is the join.
    assert not sb._touch(('seg', _seg(3.2, 4, 3.2, 6)), ('seg', member), join)


def test_pad_crossed_off_centre_is_a_sense_pad():
    """VDD pad U5.1 (2 x 2 mm at (10, 5.4)) with the sense trace running
    through it 0.4 mm off its centre, neither trace end on it: KiCad joins it
    to the trace (pcbnew 10.0.3), so it is a sense pad and never a place to
    attach a load. Before, it stayed a routable pad and the route ran C1.1 ->
    U5.1 -> U2.1, loading the trace from C1 to U5. (0.4 mm, not more: past
    0.6 mm check_connected's #479 through-pad credit no longer joins U5.1 to
    the trace although KiCad does, and the run reports the pad open.)"""
    board = _variant(_src().replace(
        ANCHOR, _footprint('U5', 10, 5.4, '(pad "1" smd rect (at 0 0) (size 2 2) '
                                          '(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VDD"))')
        + ANCHOR), 'thru')
    _p, b = _branch(board)
    assert b.protected, b.reason
    pads = sorted(f'{q.component_ref}.{q.pad_number}' for q in b.sense_pads)
    assert pads == ['U1.1', 'U5.1'], f'sense pads {pads}: U5.1 on the trace is not one'
    out, log = _route(board)
    assert re.search(r'"multipoint_pads_connected": 5, "multipoint_pads_total": 5', log), log[-2000:]
    assert _tapped(out) == ['U1.1', 'U5.1'], f'a load reached the sense copper off C1.1: {_tapped(out)}'


TESTS = [
    test_x_crossing_is_a_tap,
    test_pad_crossed_off_centre_is_a_sense_pad,
]


def main():
    evidence(BOARD, 'the sense_star fixture')
    evidence(PRO, "the sense_star fixture's .kicad_pro")
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
