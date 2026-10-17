"""#1241: no edge seat keeps pad copper past the board outline, `--repair` is
gated on that copper as a seed is, and the seed's other two copper channels
read the poses it WROTE.

On a connector with no drawn body `edge_seat_ok` asks only whether each pad
CENTRE is on the board, and #975's floor fallback kept the first conflict-free
rung "rather than leave the connector unseated" -- so a band `min 1.5` on a
body-less 0.6 x 6 mm pad (the issue's J1) left its copper 1.2 mm past the
north edge. The seat now moves such a rung inward onto the board
(`seeder._onto_the_board`), or does not keep it. When the band can be met
only with copper past the outline, it contradicts the footprint: the part
stays on its declared edge at the floor-clearing pose, the contradiction is
named (`edge_band_contradiction`, a NOTE), and the grade keeps its band error,
so the seed exits 4 on THAT rather than on copper nobody can solder.

  S. THE SEAT
     S1 the issue's board: stage 1 seats J1 on the north edge, pads on the
        board and clear of the floor, the band named; blind twin (the copper
        reading patched to zero) keeps the 1.2 mm-past seat, as before.
     S2 a band the pads CAN meet ({0, 2}) is the floor ladder's, unchanged:
        no contradiction.
     S3 a drawn-body connector, refused at every rung for its copper alone,
        is seated on its edge instead of thrown into the interior.
     S4 a keep-out that refuses every inward pose: nothing is kept past the
        outline, and the refusal names the keep-out.
     S5 `_seat_edge` (the repair's seat): the same, recorded on
        `contradictions`.
     S6 stage 1's crowding fallback is moved onto the board only if that
        crowds its neighbour no deeper; here it would, so it is not kept.
  R. --repair
     R1 the issue's off-board seat: the census charges it (a band exempts the
        body's overhang, never pad copper) and the repair seats J1 with its
        pads on the board, exit 4 on the band. Before: 0 violators, exit 0.
     R2 a repair that leaves copper past the outline exits 4 naming it.
        Before: exit 0, the part called repaired. (Since #1257 only the
        blind, courtyard-only search leaves it there; see the test.)
     R3 `repair_oob_split`: pinned listed apart, moved charged, the
        board's own reported on a `--reseat`.
     R4 a LOCKED part with pad copper past the outline fails a `--reseat`
        alone: a lock is no waiver (the rule the repair's own pinned
        census follows). Before: "pinned, reported not charged", exit 0.
  W. THE WRITTEN POSES
     W1 `split_pad_pairs`: a pair moved only after stage 1 is the seed's
        when new or deeper, the board's own when no deeper.
     W2 end to end: a polish move that makes a new pad pair between parts
        stage 1 never moved is charged, also in place; blind twin (the old
        wiring) calls it the board's own and exits 0.
"""

import contextlib
import io
import json
import os
import random
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import place_seed                                            # noqa: E402
import pose_score                                            # noqa: E402
from kicad_parser import parse_kicad_pcb                     # noqa: E402
from placement import floorplan, seeder                      # noqa: E402
from run_utils import check, evidence                        # noqa: E402
from test_1174_emit_round_trip import _TALL_J1, _box, _synthetic  # noqa: E402
from test_1241_seed_gate_off_board import (                  # noqa: E402
    _lap0, _part, _run, _scoped)

PY = sys.executable
NORTH = {'min': 1.5, 'max': 2.0}

#: A drawn-body J1: an F.Fab body 4 x 6 mm with a 1 x 1 mm pad at its north
#: end, so a band target overhang puts that pad past the outline -- which
#: `edge_seat_ok`'s drawn-body conjunct refuses at every rung.
BODY_J1 = ('  (footprint "t:BODY" (layer "F.Cu") (at 20 20)\n'
           '    (property "Reference" "J1" (at 0 0) (layer "F.SilkS"))\n'
           '    (fp_rect (start -2 -3) (end 2 3) (stroke (width 0.1)'
           ' (type default)) (layer "F.Fab"))\n'
           '    (pad "1" smd rect (at 0 -2.5) (size 1 1) (layers "F.Cu")'
           ' (net 2 "N2"))\n'
           '    (pad "2" smd rect (at 0 2) (size 1 1) (layers "F.Cu")'
           ' (net 1 "N1")))\n')


def _claim(band, **extra):
    return dict({'edge_connectors': [{'ref': 'J1', 'edge': 'north',
                                      'overhang_mm': band}]}, **extra)


def _board(td, j1=_TALL_J1):
    return _synthetic(td, [j1, _box('R1', 25, 25, 1, 0.6)])


def _stage1(path, doc):
    """`seed_from_intent` on `path`: (J1's pose, the result)."""
    res = seeder.seed_from_intent(
        parse_kicad_pcb(path), path,
        floorplan.intent_from_dict(dict({'schema': floorplan.SCHEMA_VERSION,
                                         'kind': floorplan.KIND,
                                         'units': 'mm'}, **doc)),
        random.Random(0), clearance=0.25, board_edge_clearance=0.55)
    (p,) = [q for q in res['placements'] if q['reference'] == 'J1']
    return (p['new_x'], p['new_y'], p['new_rotation']), res


def _past(path, pose, ref='J1'):
    """How far `ref`'s pad copper reaches past the outline at `pose`."""
    st = pose_score.make_state(parse_kicad_pcb(path), path, clearance=0.25,
                               board_edge_clearance=0.55)
    part = st.parts[ref]
    part.rot = pose[2]
    return seeder._pads_past_outline(st, part, pose[0], pose[1])


class TheSeat(unittest.TestCase):

    def test_s1_the_issue_board_keeps_its_pads_on_the_board(self):
        with tempfile.TemporaryDirectory() as td:
            path = _board(td)
            pose, res = _stage1(path, _claim(NORTH))
            # The blind twin: with the copper reading patched to zero the
            # seat is #975's floor fallback, 1.2 mm past the north edge.
            with patch.object(seeder, '_pads_past_outline',
                              lambda *a, **k: 0.0):
                blind, _r = _stage1(path, _claim(NORTH))
            self.assertAlmostEqual(_past(path, blind), 1.2, places=6)
            self.assertAlmostEqual(_past(path, pose), 0.0, places=9)
        # On its declared edge, at the floor-clearing pose: the pad's top at
        # 0.551 mm, one grid step clear of the 0.55 mm floor.
        self.assertEqual(pose, (20.0, 3.551, 0.0))
        self.assertEqual(blind, (20.0, 1.8, 0.0))
        rec = res['edge_band_contradiction']['J1']
        self.assertEqual((rec['why'], rec['kept'], rec['floor_clear']),
                         ('band_min', 'floor', True))
        # The copper reach at the band's MINIMUM, which no seat meets.
        self.assertAlmostEqual(rec['pads_past_at_band_min_mm'], 0.95,
                               places=4)
        self.assertEqual(res['edge_floor_fallback'], {})
        self.assertTrue([n for n in res['notes'] if 'band contradicts the '
                         'footprint' in n and n.startswith('edge connector J1')],
                        res['notes'])

    def test_s1_place_seed_exits_4_on_the_band_naming_it(self):
        with tempfile.TemporaryDirectory() as td:
            r, s = _run(_board(td), _claim(NORTH), code=4,
                        refuse='does NOT satisfy its intent')
        self.assertEqual(s['grade_error_rules'], {'edge_connector': 1})
        self.assertIn('under the declared minimum 1.50mm', r.stdout)
        self.assertIn('J1', s['edge_band_contradiction'])
        self.assertEqual(s['pad_copper_off_board_seeded'], [])
        self.assertGreaterEqual(
            s['pad_edge_after']['minimum_gap_by_ref_mm']['J1'], 0.55)

    def test_s2_a_band_the_pads_can_meet_is_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            path = _board(td)
            pose, res = _stage1(path, _claim({'min': 0.0, 'max': 2.0}))
            with patch.object(seeder, '_pads_past_outline',
                              lambda *a, **k: 0.0):
                blind, _r = _stage1(path, _claim({'min': 0.0, 'max': 2.0}))
        # #975's own inward move, which needs nothing from #1241.
        self.assertEqual(pose, blind)
        self.assertEqual(res['edge_band_contradiction'], {})

    def test_s3_a_drawn_body_connector_stays_on_its_edge(self):
        for band, rules in (({'min': 0.0, 'max': 2.0}, {}),
                            (NORTH, {'edge_connector': 1})):
            with self.subTest(band=band), tempfile.TemporaryDirectory() as td:
                path = _board(td, BODY_J1)
                pose, res = _stage1(path, _claim(band))
                # Before: "the north band is refused by pad copper ... past
                # the outline" at every rung, and the later stages put J1 in
                # the interior, nearest the south edge.
                self.assertEqual(pose, (20.0, 3.551, 0.0))
                self.assertFalse([n for n in res['notes']
                                  if 'left it for the later stages' in n],
                                 res['notes'])
                self.assertEqual(bool(res['edge_band_contradiction']),
                                 bool(rules))
                _r, s = _run(path, _claim(band), code=4 if rules else 0,
                             refuse='does NOT satisfy its intent')
                self.assertEqual(s['grade_error_rules'], rules)

    def test_s4_a_keepout_that_refuses_every_inward_pose(self):
        doc = _claim(NORTH, keepouts=[{'name': 'ko', 'rect': [0, 2, 40, 8]}])
        with tempfile.TemporaryDirectory() as td:
            path = _board(td)
            pose, res = _stage1(path, doc)
            self.assertEqual(_past(path, pose), 0.0)
        self.assertNotIn('J1', res['edge_band_contradiction'])
        self.assertTrue([n for n in res['notes'] if n.startswith(
            "edge connector J1: the north band is refused by keep-out 'ko'")],
            res['notes'])

    def test_s6_a_crowding_seat_is_not_moved_deeper_into_its_neighbour(self):
        # A locked pad strip across the whole board 5.2 mm in: every rung
        # crowds it, so stage 1 falls back to the nearest crowding seat --
        # whose copper is 1.2 mm past the edge -- and moving that inward
        # crowds the strip deeper. Neither is kept.
        strip = _part('R9', 20, 5.2, [(1, 0, 0, 36, 0.6, 9)], hw=18.5,
                      hh=0.5, locked=True)
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, [_TALL_J1, _box('R1', 25, 25, 1, 0.6),
                                   strip])
            pose, res = _stage1(path, _claim(NORTH))
            # The precondition: with the copper reading patched to zero the
            # crowding fallback is kept, its copper past the edge.
            with patch.object(seeder, '_pads_past_outline',
                              lambda *a, **k: 0.0):
                blind, rb = _stage1(path, _claim(NORTH))
            self.assertTrue([n for n in rb['notes'] if 'no seat on the north '
                             'band clears R9' in n], rb['notes'])
            self.assertGreater(_past(path, blind), 1.0)
            self.assertEqual(_past(path, pose), 0.0)
        self.assertTrue([n for n in res['notes'] if n.startswith(
            'edge connector J1: every seat on the north band leaves pad '
            'copper past the board outline')], res['notes'])

    def test_s5_seat_edge_moves_the_seat_onto_the_board(self):
        with tempfile.TemporaryDirectory() as td:
            path = _board(td)
            st = pose_score.make_state(parse_kicad_pcb(path), path,
                                       clearance=0.25,
                                       board_edge_clearance=0.55)
            notes, contra, floor = [], {}, {}
            ok = seeder._seat_edge(
                st, 'J1', {'ref': 'J1', 'edge': 'north',
                           'overhang_mm': dict(NORTH)}, set(), notes,
                disclose=floor, contradictions=contra)
            p = st.parts['J1']
            self.assertTrue(ok, notes)
            self.assertEqual((p.x, p.y), (20.0, 3.551))
            self.assertEqual(_past(path, (p.x, p.y, p.rot)), 0.0)
        self.assertEqual(list(contra), ['J1'])
        self.assertEqual(floor, {})
        self.assertTrue([n for n in notes if 'band contradicts' in n], notes)


class TheRepairGate(unittest.TestCase):

    def test_r1_the_issues_seat_is_charged_and_moved_onto_the_board(self):
        # J1 at the seat #975's floor fallback used to keep: 1.2 mm past.
        with tempfile.TemporaryDirectory() as td:
            path = _board(td, _TALL_J1.replace('(at 20 20)', '(at 20 1.8)'))
            r, s = _run(path, _claim(NORTH), code=4,
                        refuse='GRADE ERROR [edge_connector] J1 overhangs '
                               'the outline by 0.00mm, under the declared '
                               'minimum 1.50mm',
                        flags=('--repair',))
        self.assertIn('J1: its pad copper reaches 1.200mm past the board '
                      'outline -- charged', r.stdout)
        self.assertIn('J1', s['moved_refs'])
        self.assertIn('J1', s['edge_band_contradiction'])
        self.assertEqual(s['pad_copper_off_board_charged'], [])
        self.assertGreaterEqual(
            s['pad_edge_after']['minimum_gap_by_ref_mm']['J1'], 0.55)
        self.assertEqual(s['grade_error_rules'], {'edge_connector': 1})

    def test_r2_a_repair_that_leaves_copper_past_the_outline_fails(self):
        # R2's pad sits 4 mm outside its courtyard; the BLIND seat search
        # (courtyard only, as before #1257) leaves the pad past the east edge
        # (0.904 mm when recorded; 1.904 since the seeder's MST target aims
        # R2 1 mm further east) while calling R2 repaired, and the exit gate
        # charges it
        # anyway. The real search now re-seats R2 with its pad on the board,
        # and a part no seat can hold is charged unrepaired
        # (test_1257_seat_pad_copper.py).
        with tempfile.TemporaryDirectory() as td:
            lap0 = _lap0(td)
            r, s = _run(lap0, {}, code=4,
                        refuse='the repair leaves pad copper past the board '
                               'outline on R2',
                        out_name='rep.kicad_pcb', flags=('--repair',),
                        blind=True)
        self.assertEqual([r for r, _mm in s['pad_copper_off_board_charged']], ['R2'])
        self.assertGreater(s['pad_copper_off_board_charged'][0][1], 0.5)
        self.assertEqual(s['pad_copper_off_board_pinned'], [])
        self.assertEqual(s['grade_errors'], 0)
        self.assertIn('R2', s['repaired_refs'])

    def test_r3_the_split(self):
        split = place_seed.repair_oob_split
        gating = ['A', 'B', 'C', 'D']
        over = {'A': 1.0, 'B': 0.5, 'C': 0.25, 'D': 0.125}
        # --repair: everything movable is charged; a pinned part is listed
        # apart (the caller charges it too, R4).
        self.assertEqual(split(gating, over, {'A'}, {'B'}, True),
                         ([('B', 0.5), ('C', 0.25), ('D', 0.125)],
                          [('A', 1.0)], []))
        # --reseat alone: only what it moved; the rest is the board's own.
        self.assertEqual(split(gating, over, {'A'}, {'B'}, False),
                         ([('B', 0.5)], [('A', 1.0)],
                          [('C', 0.25), ('D', 0.125)]))
        self.assertEqual(split((), over, set(), set(), True), ([], [], []))

    def test_r4_a_locked_part_past_the_outline_fails_a_reseat(self):
        # J1 locked with its tall pad 1.2 mm past the north edge; the re-seat
        # scope is R1 alone, so nothing moves J1 and only the delivered-board
        # gate can see it.
        locked = _TALL_J1.replace('(at 20 20)', '(at 20 1.8)').replace(
            '(layer "F.Cu")', '(locked yes) (layer "F.Cu")', 1)
        with tempfile.TemporaryDirectory() as td:
            path = _board(td, locked)
            self.assertTrue(parse_kicad_pcb(path).footprints['J1'].locked)
            r, s = _run(path, {}, code=4, refuse='a lock is no waiver',
                        out_name='reseat.kicad_pcb',
                        flags=('--reseat', 'R1'))
        self.assertEqual(s['pad_copper_off_board_pinned'], [['J1', 1.2]])
        self.assertEqual(s['pad_copper_off_board_charged'], [])
        self.assertEqual(s['pad_copper_off_board_unscoped'], [])


#: A part beside R4 (net 4) in `_scoped`'s placed half, which stage 1 never
#: moves: the fake polish below pushes R5 onto R4's pad.
def _polish_r5_onto_r4(*_a, **_k):
    return [{'reference': 'R5', 'new_x': 30.3, 'new_y': 10.0,
             'new_rotation': 0.0}]


def _in_process(argv):
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = ['place_seed.py'] + [str(a) for a in argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = place_seed.main()
    finally:
        sys.argv = old
    m = [ln for ln in out.getvalue().splitlines()
         if ln.startswith('JSON_SUMMARY:') and '"pad_edge_after"' in ln]
    assert m, out.getvalue()[-1500:] + err.getvalue()[-1500:]
    return rc, json.loads(m[-1].split(':', 1)[1]), out.getvalue(), \
        err.getvalue()


class TheWrittenPoses(unittest.TestCase):

    def test_w1_split_pad_pairs_charges_a_late_pair_on_the_delta(self):
        split = place_seed.split_pad_pairs
        worst = [('A', 'B', 0.2), ('C', 'D', 0.1), ('E', 'F', 0.3),
                 ('G', 'H', 0.4)]
        before = {frozenset(('C', 'D')): 0.1, frozenset(('E', 'F')): 0.2}
        mine, against = split(worst, {'G'}, set(), late={'A', 'C', 'E'},
                              before=before)
        # A-B is new and E-F deeper: the seed's. C-D no deeper: the board's.
        # G-H has a member stage 1 moved: the seed's, as always.
        self.assertEqual(mine, [('A', 'B', 0.2), ('E', 'F', 0.3),
                                ('G', 'H', 0.4)])
        self.assertEqual(against, [])
        # Without a baseline every late pair is charged; without `late`,
        # nothing changes.
        self.assertEqual(len(split(worst, set(), set(), late={'C'})[0]), 1)
        self.assertEqual(split(worst, {'G'}, set()),
                         ([('G', 'H', 0.4)], []))
        with self.assertRaises(TypeError):
            split(worst, set(), set(), late='A')

    def _polished(self, td, in_place=False, blind=False):
        path = _synthetic(td, _scoped())
        ip = os.path.join(td, 'i.json')
        with open(ip, 'w', encoding='utf-8') as fh:
            json.dump({'schema': floorplan.SCHEMA_VERSION,
                       'kind': floorplan.KIND, 'units': 'mm'}, fh)
        out = path if in_place else os.path.join(td, 's.kicad_pcb')
        real = place_seed.split_pad_pairs
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('placement.quench.quench',
                                      _polish_r5_onto_r4))
            if blind:
                # The old wiring: "moved" read off stage 1's poses only.
                stack.enter_context(patch.object(
                    place_seed, 'split_pad_pairs',
                    lambda w, s, u, late=(), before=None: real(w, s, u)))
            res = _in_process([path, out, '--intent', ip])
        evidence(out, 'written seed')
        return res

    def test_w2_a_pad_pair_the_polish_made_is_charged(self):
        for in_place in (False, True):
            with self.subTest(in_place=in_place), \
                    tempfile.TemporaryDirectory() as td:
                rc, s, out, err = self._polished(td, in_place)
                self.assertEqual(rc, 4, out[-800:] + err[-800:])
                self.assertEqual([sorted(p[:2]) for p in
                                  s['pad_conflicts_seeded_pairs']],
                                 [['R4', 'R5']])
                self.assertIn('pads closer than their clearance', err)
        with tempfile.TemporaryDirectory() as td:
            rc, s, out, _err = self._polished(td, blind=True)
        self.assertEqual(rc, 0)
        self.assertEqual(s['pad_conflicts_seeded'], 0)
        self.assertEqual(s['pad_conflicts_inherited'], 1)


if __name__ == '__main__':
    unittest.main()
