"""#1240: place_seed's post-polish re-seat seats an edge claim ON ITS EDGE.

Stage 1 seats an edge connector on its edge without reading its block's zone,
so on the round-3 verifier's lateral board -- zone [0, 0, 10, 10] in the
north-west corner, a 4 x 4 mm J1 declared north -- it lands at (20, 0.851),
outside the zone's closed east side. The post-polish re-seat searched with
`_try_place`, which demands the whole courtyard inside the zone and on the
board, so it pulled J1 off its edge: (8.0, 2.851), overhang 0, exit 4 on the
band minimum 1.5; at band minimum 0, (8.0, 2.55), exit 0 with the declared
overhang silently given up. The re-seat now tries `place_seed.reseat_on_edge`
first -- `seeder._seat_edge`, as `--repair` seats an edge claim -- and keeps
it only when the grade shows no zone / keep-out / exclusive-zone / edge error
on the part, no floor fallback and no band contradiction; otherwise it falls
back to `_try_place` as before.

  1. band min 1.5: (5.0, 0.851), exit 0; blind twin (5) the old (8.0, 2.851),
     exit 4.
  2. band min 0: (5.0, 1.55), on its edge; blind, (8.0, 2.55), no overhang.
  3. a band the footprint contradicts (#1241) refuses the edge seat, so the
     fall-back runs and the zone error is still repaired.
  4. the line says WHO broke the rule: a part the seed placed outside, which
     the polish moved and left outside, is not "walked" by the polish; one
     the polish moved out of a zone it was in is.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import place_seed                                            # noqa: E402
from kicad_parser import parse_kicad_pcb                     # noqa: E402
from placement import floorplan                              # noqa: E402
from run_utils import evidence                               # noqa: E402
from test_1174_emit_round_trip import _TALL_J1, _box, _synthetic  # noqa: E402

LATERAL = [0, 0, 10, 10]
BOX_J1 = _box('J1', 20, 20, 2, 2)


def _seed(td, j1=BOX_J1, zone=LATERAL, band_min=1.5, polish=None,
          blind=False):
    """place_seed in process: (rc, J1's written pose, summary, stdout)."""
    path = _synthetic(td, [j1, _box('R1', 25, 25, 1, 0.6)])
    ip = os.path.join(td, 'i.json')
    with open(ip, 'w', encoding='utf-8') as fh:
        json.dump({'schema': floorplan.SCHEMA_VERSION, 'kind': floorplan.KIND,
                   'units': 'mm',
                   'blocks': [{'name': 'b', 'refs': ['J1'], 'zone': zone,
                               'tolerance_mm': 0}],
                   'edge_connectors': [{
                       'ref': 'J1', 'edge': 'north',
                       'overhang_mm': {'min': band_min, 'max': 2.0}}]}, fh)
    out_path = os.path.join(td, 's.kicad_pcb')
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = ['place_seed.py', path, out_path, '--intent', ip, '--force']
    try:
        with contextlib.ExitStack() as stack:
            if polish is not None:
                stack.enter_context(patch('placement.quench.quench', polish))
            if blind:
                stack.enter_context(patch.object(
                    place_seed, 'reseat_on_edge', lambda *a, **k: None))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = place_seed.main()
    finally:
        sys.argv = old
    fp = parse_kicad_pcb(evidence(out_path, 'written seed')).footprints['J1']
    summary = [json.loads(ln.split(':', 1)[1])
               for ln in out.getvalue().splitlines()
               if ln.startswith('JSON_SUMMARY:') and '"pad_edge_after"' in ln]
    return (rc, (fp.x, fp.y, fp.rotation or 0.0), summary[-1],
            out.getvalue() + err.getvalue())


def _edge_rows(summary):
    return {r['ref']: r for r in
            summary['connector_requirements']['overhang_evidence']}


class TheReseatKeepsTheEdge(unittest.TestCase):

    def test_1_band_min_1_5_is_seated_in_its_zone_on_its_edge(self):
        with tempfile.TemporaryDirectory() as td:
            rc, pose, s, out = _seed(td)
        self.assertEqual((rc, pose), (0, (5.0, 0.851, 0.0)), out[-1500:])
        self.assertIn('the seed placed J1 outside a declared '
                      'zone_containment and the polish did not move it',
                      out)
        self.assertEqual(s['grade_errors'], 0)
        with tempfile.TemporaryDirectory() as td:
            rc, pose, s, out = _seed(td, blind=True)
        # The blind twin: `_try_place` pulls it in, overhang 0.
        self.assertEqual((rc, pose), (4, (8.0, 2.851, 0.0)))
        self.assertIn('under the declared minimum 1.50mm', out)

    def test_2_band_min_0_keeps_its_overhang(self):
        with tempfile.TemporaryDirectory() as td:
            rc, pose, s, _out = _seed(td, band_min=0.0)
        self.assertEqual((rc, pose), (0, (5.0, 1.55, 0.0)))
        self.assertAlmostEqual(_edge_rows(s)['J1']['overhang_mm'], 1.0,
                               places=3)
        with tempfile.TemporaryDirectory() as td:
            rc, pose, s, _out = _seed(td, band_min=0.0, blind=True)
        self.assertEqual((rc, pose), (0, (8.0, 2.55, 0.0)))
        self.assertEqual(_edge_rows(s)['J1']['overhang_mm'], 0.0)

    def test_3_a_contradicted_band_falls_back_and_repairs_the_zone(self):
        # The issue's tall J1 in the lateral zone: stage 1 seats it on its
        # edge at the floor-clearing pose (#1241), outside the zone. The edge
        # seat inside the zone carries the band's contradiction, so it is not
        # kept, and `_try_place` repairs the zone as before.
        with tempfile.TemporaryDirectory() as td:
            rc, pose, s, out = _seed(td, j1=_TALL_J1)
        self.assertEqual(rc, 4)
        self.assertEqual(s['grade_error_rules'], {'edge_connector': 1})
        self.assertIn('under the declared minimum 1.50mm', out)
        self.assertLessEqual(pose[0] + 2.0, 10.0)        # inside the zone
        self.assertIn('re-seated against the polished board', out)


class TheLineNamesWhoBrokeTheRule(unittest.TestCase):

    def test_4_a_seed_misplacement_the_polish_moved_is_the_seeds(self):
        # A polish (faked: the real one locks edge claims) that slides J1
        # 1 mm along the edge, still outside its zone.
        def nudge(*_a, **_k):
            return [{'reference': 'J1', 'new_x': 21.0, 'new_y': 0.851,
                     'new_rotation': 0.0}]
        with tempfile.TemporaryDirectory() as td:
            rc, _pose, _s, out = _seed(td, polish=nudge)
        self.assertEqual(rc, 0, out[-1500:])
        self.assertIn('the seed placed J1 outside a declared '
                      'zone_containment and the polish moved it without '
                      'bringing it back', out)
        self.assertNotIn('polish walked J1', out)

    def test_4_a_part_the_polish_walked_out_is_the_polishs(self):
        # The control: J1 seeded INSIDE its zone [10, 0, 30, 10], and a
        # polish that moves it out of it.
        def walk(*_a, **_k):
            return [{'reference': 'J1', 'new_x': 35.0, 'new_y': 0.851,
                     'new_rotation': 0.0}]
        with tempfile.TemporaryDirectory() as td:
            rc, _pose, _s, out = _seed(td, zone=[10, 0, 30, 10], polish=walk)
        self.assertEqual(rc, 0, out[-1500:])
        self.assertIn('polish walked J1 out of a declared zone_containment',
                      out)


if __name__ == '__main__':
    unittest.main()
