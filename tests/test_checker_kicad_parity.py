#!/usr/bin/env python3
"""check_connected credits copper the way KiCad's connectivity does.

Two gaps found reviewing #1279 (Kelvin sense branches), where the checker
graded a board open that KiCad grades connected, or the reverse:

- a track passing THROUGH a pad mid-span: the #479 credit was a disc of a
  quarter of the pad's short side about its centre, so a track crossing a
  2 x 2 mm pad 0.7 mm off centre read as not touching it. The credit is now
  the track's copper overlapping the pad's real copper, and the router's
  terminal selector (connectivity._pad_on_group) asks the same question.
  Every case below carries the verdict pcbnew 9.0.6 and 10.0.3 gave (two
  pads, one track anchored on P2, P1 connected iff the ratsnest is empty);
  test_kicad_cross_check re-asks pcbnew when KiCad's python is installed.

Run:
    python3 tests/test_checker_kicad_parity.py
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from check_connected import check_net_connectivity  # noqa: E402

RUN_ALL_TIMEOUT = 300

_TMP = []


def _tmpdir():
    d = tempfile.mkdtemp(prefix='tckp_')
    _TMP.append(d)
    return d


HEAD = '''(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "VDD")
'''
TAIL = ('  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1) (type default)) (fill no) '
        '(layer "Edge.Cuts") (uuid "22222222-0000-0000-0000-000000000001"))\n)\n')


def _fp(ref, x, y, pad):
    return (f'  (footprint "T:P" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS") '
            f'(effects (font (size 1 1))))\n    {pad})\n')


def _pad(sx, sy, shape='rect', extra=''):
    return (f'(pad "1" smd {shape} (at 0 0) (size {sx} {sy}) {extra}'
            f'(layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VDD"))')


ROUNDRECT = _pad(2, 2, 'roundrect', '(roundrect_rratio 0.25) ')
#: steppenprobe C7.1 (tests/test_track_through_pad_connectivity.py), moved
#: from (132.2125, 78.6) to (10, 10).
C7 = _pad(0.875, 0.95, 'roundrect', '(roundrect_rratio 0.25) ')
_C7X, _C7Y = 10 - 132.2125, 10 - 78.6


def _cases():
    """(name, P1 pad at (10, 10), track (x1, y1, x2, y2, width), KiCad connects)."""
    out = []
    # A 2 x 2 rect; horizontal 0.2 mm track at y = 10 + off. Its copper edge
    # meets the pad edge (y = 11) at off = 1.1: overlap 0.1, 0.05, 0.005,
    # exactly tangent, 0.005 short.
    for off, kicad in ((0.7, True), (1.05, True), (1.095, True), (1.1, False), (1.105, False)):
        out.append((f'rect_off{off}', _pad(2, 2), (5, 10 + off, 15, 10 + off, 0.2), kicad))
    # The rect's corner (11, 11): a 45-degree track overlapping it by 0.01,
    # and one 0.01 short of it.
    for gap, kicad in ((-0.01, True), (0.01, False)):
        c = 22 + math.sqrt(2) * (0.1 + gap)
        out.append((f'corner_gap{gap}', _pad(2, 2), (c - 15, 15, 15, c - 15, 0.2), kicad))
    # A 2 x 2 roundrect, corner radius 0.5 about (10.5, 10.5): a track 0.02
    # into the rounded corner touches; ones passing the box corner but 0.05
    # and 0.15 short of the arc do not.
    for gap, kicad in ((-0.02, True), (0.05, False), (0.15, False)):
        c = 21 + math.sqrt(2) * (0.5 + gap + 0.1)
        out.append((f'roundrect_gap{gap}', ROUNDRECT, (c - 15, 15, 15, c - 15, 0.2), kicad))
    # steppenprobe C7: the threading track, and the one #479 shifted by
    # (+0.55, -0.55) as "grazing only the outline corner" -- its copper
    # overlaps the pad by 0.073 mm, and KiCad connects it.
    out.append(('c7_thread', C7, (131.05 + _C7X, 77.75 + _C7Y, 132.8 + _C7X, 79.5 + _C7Y, 0.15), True))
    out.append(('c7_graze', C7, (131.6 + _C7X, 77.2 + _C7Y, 133.35 + _C7X, 78.95 + _C7Y, 0.15), True))
    return out


def _board(d, name, pad, track):
    x1, y1, x2, y2, w = track
    path = os.path.join(d, name + '.kicad_pcb')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(HEAD + _fp('P1', 10, 10, pad) + _fp('P2', x1, y1, _pad(0.3, 0.3))
                + f'  (segment (start {x1:.6f} {y1:.6f}) (end {x2:.6f} {y2:.6f}) (width {w}) '
                  f'(layer "F.Cu") (net 1) (uuid "11111111-0000-0000-0000-000000000001"))\n'
                + TAIL)
    return path


def _boards():
    d = _tmpdir()
    return d, [(name, _board(d, name, pad, track), kicad) for name, pad, track, kicad in _cases()]


def test_track_through_pad_is_kicads_answer():
    """check_net_connectivity joins P1 to the track exactly where pcbnew does."""
    _d, boards = _boards()
    wrong = []
    for name, path, kicad in boards:
        p = parse_kicad_pcb(path)
        r = check_net_connectivity(1, p.segments, p.vias, p.pads_by_net[1], [])
        if r['connected'] != kicad:
            wrong.append(f'{name}: checker {r["connected"]}, KiCad {kicad}')
    assert not wrong, '; '.join(wrong)


def test_terminal_selector_agrees():
    """connectivity._pad_on_group (_pad_touches_copper_group) puts P1 on the
    track's copper group exactly where the grader joins it -- #479's
    "nothing to route" loop needed the two to disagree."""
    from connectivity import _pad_touches_copper_group
    _d, boards = _boards()
    wrong = []
    for name, path, kicad in boards:
        p = parse_kicad_pcb(path)
        p1 = next(q for q in p.footprints['P1'].pads)
        on = _pad_touches_copper_group(p1, p.segments, [], ['F.Cu', 'B.Cu'])
        if on != kicad:
            wrong.append(f'{name}: selector {on}, KiCad {kicad}')
    assert not wrong, '; '.join(wrong)


#: Runs in KiCad's python: one line per board, P1 connected iff no ratsnest.
PROBE = r'''
import os, sys
import pcbnew
for path in sys.argv[1:]:
    b = pcbnew.LoadBoard(path)
    b.BuildConnectivity()
    n = b.GetConnectivity().GetUnconnectedCount(False)
    print('RESULT', os.path.basename(path)[:-len('.kicad_pcb')], n == 0)
'''


def test_kicad_cross_check():
    """pcbnew itself, when KiCad's python is installed: the recorded verdicts
    above are still KiCad's (SKIP otherwise -- the recorded ones stand)."""
    from kicad_exact_fill import find_kicad_python
    kpy = find_kicad_python()
    if kpy is None:
        print("    SKIP: KiCad's bundled python not found; the recorded verdicts stand")
        return
    _d, boards = _boards()
    r = subprocess.run([kpy, '-c', PROBE] + [path for _n, path, _k in boards],
                       capture_output=True, text=True, timeout=240)
    got = {}
    for line in r.stdout.splitlines():
        if line.startswith('RESULT '):
            _tag, name, ok = line.split()
            got[name] = ok == 'True'
    assert r.returncode == 0 and len(got) == len(boards), \
        f'BROKEN: the pcbnew probe ran {sorted(got)} (exit {r.returncode}): {r.stderr[-2000:]}'
    wrong = [f'{n}: recorded {k}, pcbnew {got[n]}' for n, _p, k in boards if got[n] != k]
    assert not wrong, '; '.join(wrong)


TESTS = [
    test_track_through_pad_is_kicads_answer,
    test_terminal_selector_agrees,
    test_kicad_cross_check,
]


def main():
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
