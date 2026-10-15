"""#1257: the seat search refuses a pose whose PAD COPPER lies past the
outline, not only one whose courtyard does.

`seeder.pose_ok` -- the predicate behind every `_try_place` seat (stages 1.5
to 4, the row seat, the pose census, place_seed's post-polish re-seat and
`--repair`'s seat search) -- held the COURTYARD inside the outline and never
looked at the pads. A part whose pad lies outside its own courtyard (`_scoped`'s
R2: a 3 mm pad 4 mm east of a 2 x 1.2 mm courtyard) was seated with that pad
past the east edge, and `--repair` called such a part repaired without
moving it. #1241's gates caught the copper (exit 4), but the search kept
choosing it. The pad copper is now a conjunct of `pose_ok`, judged by
check_assembly's own gate (`legality.pad_copper_gating_mm`: per pad, margin
0, an edge-straddling castellated pad exempt), and asked only of a part whose
pads reach past its courtyard by more than the edge margin
(`seeder._pad_copper_overhang`), so every other part pays one cached compare.

  P. THE PREDICATE
     P1 R2 at its old lap-0 seat (pad 0.904 mm past the east edge) is refused,
        1 mm west is a seat; blind twin (the conjunct off) admits the old one.
     P2 the conjunct holds at a turn: at 90 degrees the pad points north.
     P3 a castellated pad STRADDLING the outline is exempt, as check_assembly
        exempts it; one wholly past it, or a plain pad, is refused.
     P4 the precompute: only a part reaching past its courtyard by more than
        the margin is ever pad-tested (a 0.35 mm reach is not; R2 is).
  S. THE SEED (`_scoped` --force): R2 is seated with its pad on the board,
     exit 0. Before: 0.904 mm past, exit 4.
  R. --repair on R2 at that seat: re-seated with its pad on the board (in
     place at 90 degrees undeclared; 1.00 mm west at a declared 0), exit 0.
     Before: "repaired" 0 mm, exit 4 on the copper.
  N. A part whose pad cannot be on the board in any pose: the seed leaves it
     UNSEATED by name, `--repair` reports it unrepaired by name and its gate
     charges the copper. Before: seeded 33.2 mm past, and "repaired" 0 mm.

The polish and a declared fixed pose asked only the courtyard too; both now
ask the same pad question (second #1257 commit):

  Q. THE QUENCH (`candidate_valid`)
     Q1 the old seat is refused, veto `pad_copper_outline`; blind admits it.
     Q2 the off-board unfreeze branch: coming home, a part may not end with
        its pad copper further past the outline than it starts -- refused
        from a start whose pads are on the board, admitted from one whose
        pads are further out.
     Q3 end to end, the polish keeps R2's pad on the board (R2 placed 0.5 mm
        inside: kept inside; placed 0.5 mm past: brought 0.5 mm inside),
        exit 0. Before: pulled 1.5 mm past, exit 4.
  F. A FIXED POSE whose courtyard is inside and whose pad copper is past the
     outline is REFUSED with its measurement and the part held out, exit 4,
     as an overhanging pose with copper past the outline always was; the pose
     with its pad on the board is seated. Before: seated, exit 4 on copper.
"""

import contextlib
import io
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

import pose_score                                            # noqa: E402
from kicad_parser import parse_kicad_pcb                     # noqa: E402
from placement import floorplan, legality, seeder            # noqa: E402
from test_1174_emit_round_trip import _synthetic             # noqa: E402
from test_1241_seed_gate_off_board import (                  # noqa: E402
    _R2_ROT, _part, _run, _scoped)

#: R2's seat at #1241 (main c0d75d93): its pad's east end at 40.904, past
#: the 40 mm board's east edge by 0.904 mm.
OLD_SEAT = (35.404, 21.482, 0.0)

#: The poses `_scoped`'s --force seed wrote at c0d75d93, R2 aside, so a
#: `--repair` input carries R2 as its one violator.
_SEEDED = {'P1': (20.63, 19.289), 'P2': (20.563, 21.489),
           'P3': (20.43, 16.836), 'R4': (14.071, 18.797),
           'R5': (23.938, 17.496), 'R2': OLD_SEAT[:2]}


def _blind():
    """The conjunct off: `pose_ok` as it was before #1257."""
    return patch.object(seeder, '_pad_copper_overhang', lambda *a, **k: 0.0)


def _state(path):
    return pose_score.make_state(parse_kicad_pcb(path), path, clearance=0.25,
                                 board_edge_clearance=0.55)


def _ok(st, ref, pose):
    """`pose_ok` with every other part excluded: the board alone decides."""
    return seeder.pose_ok(st, ref, *pose, set(st.parts) - {ref})


def _gating(path, ref):
    """check_assembly's verdict on `ref` in a WRITTEN board: its overrun in
    `oob_pad_copper_overrun_mm` if it is in the gating list, else 0.0."""
    rep = legality.grade_pad_legality(parse_kicad_pcb(path), 0.25, worst_n=0,
                                      edge_margin=0.55, pcb_file=path)
    if ref not in rep['oob_pad_copper_gating_refs']:
        return 0.0
    return rep['oob_pad_copper_overrun_mm'][ref]


def _castellated(ref, x, lx, castellated=True):
    """A +-1 x +-0.6 mm courtyard with one 1 x 1 mm pad at local (lx, 0)."""
    prop = ' (property pad_prop_castellated)' if castellated else ''
    return (f'  (footprint "t:C" (layer "F.Cu") (at {x} 20)\n'
            f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
            '    (fp_rect (start -1 -0.6) (end 1 0.6) (stroke (width 0.05)'
            ' (type default)) (layer "F.CrtYd"))\n'
            f'    (pad "1" smd rect (at {lx} 0) (size 1 1) (layers "F.Cu")'
            f'{prop} (net 2 "N2"))\n  )\n')


class ThePredicate(unittest.TestCase):

    def test_p1_the_old_seat_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            st = _state(_synthetic(td, _scoped()))
            self.assertAlmostEqual(seeder._pad_copper_overhang(st, 'R2'), 4.5)
            self.assertAlmostEqual(
                seeder._pad_copper_off_board(st, 'R2', *OLD_SEAT), 0.904)
            self.assertFalse(_ok(st, 'R2', OLD_SEAT))
            self.assertTrue(_ok(st, 'R2', (34.404, 21.482, 0.0)))
            # The blind twin: the courtyard alone admits the old seat.
            with _blind():
                self.assertTrue(_ok(st, 'R2', OLD_SEAT))

    def test_p2_a_turned_part_is_held_too(self):
        # At 90 degrees R2's pad sits 4 mm NORTH of its origin: y 3.0 puts
        # the courtyard 2 mm in and the pad's centre 1 mm past the edge.
        with tempfile.TemporaryDirectory() as td:
            st = _state(_synthetic(td, _scoped()))
            self.assertGreater(
                seeder._pad_copper_off_board(st, 'R2', 20.0, 3.0, 90.0), 1.0)
            self.assertFalse(_ok(st, 'R2', (20.0, 3.0, 90.0)))
            self.assertTrue(_ok(st, 'R2', (20.0, 6.0, 90.0)))
            with _blind():
                self.assertTrue(_ok(st, 'R2', (20.0, 3.0, 90.0)))

    def test_p3_a_castellated_pad_on_the_edge_is_exempt(self):
        # The courtyard ends 0.8 mm inside the east edge at x 38.2, clear of
        # the 0.55 mm margin, so only the pad can refuse the pose.
        for lx, castellated, x, seat in (
                (1.9, True, 38.2, True),      # 39.6..40.6: straddles
                (1.9, False, 38.2, False),    # same copper, not castellated
                (3.0, True, 38.4, False)):    # 40.9..41.9: wholly past
            with self.subTest(lx=lx, castellated=castellated), \
                    tempfile.TemporaryDirectory() as td:
                path = _synthetic(td, [_castellated('C1', 10, lx,
                                                    castellated)])
                st = _state(path)
                self.assertEqual(_ok(st, 'C1', (x, 20.0, 0.0)), seat)
                # The predicate's verdict is check_assembly's, at that pose
                # written: the same gate, so the seat is never charged.
                with open(path, encoding='utf-8') as fh:
                    text = fh.read()
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(text.replace('(at 10 20)', f'(at {x} 20)', 1))
                self.assertEqual(_gating(path, 'C1') > legality.EPS,
                                 not seat)

    def test_p4_only_a_part_reaching_past_the_margin_is_pad_tested(self):
        # R6 reaches 0.35 mm past its courtyard, under the 0.55 mm margin:
        # its copper is on the board wherever its courtyard is, so the seed
        # never measures it. R2 (4.5 mm) is measured.
        r6 = _part('R6', 12, 12, [(1, 1.0, 0, 0.7, 0.6, 6)])
        calls = set()
        real = seeder._pad_copper_off_board

        def spy(state, ref, *pose):
            calls.add(ref)
            return real(state, ref, *pose)
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, _scoped([r6]))
            st = _state(path)
            self.assertAlmostEqual(seeder._pad_copper_overhang(st, 'R6'),
                                   0.35)
            self.assertEqual(seeder._pad_copper_overhang(st, 'R4'), 0.0)
            with patch.object(seeder, '_pad_copper_off_board', spy):
                seeder.seed_from_intent(
                    parse_kicad_pcb(path), path,
                    floorplan.intent_from_dict({
                        'schema': floorplan.SCHEMA_VERSION,
                        'kind': floorplan.KIND, 'units': 'mm'}),
                    random.Random(0), clearance=0.25,
                    board_edge_clearance=0.55)
        self.assertEqual(calls, {'R2'})


class TheSeed(unittest.TestCase):

    def test_s_r2_is_seated_with_its_pad_on_the_board(self):
        with tempfile.TemporaryDirectory() as td:
            r, s = _run(_synthetic(td, _scoped()), {}, code=0)
            fp = parse_kicad_pcb(os.path.join(td, 's.kicad_pcb')
                                 ).footprints['R2']
        self.assertEqual((fp.x, fp.y, fp.rotation), (34.404, 21.482, 0.0))
        # The pad's east end 0.096 mm inside the edge; before, 0.904 past.
        self.assertAlmostEqual(
            s['pad_edge_after']['minimum_gap_by_ref_mm']['R2'], 0.096)
        self.assertEqual(s['pad_copper_off_board_seeded'], [])
        self.assertEqual(s['oob_pad_copper_gating_count'], 0)
        self.assertNotIn('past the board outline', r.stdout + r.stderr)

    def test_s_blind_twin_seats_it_past_the_edge(self):
        with tempfile.TemporaryDirectory() as td:
            _r, s = _run(_synthetic(td, _scoped()), {}, code=4,
                         refuse='pad copper past the board outline on R2',
                         blind=True)
        self.assertEqual(s['pad_copper_off_board_seeded'], [['R2', 0.904]])


def _at_old_seat():
    """`_scoped` with every part where c0d75d93's seed wrote it: R2 at its
    old seat is the one violator a repair has."""
    parts = []
    for text in _scoped():
        ref = re.search(r'"Reference" "([^"]+)"', text).group(1)
        if ref in _SEEDED:
            x, y = _SEEDED[ref]
            # The footprint's own `(at ...)` is the first in its text.
            text = re.sub(r'\(at [-\d.]+ [-\d.]+\)', f'(at {x} {y})', text,
                          count=1)
        parts.append(text)
    return parts


class TheRepair(unittest.TestCase):

    def _repair(self, td, doc, code=0, refuse=None, blind=False):
        path = _synthetic(td, _at_old_seat())
        # The precondition: R2 comes in with its pad past the outline, and
        # nothing else is placed where the seed would not have put it.
        self.assertAlmostEqual(_gating(path, 'R2'), 0.904)
        r, s = _run(path, doc, code=code, refuse=refuse,
                    out_name='rep.kicad_pcb', flags=('--repair',),
                    blind=blind)
        fp = parse_kicad_pcb(os.path.join(td, 'rep.kicad_pcb')
                             ).footprints['R2']
        return r, s, (fp.x, fp.y, fp.rotation)

    def test_r_undeclared_r2_turns_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            r, s, pose = self._repair(td, {})
        # Its own pose at 90 degrees: the pad points north, 4.3 mm inside.
        self.assertEqual(pose, (35.404, 21.482, 90.0))
        self.assertEqual(s['repaired_refs'], ['R2'])
        self.assertEqual(s['pad_copper_off_board_charged'], [])
        self.assertGreater(
            s['pad_edge_after']['minimum_gap_by_ref_mm']['R2'], 0.0)
        self.assertIn('R2: re-seated 0.00mm from its pose (cap 0.5)', r.stdout)

    def test_r_a_declared_angle_moves_r2_west(self):
        with tempfile.TemporaryDirectory() as td:
            r, s, pose = self._repair(td, _R2_ROT)
        self.assertEqual(pose, (34.404, 21.482, 0.0))
        self.assertEqual(s['repaired_refs'], ['R2'])
        self.assertEqual(s['pad_copper_off_board_charged'], [])
        self.assertIn('R2: re-seated 1.00mm from its pose (cap 1.0)', r.stdout)

    def test_r_blind_twin_calls_it_repaired_where_it_stands(self):
        with tempfile.TemporaryDirectory() as td:
            r, s, pose = self._repair(
                td, _R2_ROT, code=4, blind=True,
                refuse='the repair leaves pad copper past the board outline '
                       'on R2')
        self.assertEqual(pose, OLD_SEAT)
        self.assertEqual(s['repaired_refs'], ['R2'])
        self.assertEqual(s['pad_copper_off_board_charged'], [['R2', 0.904]])


#: A pad 60 mm from its courtyard: no pose puts both on a 40 mm board.
R8 = _part('R8', 20, 10, [(1, 60.0, 0, 0.6, 0.6, 3),
                          (2, -0.5, 0, 0.5, 0.6, 3)])


class NoPoseExists(unittest.TestCase):

    def test_n_the_seed_leaves_it_unseated_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, _scoped([R8]))
            _r, s = _run(path, {}, code=4, refuse='UNSEATED -- left where '
                         'they came in or staged below the board, and NOT '
                         'placed: R8')
            # Blind, it was seated with the pad 33.2 mm past the edge.
            _rb, sb = _run(path, {}, code=4, out_name='blind.kicad_pcb',
                           refuse='pad copper past the board outline on R8',
                           blind=True)
        self.assertEqual(s['unseated_refs'], ['R8'])
        self.assertEqual(s['pad_copper_off_board_seeded'], [])
        self.assertEqual(sb['unseated_refs'], [])
        self.assertEqual([r for r, _mm in sb['pad_copper_off_board_seeded']],
                         ['R8'])

    def test_n_the_repair_names_it_unrepaired_and_charges_it(self):
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, [p for p in _at_old_seat()
                                   if '"R2"' not in p] + [R8])
            r, s = _run(path, {}, code=4, out_name='rep.kicad_pcb',
                        flags=('--repair',),
                        refuse='the repair leaves pad copper past the board '
                               'outline on R8')
            _rb, sb = _run(path, {}, code=4, out_name='blind.kicad_pcb',
                           flags=('--repair',), blind=True,
                           refuse='the repair leaves pad copper past the '
                                  'board outline on R8')
        self.assertIn('R8: no legal pose within any cap', r.stdout)
        self.assertNotIn('R8', s['repaired_refs'])
        self.assertIn('R8', s['unrepairable_failed_refs'])
        self.assertEqual([r for r, _mm in s['pad_copper_off_board_charged']],
                         ['R8'])
        # Blind, it was "repaired" where it stood.
        self.assertIn('R8', sb['repaired_refs'])


def _veto(st, ref, pose):
    """`candidate_veto` with every other part excluded."""
    with contextlib.redirect_stdout(io.StringIO()):
        return st.candidate_veto(ref, *pose, exclude=set(st.parts) - {ref})


class TheQuench(unittest.TestCase):

    def test_q1_the_old_seat_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            st = _state(_synthetic(td, _scoped()))
            self.assertEqual(_veto(st, 'R2', OLD_SEAT),
                             ('pad_copper_outline', None))
            self.assertIsNone(_veto(st, 'R2', (34.404, 21.482, 0.0)))
            with _blind():
                self.assertIsNone(_veto(st, 'R2', OLD_SEAT))

    def test_q2_coming_home_the_copper_may_not_go_further_out(self):
        # R2's courtyard pokes past the east edge either way, so the
        # candidate (37, 20, 0) -- courtyard inside, pad 2.5 mm past --
        # reaches the unfreeze branch, whose courtyard term improves.
        home = (37.0, 20.0, 0.0)
        with tempfile.TemporaryDirectory() as td:
            st = _state(_synthetic(td, _scoped()))
            # From (39.6, 20, 90): pad pointing north, on the board.
            st.apply_move('R2', 39.6, 20.0, 90.0)
            self.assertEqual(st._pad_copper_past('R2', 39.6, 20.0, 90.0), 0.0)
            self.assertEqual(_veto(st, 'R2', home),
                             ('pad_copper_outline', None))
            with _blind():
                self.assertIsNone(_veto(st, 'R2', home))
            # From (39.5, 20, 0): pad 5 mm past -- the candidate is better.
            st.apply_move('R2', 39.5, 20.0, 0.0)
            self.assertAlmostEqual(
                st._pad_copper_past('R2', 39.5, 20.0, 0.0), 5.0)
            self.assertIsNone(_veto(st, 'R2', home))

    def test_q3_the_polish_keeps_the_pad_on_the_board(self):
        # `_scoped` without --force: R2 is outside the seed's scope and only
        # the polish moves it (`_R2_ROT` holds its angle).
        for x, gap in (('34', 0.5), ('35', 0.5)):
            with self.subTest(x=x), tempfile.TemporaryDirectory() as td:
                path = _synthetic(td, [p.replace('(at 34 20)',
                                                 f'(at {x} 20)', 1)
                                       for p in _scoped()])
                r, s = _run(path, _R2_ROT, code=0, flags=())
                _rb, sb = _run(path, _R2_ROT, code=4, flags=(),
                               out_name='blind.kicad_pcb', blind=True,
                               refuse='pad copper past the board outline '
                                      'on R2')
                self.assertAlmostEqual(
                    s['pad_edge_after']['minimum_gap_by_ref_mm']['R2'], gap)
                self.assertEqual(s['pad_copper_off_board_seeded'], [])
                self.assertNotIn('past the board outline', r.stdout)
                # Blind, the polish pulled it 1.5 mm past the edge.
                self.assertEqual(sb['pad_copper_off_board_seeded'],
                                 [['R2', 1.5]])


def _fixed(x):
    return {'fixed_poses': [{'ref': 'R2', 'x': x, 'y': 21.482, 'rot': 0,
                             'basis': 'declared'}]}


class TheFixedPose(unittest.TestCase):

    def test_f_the_check_names_the_copper(self):
        with tempfile.TemporaryDirectory() as td:
            st = _state(_synthetic(td, _scoped()))
            got = seeder._fixed_pose_check(st, 'R2', OLD_SEAT, {})
            self.assertEqual(got, (None, ['pad copper 0.904mm past the '
                                          'outline'], {}))
            self.assertEqual(seeder._fixed_pose_check(
                st, 'R2', (34.404, 21.482, 0.0), {})[0], 'contained')
            with _blind():
                self.assertEqual(seeder._fixed_pose_check(
                    st, 'R2', OLD_SEAT, {})[0], 'contained')

    def test_f_place_seed_refuses_it_by_name(self):
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, _scoped())
            r, s = _run(path, _fixed(OLD_SEAT[0]), code=4,
                        refuse='UNSEATED -- left where they came in or '
                               'staged below the board, and NOT placed: R2')
            _rb, sb = _run(path, _fixed(OLD_SEAT[0]), code=4,
                           out_name='blind.kicad_pcb', blind=True,
                           refuse='pad copper past the board outline on R2')
            _rc, sc = _run(path, _fixed(34.404), code=0,
                           out_name='c.kicad_pcb')
        self.assertEqual(s['fixed_refused']['R2']['reason'],
                         'pad copper 0.904mm past the outline')
        self.assertIn('fixed pose R2: REFUSED at (35.404, 21.482, 0deg) -- '
                      'pad copper 0.904mm past the outline. The pose is a '
                      'fact, so it is never nudged', r.stdout)
        self.assertEqual(s['pad_copper_off_board_seeded'], [])
        # Blind, it was seated there and its copper charged.
        self.assertEqual(list(sb['fixed_seated']), ['R2'])
        self.assertEqual(sb['pad_copper_off_board_seeded'], [['R2', 0.904]])
        # The control: the same declaration with its pad on the board.
        self.assertEqual(list(sc['fixed_seated']), ['R2'])
        self.assertEqual(sc['fixed_refused'], {})


if __name__ == '__main__':
    unittest.main()
