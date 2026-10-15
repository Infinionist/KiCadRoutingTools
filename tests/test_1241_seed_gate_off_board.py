"""#1241: place_seed's gate charges pad copper it placed past the outline.

#975's floor fallback keeps an edge connector's seat "rather than leave the
connector unseated", and on a body-less part stage 1 tests only pad CENTRES
against the board, so a band `min` that pads on the board cannot reach left
J1's copper 1.2 mm past the north edge. Nothing after stage 1 charged it:
place_seed exited 0 on a seed check_assembly calls NOT BUILDABLE. The gate now
reads the measure check_assembly gates on (`oob_pad_copper_gating_refs`)
at the WRITTEN poses, for every part except the board's own -- an UNLOCKED
part outside a partially-unplaced run's scope, at its input pose or moved by
the polish no further past the outline -- and the floor note says the copper
is off the board rather than "inside the board-edge floor". A lock exempts
nothing: placement stamps its own, so a lap-1 lock would launder its
off-board seat into lap 2's exit 0.

Since the seat half of #1241 no edge seat keeps pad copper past the outline
(test_1241_edge_seat_on_board.py), so the issue's own J1 no longer reaches
this gate: it is seated on its edge with its pads on the board and the band's
minimum named. The gate's attribution is pinned here on R2 instead, a part
whose pad lies 4 mm outside its courtyard.

Since #1257 the seat search refuses that copper too (test_1257_seat_pad_
copper.py), so no SEARCH keeps R2's off-board seat any more; the polish, which
does not search, still pushes it there (`TheWrittenPoseDecides`' polish tests,
unpatched). The tests built on lap 0 -- a seat the seed KEPT, must_lock, a
lock the last lap stamped, in place -- run place_seed with the search's
courtyard-only predicate put back (`blind=True`, the search as #1241 left
it), so the gate is still handed the copper they are about: what they pin is
the gate's attribution, which answers for every step that can put copper
there, not the search.
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

#: #1257: place_seed with the seat search's pad-copper conjunct switched off
#: (`seeder._pad_copper_overhang` reads 0.0 for every part, so `pose_ok`
#: skips it): the courtyard-only search #1241 was written against. See the
#: module docstring for why the lap-0 tests need it.
_PATHS = [os.path.join(ROOT, p) for p in ('py_placer', 'py_router')]
_BLIND_SEARCH = (
    "import runpy, sys\n"
    f"sys.path[:0] = {_PATHS!r}\n"
    "from placement import seeder\n"
    "seeder._pad_copper_overhang = lambda *a, **k: 0.0\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n")


def _run(path, doc, code, refuse=None, out_name='s.kicad_pcb',
         flags=('--force',), blind=False):
    """place_seed `path` under the intent `doc`; (run, JSON_SUMMARY).
    `blind`: with the courtyard-only seat search (`_BLIND_SEARCH`)."""
    td = os.path.dirname(path)
    ip = os.path.join(td, out_name + '.json')
    with open(ip, 'w', encoding='utf-8') as fh:
        json.dump(dict({'schema': floorplan.SCHEMA_VERSION,
                        'kind': floorplan.KIND, 'units': 'mm',
                        'blocks': [], 'edge_connectors': []}, **doc), fh)
    out = os.path.join(td, out_name)
    r = check([PY, '-X', 'utf8'] + (['-c', _BLIND_SEARCH] if blind else [])
              + [os.path.join(ROOT, 'py_placer', 'place_seed.py'),
                 path, out, '--intent', ip] + list(flags),
              **({'accept': True} if code == 0
                 else {'refuse': refuse, 'code': code}))
    evidence(out, 'written seed')
    summary = [json.loads(ln.split(':', 1)[1]) for ln in r.stdout.splitlines()
               if ln.startswith('JSON_SUMMARY:')
               and '"pad_edge_after"' in ln][-1]
    return r, summary


def _claim(band):
    return {'edge_connectors': [{'ref': 'J1', 'edge': 'north',
                                 'overhang_mm': band}]}


def _seed(td, band, code, refuse=None, extra=()):
    """Seed the body-less J1 with a north claim of `band` and no zone."""
    path = _synthetic(td, [_TALL_J1, _box('R1', 25, 25, 1, 0.6)] + list(extra))
    return _run(path, _claim(band), code, refuse)


def _part(ref, x, y, pads, hw=1.0, hh=0.6, locked=False):
    """A part with a +-hw x +-hh courtyard and `pads` =
    [(number, x, y, size_x, size_y, net)] in its own frame."""
    lk = '(locked yes) ' if locked else ''
    return (f'  (footprint "t:P" {lk}(layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
            f'    (fp_rect (start {-hw} {-hh}) (end {hw} {hh}) (stroke'
            f' (width 0.05) (type default)) (layer "F.CrtYd"))\n'
            + ''.join(f'    (pad "{n}" smd rect (at {px} {py}) (size {sx} {sy})'
                      f' (layers "F.Cu") (net {net} "N{net}"))\n'
                      for n, px, py, sx, sy, net in pads)
            + '  )\n')


class TheGateReason(unittest.TestCase):

    def test_the_off_board_arm_names_the_parts(self):
        why = place_seed.gate_reason([], [], [], 0, oob=['J1'])
        self.assertIn('satisfies its intent but leaves pad copper past the '
                      'board outline on J1', why)

    def test_nothing_fired_is_none(self):
        self.assertIsNone(place_seed.gate_reason([], [], [], 0, oob=()))


class TheIssueBoard(unittest.TestCase):
    """The issue's falsifier: no zone, so #1174's re-seat never runs."""

    def test_the_issue_board_fails_on_its_band_not_its_copper(self):
        # Before the seat half of #1241, stage 1 kept J1's pads 1.2 mm past
        # the north edge and this gate charged the copper (grade clean). The
        # seat now never keeps that: J1 sits on its edge with its pads on
        # the board, and the seed fails on the band's minimum, which no seat
        # with the pads on the board can meet -- named, not hidden.
        with tempfile.TemporaryDirectory() as td:
            r, summary = _seed(td, {'min': 1.5, 'max': 2.0}, code=4,
                               refuse='does NOT satisfy its intent')
        self.assertEqual(summary['pad_copper_off_board_seeded'], [])
        self.assertEqual(summary['oob_pad_copper_gating_count'], 0)
        self.assertEqual(summary['grade_error_rules'], {'edge_connector': 1})
        self.assertIn('J1', summary['edge_band_contradiction'])
        self.assertNotIn('past the board outline on J1', r.stderr)

    def test_a_seat_on_the_board_still_passes(self):
        # The control: with a band pads on the board can meet, the floor
        # ladder moves J1 inward and nothing is charged.
        with tempfile.TemporaryDirectory() as td:
            r, summary = _seed(td, {'min': 0.0, 'max': 2.0}, code=0)
        self.assertEqual(summary['pad_copper_off_board_seeded'], [])
        self.assertNotIn('past the board outline', r.stdout + r.stderr)

    def test_a_locked_part_off_the_board_is_charged_as_locked(self):
        # R2 comes in LOCKED with its pad 0.6 mm past the east edge, so the
        # seed leaves it there. A lock is no waiver -- placement stamps its
        # own, and check_assembly exempts none (#1096) -- so it is charged,
        # named as locked so the fix is known to be upstream.
        r2 = _box('R2', 39.8, 20, 1, 0.6, pad=(0.5, 0)).replace(
            '(layer "F.Cu") (at', '(locked yes) (layer "F.Cu") (at', 1)
        with tempfile.TemporaryDirectory() as td:
            r, summary = _seed(td, {'min': 0.0, 'max': 2.0}, code=4,
                               refuse='pad copper past the board outline '
                                      'on R2', extra=[r2])
        self.assertLess(summary['pad_edge_after']['minimum_gap_by_ref_mm']
                        ['R2'], 0.0)
        self.assertEqual(summary['pad_copper_off_board_seeded'], [])
        self.assertEqual(summary['pad_copper_off_board_locked'],
                         [['R2', 0.6]])
        self.assertEqual(summary['oob_pad_copper_gating_count'], 1)
        self.assertIn("(locked yes) in the input with pad copper past the "
                      "board outline, which this seed cannot move", r.stdout)
        self.assertNotIn("the board's own", r.stdout)


def _scoped(extra=()):
    """A PARTIALLY unplaced board: P1-P3 piled, the rest placed. Without
    --force the seed seats only the pile; R2, R3, R4, R5 stand as placed
    ("someone's work"), but the polish still moves the unlocked ones. R2's
    pad 1 sits 4 mm east of its courtyard, 0.5 mm inside the east edge,
    net-tied to a LOCKED R3 at that edge."""
    return [
        _part(f'P{i}', 10, 30, [(1, -0.5, 0, 0.5, 0.6, 1),
                                (2, 0.5, 0, 0.5, 0.6, 2)])
        for i in (1, 2, 3)] + [
        _part('R2', 34, 20, [(1, 4.0, 0, 3.0, 0.6, 3),
                             (2, -0.5, 0, 0.5, 0.6, 3)]),
        _part('R3', 38.8, 23, [(1, 0.5, 0, 0.2, 0.6, 3)], hw=0.6,
              hh=0.6, locked=True),
        _part('R5', 25, 12, [(1, 0, 0, 0.5, 0.6, 5)]),
        _part('R4', 30, 10, [(1, 0, 0, 0.5, 0.6, 4)])] + list(extra)


_R2_ROT = {'blocks': [{'name': 'r2rot', 'refs': ['R2'], 'rotation': 0}]}


def _lap0(td):
    """`_scoped` seeded with --force by the BLIND search: every unlocked part
    is in scope, and R2 -- its pad 4 mm east of its courtyard -- is written
    with that pad past the east edge, the copper this gate charges. Returns
    the written board, which the tests below re-seed as a later lap would.
    (Unpatched, the seed now puts R2's pad on the board and exits 0: #1257.)"""
    path = _synthetic(td, _scoped())
    _r, summary = _run(path, {}, code=4, out_name='lap0.kicad_pcb',
                       refuse='pad copper past the board outline on R2',
                       blind=True)
    # The precondition: R2 is the seed's, past the outline.
    assert [r for r, _mm in summary['pad_copper_off_board_seeded']] == ['R2']
    return os.path.join(td, 'lap0.kicad_pcb')


class TheWrittenPoseDecides(unittest.TestCase):
    """The verifiers' escapes: "moved" read off stage 1's poses missed copper
    the polish pushed off the board and a seat the seed KEPT; a `must_lock`
    exemption let a seat stage 1.5 chose pass as the board's own."""

    def test_copper_the_polish_pushes_off_the_board_is_charged(self):
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, _scoped())
            # The control: stage 1 leaves R2 where it was, copper on board.
            r0, s0 = _run(path, _R2_ROT, code=0, out_name='np.kicad_pcb',
                          flags=('--no-polish',))
            self.assertGreater(s0['pad_edge_after']['minimum_gap_by_ref_mm']
                               ['R2'], 0.0)
            # The polish pulls R2 toward R3 and its pad ends past the edge.
            r, summary = _run(path, _R2_ROT, code=4, flags=(),
                              refuse='pad copper past the board outline '
                                     'on R2')
        self.assertEqual([ref for ref, _mm in
                          summary['pad_copper_off_board_seeded']], ['R2'])
        self.assertNotIn("the board's own", r.stdout)

    def test_copper_outside_the_runs_scope_is_the_boards_own(self):
        # R6 stands as placed with its pad 1 mm past the east edge. Without
        # --force it is outside the seed's scope, and with no polish nothing
        # moves it: inherited, named and not charged.
        r6 = _part('R6', 30, 5, [(1, 11.0, 0, 0.6, 0.6, 6)])
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, _scoped([r6]))
            r, summary = _run(path, _R2_ROT, code=0, flags=('--no-polish',))
        self.assertLess(summary['pad_edge_after']['minimum_gap_by_ref_mm']
                        ['R6'], 0.0)
        self.assertEqual(summary['pad_copper_off_board_seeded'], [])
        self.assertIn("the board's own, reported not charged: R6", r.stdout)
        # Reported in the board total, though not charged.
        self.assertEqual(summary['oob_pad_copper_gating_count'], 1)

    def _r2_at(self, x, code, refuse=None):
        """`_scoped` with the out-of-scope R2 at `x` (its pad's east end at
        x + 5.5, against the outline at 40), seeded with the polish on."""
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, [p.replace('(at 34 20)', f'(at {x} 20)', 1)
                                   for p in _scoped()])
            return _run(path, _R2_ROT, code=code, refuse=refuse, flags=())

    def test_a_polish_move_that_shrinks_the_overrun_is_not_charged(self):
        # R2 comes in 2.5 mm past the east edge; the polish pulls it to 1.5.
        # It moved, but no further off the board: the board's own, reported.
        r, summary = self._r2_at(37, code=0)
        self.assertEqual(summary['pad_copper_off_board_seeded'], [])
        self.assertEqual(summary['pad_copper_off_board_locked'], [])
        self.assertIn("polish moved no further past the board outline -- "
                      "the board's own, reported not charged: R2 (2.500 -> "
                      "1.500mm past)", r.stdout)
        self.assertNotIn('this seed placed', r.stdout)

    def test_a_polish_move_that_grows_the_overrun_is_charged(self):
        # The control: R2 comes in 0.5 mm past, already off the board, and
        # the polish pushes it to 1.5 -- the delta grew, so it is charged.
        r, summary = self._r2_at(35, code=4,
                                 refuse='pad copper past the board outline '
                                        'on R2')
        self.assertEqual([ref for ref, _mm in
                          summary['pad_copper_off_board_seeded']], ['R2'])
        self.assertIn("polish moved FURTHER past the board outline: R2 "
                      "(0.500 -> 1.500mm past)", r.stdout)
        self.assertNotIn("the board's own", r.stdout)

    def test_a_must_lock_seat_is_the_seeds_own(self):
        # Stage 1.5 seats every unlocked must_lock part itself; re-seeding
        # lap 0 with R2 must_lock keeps R2's off-board seat (blind: the
        # search now moves it onto the board), and that seat is still the
        # seed's choice.
        with tempfile.TemporaryDirectory() as td:
            lap0 = _lap0(td)
            r, summary = _run(lap0, {'must_lock': ['R2']}, code=4,
                              refuse='pad copper past the board outline '
                                     'on R2', out_name='s2.kicad_pcb',
                              blind=True)
        self.assertEqual([ref for ref, _mm in
                          summary['pad_copper_off_board_seeded']], ['R2'])
        self.assertNotIn("the board's own", r.stdout)

    def test_a_seat_the_seed_kept_is_charged(self):
        # Re-seed lap 0: R2 comes in AT the seat the (blind) seed gives it
        # again, unlocked, so it does not "move" -- and is still the seed's
        # choice, not the board's.
        from kicad_parser import parse_kicad_pcb
        with tempfile.TemporaryDirectory() as td:
            lap0 = _lap0(td)
            r, summary = _run(lap0, {}, code=4,
                              refuse='pad copper past the board outline on '
                                     'R2', out_name='s2.kicad_pcb',
                              blind=True)
            a = parse_kicad_pcb(lap0).footprints['R2']
            b = parse_kicad_pcb(os.path.join(td, 's2.kicad_pcb')
                                ).footprints['R2']
        # The precondition: R2 did not move.
        self.assertFalse(place_seed.pose_moved((a.x, a.y, a.rotation),
                                               (b.x, b.y, b.rotation)))
        self.assertEqual([ref for ref, _mm in
                          summary['pad_copper_off_board_seeded']], ['R2'])

    def test_a_lock_the_last_lap_stamped_launders_nothing(self):
        # Lap 1 keeps R2 off the board under must_lock (blind) and STAMPS it
        # locked. Lap 2 re-seeds lap 1's board with the real search: R2 now
        # comes in `(locked yes)` at that seat, and the gate must still fail
        # -- check_assembly calls the board NOT BUILDABLE whoever locked the
        # part.
        from kicad_parser import parse_kicad_pcb
        with tempfile.TemporaryDirectory() as td:
            lap0 = _lap0(td)
            doc = {'must_lock': ['R2']}
            _run(lap0, doc, code=4, out_name='lap1.kicad_pcb',
                 refuse='pad copper past the board outline on R2',
                 blind=True)
            lap1 = os.path.join(td, 'lap1.kicad_pcb')
            # The precondition: lap 1 stamped the lock lap 2 reads.
            self.assertTrue(parse_kicad_pcb(lap1).footprints['R2'].locked)
            r, summary = _run(lap1, doc, code=4, out_name='lap2.kicad_pcb',
                              refuse='pad copper past the board outline '
                                     'on R2')
        self.assertEqual([ref for ref, _mm in
                          summary['pad_copper_off_board_locked']], ['R2'])
        self.assertIn("(locked yes) in the input with pad copper past the "
                      "board outline", r.stdout)
        self.assertNotIn("the board's own", r.stdout)

    def test_an_unseated_part_is_charged_as_unseated_only(self):
        # U9 fits nowhere on the board and comes in OFF it, so it is written
        # at its input pose with its copper past the outline; the gate names
        # it as UNSEATED, not again as off-board copper, and not as the
        # board's own.
        u9 = _part('U9', 20, 60, [(1, 0, 0, 0.6, 0.6, 2)], hw=25, hh=25)
        with tempfile.TemporaryDirectory() as td:
            r, summary = _seed(td, {'min': 0.0, 'max': 2.0}, code=4,
                               refuse='UNSEATED', extra=[u9])
        self.assertIn('U9', summary['unseated_refs'])
        # The precondition: without it the exclusion is never exercised (an
        # on-board U9 let the filter's removal pass, #1241's battery).
        self.assertLess(
            summary['pad_edge_after']['minimum_gap_by_ref_mm']['U9'], 0)
        self.assertEqual(summary['pad_copper_off_board_seeded'], [])
        self.assertNotIn("the board's own", r.stdout)


class InPlace(unittest.TestCase):
    """`place_seed X X` overwrites its input (recorded manifests run steps
    that way). Re-reading the input FILE for "the board's own" then saw
    every part unmoved and every lock this run stamped as the input's
    (#1239's round-3 verifier)."""

    def test_polished_copper_is_charged_in_place(self):
        with tempfile.TemporaryDirectory() as td:
            path = _synthetic(td, _scoped())
            r, summary = _run(path, _R2_ROT, code=4, flags=(),
                              refuse='pad copper past the board outline '
                                     'on R2',
                              out_name=os.path.basename(path))
        self.assertEqual([ref for ref, _mm in
                          summary['pad_copper_off_board_seeded']], ['R2'])
        self.assertNotIn("the board's own", r.stdout)

    def test_a_must_lock_seat_is_charged_in_place(self):
        # Stage 1.5 (blind) keeps lap 0's R2 at its off-board seat, and the
        # run stamps it locked over the input it read; neither makes it the
        # board's own.
        with tempfile.TemporaryDirectory() as td:
            lap0 = _lap0(td)
            r, summary = _run(lap0, {'must_lock': ['R2']}, code=4,
                              refuse='pad copper past the board outline '
                                     'on R2',
                              out_name=os.path.basename(lap0), blind=True)
        self.assertEqual([ref for ref, _mm in
                          summary['pad_copper_off_board_seeded']], ['R2'])
        self.assertNotIn("the board's own", r.stdout)


class PoseMoved(unittest.TestCase):
    """`place_seed.pose_moved`, which both the gate and the re-seat line
    read (#1240, #1241)."""

    def test_a_rotation_alone_is_a_move(self):
        self.assertTrue(place_seed.pose_moved((1, 2, 0), (1, 2, 90)))

    def test_rotations_compare_on_the_circle(self):
        self.assertFalse(place_seed.pose_moved((1, 2, -90), (1, 2, 270)))
        self.assertFalse(place_seed.pose_moved((1, 2, 359.9999999), (1, 2, 0)))

    def test_the_writers_rounding_is_not_a_move(self):
        # 180.1234567 is written as 180.123 (6 significant digits).
        self.assertFalse(place_seed.pose_moved((1, 2, 180.1234567),
                                               (1, 2, 180.123)))
        self.assertTrue(place_seed.pose_moved((1, 2, 0), (1.01, 2, 0)))
        self.assertTrue(place_seed.pose_moved((1, 2, 0), (1, 2.01, 0)))


class TheFloorNote(unittest.TestCase):
    """#1241 (d): the note says where the copper is."""

    def _note(self, gap):
        from placement.seeder import _floor_note
        return _floor_note('NOTE: edge connector ', 'J1', {
            'edge': 'north', 'required_mm': 0.55, 'why': 'band_min',
            'shortfall_mm': 0.55 - gap, 'overhang_after_mm': 0.0,
            'band_min_mm': 1.5,
            'pads': [{'pad_ref': 'J1.1', 'gap_mm': gap, 'side': 'north'}]})

    def test_copper_off_the_board_says_so(self):
        note = self._note(-1.2)
        self.assertIn('leaves pad copper past the board outline -- J1.1 '
                      'reaches 1.200mm past the north edge (1.750mm short of '
                      'the 0.55mm board-edge floor)', note)

    def test_copper_inside_the_floor_says_so(self):
        note = self._note(0.2)
        self.assertIn('leaves pad copper inside the 0.55mm board-edge floor '
                      '-- J1.1 is 0.200mm from the north edge (0.350mm short '
                      'of the 0.55mm board-edge floor)', note)
        self.assertNotIn('past the board outline', note)


if __name__ == '__main__':
    unittest.main()
