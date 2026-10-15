#!/usr/bin/env python3
"""The end-of-run re-grade charges a zone net the run made worse (#1069).

`_final_regrade` grades the board the run ships, and charges a net outside
pass 1's scope only when it grades WORSE than on the input. It graded the
input on pcb_data, swapping only the track lists. But a zone's fill model is
cached on the board it is built for, from the copper that board carries
when the zone is first consulted. At the end of a run that copper is the
run's own, so the "before" grade read the pour as the run left it. A pour
the run cut read as cut on the input too, and the net was reported "no
worse than on the input" (or dropped altogether) when the run had broken
it. The GUI front also graded the shipped copper through pcb_data's fill.
Both states now go through their own `CopperView`.

The board: ZONE_NET's two pads, J3 (20, 5) and J4 (20, 25), joined only by
a pour 12 mm wide across the full height. The "run" routed SIG2 straight
across it at y = 15. As at the end of a real run, pcb_data carries that
track. Driven through `_final_regrade` directly, as
test_1215_failed_counts_casualties is, with summaries shaped like a run's.

Rows, on the CLI front (the written file) and the GUI front (a write
model):
  * ZONE_NET named by a later pass, out of pass 1's scope: broken_by_run
    names it, and the headline calls it broken by this run;
  * ZONE_NET named by no pass, its own copper changed (an input stub the
    run removed): the re-grade keeps it as broken (`unowned_broken`) and
    counts it in `failed`.

    python3 tests/test_1069_regrade_fresh_fill.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _d))

import run_utils                                              # noqa: E402
from test_1260_pour_guard import _HEAD, _edge, _pad_fp, _zone  # noqa: E402

failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}"
          + (f" ({detail})" if detail else ''))
    if not ok:
        failures.append(label)


def _seg(x1, y1, x2, y2, net):
    return ('\t(segment\n\t\t(start %g %g)\n\t\t(end %g %g)\n\t\t(width 0.25)\n'
            '\t\t(layer "F.Cu")\n\t\t(net "%s")\n\t)\n' % (x1, y1, x2, y2, net))


def board_text(extra):
    parts = [_HEAD, _pad_fp('J3', 20, 5, 'ZONE_NET'),
             _pad_fp('J4', 20, 25, 'ZONE_NET'), _pad_fp('J5', 5, 15, 'SIG2'),
             _pad_fp('J6', 35, 15, 'SIG2')]
    corners = [(0, 0), (40, 0), (40, 30), (0, 30)]
    for i in range(4):
        parts.append(_edge(*corners[i], *corners[(i + 1) % 4]))
    parts.append(_zone('ZONE_NET', [(14, 1), (26, 1), (26, 29), (14, 29)]))
    parts += extra
    parts.append(')\n')
    return ''.join(parts)


CUT = _seg(5, 15, 35, 15, 'SIG2')
STUB = _seg(20, 25, 20, 27, 'ZONE_NET')     # ZONE_NET's own input copper


def scenario(tmp, tag, with_stub):
    """(input pcb_data carrying the run's end state, input copper by net,
    shipped file path, the shipped copper)."""
    from kicad_parser import parse_kicad_pcb
    src = os.path.join(tmp, f'{tag}_in.kicad_pcb')
    out = os.path.join(tmp, f'{tag}_out.kicad_pcb')
    with open(src, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(board_text([STUB] if with_stub else []))
    with open(out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(board_text([CUT]))           # the run removed the stub
    with contextlib.redirect_stdout(io.StringIO()):
        pcb = parse_kicad_pcb(run_utils.evidence(src, 'input board'))
        shipped = parse_kicad_pcb(run_utils.evidence(out, 'shipped board'))
    segs_in, vias_in = {}, {}
    for s in pcb.segments:
        segs_in.setdefault(s.net_id, []).append(s)
    # The run's end state, as pcb_data carries it: SIG2's track, no stub.
    sig2 = {n.name: i for i, n in pcb.nets.items()}['SIG2']
    cut = [s for s in shipped.segments][0]
    cut.net_id = sig2
    pcb.segments = [cut]
    return pcb, segs_in, vias_in, out, [cut]


def run_regrade(pcb, out, segs_in, vias_in, front, shipped_segs, summaries,
                signature):
    import route
    route._SUMMARY_SINK.clear()
    route._SUMMARY_SINK.extend(summaries)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            if front == 'cli':
                rec = route._final_regrade(pcb, out, False, None, None,
                                           ['SIG2'], signature, segs_in,
                                           vias_in)
            else:
                def write_model(_rd):
                    by = {}
                    for s in shipped_segs:
                        by.setdefault(s.net_id, []).append(s)
                    return by, {}
                rec = route._final_regrade(pcb, '', True, {}, write_model,
                                           ['SIG2'], signature, segs_in,
                                           vias_in)
    finally:
        route._SUMMARY_SINK.clear()
    if rec is None:
        raise AssertionError(f'BROKEN TEST: no re-grade record\n'
                             f'{buf.getvalue()[-800:]}')
    return rec, buf.getvalue()


def test_named(tmp, front):
    pcb, segs_in, vias_in, out, shipped = scenario(tmp, f'named_{front}', False)
    summaries = [
        {'scope': 'run', 'routed_single': ['SIG2'], 'failed_single': [],
         'open_single': [], 'failed_multipoint': []},
        {'scope': 'reconciliation-subset', 'routed_single': [],
         'failed_single': ['ZONE_NET'], 'open_single': [],
         'failed_multipoint': []}]
    rec, con = run_regrade(pcb, out, segs_in, vias_in, front, shipped,
                           summaries, None)
    check(f"{front}: ZONE_NET, cut by the run, is broken_by_run",
          rec.get('broken_by_run') == ['ZONE_NET'],
          str(rec.get('broken_by_run')))
    line = next((l for l in con.splitlines() if 'Broken by this run' in l), '')
    check(f"{front}: the headline calls it broken by this run",
          'ZONE_NET' in line and not any(
              'no worse than on the input' in l and 'ZONE_NET' in l
              for l in con.splitlines()), line)


def test_disturbed(tmp, front):
    from improvement_gate import copper_signature
    pcb, segs_in, vias_in, out, shipped = scenario(tmp, f'dist_{front}', True)
    names = {i: n.name for i, n in pcb.nets.items()}
    signature = copper_signature([s for l in segs_in.values() for s in l], [],
                                 names.get)
    summaries = [{'scope': 'run', 'routed_single': ['SIG2'],
                  'failed_single': [], 'open_single': [],
                  'failed_multipoint': []}]
    rec, _con = run_regrade(pcb, out, segs_in, vias_in, front, shipped,
                            summaries, signature)
    check(f"{front}: ZONE_NET, its stub removed and its pour cut, is kept "
          f"as broken by the re-grade",
          rec.get('unowned_broken') == ['ZONE_NET'] and rec.get('failed') == 1,
          f"unowned {rec.get('unowned_broken')} failed {rec.get('failed')}")


def main():
    tmp = tempfile.mkdtemp(prefix='t1069f_')
    try:
        for front in ('cli', 'gui'):
            print(f"{front} front")
            test_named(tmp, front)
            test_disturbed(tmp, front)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\nFAILED {len(failures)}: " + '; '.join(failures))
        return 1
    print("\nall #1069 fresh-fill re-grade checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
