"""#1172 (and #1155's generator half): stage 1 chooses an edge connector's
angle, and keeps its courtyard off the connectors it has already seated.

On glasgow_revC's pile, stage 1 seated the IDC box headers J2 (north) and J3
(south) at the pile's arbitrary rotation 0 -- each one's long axis ACROSS the
board -- and checked only pads against what it had seated, so their shells
interpenetrated (76.3 mm2 on the fa10 input). Two mechanisms, two fixes:

  1. `_stage1_claims`: an edge connector whose INPUT angle carries no
     information (`_uninformative_refs`: the board is a pile, or its pads are
     all off the outline) gets every quarter turn, least depth into the board
     first; a STRONG mating face (#1155) decides the angle outright, and
     turns a part facing away on any board. Everything else keeps its angle.
  2. `_placed_courtyard_hits` at rung admission: a rung that overlaps a
     placed courtyard (the floorplan budget's currency) is refused in a
     first pass; only when no rung clears both pads and courtyards does the
     old pad-only ladder run (`_stage1_one`), so it cannot lose a seat.

`_seat_edge` (the repair path) turns a strong face that points away to the
facing quarter turn and never seats it reversed. Each has a blind twin: the
module flags `_stage1_choose_edge_angle` / `_edge_courtyard_tier` off.
"""

import json
import os
import random
import sys
import tempfile
import unittest
from contextlib import contextmanager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

from kicad_parser import parse_kicad_pcb                      # noqa: E402
from placement import floorplan, legality, seeder            # noqa: E402
from placement.body import board_bodies                      # noqa: E402
from run_utils import check                                  # noqa: E402

PY = sys.executable
KF = os.path.join(ROOT, 'kicad_files')
GLASGOW = os.path.join(KF, 'glasgow_revC.kicad_pcb')
SONDE = os.path.join(KF, 'sonde_u.kicad_pcb')
MK = {'MK1': (54, 116), 'MK2': (54, 75), 'MK3': (126, 75), 'MK4': (126, 116)}


@contextmanager
def flags(**kw):
    saved = {k: getattr(seeder, k) for k in kw}
    for k, v in kw.items():
        setattr(seeder, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(seeder, k, v)


def _pile(td, src, name, *extra_ops):
    """`make_unplaced` of a tracked board, then `place_pose` ops on it."""
    pile = os.path.join(td, name + '_pile.kicad_pcb')
    check([PY, '-X', 'utf8', '.claude/skills/pcb-free-agent/scripts/'
           'make_unplaced.py', src, pile], accept=True)
    if not extra_ops:
        return pile
    out = os.path.join(td, name + '.kicad_pcb')
    check([PY, '-X', 'utf8', 'py_placer/place_pose.py', pile, out, '--force']
          + list(extra_ops), accept=True)
    return out


def _intent(td, board, entries, name='i'):
    doc = {'schema': floorplan.SCHEMA_VERSION, 'kind': floorplan.KIND,
           'units': 'mm', 'edge_connectors': entries}
    p = os.path.join(td, name + '.json')
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    return floorplan.load_intent(p)


def _seed(board, intent, refs):
    pcb = parse_kicad_pcb(board)
    res = seeder.seed_from_intent(pcb, board, intent, random.Random(1),
                                  seed_refs=set(refs))
    poses = {p['reference']: (p['new_x'], p['new_y'], p['new_rotation'])
             for p in res['placements']}
    return pcb, res, poses


def _overlap(board, poses, a, b):
    """The courtyard overlap of a and b at `poses`, in the budget's currency."""
    pcb = parse_kicad_pcb(board)
    cen = legality.CourtyardCensus(pcb, board)
    gp, _ = legality.courtyard_budget_universe(cen, [a, b], poses)
    return legality.pair_overlap_area_exact(gp[a], gp[b])


def _extent(board, ref, pose):
    pcb = parse_kicad_pcb(board)
    lb = legality.part_local_bounds(pcb, board)[ref]
    r = legality.rotate_local_bounds(*lb.local, pose[2])
    return r[2] - r[0], r[3] - r[1]


GLASGOW_CLAIMS = [{'ref': 'J2', 'edge': 'north',
                   'overhang_mm': {'min': 0, 'max': 0.55}},
                  {'ref': 'J3', 'edge': 'south',
                   'overhang_mm': {'min': 0, 'max': 0.55}}]


class GlasgowBoxHeaders(unittest.TestCase):
    """The issue's own input: glasgow's pile, MK1-4 placed and locked."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.mkdtemp()
        ops = []
        for ref, (x, y) in MK.items():
            ops += ['set', ref, str(x), str(y), '--rot', '0']
        ops += ['lock'] + list(MK)
        cls.board = _pile(cls.td, GLASGOW, 'g', *ops)
        cls.intent = _intent(cls.td, cls.board, GLASGOW_CLAIMS)

    def test_the_headers_lie_along_their_edges_and_clear_each_other(self):
        _pcb, res, poses = _seed(self.board, self.intent, ('J2', 'J3'))
        for ref in ('J2', 'J3'):
            w, h = _extent(self.board, ref, poses[ref])
            self.assertGreater(w, h, (ref, poses[ref]))
        self.assertLess(_overlap(self.board, poses, 'J2', 'J3'),
                        legality.EPS)
        self.assertEqual(sorted(res['edge_angle_chosen']), ['J2', 'J3'])
        self.assertTrue(any('stage 1 turned it' in n and '#1172' in n
                            for n in res['notes']))

    def test_blind_twin_reproduces_the_issue(self):
        with flags(_stage1_choose_edge_angle=False,
                   _edge_courtyard_tier=False):
            _pcb, res, poses = _seed(self.board, self.intent, ('J2', 'J3'))
        self.assertEqual({r: poses[r][2] % 360 for r in ('J2', 'J3')},
                         {'J2': 0.0, 'J3': 0.0})
        self.assertGreater(_overlap(self.board, poses, 'J2', 'J3'), 1.0)
        self.assertEqual(res['edge_angle_chosen'], {})

    def test_the_courtyard_tier_alone_separates_them(self):
        """Mechanism 2 by itself: both DECLARED at 0, so no angle is chosen,
        and J3 slides along its edge off J2's courtyard."""
        doc = {'schema': floorplan.SCHEMA_VERSION, 'kind': floorplan.KIND,
               'units': 'mm', 'edge_connectors': GLASGOW_CLAIMS,
               'blocks': [{'name': 'rotation:J2', 'refs': ['J2'],
                           'rotation': 0},
                          {'name': 'rotation:J3', 'refs': ['J3'],
                           'rotation': 0}]}
        p = os.path.join(self.td, 'decl.json')
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        intent = floorplan.load_intent(p)
        _pcb, res, poses = _seed(self.board, intent, ('J2', 'J3'))
        self.assertEqual({r: poses[r][2] % 360 for r in ('J2', 'J3')},
                         {'J2': 0.0, 'J3': 0.0})
        self.assertLess(_overlap(self.board, poses, 'J2', 'J3'),
                        legality.EPS)
        with flags(_edge_courtyard_tier=False):
            _pcb, _res, off = _seed(self.board, intent, ('J2', 'J3'))
        self.assertGreater(_overlap(self.board, off, 'J2', 'J3'), 1.0)

    def test_it_cannot_lose_a_seat(self):
        """When EVERY rung overlaps a placed courtyard, the seat is the one
        the old pad-only ladder writes, and the overlap is named."""
        orig = seeder._placed_courtyard_hits
        try:
            seeder._placed_courtyard_hits = (
                lambda state, ref, pose, others: ['MK1'])
            _pcb, res, poses = _seed(self.board, self.intent, ('J2', 'J3'))
        finally:
            seeder._placed_courtyard_hits = orig
        with flags(_edge_courtyard_tier=False):
            _pcb, _r, off = _seed(self.board, self.intent, ('J2', 'J3'))
        self.assertEqual({r: poses[r] for r in ('J2', 'J3')},
                         {r: off[r] for r in ('J2', 'J3')})
        self.assertTrue(any('overlaps it' in n and 'MK1' in n
                            for n in res['notes']))

    def test_determinism(self):
        a = _seed(self.board, self.intent, ('J2', 'J3'))[2]
        b = _seed(self.board, self.intent, ('J2', 'J3'))[2]
        self.assertEqual({r: a[r] for r in ('J2', 'J3')},
                         {r: b[r] for r in ('J2', 'J3')})


class TheMatingFaceDecides(unittest.TestCase):
    """sonde_u's J2 is #1155's DSUB edge-mount: a strong face (7.4 mm)."""

    #: The band `emit_intent` writes for J2 off the designer's board: the
    #: shell overhangs the edge by ~14 mm when it faces it.
    ENTRY = {'ref': 'J2', 'edge': 'east', 'class': 'edge_receptacle',
             'overhang_mm': {'min': 0.0, 'max': 15.15}}

    def _facing(self, board, pose):
        pcb = parse_kicad_pcb(board)
        geom = board_bodies(pcb, board)['J2']
        return floorplan.edge_facing(self.ENTRY, pcb.footprints['J2'], geom,
                                     rotation=pose[2])

    def test_from_a_pile_it_is_seated_facing_its_edge(self):
        with tempfile.TemporaryDirectory() as td:
            board = _pile(td, SONDE, 's')
            intent = _intent(td, board, [dict(self.ENTRY)])
            _pcb, res, poses = _seed(board, intent, ('J2',))
            self.assertEqual(self._facing(board, poses['J2'])['facing'],
                             'east')
            self.assertFalse(self._facing(board, poses['J2'])['fails'])
            self.assertIn('J2', res['edge_angle_chosen'])
            with flags(_stage1_choose_edge_angle=False):
                _pcb, _r, off = _seed(board, intent, ('J2',))
            # blind: seated at the pile's angle, facing away -- an ERROR
            ef = self._facing(board, off['J2'])
            self.assertTrue(ef['fails'] and ef['strong'], ef)

    def test_a_placed_connector_facing_its_edge_is_not_turned(self):
        """The designer's own board: informative input, already facing."""
        with tempfile.TemporaryDirectory() as td:
            intent = _intent(td, SONDE, [dict(self.ENTRY)])
            j2 = parse_kicad_pcb(SONDE).footprints['J2']
            _pcb, res, poses = _seed(SONDE, intent, ('J2',))
        self.assertEqual(res['edge_angle_chosen'], {})
        self.assertAlmostEqual(poses['J2'][2] % 360, j2.rotation % 360)

    def test_repair_turns_a_reversed_connector_and_never_seats_it_reversed(
            self):
        import pose_score
        with tempfile.TemporaryDirectory() as td:
            rev = os.path.join(td, 'rev.kicad_pcb')
            j2 = parse_kicad_pcb(SONDE).footprints['J2']
            check([PY, '-X', 'utf8', 'py_placer/place_pose.py', SONDE, rev,
                   '--allow-routed', '--force', 'set', 'J2', str(j2.x),
                   str(j2.y), '--rot', '90'], accept=True)
            self.assertTrue(self._facing(rev, (0, 0, 90.0))['fails'])
            pcb = parse_kicad_pcb(rev)
            st = pose_score.make_state(pcb, rev)
            notes = []
            ok = seeder._seat_edge(st, 'J2', dict(self.ENTRY), set(), notes)
            self.assertTrue(ok, notes)
            self.assertAlmostEqual(st.parts['J2'].rot % 360, 270.0)
            self.assertTrue(any('#1155' in n for n in notes), notes)
            # blind twin: without the facing ladder it is seated reversed
            st2 = pose_score.make_state(parse_kicad_pcb(rev), rev)
            orig = seeder._strong_facing_rots
            try:
                seeder._strong_facing_rots = lambda *a, **k: None
                seeder._seat_edge(st2, 'J2', dict(self.ENTRY), set(), [])
            finally:
                seeder._strong_facing_rots = orig
            self.assertAlmostEqual(st2.parts['J2'].rot % 360, 90.0)


if __name__ == '__main__':
    unittest.main()
