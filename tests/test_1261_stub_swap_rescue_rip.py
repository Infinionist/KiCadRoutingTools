#!/usr/bin/env python3
"""#1261 follow-up: the stub-swap rescue's rip-then-swap step can succeed.

When the swap of a failing net's stub is blocked, the rescue (#424) rips the
rippable blockers and re-validates. The re-validation used the stub registry
built BEFORE the rip, which still held the ripped net's segments, so it
refused every swap the rip had cleared: the rescue ripped, re-validated,
refused, restored -- every time. It now re-validates against a registry of the
board after the rips.

The board, routed on In1/In2 only: NET_A's F.Cu stub sits inside a locked
B.Cu ring admitting a via only at P1's exact (off-grid) centre, so the stub
must leave F.Cu; NET_B, an unlocked pre-existing net, crosses under the stub
on both inner layers, so the swap is blocked until NET_B is ripped.

Checks:
  1. Swap arm: the rescue rips NET_B, swaps the stub and routes NET_A
     ("STUB-SWAP RESCUE ... routed", after the rip); NET_B reroutes and the
     board grades connected and DRC-clean. (Base: rip, refuse, restore.)
  2. Control: a LOCKED NET_D also crosses under the stub. The rip of NET_B
     happens, the re-validation still fails on NET_D, and NET_B is restored
     exactly: its input copper ships byte-identical and no swap is made.

    python3 tests/test_1261_stub_swap_rescue_rip.py
"""
import os
import re
import shutil
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from run_utils import check as run_check, evidence, tool        # noqa: E402
from synth import RoutingBoard                                  # noqa: E402

failures = []
RIP = "PRE-EXISTING rip: 'NET_B'"
SWAP = 'STUB-SWAP RESCUE: NET_A stub F.Cu -> In1.Cu routed'


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def board(control):
    b = RoutingBoard(layers=('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'))
    px, py, ex = 10.05, 15.05, 14.05
    b.pad('P1', px, py, 'NET_A')
    b.pad('P2', 30, 15, 'NET_A', sx=1.6, sy=1.6, tht=True)
    b.seg(px, py, ex, py, 'NET_A')
    b.seg(30, 15, 26, 15, 'NET_A', layer='In2.Cu')
    b.pad('B1', 12, 8, 'NET_B', sx=1.6, sy=1.6, tht=True)
    b.pad('B2', 12, 22, 'NET_B', sx=1.6, sy=1.6, tht=True)
    b.seg(12, 8, 12, 22, 'NET_B', layer='In1.Cu', uid='netb-in1')
    b.seg(12, 8, 12, 22, 'NET_B', layer='In2.Cu', uid='netb-in2')
    if control:
        b.pad('D1', 13.2, 10, 'NET_D', sx=1.0, sy=1.0, tht=True)
        b.pad('D2', 13.2, 20, 'NET_D', sx=1.0, sy=1.0, tht=True)
        b.seg(13.2, 10, 13.2, 20, 'NET_D', layer='In1.Cu', locked=True)
        b.seg(13.2, 10, 13.2, 20, 'NET_D', layer='In2.Cu', locked=True)
    d = 0.25 + 0.25 + 0.15 + 0.01
    b.ring([(round(px - d, 4), round(py - d, 4)), (round(ex + d, 4), round(py - d, 4)),
            (round(ex + d, 4), round(py + d, 4)), (round(px - d, 4), round(py + d, 4))],
           'NET_C', w=0.3, layer='B.Cu', locked=True)
    b.pad('C1', 4, 15, 'NET_C', sx=1.6, sy=1.6, tht=True)
    b.pad('C2', 4, 25, 'NET_C', sx=1.6, sy=1.6, tht=True)
    b.seg(4, 15, round(px - d, 4), round(py - d, 4), 'NET_C', layer='B.Cu', locked=True)
    b.seg(4, 15, 4, 25, 'NET_C', layer='B.Cu', locked=True)
    return b.text()


def netb_lines(path):
    with open(path, encoding='utf-8') as fh:
        return sorted(re.findall(r'\(segment [^\n]*"netb-in[12]"\)', fh.read()))


def route(tmp, tag, control):
    inp = os.path.join(tmp, f'{tag}_in.kicad_pcb')
    out = os.path.join(tmp, f'{tag}_out.kicad_pcb')
    with open(inp, 'w', encoding='utf-8') as fh:
        fh.write(board(control))
    evidence(inp, 'synthetic input board')
    env = dict(os.environ, KICAD_PLANE_FINALIZE='0')
    r = run_check([sys.executable, '-X', 'utf8', tool('route.py'), inp, out,
                   '--nets', '*', '--layers', 'In1.Cu', 'In2.Cu',
                   '--keep-input-copper', '--escalation', 'off',
                   '--no-fix-drc-settings'], accept=True, env=env)
    evidence(out, 'routed output')
    return r.stdout + r.stderr, inp, out


def main():
    tmp = tempfile.mkdtemp(prefix='t1261q_')
    try:
        print("1. rip-then-swap")
        con, inp, out = route(tmp, 'swap', control=False)
        check("the rescue rips the swap blocker", RIP in con)
        check("the swap then succeeds", SWAP in con,
              '; '.join(l.strip() for l in con.splitlines()
                        if 'RESCUE' in l or 'PRE-EXISTING' in l)[:300])
        if RIP in con and SWAP in con:
            check("the swap follows the rip", con.index(RIP) < con.index(SWAP))
        c = run_check([sys.executable, '-X', 'utf8', tool('check_connected.py'),
                       out], accept=True)
        check("the board ships fully connected (NET_B rerouted)",
              'ALL NETS FULLY CONNECTED' in c.stdout,
              '' if 'ALL NETS FULLY CONNECTED' in c.stdout else c.stdout[-300:])
        d = run_check([sys.executable, '-X', 'utf8', tool('check_drc.py'), out,
                       '--clearance', '0.25'], accept=True)
        check("and DRC-clean at the routed clearance",
              'NO DRC VIOLATIONS' in d.stdout,
              '' if 'NO DRC VIOLATIONS' in d.stdout else d.stdout[-300:])

        print("2. control: the re-validation still fails")
        con, inp, out = route(tmp, 'control', control=True)
        check("the rescue rips the swap blocker", RIP in con)
        check("no swap is made", 'STUB-SWAP RESCUE: NET_A' not in con)
        before, after = netb_lines(inp), netb_lines(out)
        check("NET_B's input copper ships byte-identical",
              len(before) == 2 and after == before,
              '' if after == before else f"{before} -> {after}")
        check("and is reported restored",
              "'NET_B': original copper restored" in con)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('FAILED: ' + ', '.join(failures) if failures else 'PASS')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
