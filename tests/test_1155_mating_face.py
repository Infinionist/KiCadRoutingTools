"""#1155: a connector's MATING FACE, read off its drawn body (`placement.mating`).

The edge rule used to know only which edge a connector's courtyard was nearest,
so a DSUB seated backwards -- shell over the board, opening inward -- passed
whenever its shell grazed the declared edge. The fact that decides it is where
the body sits relative to the pad row; these tests pin how that is read:

  * the face is the body centre's offset off the pad-bbox centre, in the
    footprint's own frame, mapped through the pose with `connector_geometry`'s
    sign and NO extra B-side mirror;
  * it abstains -- it never guesses -- on symmetric parts, disagreeing
    sources, vertical mounts, board-copper plugs and diagonal poses;
  * `floorplan.edge_facing` grades receptacles and declared edge/through
    mounts only, and is the one predicate the rule and the seeder share;
  * the tracked corpus's verdicts are a committed change detector
    (`tests/fixtures/1155_mating_census.json`, re-pinned with
    `tests/measure_1155_mating_census.py --no-fa10 --write-fixture`).
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

from kicad_parser import parse_kicad_pcb                      # noqa: E402
from placement import floorplan, mating                      # noqa: E402
from placement.body import BodyGeometry, board_bodies        # noqa: E402
from run_utils import corpus_boards                          # noqa: E402

PADS = (-6.0, -1.0, 6.0, 1.0)      # a 1x9-ish pad row along x


def _fp(name='Lib:Connector_Horizontal', attrs=(), pads=(), rotation=0.0,
        layer='F.Cu', ref='J1'):
    return SimpleNamespace(footprint_name=name, attrs=attrs, has_model=False,
                           net_tie_groups=None, pads=list(pads),
                           rotation=rotation, layer=layer, reference=ref)


def _geom(court=None, drawn=None, drawn_source='fab', pads=PADS):
    src = 'courtyard' if court is not None else (
        drawn_source if drawn is not None else 'pad_bbox')
    body = court if court is not None else (drawn if drawn is not None
                                            else pads)
    return BodyGeometry('J1', body, body, src,
                        drawn_local=drawn,
                        drawn_source=drawn_source if drawn is not None
                        else 'none',
                        pads_local=pads)


class TheFace(unittest.TestCase):

    def test_a_body_beyond_the_pads_names_that_side(self):
        lf = mating.mating_face_local(_fp(), _geom(court=(-8, -15, 8, 1.5)))
        self.assertEqual(lf.face, '-y')
        self.assertEqual(lf.basis, 'courtyard')
        self.assertGreater(lf.offset_mm, mating.FACE_MIN_OFFSET_MM)

    def test_both_sources_agreeing_are_named_together(self):
        lf = mating.mating_face_local(
            _fp(), _geom(court=(-8, -1.5, 8, 15), drawn=(-7, -1, 7, 14)))
        self.assertEqual((lf.face, lf.basis), ('+y', 'courtyard+fab'))

    def test_a_symmetric_part_abstains(self):
        lf = mating.mating_face_local(_fp(), _geom(court=(-8, -3, 8, 3)))
        self.assertIsNone(lf.face)
        self.assertIn('symmetric', lf.reason)

    def test_an_offset_below_the_floor_abstains(self):
        d = mating.FACE_MIN_OFFSET_MM * 0.9
        lf = mating.mating_face_local(_fp(),
                                      _geom(court=(-8, -2, 8, 2 + 2 * d)))
        self.assertIsNone(lf.face)

    def test_an_offset_without_dominance_abstains(self):
        # 2 mm off in y AND 1.5 mm off in x: not a face, a lopsided part.
        lf = mating.mating_face_local(_fp(), _geom(court=(-5, -1, 10, 5)))
        self.assertIsNone(lf.face)

    def test_disagreeing_sources_abstain_and_say_so(self):
        lf = mating.mating_face_local(
            _fp(), _geom(court=(-8, -1.5, 8, 15), drawn=(-7, -14, 7, 1)))
        self.assertIsNone(lf.face)
        self.assertIn('disagree', lf.reason)

    def test_a_vertical_footprint_name_abstains(self):
        # A JST-XH vertical draws its plastic 0.5 mm off its pad row.
        lf = mating.mating_face_local(
            _fp(name='Connector_JST:JST_XH_B3B-XH-A_1x03_P2.50mm_Vertical'),
            _geom(court=(-8, -15, 8, 1.5)))
        self.assertIsNone(lf.face)
        self.assertIn('vertical', lf.reason)

    def test_the_item_name_decides_not_the_library(self):
        # The pcbnew path keeps no library prefix; a library NAMED vertical
        # must not abstain a horizontal part.
        self.assertFalse(mating.is_vertical_name(
            _fp(name='Vertical_Lib:USB_C_Receptacle_Horizontal')))
        self.assertTrue(mating.is_vertical_name(_fp(name='PinHeader_Vertical')))

    def test_no_pads_abstains(self):
        lf = mating.mating_face_local(_fp(), _geom(court=(-8, -15, 8, 1.5),
                                                   pads=None))
        self.assertIsNone(lf.face)

    def test_a_board_copper_plug_abstains(self):
        pads = [SimpleNamespace(pad_type='smd', layers=['F.Cu'], drill=0,
                                net_id=n) for n in (1, 2, 3, 4)]
        fp = _fp(attrs=('board_only',), pads=pads)
        self.assertTrue(floorplan.is_board_copper_plug(fp))
        lf = mating.mating_face_local(fp, _geom(court=(-8, -15, 8, 1.5)))
        self.assertIsNone(lf.face)
        self.assertIn('plug', lf.reason)
        # ...and three fingers are a jumper, not a plug.
        self.assertFalse(floorplan.is_board_copper_plug(
            _fp(attrs=('board_only',), pads=pads[:3])))


class TheBoardFrame(unittest.TestCase):

    def test_each_quarter_turn(self):
        # connector_geometry.rect: (u, v) -> (c u + s v, -s u + c v),
        # board y grows SOUTH.
        self.assertEqual([mating.facing_edge('-y', r)
                          for r in (0, 90, 180, 270, -90)],
                         ['north', 'west', 'south', 'east', 'east'])
        self.assertEqual([mating.facing_edge('+x', r) for r in (0, 90)],
                         ['east', 'north'])

    def test_a_diagonal_pose_names_no_edge(self):
        self.assertIsNone(mating.facing_edge('-y', 45.0))
        self.assertEqual(mating.facing_edge('-y', 20.0), 'north')

    def test_facing_rotations(self):
        self.assertEqual(mating.facing_rotations('-y', 'east'), (270.0,))
        self.assertEqual(mating.facing_rotations('+y', 'north'), (180.0,))
        self.assertEqual(mating.facing_rotations(None, 'north'), ())

    def test_b_side_parts_are_not_mirrored_twice(self):
        """sonde_u J2 is the #1155 DSUB on B.Cu, posed correctly at -90 on
        the east edge. Its file coordinates are stored mirrored already, so
        mirroring again would call the correct pose reversed."""
        path = os.path.join(ROOT, 'kicad_files', 'sonde_u.kicad_pcb')
        pcb = parse_kicad_pcb(path)
        faces = mating.board_faces(pcb, path)
        j2 = pcb.footprints['J2']
        self.assertEqual(j2.layer, 'B.Cu')
        self.assertEqual(faces['J2'].face, '-y')
        self.assertEqual(mating.facing_edge(faces['J2'].face, j2.rotation),
                         'east')
        # The reversed pose of #1155: rot 90 faces west, into the board.
        self.assertEqual(mating.facing_edge(faces['J2'].face, 90.0), 'west')


class ThePolicy(unittest.TestCase):

    def test_order(self):
        fp = _fp(name='Connector_Dsub:DSUB-9_Male_EdgeMount_P2.77mm')
        pol = floorplan.edge_facing_policy
        self.assertEqual(pol({'context': {'mount_mode': 'top_mount'},
                              'class': 'edge_receptacle'}, fp)[:6], 'exempt')
        self.assertEqual(pol({'context': {'mount_mode': 'edge_mount'},
                              'class': 'connector_affinity'}, fp), 'graded')
        self.assertEqual(pol({'context': {'mount_mode': 'through_edge'}}, fp),
                         'graded')
        self.assertEqual(pol({'class': 'edge_receptacle'}, fp), 'graded')
        self.assertTrue(pol({'class': 'connector_affinity'}, fp)
                        .startswith('exempt'))
        self.assertTrue(pol({'class': 'edge_actuator'}, fp)
                        .startswith('exempt'))
        # Undeclared: the part's own class decides (a DSUB is a receptacle).
        self.assertEqual(pol({}, fp), 'graded')

    def test_the_reversed_dsub_fails_and_the_correct_one_passes(self):
        path = os.path.join(ROOT, 'kicad_files', 'sonde_u.kicad_pcb')
        pcb = parse_kicad_pcb(path)
        geom = board_bodies(pcb, path)['J2']
        fp = pcb.footprints['J2']
        for entry in ({'ref': 'J2', 'edge': 'east'},
                      {'ref': 'J2', 'edge': 'east',
                       'class': 'edge_receptacle'}):
            self.assertFalse(floorplan.edge_facing(entry, fp, geom)['fails'])
            bad = floorplan.edge_facing(entry, fp, geom, rotation=90.0)
            self.assertTrue(bad['fails'])
            self.assertEqual(bad['facing'], 'west')
        # Exempt is exempt, reversed or not.
        self.assertFalse(floorplan.edge_facing(
            {'ref': 'J2', 'edge': 'east', 'class': 'connector_affinity'},
            fp, geom, rotation=90.0)['fails'])

    def test_no_geometry_never_fails(self):
        ef = floorplan.edge_facing({'edge': 'east', 'class': 'edge_receptacle'},
                                   _fp(), None)
        self.assertFalse(ef['fails'])
        self.assertIsNone(ef['facing'])


class TheCorpusCensus(unittest.TestCase):
    """The tracked boards' verdicts, against the committed fixture."""

    def test_census_matches_the_fixture(self):
        boards = corpus_boards()
        if not boards:
            self.skipTest('git cannot list the tracked boards')
        import measure_1155_mating_census as m
        with open(m.FIXTURE, encoding='utf-8') as fh:
            want = json.load(fh)
        got = {}
        for path in boards:
            parts, _e, _err = m.census(parse_kicad_pcb(path), path,
                                       emit=False)
            got[os.path.basename(path)] = {
                r: [p['face'], p['basis'], p['facing']]
                for r, p in parts.items()}
        self.assertEqual(sorted(got), sorted(want))
        for name in sorted(want):
            self.assertEqual(got[name], want[name], name)

    def test_every_faced_receptacle_faces_the_edge_it_is_nearest(self):
        """On the designers' own boards a receptacle opens toward the edge
        it sits at; a decisive face pointing elsewhere would be a false
        positive of the rule."""
        import measure_1155_mating_census as m
        boards = corpus_boards()
        if not boards:
            self.skipTest('git cannot list the tracked boards')
        bad = []
        for path in boards:
            parts, _e, _err = m.census(parse_kicad_pcb(path), path,
                                       emit=False)
            bad += [(os.path.basename(path), r, p['facing'], p['nearest'])
                    for r, p in parts.items()
                    if p['class'] == 'edge_receptacle' and p['facing']
                    and p['facing'] != p['nearest']]
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
