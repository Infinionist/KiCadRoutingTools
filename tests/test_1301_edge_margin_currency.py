"""#1301: an edge connector's `overhang_mm` band, graded on the courtyard,
changed value when a routing step rewrote the project floors.

Pinned here on a synthetic board (40 x 30 mm, rectangular outline,
courtyard-only connectors so no drawn body is measured):

1. An UNSET `min_copper_edge_clearance` (0 or absent) resolves to the fab
   edge floor every routing step writes back -- `fab_edge_floor`, 0.20 --
   not a fixed 0.55, so `board_floor_knobs` answers the same value before
   and after `fix_project_for_output` (the routing writeback) runs. A
   declared value, an explicit one and a caller's own default are unchanged.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

from kicad_parser import parse_kicad_pcb                      # noqa: E402
from placement import floorplan                              # noqa: E402
from test_1184_container_classification import _board        # noqa: E402

EPS = 1e-6
FAB_SOURCE = 'fab edge floor (what routing writes)'
#: (clearance, board edge clearance) pairs the grade is asked at: daplink's
#: placed and routed floors, sonde's, and a wide one.
MARGINS = ((0.2, 0.55), (0.1818, 0.2), (0.254, 0.01), (0.2223, 0.2),
           (0.3, 1.0))


def _fp(ref, x, y, crt, pad=(0.0, 0.0)):
    """A courtyard-only footprint: no Fab, no SilkS, so its band takes the
    legacy (courtyard) path."""
    x0, y0, x1, y1 = crt
    return (f'  (footprint "t:CONN" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")'
            ' (hide yes))\n'
            f'    (fp_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width'
            ' 0.05) (type default)) (layer "F.CrtYd"))\n'
            f'    (pad "1" smd rect (at {pad[0]} {pad[1]}) (size 1 1)'
            ' (layers "F.Cu") (net 1 "N1"))\n'
            '  )\n')


#: J1: courtyard x in [-1.0, 3.0] -> 1.0 mm past the west edge (x = 0).
#: J2: an edge receptacle 0.52 mm inside the north edge (y = 0), no overhang.
BODY = (_fp('J1', 1.0, 15.0, (-2, -3, 2, 3))
        + _fp('J2', 20.0, 2.02, (-2, -1.5, 2, 1.5)))

INTENT = {'schema': 1, 'kind': 'floorplan-intent', 'units': 'mm',
          'edge_connectors': [
              {'ref': 'J1', 'edge': 'west',
               'overhang_mm': {'min': 0.95, 'max': 1.05}},
              {'ref': 'J2', 'edge': 'north', 'class': 'edge_receptacle',
               'overhang_mm': {'min': 0.0}}]}


def _project(pcb_path, rules, clearance=0.2):
    with open(os.path.splitext(pcb_path)[0] + '.kicad_pro', 'w',
              encoding='utf-8') as f:
        json.dump({'net_settings': {'classes': [
            {'name': 'Default', 'clearance': clearance,
             'track_width': 0.2}]},
            'board': {'design_settings': {'rules': rules}}}, f)


class UnsetEdgeIsTheFloorRoutingWrites(unittest.TestCase):

    def test_unset_reads_the_fab_edge_floor_before_and_after_routing(self):
        from fix_kicad_drc_settings import fab_edge_floor, fix_project_for_output
        from list_nets import board_floor_knobs
        for rules in ({'min_copper_edge_clearance': 0.0}, {}):
            with self.subTest(rules=rules), \
                    tempfile.TemporaryDirectory() as td:
                path = _board(td, BODY, size=(40, 30))
                _project(path, rules)
                floor = fab_edge_floor(path)
                self.assertAlmostEqual(floor, 0.2)
                _c, edge, knobs = board_floor_knobs(path)
                self.assertAlmostEqual(edge, floor)
                self.assertEqual(knobs['board_edge_clearance']['source'],
                                 FAB_SOURCE)
                # The routing writeback (every route/plane/fanout step).
                fix_project_for_output(path, path, clearance=0.1818,
                                       verbose=False)
                with open(os.path.splitext(path)[0] + '.kicad_pro') as f:
                    wrote = json.load(f)['board']['design_settings'][
                        'rules']['min_copper_edge_clearance']
                self.assertAlmostEqual(wrote, floor)
                _c, after, knobs = board_floor_knobs(path)
                self.assertAlmostEqual(after, edge)
                self.assertEqual(knobs['board_edge_clearance']['source'],
                                 'board constraint')

    def test_declared_explicit_and_caller_default_are_unchanged(self):
        from list_nets import board_floor_knobs
        with tempfile.TemporaryDirectory() as td:
            path = _board(td, BODY, size=(40, 30))
            _project(path, {'min_copper_edge_clearance': 0.5})
            self.assertEqual(board_floor_knobs(path)[1], 0.5)
            self.assertEqual(board_floor_knobs(path, None, 0.3)[1], 0.3)
            _project(path, {'min_copper_edge_clearance': 0.0})
            _c, edge, knobs = board_floor_knobs(path, edge_default=0.55)
            self.assertEqual((edge, knobs['board_edge_clearance']['source']),
                             (0.55, 'fixed default'))

    def test_no_board_file_reads_the_same_floor(self):
        from list_nets import unset_board_edge_clearance
        self.assertEqual(unset_board_edge_clearance(None), (0.2, FAB_SOURCE))


if __name__ == '__main__':
    unittest.main(verbosity=2)
