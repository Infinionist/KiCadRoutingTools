#!/usr/bin/env python3
"""#1389: board_score / check_complete never read the project's `net_impedance`.

A route step run with --impedance records each net's declaration (ohms and
coplanar gap) in the sibling .kicad_pro. board_score graded impedance only for
nets named by --impedance-nets, so a declared net whose coplanar promise was
broken was never looked at. Now the recorded nets are MEASURED and disclosed,
not counted (a declaration nothing named is not made a blocker):

  1. The recorded nets are measured with no flag at all and named in
     `declared_failing`; the component stays ungraded.
  2. A graded net's `count` includes a net whose declared coplanar gap the
     copper does not have (not only a reference-plane crossing).
  3. A declaration recorded `applied: false` carries no promise: not measured.
  4. --impedance-nets names are graded beside the measured declarations; a net
     name containing glob metacharacters is matched literally.
  5. check_complete (which passes nothing) carries the measured declaration
     and does not grade it.

    python3 tests/test_1389_impedance_declaration_graded.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS_DIR)
for _p in ('py_router', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import run_utils                                               # noqa: E402
from copy_board import copy_board                              # noqa: E402

SRC = os.path.join(ROOT, 'kicad_files', 'routed_output.kicad_pcb')
NET = 'Net-(U2A-DATA_13)'
OTHER = 'Net-(U2A-BE_0)'
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def stage(tmp, name, specs):
    dst = os.path.join(tmp, name)
    with contextlib.redirect_stdout(io.StringIO()):
        copy_board(SRC, dst)
    pro = dst[:-len('.kicad_pcb')] + '.kicad_pro'
    doc = {}
    if os.path.isfile(pro):
        with open(pro, encoding='utf-8') as fh:
            doc = json.load(fh)
    ns = doc.setdefault('kicad_routing_tools', {})
    ns.pop('net_impedance', None)
    if specs:
        ns['net_impedance'] = specs
    with open(pro, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    return dst


def score(board, *extra):
    js = board + '.score.json'
    subprocess.run([sys.executable, '-X', 'utf8',
                    os.path.join(ROOT, 'py_tools', 'board_score.py'), board,
                    '--quiet', '--json', js, *extra], cwd=ROOT,
                   capture_output=True, text=True, encoding='utf-8',
                   errors='replace')
    run_utils.evidence(js, 'board_score json')
    with open(js, encoding='utf-8') as fh:
        return json.load(fh)['components']['impedance']


def main():
    tmp = tempfile.mkdtemp(prefix='t1389_')
    try:
        decl = {'ohms': 50.0, 'differential': False, 'coplanar_gap': 0.25,
                'applied': True}
        b0 = stage(tmp, 'none.kicad_pcb', None)
        c = score(b0)
        check('no record, no flag: impedance is ungraded',
              c.get('ran') is False and c.get('count') is None, str(c))

        b1 = stage(tmp, 'decl.kicad_pcb', {NET: dict(decl)})
        c = score(b1)
        check('a recorded declaration is measured with no flag',
              c.get('declared_nets') == [NET]
              and c.get('declared_failing') == [NET], str(c))
        check('...and not graded or counted',
              c.get('ran') is False and c.get('count') is None, str(c))

        # A broken coplanar promise with no crossing at all counts too: the
        # report is stubbed so the case does not depend on a board's layers.
        import board_score
        real = board_score.run_tool

        def stub(root, tool, board, *a):
            out = a[a.index('--json') + 1]
            row = {'net_name': NET, 'crossings': 0, 'length_no_ground_mm': 9.0,
                   'length_gap_wrong_mm': 0.0}
            ok = {'net_name': OTHER, 'crossings': 0, 'length_no_ground_mm': 0.0,
                  'length_gap_wrong_mm': 0.0}
            with open(out, 'w', encoding='utf-8') as fh:
                json.dump({'totals': {'nets_analyzed': 2, 'nets_with_crossing': 0,
                                      'crossings': 0, 'segments_over_void': 0},
                           'nets': [row, ok]}, fh)
            return 1, ''
        board_score.run_tool = stub
        try:
            c = board_score.score_impedance(ROOT, b1, [NET], tmp)
        finally:
            board_score.run_tool = real
        check('a broken coplanar promise with no crossing counts',
              c.get('count') == 1 and c.get('nets_broken_promise') == [NET]
              and c.get('nets_with_crossing') == 0, str(c))

        b2 = stage(tmp, 'unapplied.kicad_pcb',
                   {NET: dict(decl, applied=False)})
        c = score(b2)
        check('a declaration recorded applied:false is not measured',
              c.get('ran') is False and not c.get('declared_nets'), str(c))

        c = score(b1, '--impedance-nets', OTHER)
        check('--impedance-nets is graded beside the measured declarations',
              c.get('ran') is True and c.get('nets_analyzed') == 1
              and c.get('declared_nets') == [NET]
              and c.get('declared_failing') == [NET], str(c))

        # check_complete passes no impedance flag: the declaration is carried,
        # not graded.
        js = b1 + '.cc.json'
        subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'check_complete.py'), b1, '--skip-slow',
                        '--json', js], cwd=ROOT, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
        run_utils.evidence(js, 'check_complete json')
        with open(js, encoding='utf-8') as fh:
            doc = json.load(fh)
        imp = doc['score']['components']['impedance']
        check('check_complete carries the measured declaration, ungraded',
              imp.get('ran') is False and imp.get('declared_failing') == [NET]
              and 'impedance' in doc.get('ungraded', ()), str(imp)[:200])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('FAILED: ' + ', '.join(failures) if failures else 'PASS')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
