"""#1241: place_seed's gate charges pad copper it placed past the outline.

#975's floor fallback keeps an edge connector's seat "rather than leave the
connector unseated", and on a body-less part stage 1 tests only pad CENTRES
against the board, so a band `min` that pads on the board cannot reach left
J1's copper 1.2 mm past the north edge. Nothing after stage 1 charged it:
place_seed exited 0 on a seed check_assembly calls NOT BUILDABLE. The gate now
reads the measure check_assembly gates on (`oob_pad_copper_gating_refs`),
for the parts this seed moved and did seat, and the floor note says the
copper is off the board rather than "inside the board-edge floor".

What this does NOT change (still open in #1241): stage 1 keeps the seat, and
`--repair` is not gated this way.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import place_seed                                            # noqa: E402
from placement import floorplan                              # noqa: E402
from run_utils import check, evidence                        # noqa: E402
from test_1174_emit_round_trip import _TALL_J1, _box, _synthetic  # noqa: E402

PY = sys.executable


def _seed(td, band, code, refuse=None, extra=()):
    """Seed the body-less J1 with a north claim of `band` and no zone."""
    path = _synthetic(td, [_TALL_J1, _box('R1', 25, 25, 1, 0.6)] + list(extra))
    ip = os.path.join(td, 'i.json')
    with open(ip, 'w', encoding='utf-8') as fh:
        json.dump({'schema': floorplan.SCHEMA_VERSION, 'kind': floorplan.KIND,
                   'units': 'mm', 'blocks': [],
                   'edge_connectors': [{'ref': 'J1', 'edge': 'north',
                                        'overhang_mm': band}]}, fh)
    out = os.path.join(td, 's.kicad_pcb')
    r = check([PY, '-X', 'utf8',
               os.path.join(ROOT, 'py_placer', 'place_seed.py'),
               path, out, '--intent', ip, '--force'],
              **({'accept': True} if code == 0
                 else {'refuse': refuse, 'code': code}))
    evidence(out, 'written seed')
    summary = [json.loads(ln.split(':', 1)[1]) for ln in r.stdout.splitlines()
               if ln.startswith('JSON_SUMMARY:')
               and '"pad_edge_after"' in ln][-1]
    return r, summary


class TheGateReason(unittest.TestCase):

    def test_the_off_board_arm_names_the_parts(self):
        why = place_seed.gate_reason([], [], [], 0, oob=['J1'])
        self.assertIn('satisfies its intent but leaves pad copper past the '
                      'board outline on J1', why)

    def test_nothing_fired_is_none(self):
        self.assertIsNone(place_seed.gate_reason([], [], [], 0, oob=()))


class TheIssueBoard(unittest.TestCase):
    """The issue's falsifier: no zone, so #1174's re-seat never runs."""

    def test_a_seat_with_copper_off_the_board_fails_the_gate(self):
        with tempfile.TemporaryDirectory() as td:
            r, summary = _seed(td, {'min': 1.5, 'max': 2.0}, code=4,
                               refuse='pad copper past the board outline '
                                      'on J1')
        # Stage 1 still keeps the seat (#975), and says where the copper is.
        self.assertIn('J1', summary['edge_floor_fallback'])
        self.assertIn('leaves pad copper past the board outline -- J1.1 '
                      'reaches 1.200mm past the north edge', r.stdout)
        # The grade is clean: this is the copper channel, not the intent's.
        self.assertEqual(summary['grade_errors'], 0)
        [(ref, mm)] = summary['pad_copper_off_board_seeded']
        self.assertEqual(ref, 'J1')
        self.assertGreater(mm, 1.0)

    def test_a_seat_on_the_board_still_passes(self):
        # The control: with a band pads on the board can meet, the floor
        # ladder moves J1 inward and nothing is charged.
        with tempfile.TemporaryDirectory() as td:
            r, summary = _seed(td, {'min': 0.0, 'max': 2.0}, code=0)
        self.assertEqual(summary['pad_copper_off_board_seeded'], [])
        self.assertNotIn('past the board outline', r.stdout + r.stderr)

    def test_copper_the_seed_did_not_place_is_not_charged(self):
        # R2 comes in LOCKED with its pad 0.6 mm past the east edge, so the
        # seed leaves it there: the board's own, like an inherited short. It
        # is reported (check_assembly gates the board on it), not charged.
        r2 = _box('R2', 39.8, 20, 1, 0.6, pad=(0.5, 0)).replace(
            '(layer "F.Cu") (at', '(locked yes) (layer "F.Cu") (at', 1)
        with tempfile.TemporaryDirectory() as td:
            r, summary = _seed(td, {'min': 0.0, 'max': 2.0}, code=0,
                               extra=[r2])
        self.assertLess(summary['pad_edge_after']['minimum_gap_by_ref_mm']
                        ['R2'], 0.0)
        self.assertEqual(summary['pad_copper_off_board_seeded'], [])


if __name__ == '__main__':
    unittest.main()
