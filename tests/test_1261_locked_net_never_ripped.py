#!/usr/bin/env python3
"""#1261: no in-run rip picks a KiCad-locked (or protected) net as a victim.

filter_rippable_blockers refused protected nets (#521), but several victim
picks never went through it: the via-in-pad decline blame (#331) inserted its
keystone at the front of the ladder unfiltered -- "Via-in-pad decline blamed
NET_A ... ripping it first", next to a hint calling NET_A protected -- and so
did the N>1 re-analysis picks of every ladder, the stub-swap rescue's swap-
blocker rip, the diff-pair graze/probe/fallback rips and the victim-priority
squatter rip. They all ask protected_nets.rip_refusal now.

Checks:
  1. rip_refusal: 'locked' is refused even for an exactly-named net; another
     reason lifts for an exact name; a diff pair is refused by its partner;
     the refusal lands in PROTECTED_SKIPPED under its context.
  2. route.py on a board where NET_A's locked copper boxes NET_X's pad: the
     decline blames NET_A, but NET_A is not ripped; --json-out records the
     refusal; every locked item comes out byte-identical. A control with
     NET_A unlocked proves the board reaches the blame rip.
  3. Every in-run victim pick in the rip ladders asks rip_refusal (source
     scan -- the N>1 picks have no cheap whole-board repro).

    python3 tests/test_1261_locked_net_never_ripped.py
"""
import json
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
from synth import RoutingBoard, locked_items, make_net, make_pcb  # noqa: E402
import protected_nets                                           # noqa: E402

failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def unit():
    print("1. rip_refusal")
    refusal = getattr(protected_nets, 'rip_refusal', None)
    check("protected_nets.rip_refusal exists", refusal is not None)
    if refusal is None:
        return
    from types import SimpleNamespace
    pcb = make_pcb(nets={1: make_net(1, 'LK'), 2: make_net(2, 'USR'),
                         3: make_net(3, 'FREE'), 4: make_net(4, 'P'),
                         5: make_net(5, 'N')})
    pcb._protection_map_memo = {'LK': 'locked', 'USR': 'user', 'N': 'locked'}
    pcb._rip_override_names = {'LK', 'USR'}
    protected_nets.clear_skipped()
    check("a locked net is refused even when named exactly",
          refusal(pcb, 1, 'ctx') == 'locked')
    check("another reason lifts for an exactly-named net",
          refusal(pcb, 2, 'ctx') is None)
    check("an unprotected net is rippable", refusal(pcb, 3, 'ctx') is None)
    pair = SimpleNamespace(p_net_id=4, n_net_id=5)
    check("a diff pair is refused by its locked partner",
          refusal(pcb, 4, 'ctx', {4: ('PAIR', pair), 5: ('PAIR', pair)})
          == 'locked')
    check("refusals are recorded under their context",
          protected_nets.PROTECTED_SKIPPED.get('ctx') == {'LK': 'locked',
                                                          'N': 'locked'},
          str(protected_nets.PROTECTED_SKIPPED.get('ctx')))
    protected_nets.clear_skipped()


def blame_board(lock):
    """NET_A (routed first) runs on B.Cu directly under NET_X's pad X1, which
    a locked GND ring boxes on F.Cu: X1's via-in-pad unblock declines and
    blames NET_A."""
    b = RoutingBoard()
    b.pad('P1', 10, 10, 'NET_A')
    b.pad('P2', 30, 25, 'NET_A')
    b.seg(10, 10, 12, 10, 'NET_A', locked=lock)
    b.via(12, 10, 'NET_A', locked=lock)
    b.seg(12, 10, 26, 10, 'NET_A', layer='B.Cu', locked=lock)
    b.pad('X1', 20, 10, 'NET_X')
    b.pad('X2', 35, 5, 'NET_X')
    b.ring([(19, 9), (21, 9), (21, 11), (19, 11)], 'GND', locked=True)
    return b.text()


def route(text, tmp, tag):
    inp = os.path.join(tmp, f'{tag}_in.kicad_pcb')
    out = os.path.join(tmp, f'{tag}_out.kicad_pcb')
    js = os.path.join(tmp, f'{tag}.json')
    with open(inp, 'w', encoding='utf-8') as fh:
        fh.write(text)
    evidence(inp, 'synthetic input board')
    env = dict(os.environ, KICAD_PLANE_FINALIZE='0')
    r = run_check([sys.executable, '-X', 'utf8', tool('route.py'), inp, out,
                   '--nets', 'NET_A', 'NET_X', '--ordering', 'original',
                   '--json-out', js, '--keep-input-copper', '--escalation',
                   'off', '--no-fix-drc-settings'], accept=True, env=env)
    evidence(out, 'routed output')
    evidence(js, '--json-out')
    with open(js, encoding='utf-8') as fh:
        doc = json.load(fh)
    return r.stdout + r.stderr, doc, locked_items(inp), locked_items(out)


def whole_board():
    print("2. the via-in-pad decline blame on a locked net")
    blame = 'Via-in-pad decline blamed NET_A (copper under the boxed pad) - ripping it first'
    tmp = tempfile.mkdtemp(prefix='t1261r_')
    try:
        con, _, _, _ = route(blame_board(lock=False), tmp, 'control')
        if blame not in con:
            print("  [BROKEN TEST] the unlocked control never blamed NET_A: "
                  "this board does not reach the decline blame")
            failures.append('broken fixture')
            return
        print("  [ok] control: the unlocked NET_A is blamed and ripped first")
        con, doc, before, after = route(blame_board(lock=True), tmp, 'locked')
        check("the decline still names NET_A", 'decline blamed' in con
              and 'NET_A' in con)
        check("NET_A is not ripped", blame not in con
              and 'Ripping up NET_A' not in con and 'Ripped NET_A' not in con,
              '; '.join(l.strip() for l in con.splitlines()
                        if 'NET_A' in l and 'Rip' in l))
        skipped = doc.get('protected_skipped') or {}
        check("--json-out records the refusal",
              skipped.get('via-in-pad decline blame') == {'NET_A': 'locked'},
              str(skipped))
        check("every locked item comes out byte-identical", after == before,
              f"lost/changed: {sorted(set(before) - set(after))[:2]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# A victim pick: a line deciding a net may be ripped by asking only whether
# it holds this run's copper. Each must also ask rip_refusal within the same
# statement (the condition may wrap onto the next lines).
_PICK = re.compile(r'(\bb\.net_id in routed_results\b|\bgnet not in routed_results\b'
                   r'|\bbnid not in routed_results\b|\b_o in routed_results\b)')
_MODULES = ('single_ended_loop.py', 'diff_pair_loop.py', 'reroute_loop.py',
            'phase3_routing.py', 'layer_swap_fallback.py')


def source_scan():
    print("3. every in-run victim pick asks rip_refusal")
    seen = 0
    for mod in _MODULES:
        path = os.path.join(ROOT, 'py_router', mod)
        with open(path, encoding='utf-8') as fh:
            lines = fh.read().splitlines()
        for i, line in enumerate(lines):
            if line.lstrip().startswith('#') or not _PICK.search(line):
                continue
            seen += 1
            window = '\n'.join(lines[i:i + 4])
            # _rip_blocker asks on its own line just below the membership test
            if mod == 'single_ended_loop.py' and 'bnid not in routed_results' in line:
                window = '\n'.join(lines[i:i + 6])
            check(f"{mod}:{i + 1} asks rip_refusal", 'rip_refusal(' in window,
                  line.strip())
    check("the scan found the victim picks it guards", seen >= 11, f"{seen} found")


def main():
    unit()
    whole_board()
    source_scan()
    print('FAILED: ' + ', '.join(failures) if failures else 'PASS')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
