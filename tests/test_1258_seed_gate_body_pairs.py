"""#1258: place_seed's gate charges the body pairs check_assembly gates on.

Stage 1 keeps an edge seat that crowds a placed courtyard when no rung clears
it ("the courtyard budget and check_assembly grade that overlap", #1172), and
nothing in place_seed's gate graded it: glasgow_revC's make_unplaced pile,
seeded under its designer intent, put J3's drawn body 71% inside SW1's, exited
0, and check_assembly called the board NOT BUILDABLE. compare_seeds and the
pcb-free-agent loop read that exit code to accept and rank seeds.

The gate now reads check_assembly's own grader (`grade_body_overlap`, called
through `place_seed.body_gate_pairs`) on the WRITTEN board:
`containment_blocking_pairs`, which no class waiver exempts, and
`courtyard_blocking_pairs`, which check_assembly gates when a member moved
against its baseline (this run's baseline is its input). A pair is charged
when a member is a part the seed placed -- one it moved, or one that was its
to place (unlocked, in scope) at a pose it kept -- and is the board's own when
both members are locked in the input or outside a partial run's scope and
unmoved: named, not charged (#1193's pinned-part contract; pad copper past
the outline stays the one finding where a lock is no waiver). `--repair` /
`--reseat` charge the same pairs on the board they deliver.

The fixture is the glasgow shape on a 30 x 20 board: J2, a 26 mm wide
connector with pads at its ends, and J1, a 6 mm one, both claimed on the
south edge. Stage 1 seats J1 first; no rung on the south edge clears it for
J2, so J2 keeps a pad-clear seat over J1, whose body then lies wholly inside
J2's.
"""

import collections
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import compare_seeds                                         # noqa: E402
import place_seed                                            # noqa: E402
from run_utils import check, evidence                        # noqa: E402

PY = sys.executable
W, H = 30, 20
J2W = 26
J2PADS = [(1, -J2W / 2 + 1, 2, 1), (2, J2W / 2 - 1, 2, 2)]
J1PADS = [(1, -1, 1, 1), (2, 1, 1, 2)]
RPADS = [(1, -0.5, 0, 1), (2, 0.5, 0, 2)]
PILE = (W / 2, H + 10)          # off the board
SOUTH = [{'ref': r, 'edge': 'south', 'overhang_mm': {'min': 0, 'max': 0.5}}
         for r in ('J1', 'J2')]


def _part(ref, at, hw, hh, pads, fab=True, locked=False, name='t:CONN'):
    """A part with a +-hw x +-hh courtyard, a drawn .Fab body just inside
    it unless `fab` is False, and `pads` = [(number, x, y, net)]."""
    lk = '    (locked yes)\n' if locked else ''
    s = (f'  (footprint "{name}" (layer "F.Cu") (at {at[0]} {at[1]})\n' + lk
         + f'    (property "Reference" "{ref}" (at 0 0) (layer "F.SilkS"))\n'
         f'    (fp_rect (start {-hw} {-hh}) (end {hw} {hh}) (stroke'
         f' (width 0.05) (type default)) (layer "F.CrtYd"))\n')
    if fab:
        s += (f'    (fp_rect (start {-hw + 0.1} {-hh + 0.1}) (end '
              f'{hw - 0.1} {hh - 0.1}) (stroke (width 0.1) (type default))'
              f' (layer "F.Fab"))\n')
    for n, px, py, net in pads:
        s += (f'    (pad "{n}" smd rect (at {px} {py}) (size 1 1)'
              f' (layers "F.Cu") (net {net} "N{net}"))\n')
    return s + '  )\n'


def _j2(at=PILE, **kw):
    return _part('J2', at, J2W / 2, 3, J2PADS, **kw)


def _j1(at=PILE, **kw):
    return _part('J1', at, 3, 2, J1PADS, **kw)


def _r(ref, at=PILE):
    return _part(ref, at, 1, 0.6, RPADS, name='t:R')


def _board(td, parts, h=H):
    text = ('(kicad_pcb (version 20240108) (generator pcbnew)\n'
            '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal)'
            ' (44 "Edge.Cuts" user))\n'
            '  (net 0 "") (net 1 "N1") (net 2 "N2")\n'
            f'  (gr_rect (start 0 0) (end {W} {h}) (stroke'
            ' (width 0.1) (type default)) (layer "Edge.Cuts"))\n'
            + ''.join(parts) + ')\n')
    path = os.path.join(td, 'b.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return path


def _intent(td, claims=()):
    path = os.path.join(td, 'i.json')
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'schema': 1, 'kind': 'floorplan-intent', 'units': 'mm',
                   'blocks': [], 'edge_connectors': list(claims)}, fh)
    return path


def _summary(r):
    return [json.loads(ln.split(':', 1)[1]) for ln in r.stdout.splitlines()
            if ln.startswith('JSON_SUMMARY:')][-1]


def _seed(td, parts, claims, code, refuse=None, flags=('--force',)):
    """place_seed the board `parts` under `claims`: (run, summary, out)."""
    path, ip = _board(td, parts), _intent(td, claims)
    out = os.path.join(td, 's.kicad_pcb')
    # 0.55: the edge floor this fixture was built at -- it declares none,
    # which #1301 reads as the 0.2 fab floor; there the fallback seat slides
    # J2's courtyard onto the east edge and its nearest-edge error fires first.
    r = check([PY, '-X', 'utf8',
               os.path.join(ROOT, 'py_placer', 'place_seed.py'),
               path, out, '--intent', ip, '--board-edge-clearance', '0.55']
              + list(flags),
              **({'accept': True} if code == 0
                 else {'refuse': refuse, 'code': code}))
    evidence(out, 'written seed')
    return r, _summary(r), out


def _assembly(td, board, baseline=None):
    """check_assembly's own verdict on `board` (the gate must agree)."""
    js = os.path.join(td, 'ca.json')
    argv = [PY, '-X', 'utf8', os.path.join(ROOT, 'py_tools',
                                           'check_assembly.py'),
            board, '--intent', os.path.join(td, 'i.json'), '--json', js]
    if baseline:
        argv += ['--baseline', baseline]
    r = subprocess.run([str(a) for a in argv], capture_output=True,
                       text=True, encoding='utf-8', errors='replace',
                       cwd=ROOT)
    # 0 or 4 is a verdict; anything else (2, a traceback) is a broken test.
    assert r.returncode in (0, 4), r.stdout + r.stderr
    evidence(js, 'check_assembly --json')
    with open(js, encoding='utf-8') as fh:
        doc = json.load(fh)
    # The JSON verdict and the exit code are one predicate there.
    assert doc['buildable'] == (r.returncode == 0), (doc['buildable'],
                                                     r.returncode)
    return doc


def _pairs(rows):
    return [sorted(row[:2]) for row in rows]


def _old_gate(s):
    """What place_seed's gate said before #1258, from the same summary: the
    reason with the two new channels left out. The blind twin."""
    return place_seed.gate_reason(
        s['unseated_refs'], [None] * s['grade_errors'],
        s['pad_conflicts_seeded_pairs'], s['hole_conflicts_added'],
        band=[r for r, _a in s['keepout_copper_seeded']],
        oob=[r for r, _a in s['pad_copper_off_board_seeded']
             + s['pad_copper_off_board_locked']])


class TheGateReason(unittest.TestCase):

    def test_the_containment_arm_names_the_pairs(self):
        why = place_seed.gate_reason([], [], [], 0, contained=[('J1', 'J2')])
        self.assertIn("satisfies its intent but leaves a part's drawn body "
                      "inside another's on J1 <-> J2, which cannot be "
                      "assembled", why)

    def test_the_courtyard_arm_names_the_pairs(self):
        why = place_seed.gate_reason([], [], [], 0, courtyard=[('A', 'B')])
        self.assertIn("courtyards interpenetrating past check_assembly's "
                      "blocking floors on A <-> B", why)

    def test_nothing_fired_is_none(self):
        self.assertIsNone(place_seed.gate_reason([], [], [], 0,
                                                 contained=(), courtyard=()))


_Q = collections.namedtuple('_Q', 'a b area_mm2')


class TheSplit(unittest.TestCase):
    """`split_body_pairs`: the pad channel's attribution, called."""

    def test_a_pair_with_a_part_the_seed_placed_is_the_seeds(self):
        q = _Q('J1', 'J2', 20.0)
        mine, against, own = place_seed.split_body_pairs([q], {'J1'}, [])
        self.assertEqual((mine, against, own), ([q], [], []))

    def test_a_pair_between_parts_it_did_not_place_is_the_boards(self):
        q = _Q('J1', 'J2', 20.0)
        self.assertEqual(place_seed.split_body_pairs([q], {'R1'}, []),
                         ([], [], [q]))

    def test_a_pair_against_an_unseated_part_is_reported_apart(self):
        q = _Q('J1', 'U9', 20.0)
        self.assertEqual(place_seed.split_body_pairs([q], {'J1'}, ['U9']),
                         ([], [q], []))

    def test_a_late_move_is_charged_on_the_delta(self):
        q = _Q('J1', 'J2', 20.0)
        key = frozenset(('J1', 'J2'))
        # Moved by the polish, out of scope: the input had it no shallower.
        self.assertEqual(place_seed.split_body_pairs(
            [q], set(), [], late={'J1'}, before={key: 20.0}), ([], [], [q]))
        # ...deeper than the input had it, or not there at all: the seed's.
        self.assertEqual(place_seed.split_body_pairs(
            [q], set(), [], late={'J1'}, before={key: 12.0}), ([q], [], []))
        self.assertEqual(place_seed.split_body_pairs(
            [q], set(), [], late={'J1'}, before={}), ([q], [], []))

    def test_the_input_is_graded_only_for_a_late_only_pair(self):
        q = _Q('J1', 'J2', 20.0)
        self.assertFalse(place_seed._pair_needs_before([q], {'J1'}, {'J2'}))
        self.assertTrue(place_seed._pair_needs_before([q], set(), {'J2'}))
        self.assertFalse(place_seed._pair_needs_before([q], set(), set()))


class TheRepairSplit(unittest.TestCase):

    def _split(self, pinned, moved, whole):
        q = _Q('J1', 'J2', 20.0)
        return place_seed.repair_pair_split([q], set(pinned), set(moved),
                                            whole)

    def test_a_pinned_pair_is_reported_not_charged(self):
        self.assertEqual(self._split({'J1', 'J2'}, (), True)[1][0].a, 'J1')

    def test_a_pair_the_pass_moved_into_is_charged(self):
        self.assertTrue(self._split({'J2'}, {'J1'}, False)[0])

    def test_repair_answers_for_a_free_member_it_left(self):
        self.assertTrue(self._split({'J2'}, (), True)[0])

    def test_outside_a_reseat_scope_it_is_the_boards_own(self):
        self.assertTrue(self._split({'J2'}, (), False)[2])


class TheSeedGate(unittest.TestCase):

    def test_a_body_the_seed_put_inside_another_is_charged(self):
        with tempfile.TemporaryDirectory() as td:
            r, s, out = _seed(td, [_j2(), _j1(), _r('R1')], SOUTH, code=4,
                              refuse="leaves a part's drawn body inside "
                                     "another's on J1 <-> J2")
            ca = _assembly(td, out)
        # The blind twin: before #1258 nothing else fired -- the intent
        # grades clean, no pad conflict, no copper off the board.
        self.assertIsNone(_old_gate(s))
        self.assertEqual(s['grade_error_rules'], {})
        self.assertEqual(_pairs(s['containment_seeded']), [['J1', 'J2']])
        self.assertEqual(s['containment_seeded'][0][3], 1.0)
        self.assertEqual(_pairs(s['courtyard_blocking_seeded']),
                         [['J1', 'J2']])
        self.assertEqual(s['containment_inherited'], [])
        # check_assembly agrees: the board is NOT BUILDABLE on this pair.
        self.assertFalse(ca['buildable'])
        self.assertEqual(ca['containment_blocking'], 1)
        self.assertIn('keeps a pad-clear seat', r.stdout)

    def test_a_courtyard_pair_is_charged_without_a_drawn_body(self):
        # No .Fab bodies: the containment channel cannot judge, and the
        # courtyard channel is what fires -- the pair check_assembly gates
        # against the board the run started from.
        with tempfile.TemporaryDirectory() as td:
            _r_, s, out = _seed(td, [_j2(fab=False), _j1(fab=False),
                                     _r('R1')], SOUTH, code=4,
                                refuse="courtyards interpenetrating past "
                                       "check_assembly's blocking floors on "
                                       "J1 <-> J2")
            ca = _assembly(td, out, baseline=os.path.join(td, 'b.kicad_pcb'))
        self.assertIsNone(_old_gate(s))
        self.assertEqual(s['containment_seeded'], [])
        self.assertEqual(_pairs(s['courtyard_blocking_seeded']),
                         [['J1', 'J2']])
        self.assertFalse(ca['buildable'])
        self.assertEqual(ca['courtyard_blocking_gating'], 1)

    def test_a_lock_on_the_host_waives_nothing(self):
        # J2 is LOCKED on the south edge in the input; the seed seats J1
        # over it. The seed placed J1, so the pair is the seed's.
        with tempfile.TemporaryDirectory() as td:
            _r_, s, _out = _seed(td, [_j2(at=(W / 2, H - 3), locked=True),
                                      _j1(), _r('R1')], SOUTH[:1], code=4,
                                 refuse="drawn body inside another's on "
                                        "J1 <-> J2")
        self.assertEqual(_pairs(s['containment_seeded']), [['J1', 'J2']])
        self.assertEqual(s['containment_inherited'], [])

    def test_a_pair_between_two_locked_parts_is_the_boards_own(self):
        # J1 inside J2, both locked in the input: the board's own, named
        # and not charged. check_assembly still calls the board NOT
        # BUILDABLE -- reported here, not hidden.
        with tempfile.TemporaryDirectory() as td:
            r, s, out = _seed(td, [_j2(at=(W / 2, H - 3), locked=True),
                                   _j1(at=(W / 2 + 5, H - 3), locked=True),
                                   _r('R1'), _r('R2')], [], code=0)
            ca = _assembly(td, out)
        self.assertEqual(s['containment_seeded'], [])
        self.assertEqual(s['courtyard_blocking_seeded'], [])
        self.assertEqual(_pairs(s['containment_inherited']), [['J1', 'J2']])
        self.assertEqual(_pairs(s['courtyard_blocking_inherited']),
                         [['J1', 'J2']])
        self.assertIn("containment pair(s) between parts this seed did not "
                      "place -- the board's own, reported not charged: "
                      "J1 <-> J2", r.stdout)
        self.assertFalse(ca['buildable'])

    def test_a_pair_outside_a_partial_runs_scope_is_the_boards_own(self):
        # Partially unplaced: P1-P3 piled, the rest placed. Without --force
        # the seed seats only the pile; J1 inside J2 stands as placed.
        parts = ([_j2(at=(W / 2, H - 3)), _j1(at=(W / 2 + 5, H - 3))]
                 + [_r(f'P{i}', (W / 2, 5)) for i in (1, 2, 3)]
                 + [_r(f'R{i}', xy) for i, xy in enumerate(
                     [(4, 4), (26, 4), (4, 10), (26, 10)], 4)])
        with tempfile.TemporaryDirectory() as td:
            r, s, _out = _seed(td, parts, [], code=0,
                               flags=('--no-polish',))
        self.assertIn('partially unplaced', r.stdout)
        self.assertEqual(s['containment_seeded'], [])
        self.assertEqual(_pairs(s['containment_inherited']), [['J1', 'J2']])


class TheRepairGate(unittest.TestCase):
    """`--repair` charges the pairs check_assembly gates on the board it
    delivers, by `repair_pair_split`."""

    def _repair(self, td, parts, code, refuse=None, h=H):
        path, ip = _board(td, parts, h=h), _intent(td)
        out = os.path.join(td, 'r.kicad_pcb')
        r = check([PY, '-X', 'utf8',
                   os.path.join(ROOT, 'py_placer', 'place_seed.py'),
                   path, out, '--intent', ip, '--repair'],
                  **({'accept': True} if code == 0
                     else {'refuse': refuse, 'code': code}))
        evidence(out, 'delivered board')
        return r, _summary(r)

    def test_a_pinned_pair_is_reported_not_a_gate_failure(self):
        with tempfile.TemporaryDirectory() as td:
            r, s = self._repair(td, [
                _j2(at=(W / 2, H - 3), locked=True),
                _j1(at=(W / 2 + 5, H - 3), locked=True),
                _r('R1', (5, 5)), _r('R2', (25, 5))], code=0)
        self.assertEqual(_pairs(s['containment_pinned']), [['J1', 'J2']])
        self.assertEqual(s['containment_charged'], [])
        self.assertIn('reported, not a gate failure', r.stdout)

    def test_a_free_part_left_inside_a_body_fails_the_gate(self):
        # J1 is free inside the locked J2 on a 30 x 10 board with no room
        # left for it: the repair cannot clear the pair, and the delivered
        # board carries it.
        with tempfile.TemporaryDirectory() as td:
            _r_, s = self._repair(td, [
                _j2(at=(W / 2, 7), locked=True), _j1(at=(W / 2 + 5, 7))],
                code=4, refuse="leaves a part's drawn body inside another's "
                               "on J1 <-> J2", h=10)
        self.assertEqual(_pairs(s['containment_charged']), [['J1', 'J2']])


class CompareSeedsDecapArm(unittest.TestCase):
    """#1191's decap-only repair arm must not read a seed failing on a body
    pair as decap-only (as 79615303 taught it #1241's keys)."""

    def test_a_body_pair_is_not_decap_only(self):
        base = {'grade_error_rules': {'decap_distance': 1}, 'unseated': 0,
                'pad_conflicts_seeded': 0, 'hole_conflicts_added': 0,
                'keepout_copper_seeded': [],
                'pad_copper_off_board_seeded': [],
                'pad_copper_off_board_locked': [],
                'containment_seeded': [], 'courtyard_blocking_seeded': []}
        self.assertTrue(compare_seeds.decap_only(base))
        for k in ('containment_seeded', 'courtyard_blocking_seeded'):
            self.assertFalse(compare_seeds.decap_only(
                dict(base, **{k: [['J1', 'J2', 20.0, 1.0]]})), k)


if __name__ == '__main__':
    unittest.main()
