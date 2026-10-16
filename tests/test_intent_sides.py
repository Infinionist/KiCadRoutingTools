"""The floorplan's side declarations can be applied before ``place_seed``.

The seeder correctly refuses to interpret front-side geometry as back-side
geometry, but until this actor existed there was no intent-driven way to make
its input agree with a two-sided plan.  A board could therefore seed every
X/Y zone and still leave all footprints on F.Cu.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))

from kicad_parser import parse_kicad_pcb
from placement.legality import footprint_side

TOOL = os.path.join(ROOT, 'py_placer', 'place_intent_sides.py')


def _board(path):
    path.write_text('''(kicad_pcb
  (version 20241229)
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))
  (footprint "test:R" (layer "F.Cu") (uuid "fp-r1") (at 5 5 90)
    (property "Reference" "R1" (at 0 0 90) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1 -0.5) (end 1 0.5) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
    (pad "1" smd rect (at -0.5 0 90) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask")))
  (footprint "test:C" (layer "F.Cu") (uuid "fp-c1") (at 10 5)
    (property "Reference" "C1" (at 0 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1 -0.5) (end 1 0.5) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask"))))
''')


def _intent(path, c_side='F'):
    path.write_text(json.dumps({
        'schema': 1,
        'kind': 'floorplan-intent',
        'units': 'mm',
        'blocks': [
            {'name': 'rear', 'refs': ['R1'], 'zone': [1, 1, 9, 9], 'side': 'B'},
            {'name': 'front', 'refs': ['C1'], 'zone': [9, 1, 19, 9], 'side': c_side},
        ],
    }))


def test_cli_mirrors_only_mismatched_declared_sides():
    temp = tempfile.TemporaryDirectory()
    tmp_path = Path(temp.name)
    board = tmp_path / 'in.kicad_pcb'
    intent = tmp_path / 'intent.json'
    output = tmp_path / 'out.kicad_pcb'
    _board(board)
    _intent(intent)

    run = subprocess.run(
        [sys.executable, TOOL, str(board), str(output), '--intent', str(intent),
         '--group-by', 'none'], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'mirrored 1 footprint(s)' in run.stdout
    pcb = parse_kicad_pcb(str(output))
    assert footprint_side(pcb.footprints['R1']) == 'B'
    assert footprint_side(pcb.footprints['C1']) == 'F'
    assert (pcb.footprints['R1'].x, pcb.footprints['R1'].y,
            pcb.footprints['R1'].rotation) == (5.0, 5.0, 90.0)
    temp.cleanup()


def test_cli_refuses_contradictory_side_claims():
    temp = tempfile.TemporaryDirectory()
    tmp_path = Path(temp.name)
    board = tmp_path / 'in.kicad_pcb'
    intent = tmp_path / 'intent.json'
    output = tmp_path / 'out.kicad_pcb'
    _board(board)
    _intent(intent, c_side='F')
    doc = json.loads(intent.read_text())
    doc['fixed_poses'] = [{
        'ref': 'R1', 'x': 5, 'y': 5, 'side': 'F',
        'basis': 'declared', 'why': 'negative control',
    }]
    intent.write_text(json.dumps(doc))

    run = subprocess.run(
        [sys.executable, TOOL, str(board), str(output), '--intent', str(intent),
         '--group-by', 'none'], capture_output=True, text=True)
    assert run.returncode == 2
    assert 'contradictory side declarations' in run.stderr
    assert not output.exists()
    temp.cleanup()


TESTS = [
    test_cli_mirrors_only_mismatched_declared_sides,
    test_cli_refuses_contradictory_side_claims,
]


if __name__ == '__main__':
    for test in TESTS:
        print(f'--- {test.__name__}')
        test()
    print('ALL PASS')
