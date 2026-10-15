#!/usr/bin/env python3
"""A run that cuts an out-of-scope pour apart says so (#1260).

The pour guard (test_1260_pour_guard.py) blocks a pour island no routed net
has a terminal inside. An island that DOES hold one stays crossable, since
ordinary routing over a full-board pour depends on it. Another routed net
can still cut that island apart. Before this audit the run reported plain
SUCCESS when that happened.

The board: a ZONE_NET pour 12 mm wide across the full height, with
ZONE_NET's two pads at (20, 5) and (20, 25), joined only by the pour. SIG's
J1 sits INSIDE it at (20, 15), so the island is crossable. SIG2 runs from
(5, 21) to (35, 21), across the pour below J1, and cuts it in two. The grader
(check_connected) reads ZONE_NET open on that board. The run now names the
cut too: on the console, in --json-out (`pour_cuts`) and in
JSON_SUMMARY_MIN.

Rows:
  * route.py end to end on that board: the cut is reported, and
    check_connected agrees that ZONE_NET is open. Control: the board without
    SIG has no routed terminal in the island, so the guard blocks it, SIG2
    fails and there is nothing to report;
  * audit_pour_cuts in process, before and after copper chosen directly: a
    track across the pour reports ZONE_NET 0 -> 1+ open pads (both states
    read through one board object, so a shared fill cache would hide it), a
    track that stops short of it or a cut the input already had reports
    nothing, and the board's own fill cache is left alone;
  * audit_nets: only nets with a crossable island are audited.

    python3 tests/test_1260_pour_cut_audit.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _d))

import run_utils                                              # noqa: E402
from test_1260_pour_guard import (_HEAD, _edge, _pad_fp,       # noqa: E402
                                  _zone)

RUN_ALL_TIMEOUT = 900
ROUTE = run_utils.tool('route.py')
CHECK_CONNECTED = run_utils.tool('check_connected.py')
PY = [sys.executable, '-X', 'utf8']
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}"
          + (f" ({detail})" if detail else ''))
    if not ok:
        failures.append(label)


def write_crosscut(path, with_j1=True):
    parts = [_HEAD]
    if with_j1:
        parts += [_pad_fp('J1', 20, 15, 'SIG'), _pad_fp('J2', 35, 15, 'SIG')]
    parts += [_pad_fp('J3', 20, 5, 'ZONE_NET'), _pad_fp('J4', 20, 25, 'ZONE_NET'),
              _pad_fp('J5', 5, 21, 'SIG2'), _pad_fp('J6', 35, 21, 'SIG2')]
    corners = [(0, 0), (40, 0), (40, 30), (0, 30)]
    for i in range(4):
        parts.append(_edge(*corners[i], *corners[(i + 1) % 4]))
    parts.append(_zone('ZONE_NET', [(14, 1), (26, 1), (26, 29), (14, 29)]))
    parts.append(')\n')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(''.join(parts))
    return run_utils.evidence(path, 'crosscut board')


def test_end_to_end(tmp):
    src = write_crosscut(os.path.join(tmp, 'crosscut.kicad_pcb'))
    out = os.path.join(tmp, 'crosscut_out.kicad_pcb')
    js = os.path.join(tmp, 'crosscut.json')
    r = run_utils.check(PY + [ROUTE, src, out, '--nets', 'SIG', 'SIG2',
                              '--layers', 'F.Cu', '--escalation', 'off',
                              '--json-out', js], accept=True)
    log = r.stdout + r.stderr
    run_utils.evidence(out, 'crosscut output')
    m = re.findall(r'JSON_SUMMARY_MIN: (\{.*\})', log)
    if not m:
        raise AssertionError(f'BROKEN TEST: no JSON_SUMMARY_MIN\n{log[-1500:]}')
    mins = json.loads(m[-1])
    doc = json.load(open(run_utils.evidence(js, '--json-out'), encoding='utf-8'))
    check("both signal nets route (the guard left the island crossable)",
          mins.get('routed') == 2 and mins.get('failed') == 0, str(mins))
    check("the console names the cut",
          'Pour cut (#1260): ZONE_NET' in log, log[-600:])
    cuts = doc.get('pour_cuts') or []
    check("--json-out carries it: ZONE_NET 0 -> 1 open pad(s)",
          len(cuts) == 1 and cuts[0]['net'] == 'ZONE_NET'
          and cuts[0]['open_pads_before'] == 0
          and cuts[0]['open_pads_after'] >= 1, str(cuts))
    check("JSON_SUMMARY_MIN carries it", mins.get('pour_cuts') == ['ZONE_NET'],
          str(mins.get('pour_cuts')))
    g = run_utils.check(PY + [CHECK_CONNECTED, out], refuse='ZONE_NET')
    check("the grader agrees ZONE_NET is open on the written board",
          'ZONE_NET' in g.stdout and g.returncode == 1)

    # Control: without SIG (whose J1 made the island crossable) the guard
    # blocks the island, SIG2 fails, and there is no cut to report.
    src2 = write_crosscut(os.path.join(tmp, 'alone.kicad_pcb'), with_j1=False)
    out2 = os.path.join(tmp, 'alone_out.kicad_pcb')
    r2 = run_utils.check(PY + [ROUTE, src2, out2, '--nets', 'SIG2',
                               '--layers', 'F.Cu', '--escalation', 'off'],
                         accept=True)
    log2 = r2.stdout + r2.stderr
    mins2 = json.loads(re.findall(r'JSON_SUMMARY_MIN: (\{.*\})', log2)[-1])
    check("control: with no routed terminal inside, the island is a barrier "
          "and nothing is cut", mins2.get('failed_single') == ['SIG2']
          and 'Pour cut' not in log2 and 'pour_cuts' not in mins2, str(mins2))


def test_in_process(tmp):
    import pour_guard
    from kicad_parser import Segment, parse_kicad_pcb
    pcb = parse_kicad_pcb(write_crosscut(os.path.join(tmp, 'cc.kicad_pcb')))
    sig2 = {n.name: i for i, n in pcb.nets.items()}['SIG2']

    def track(x1, x2):
        return Segment(start_x=x1, start_y=21.0, end_x=x2, end_y=21.0,
                       width=0.25, layer='F.Cu', net_id=sig2)

    # Both states read through ONE board object: had they shared its fill
    # cache, the uncut "before" model would answer for the "after" too.
    before = ([], [])
    cut = pour_guard.audit_pour_cuts(pcb, before, pcb, ([track(5, 35)], []),
                                     ['ZONE_NET'])
    check("a track across the pour: ZONE_NET goes from 0 to 1+ open pads",
          len(cut) == 1 and cut[0]['open_pads_before'] == 0
          and cut[0]['open_pads_after'] >= 1, str(cut))
    short = pour_guard.audit_pour_cuts(pcb, before, pcb, ([track(5, 12)], []),
                                       ['ZONE_NET'])
    check("a track that stops short of the pour: nothing", short == [], str(short))
    again = pour_guard.audit_pour_cuts(pcb, ([track(5, 35)], []), pcb,
                                       ([track(5, 35)], []), ['ZONE_NET'])
    check("a cut already on the input is not this run's", again == [], str(again))
    check("a single-pad or unknown net is skipped",
          pour_guard.audit_pour_cuts(pcb, before, pcb, ([track(5, 35)], []),
                                     ['SIG', 'NO_SUCH_NET']) == [])
    check("the shared board's own fill cache was not written",
          not getattr(pcb, '_plane_fill_models', None))
    rep = {'pours': [{'net': 'A', 'crossable_islands': 1},
                     {'net': 'B', 'crossable_islands': 0, 'blocked_islands': 2},
                     {'net': 'C', 'crossable_islands': 0, 'minor_islands': 3}]}
    check("audit_nets: only nets with a crossable island",
          pour_guard.audit_nets(rep) == ['A'] and pour_guard.audit_nets(None) == [])


def main():
    tmp = tempfile.mkdtemp(prefix='t1260a_')
    try:
        print("route.py end to end")
        test_end_to_end(tmp)
        try:
            import pour_guard
            pour_guard.audit_pour_cuts        # noqa: B018
        except (ImportError, AttributeError) as e:
            check("pour_guard.audit_pour_cuts exists", False, str(e))
        else:
            print("in process")
            test_in_process(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\nFAILED {len(failures)}: " + '; '.join(failures))
        return 1
    print("\nall #1260 pour-cut audit checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
