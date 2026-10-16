#!/usr/bin/env python3
"""Both parse paths agree on the geometry tests/test_1279_sense_branch_geometry.py
holds a sense branch to (#1279).

The GUI builds its PCBData from the live board (build_pcb_data_from_board);
the CLI parses the file (parse_kicad_pcb). For each board below this loads it
with pcbnew in KiCad's bundled python and asserts the branch signatures are
equal, compare_pcb_data reports no "Sense branches" or zone difference, and
the branch resolves as stated:

- hole: a VDD pour over the board on F.Cu with a CUT-OUT around the sense
  trace -- both paths keep the cut-out (Zone.holes; pcbnew loads every later
  `(polygon ...)` of a zone as a hole of its outline) and protect the branch;
- padedge: a VDD pour over a strip of the sense pad U1.1 -- both refuse it.

Exits 77 (SKIP) only when KiCad's bundled python cannot be found.

Run:
    python3 tests/test_1279_sense_branch_geometry_pcbnew.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

from run_utils import evidence  # noqa: E402
from kicad_exact_fill import find_kicad_python  # noqa: E402
import test_1279_sense_branch_geometry as geo  # noqa: E402

RUN_ALL_TIMEOUT = 300
SKIP_EXIT = 77

#: Runs in KiCad's python: one JSON line per board.
PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import pcbnew
from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board, compare_pcb_data
from sense_branch import branch_signature
for path in sys.argv[2:]:
    pb = build_pcb_data_from_board(pcbnew.LoadBoard(path))
    pf = parse_kicad_pcb(path)
    print('RESULT ' + json.dumps({
        'board': path,
        'pcbnew': sorted(map(list, (branch_signature(b) for b in pb.sense_branches))),
        'file': sorted(map(list, (branch_signature(b) for b in pf.sense_branches))),
        'holes': [[len(getattr(z, 'holes', None) or ()) for z in pb.zones],
                  [len(getattr(z, 'holes', None) or ()) for z in pf.zones]],
        'diff': [d for d in compare_pcb_data(pb, pf)
                 if d.startswith('Sense branches') or d.startswith('Zone')],
    }))
'''


def _variants(d):
    src = geo._src()
    texts = {
        'hole': src.replace(geo.ANCHOR, geo._zone([geo.BOARD_RING, geo.SLOT]) + geo.ANCHOR),
        'padedge': src.replace(geo.ANCHOR, geo._zone(
            [[(14, 5.3), (29.5, 5.3), (29.5, 10), (14, 10)]]) + geo.ANCHOR),
    }
    paths = {}
    for name, text in texts.items():
        p = os.path.join(d, f'{name}.kicad_pcb')
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        shutil.copy(geo.PRO, os.path.join(d, f'{name}.kicad_pro'))
        paths[name] = p
    return paths


def main():
    evidence(geo.BOARD, 'the sense_star fixture')
    kpy = find_kicad_python()
    if kpy is None:
        print("SKIP: KiCad's bundled python not found")
        return SKIP_EXIT
    d = tempfile.mkdtemp(prefix='t1279gp_')
    try:
        paths = _variants(d)
        r = subprocess.run([kpy, '-c', PROBE, os.path.join(ROOT, 'py_router')]
                           + list(paths.values()), capture_output=True, text=True,
                           timeout=240)
        rows = {}
        for line in r.stdout.splitlines():
            if line.startswith('RESULT '):
                row = json.loads(line[len('RESULT '):])
                rows[os.path.basename(row['board'])[:-len('.kicad_pcb')]] = row
        if r.returncode != 0 or set(rows) != set(paths):
            print(f'BROKEN TEST: the pcbnew probe ran {sorted(rows)} of {sorted(paths)} '
                  f'(exit {r.returncode}):\n{r.stdout[-2000:]}\n{r.stderr[-3000:]}')
            return 2
        fails = []

        def ck(name, cond, detail=''):
            print(f"  {'PASS' if cond else 'FAIL'}  {name}" + ('' if cond else f'  [{detail}]'))
            if not cond:
                fails.append(name)

        for name, row in sorted(rows.items()):
            ck(f'{name}: both paths resolve the same branches',
               row['pcbnew'] == row['file'] and not row['diff'], json.dumps(row))
            ck(f'{name}: one branch declared', len(row['file']) == 1, json.dumps(row['file']))
        # signature: (name, join, uuids, reason, sense pads, #tracks, #vias)
        hole, padedge = rows['hole'], rows['padedge']
        ck('hole: both paths keep the zone cut-out', hole['holes'] == [[1], [1]], hole['holes'])
        ck('hole: a pour cut around the trace leaves the branch protected',
           hole['pcbnew'][0][3] == '', hole['pcbnew'][0])
        ck('padedge: a pour over a strip of U1.1 refuses the branch on both paths',
           'pours over sense pad U1.1' in padedge['pcbnew'][0][3], padedge['pcbnew'][0])
        total = len(rows) * 2 + 3
        print(f'{total - len(fails)}/{total} checks passed')
        return 1 if fails else 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
