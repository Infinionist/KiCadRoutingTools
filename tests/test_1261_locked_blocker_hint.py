#!/usr/bin/env python3
"""#1261 follow-up: the pre-existing blocker hint never offers a refused flag.

preexisting_blocker_hint (#301/#103) names the pre-existing nets boxing in a
failed route and prescribes `--rip-existing-nets '<net>' ...`. For a net with
KiCad-locked copper that flag is refused -- locked has no override (#521) --
yet the hint offered it, and its PROTECTED sentence told the reader to
"override deliberately by naming the net EXACTLY". Now a locked blocker is
named as locked and left out of the command, and a blocker protected for
another reason stays in it with the exact-name override its rule allows.

The board: NET_X's pad X1 is boxed on F.Cu by a GND ring, with NET_A's
locked B.Cu run under it, so NET_X fails boxed in by pre-existing copper.

Checks:
  1. GND locked: no retry command names a locked net, the hint says no flag
     can rip them, and the PROTECTED sentence offers no override for locked
     copper. Evidence: `--rip-existing-nets GND` is refused.
  2. GND protected 'user' (in the .kicad_pro): the command keeps 'GND' and
     says naming it exactly is the override; NET_A (locked) stays out of it.
     Evidence: `--rip-existing-nets GND` makes GND rip-eligible.

    python3 tests/test_1261_locked_blocker_hint.py
"""
import json
import os
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


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def board(gnd_locked):
    b = RoutingBoard()
    b.pad('P1', 10, 10, 'NET_A')
    b.pad('P2', 30, 25, 'NET_A')
    b.seg(10, 10, 12, 10, 'NET_A', locked=True)
    b.via(12, 10, 'NET_A', locked=True)
    b.seg(12, 10, 26, 10, 'NET_A', layer='B.Cu', locked=True)
    b.seg(26, 10, 30, 25, 'NET_A', layer='B.Cu', locked=True)
    b.via(30, 25, 'NET_A', locked=True)
    b.pad('X1', 20, 10, 'NET_X')
    b.pad('X2', 35, 5, 'NET_X')
    b.pad('G1', 20, 3, 'GND')
    b.pad('G2', 25, 3, 'GND')
    b.seg(20, 3, 25, 3, 'GND', locked=gnd_locked)
    b.ring([(19, 9), (21, 9), (21, 11), (19, 11)], 'GND', locked=gnd_locked)
    b.seg(20, 3, 20, 9, 'GND', locked=gnd_locked)
    return b.text()


def route(tmp, tag, gnd_locked, extra=()):
    inp = os.path.join(tmp, f'{tag}_in.kicad_pcb')
    out = os.path.join(tmp, f'{tag}_out.kicad_pcb')
    with open(inp, 'w', encoding='utf-8') as fh:
        fh.write(board(gnd_locked))
    if not gnd_locked:
        with open(os.path.join(tmp, f'{tag}_in.kicad_pro'), 'w',
                  encoding='utf-8') as fh:
            json.dump({"kicad_routing_tools":
                       {"protected_nets": {"GND": "user"}}}, fh)
    evidence(inp, 'synthetic input board')
    env = dict(os.environ, KICAD_PLANE_FINALIZE='0')
    r = run_check([sys.executable, '-X', 'utf8', tool('route.py'), inp, out,
                   '--nets', 'NET_X', '--keep-input-copper', '--escalation',
                   'off', '--no-fix-drc-settings'] + list(extra),
                  accept=True, env=env)
    evidence(out, 'routed output')
    return r.stdout + r.stderr


def hint_lines(con):
    return [l.strip() for l in con.splitlines()
            if 'Hint: the blocking copper' in l]


def prot_lines(con):
    return [l.strip() for l in con.splitlines()
            if 'Hint: the box also includes PROTECTED' in l]


def main():
    tmp = tempfile.mkdtemp(prefix='t1261h_')
    try:
        print("1. the blockers are KiCad-locked")
        con = route(tmp, 'locked', gnd_locked=True)
        hints = hint_lines(con)
        check("precondition: the hint fires", bool(hints))
        cmd = [h for h in hints if '--rip-existing-nets' in h]
        check("no retry command names a locked net", not cmd, (cmd or [''])[0][:240])
        check("the hint says no flag can rip them",
              all('no flag can rip' in h for h in hints), (hints or [''])[0][:240])
        prot = prot_lines(con)
        check("precondition: the PROTECTED sentence fires", bool(prot))
        check("it offers no override for locked copper",
              all('naming the net EXACTLY' not in p and 'no override' in p
                  for p in prot), (prot or [''])[0][:300])
        refused = route(tmp, 'locked_rip', gnd_locked=True,
                        extra=['--rip-existing-nets', 'GND'])
        check("evidence: --rip-existing-nets GND is refused",
              'excluded from --rip-existing-nets (locked: GND)' in refused)

        print("2. GND protected 'user', NET_A locked")
        con = route(tmp, 'user', gnd_locked=False)
        hints = hint_lines(con)
        check("precondition: the hint fires", bool(hints))
        cmd = [h for h in hints if 'Retry with --rip-existing-nets' in h]
        check("the retry command keeps the overridable net",
              bool(cmd) and all("--rip-existing-nets 'GND' to" in h for h in cmd),
              (cmd or [''])[0][:240])
        check("and names the exact-name override",
              bool(cmd) and all("'GND' (user) is protected (#521): naming it "
                                "EXACTLY" in h for h in cmd))
        check("the locked net stays out of the command",
              bool(cmd) and all("'NET_A' to" not in h and "'NET_A' 'GND'" not in h
                                and "'GND' 'NET_A'" not in h for h in cmd))
        lifted = route(tmp, 'user_rip', gnd_locked=False,
                       extra=['--rip-existing-nets', 'GND'])
        check("evidence: --rip-existing-nets GND makes it rip-eligible",
              'pre-existing net(s) eligible for rip-up: GND' in lifted)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('FAILED: ' + ', '.join(failures) if failures else 'PASS')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
