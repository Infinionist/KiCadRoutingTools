#!/usr/bin/env python3
"""The #600 improvement gate sees a cut pour on the GUI front too.

The gate grades every multi-pad net before and after the run. The CLI
front parses the input and the output files, so each state gets its own
board and its own zone fill. The GUI front (`return_results=True`) graded
both states on the one `pcb_data`, swapping only the track lists. A zone's
fill model is cached ON the board it was built for, from the copper that
board carried when the zone was first consulted, which is at the end of
the run. So the "before" grade read the shipped fill. A pour the run had
cut read as cut on the input as well, and the GUI gate never listed the
net as lost. The CLI gate did.

The board: a ZONE_NET pour joins ZONE_NET's two pads, and it holds SIG's J1,
so the #1260 guard leaves it crossable. SIG2 crosses it and cuts it in two.
A pre-existing OTHER track makes the input carry copper, so the gate runs.

Rows:
  * the CLI front (route.py) lists ZONE_NET as lost, with SIG and SIG2
    gained;
  * the GUI front (batch_route with return_results and an in-memory board)
    returns the same lost and gained lists in results_data;
  * plane_fill_model.CopperView in process: two views of ONE board, cut
    and uncut, grade differently. Neither touches the board's own model
    cache or the zone-object lookup that later consumers read.

    python3 tests/test_600_gui_gate_fresh_fill.py
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
from test_1260_pour_guard import _HEAD, _edge, _pad_fp, _zone  # noqa: E402

RUN_ALL_TIMEOUT = 900
ROUTE = run_utils.tool('route.py')
PY = [sys.executable, '-X', 'utf8']
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}"
          + (f" ({detail})" if detail else ''))
    if not ok:
        failures.append(label)


def write_board(path):
    parts = [_HEAD, _pad_fp('J1', 20, 15, 'SIG'), _pad_fp('J2', 35, 15, 'SIG'),
             _pad_fp('J3', 20, 5, 'ZONE_NET'), _pad_fp('J4', 20, 25, 'ZONE_NET'),
             _pad_fp('J5', 5, 21, 'SIG2'), _pad_fp('J6', 35, 21, 'SIG2'),
             _pad_fp('J7', 3, 3, 'OTHER'), _pad_fp('J8', 8, 3, 'OTHER')]
    corners = [(0, 0), (40, 0), (40, 30), (0, 30)]
    for i in range(4):
        parts.append(_edge(*corners[i], *corners[(i + 1) % 4]))
    parts.append(_zone('ZONE_NET', [(14, 1), (26, 1), (26, 29), (14, 29)]))
    parts.append('\t(segment\n\t\t(start 3 3)\n\t\t(end 8 3)\n'
                 '\t\t(width 0.25)\n\t\t(layer "F.Cu")\n\t\t(net "OTHER")\n\t)\n')
    parts.append(')\n')
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(''.join(parts))
    return run_utils.evidence(path, 'gate board')


def _verdict(g):
    return {k: g.get(k) for k in ('lost', 'gained')} if g else None


def test_fronts(tmp):
    src = write_board(os.path.join(tmp, 'gate.kicad_pcb'))
    r = run_utils.check(PY + [ROUTE, src, os.path.join(tmp, 'cli.kicad_pcb'),
                              '--nets', 'SIG', 'SIG2', '--layers', 'F.Cu',
                              '--escalation', 'off'], accept=True)
    m = re.findall(r'JSON_IMPROVEMENT_GATE: (\{.*\})', r.stdout)
    if not m:
        raise AssertionError('BROKEN TEST: the CLI gate did not run\n'
                             + (r.stdout + r.stderr)[-1500:])
    cli = json.loads(m[-1])
    check("CLI: the gate lists ZONE_NET lost, SIG and SIG2 gained",
          cli.get('lost') == ['ZONE_NET']
          and cli.get('gained') == ['SIG', 'SIG2'], str(_verdict(cli)))

    from kicad_parser import parse_kicad_pcb
    from route import batch_route
    _ok, _bad, _t, res = batch_route(
        input_file=src, output_file='', net_names=['SIG', 'SIG2'],
        layers=['F.Cu'], return_results=True,
        pcb_data=parse_kicad_pcb(src))
    gui = res.get('improvement_gate')
    if gui is None:
        raise AssertionError('BROKEN TEST: the GUI gate did not run')
    check("GUI: the gate lists the same lost and gained nets as the CLI",
          _verdict(gui) == _verdict(cli),
          f"GUI {_verdict(gui)} vs CLI {_verdict(cli)}")


def test_view(tmp):
    from improvement_gate import net_connectivity_map
    from kicad_parser import Segment, parse_kicad_pcb
    import plane_fill_model
    from plane_fill_model import CopperView
    pcb = parse_kicad_pcb(write_board(os.path.join(tmp, 'view.kicad_pcb')))
    ids = {n.name: i for i, n in pcb.nets.items()}
    cut = Segment(start_x=5, start_y=21, end_x=35, end_y=21, width=0.25,
                  layer='F.Cu', net_id=ids['SIG2'])
    registered = dict(plane_fill_model._MODELS_BY_ZONE_ID)
    after = net_connectivity_map(CopperView(pcb, pcb.segments + [cut], pcb.vias))
    before = net_connectivity_map(CopperView(pcb, pcb.segments, pcb.vias))
    z = ids['ZONE_NET']
    check("one board, two views: ZONE_NET broken with the cut, whole without "
          "(the cut view graded FIRST)",
          after[z][0] is False and before[z][0] is True,
          f"after={after[z]} before={before[z]}")
    check("the board's own fill cache is untouched",
          not getattr(pcb, plane_fill_model._CACHE_ATTR, None))
    check("the zone-object lookup is untouched",
          plane_fill_model._MODELS_BY_ZONE_ID == registered)


def main():
    tmp = tempfile.mkdtemp(prefix='t600g_')
    try:
        print("both fronts")
        test_fronts(tmp)
        try:
            from plane_fill_model import CopperView  # noqa: F401
        except ImportError as e:
            check("plane_fill_model.CopperView exists", False, str(e))
        else:
            print("CopperView")
            test_view(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"\nFAILED {len(failures)}: " + '; '.join(failures))
        return 1
    print("\nall #600 GUI-gate checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
