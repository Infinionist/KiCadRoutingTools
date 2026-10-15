#!/usr/bin/env python3
"""#1261 follow-up: repair_planes rips no blocker when protection is unreadable.

repair_planes' --rip-blocker-nets keeps protected nets (#521) and nets with
KiCad-locked copper out of the blocker set by reading protection_map. That
lookup sat in a bare `except Exception: pass`: if it raised, the never-rip
set held only the plane nets and a locked net became an ordinary blocker,
silently. It now says so loudly and picks no blocker for the run.

The board: GND pad G1 on F.Cu, boxed by a ring of NET_S, a GND pour on B.Cu,
and a same-net pad clearance that keeps the tap via off the pad, so tapping
G1 needs NET_S ripped. The engine is called in-process, with protection_map
forced to raise where the arm says so.

Checks:
  1. Control: NET_S unlocked, protection readable -- NET_S is ripped and G1
     tapped (the fixture reaches the blocker rip).
  2. NET_S locked, protection readable -- excluded as PROTECTED, not ripped.
  3. NET_S locked, protection_map RAISES -- a loud warning, no blocker
     ripped, NET_S's locked copper untouched. (Base: ripped, silently.)
  4. NET_S unlocked, protection_map RAISES -- still no blocker ripped.

    python3 tests/test_1261_repair_planes_protection_fail_safe.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from run_utils import evidence                                  # noqa: E402
from synth import RoutingBoard, locked_items                    # noqa: E402
import protected_nets                                           # noqa: E402

failures = []
RIP = 'blocked by NET_S - ripping'


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def board(lock):
    b = RoutingBoard()
    b.pad('G1', 20, 15, 'GND', sx=1.0, sy=1.0)
    b.pad('G2', 5, 5, 'GND', sx=1.6, sy=1.6, tht=True)
    b.pad('S1', 30, 25, 'NET_S', sx=1.0, sy=1.0)
    b.pad('S2', 32, 25, 'NET_S', sx=1.0, sy=1.0)
    r = 0.95
    b.ring([(20 - r, 15 - r), (20 + r, 15 - r), (20 + r, 15 + r), (20 - r, 15 + r)],
           'NET_S', w=0.3, locked=lock)
    b.seg(20 + r, 15 + r, 30, 25, 'NET_S', locked=lock)
    b.seg(30, 25, 32, 25, 'NET_S', locked=lock)
    b.items.append(
        '  (zone (net %d) (net_name "GND") (layer "B.Cu") (uuid "synth-zone") '
        '(hatch edge 0.5) (connect_pads (clearance 0.25)) (min_thickness 0.2) '
        '(filled_areas_thickness no) (fill yes (thermal_gap 0.3) '
        '(thermal_bridge_width 0.3)) (polygon (pts (xy 1 1) (xy 39 1) '
        '(xy 39 29) (xy 1 29))))' % b.net('GND'))
    return b.text()


def run(tmp, tag, lock, fail):
    from repair_planes import repair_planes
    inp = os.path.join(tmp, f'{tag}_in.kicad_pcb')
    out = os.path.join(tmp, f'{tag}_out.kicad_pcb')
    with open(inp, 'w', encoding='utf-8') as fh:
        fh.write(board(lock))
    evidence(inp, 'synthetic input board')
    real = protected_nets.protection_map

    def boom(*a, **k):
        raise RuntimeError('forced: protection unreadable (test)')
    if fail:
        protected_nets.protection_map = boom
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            repair_planes(inp, out, net_names=['GND'], plane_layers=['B.Cu'],
                          rip_blocker_nets=True, same_net_pad_clearance=0.2)
    finally:
        protected_nets.protection_map = real
    evidence(out, 'repaired output')
    return buf.getvalue(), inp, out


def main():
    tmp = tempfile.mkdtemp(prefix='t1261p_')
    try:
        print("1. control: NET_S unlocked, protection readable")
        con, _, _ = run(tmp, 'control', lock=False, fail=False)
        if RIP not in con:
            print("  [BROKEN TEST] the control never ripped NET_S: this board "
                  "does not reach the blocker rip")
            return 1
        print("  [ok] NET_S is ripped to tap G1")

        print("2. NET_S locked, protection readable")
        con, inp, out = run(tmp, 'locked', lock=True, fail=False)
        check("NET_S is excluded as protected",
              'PROTECTED net(s) excluded from blocker rip-up (NET_S)' in con)
        check("and not ripped", RIP not in con)

        print("3. NET_S locked, protection_map raises")
        con, inp, out = run(tmp, 'locked_fail', lock=True, fail=True)
        check("the failure is reported loudly",
              'could not be read (RuntimeError: forced: protection unreadable'
              in con and 'NO blocker will be ripped' in con)
        check("no blocker is ripped", RIP not in con,
              '; '.join(l.strip() for l in con.splitlines() if 'rip' in l)[:300])
        before, after = locked_items(inp), locked_items(out)
        check("NET_S's locked copper is untouched",
              len(before) == 6 and after == before,
              f"{len(before)} in, {len(after)} out")

        print("4. NET_S unlocked, protection_map raises")
        con, _, _ = run(tmp, 'free_fail', lock=False, fail=True)
        check("still no blocker is ripped", RIP not in con)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('FAILED: ' + ', '.join(failures) if failures else 'PASS')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
