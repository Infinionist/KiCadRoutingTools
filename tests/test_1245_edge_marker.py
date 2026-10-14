"""#1245: the "PCB Edge" marker a footprint draws confirms or vetoes its face.

`mating.mating_face_local` reads a connector's mating face off its body: the
side the courtyard and the drawn body leave the pad row toward. Below
`FACE_STRONG_OFFSET_MM` that face is WEAK -- reported at WARN, only a
preference to the seeder -- because pad layouts, not housings, set some
stock receptacles' offsets. Many footprints say where the board edge goes
themselves: a "PCB Edge" text beside a line drawn there. Nothing read it.

`mating.marker_face_local` reads the LINE (the text position is wrong for
sonde_u's DSUBs, whose text sits 21.55 mm along the edge line): the straight
line parallel to the text's baseline, nearest it, on any layer but a
courtyard or copper; the mating face is the side away from the pads.
`mating_face_local` then lets it CONFIRM the body's face (strong) or VETO it
(an abstention), never name one alone. Calibrated on KiCad 10's stock
library by `tests/measure_1245_edge_marker.py`: it agrees with all 73 strong
body faces and contradicts none.
"""

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
from placement.parser import (EDGE_MARKER_TEXT, EdgeMarker,   # noqa: E402
                              edge_marker_of)

KF = os.path.join(ROOT, 'kicad_files')
ESP = os.path.join(KF, 'esp_prog.kicad_pcb')
ORANGE = os.path.join(KF, 'orangecrab_ext_pll.kicad_pcb')
SONDE = os.path.join(KF, 'sonde_u.kicad_pcb')

PADS = (-4.0, -1.0, 4.0, 1.0)            # a pad row centred on the origin


def _fp(rot=0.0, name='t:Conn'):
    return SimpleNamespace(rotation=rot, footprint_name=name, attrs=(),
                           pads=[], net_tie_groups=None)


def _geom(marker=None, body=None, pads=PADS):
    """A courtyard-only body (`body`, default symmetric about the pads)."""
    body = body or (pads[0] - 0.5, pads[1] - 0.5, pads[2] + 0.5,
                    pads[3] + 0.5)
    return BodyGeometry('J', body, body, 'courtyard', pads_local=pads,
                        edge_marker=marker)


def _marker(texts, lines):
    return EdgeMarker(tuple(texts), tuple(lines))


class TheTrackedMarkedParts(unittest.TestCase):
    """The four tracked parts that draw the marker, at their real poses."""

    @classmethod
    def setUpClass(cls):
        cls.faces = {}
        for path, refs in ((ESP, ('USB1',)), (ORANGE, ('J3',)),
                           (SONDE, ('J1', 'J2'))):
            f = mating.board_faces(parse_kicad_pcb(path), path)
            for r in refs:
                cls.faces[(os.path.basename(path), r)] = f[r]

    def test_the_marker_agrees_with_the_body_on_all_four(self):
        want = {('esp_prog.kicad_pcb', 'USB1'): '+x',
                ('orangecrab_ext_pll.kicad_pcb', 'J3'): '+y',
                ('sonde_u.kicad_pcb', 'J1'): '+y',
                ('sonde_u.kicad_pcb', 'J2'): '-y'}
        for key, face in want.items():
            lf = self.faces[key]
            self.assertEqual((lf.face, lf.marker.face), (face, face), key)
            self.assertTrue(lf.basis.endswith('+marker'), (key, lf.basis))
            self.assertTrue(lf.strong, key)

    def test_the_weak_faces_it_confirms_are_strong_now(self):
        """USB1 (1.05 mm) and J3 (0.63 mm) were weak: below the 2 mm floor.
        The marker is what makes them strong; the offset is unchanged."""
        for key, off in ((('esp_prog.kicad_pcb', 'USB1'), 1.05),
                         (('orangecrab_ext_pll.kicad_pcb', 'J3'), 0.6334)):
            lf = self.faces[key]
            self.assertAlmostEqual(lf.offset_mm, off, places=3)
            self.assertLess(lf.offset_mm, mating.FACE_STRONG_OFFSET_MM)
            self.assertTrue(lf.strong)

    def test_the_line_not_the_text_position_is_read(self):
        """sonde_u J1's text sits at (-21.55, 1.32) in its own frame: read by
        its position it would say -x. The Dwgs.User line beside it, y = 1.99,
        says +y, which is the face its body reads (7.39 mm, strong)."""
        lf = self.faces[('sonde_u.kicad_pcb', 'J1')]
        geom = board_bodies(parse_kicad_pcb(SONDE), SONDE)['J1']
        (tx, ty, _a, layer, _t), = geom.edge_marker.texts
        cx, cy = ((geom.pads_local[0] + geom.pads_local[2]) / 2.0,
                  (geom.pads_local[1] + geom.pads_local[3]) / 2.0)
        self.assertGreater(abs(tx - cx), 10.0 * abs(ty - cy))
        self.assertEqual(lf.marker.line[:3], ('y', 1.99, 'Dwgs.User'))
        self.assertEqual(lf.marker.face, '+y')


class EdgeFacingGradesAConfirmedFaceAtError(unittest.TestCase):
    """What changes downstream: esp_prog's USB1 turned to face away from its
    west edge fails the facing conjunct STRONG (an ERROR) now -- before the
    marker it was weak, a WARN."""

    def test_usb1_turned_away_fails_strong(self):
        pcb = parse_kicad_pcb(ESP)
        geom = board_bodies(pcb, ESP)['USB1']
        fp = pcb.footprints['USB1']
        entry = {'ref': 'USB1', 'edge': 'west', 'class': 'edge_receptacle'}
        here = floorplan.edge_facing(entry, fp, geom)
        away = floorplan.edge_facing(entry, fp, geom,
                                     rotation=(fp.rotation or 0.0) + 180.0)
        self.assertEqual((here['facing'], here['fails'], here['strong']),
                         ('west', False, True))
        self.assertEqual((away['facing'], away['fails'], away['strong']),
                         ('east', True, True))
        blind = floorplan.edge_facing(entry, fp,
                                      geom._replace(edge_marker=None),
                                      rotation=(fp.rotation or 0.0) + 180.0)
        self.assertEqual((blind['fails'], blind['strong']), (True, False))


class TheReader(unittest.TestCase):
    """`marker_face_local` on drawn geometry, pads at y -1..1."""

    def read(self, texts, lines, rot=0.0):
        return mating.marker_face_local(_fp(rot), _geom(_marker(texts,
                                                                lines)))

    def test_the_parallel_line_beside_the_text(self):
        mk = self.read([(0.0, 2.6, 0.0, 'Dwgs.User', 'PCB Edge')],
                       [(-3.0, 3.0, 3.0, 3.0, 'Dwgs.User')])
        self.assertEqual(mk.face, '+y')
        self.assertEqual(mk.line[:3], ('y', 3.0, 'Dwgs.User'))

    def test_an_arrow_shaft_is_not_the_edge(self):
        """Stock DIN41612: "Board edge" 2 mm off the edge line with an arrow
        pointing at it. The shaft is the NEAREST line (0 mm) but runs
        across the text's baseline; the edge line runs along it."""
        mk = self.read([(0.0, -7.3, 0.0, 'Cmts.User', 'Board edge')],
                       [(0.0, -6.7, 0.0, -5.4, 'Cmts.User'),
                        (-0.2, -5.9, 0.0, -5.4, 'Cmts.User'),
                        (0.0, -5.4, 0.2, -5.9, 'Cmts.User'),
                        (-40.0, -5.3, 40.0, -5.3, 'Dwgs.User')])
        self.assertEqual(mk.face, '-y')
        self.assertEqual(mk.line[:2], ('y', -5.3))

    def test_the_text_angle_is_read_less_the_footprint_rotation(self):
        """A board file stores the text angle on screen: a footprint at 90
        whose marker reads 90 has a baseline along its own x."""
        texts = [(0.0, 2.6, 90.0, 'Dwgs.User', 'PCB Edge')]
        lines = [(-3.0, 3.0, 3.0, 3.0, 'Dwgs.User'),
                 (4.5, -3.0, 4.5, 3.0, 'Dwgs.User')]
        self.assertEqual(self.read(texts, lines, rot=90.0).face, '+y')
        self.assertEqual(self.read(texts, lines, rot=0.0).face, '+x')

    def test_courtyard_and_copper_lines_are_not_edges(self):
        mk = self.read([(0.0, 2.6, 0.0, 'Dwgs.User', 'PCB Edge')],
                       [(-3.0, 2.6, 3.0, 2.6, 'F.CrtYd'),
                        (-3.0, 2.6, 3.0, 2.6, 'F.Cu')])
        self.assertIsNone(mk.face)
        self.assertIn('no straight line', mk.reason)

    def test_it_abstains_on_a_line_through_the_pad_row(self):
        mk = self.read([(0.0, 0.3, 0.0, 'Dwgs.User', 'PCB Edge')],
                       [(-3.0, 0.2, 3.0, 0.2, 'Dwgs.User')])
        self.assertIsNone(mk.face)
        self.assertIn('through its pad row', mk.reason)

    def test_it_abstains_when_a_rival_line_is_nearly_as_near(self):
        """The text between y = 2 and y = -2.2: both sides read, neither
        twice as near."""
        mk = self.read([(0.0, -0.1, 0.0, 'Dwgs.User', 'PCB Edge')],
                       [(-3.0, 2.0, 3.0, 2.0, 'Dwgs.User'),
                        (-3.0, -2.2, 3.0, -2.2, 'Dwgs.User')])
        self.assertIsNone(mk.face)
        self.assertIn('name different faces', mk.reason)

    def test_it_abstains_on_a_diagonal_text(self):
        mk = self.read([(0.0, 2.6, 45.0, 'Dwgs.User', 'PCB Edge')],
                       [(-3.0, 3.0, 3.0, 3.0, 'Dwgs.User')])
        self.assertIsNone(mk.face)
        self.assertIn('off both axes', mk.reason)

    def test_it_abstains_when_two_markers_disagree(self):
        mk = self.read([(0.0, 2.6, 0.0, 'Dwgs.User', 'PCB Edge'),
                        (0.0, -2.6, 0.0, 'Dwgs.User', 'PCB Edge')],
                       [(-3.0, 3.0, 3.0, 3.0, 'Dwgs.User'),
                        (-3.0, -3.0, 3.0, -3.0, 'Dwgs.User')])
        self.assertIsNone(mk.face)
        self.assertIn('disagree', mk.reason)

    def test_no_marker_reads_none(self):
        self.assertIsNone(mating.marker_face_local(_fp(), _geom(None)))


class ConfirmOrVetoNeverName(unittest.TestCase):
    """How `mating_face_local` uses the reading."""

    WEAK = (-4.5, -1.5, 4.5, 2.7)       # courtyard 0.6 mm off toward +y
    STRONG = (-4.5, -1.5, 4.5, 5.5)     # 2.0 mm off toward +y
    UP = [(0.0, 2.6, 0.0, 'Dwgs.User', 'PCB Edge')]
    LINE_UP = [(-3.0, 3.0, 3.0, 3.0, 'Dwgs.User')]
    DOWN = [(0.0, -2.6, 0.0, 'Dwgs.User', 'PCB Edge')]
    LINE_DOWN = [(-3.0, -3.0, 3.0, -3.0, 'Dwgs.User')]

    def face(self, body, texts=None, lines=None):
        mk = _marker(texts, lines) if texts else None
        return mating.mating_face_local(_fp(), _geom(mk, body=body))

    def test_no_marker_is_todays_reading(self):
        lf = self.face(self.WEAK)
        self.assertEqual((lf.face, lf.strong, lf.basis, lf.marker),
                         ('+y', False, 'courtyard', None))

    def test_an_agreeing_marker_makes_a_weak_face_strong(self):
        lf = self.face(self.WEAK, self.UP, self.LINE_UP)
        self.assertEqual((lf.face, lf.strong, lf.basis),
                         ('+y', True, 'courtyard+marker'))
        self.assertLess(lf.offset_mm, mating.FACE_STRONG_OFFSET_MM)

    def test_a_contradicting_marker_vetoes_a_weak_face(self):
        lf = self.face(self.WEAK, self.DOWN, self.LINE_DOWN)
        self.assertIsNone(lf.face)
        self.assertFalse(lf.strong)
        self.assertTrue(lf.reason.startswith('its PCB-edge marker puts the '
                                             'board edge on its -y side'),
                        lf.reason)

    def test_a_contradicting_marker_vetoes_a_strong_face_too(self):
        """Never a fail: a contradiction is an abstention, whatever the
        body's offset."""
        lf = self.face(self.STRONG, self.DOWN, self.LINE_DOWN)
        self.assertIsNone(lf.face)
        self.assertEqual(lf.marker.face, '-y')

    def test_the_marker_never_names_a_face_alone(self):
        """A body symmetric about its pads abstains, marker or not; the
        reading is disclosed."""
        lf = self.face(None, self.UP, self.LINE_UP)
        self.assertIsNone(lf.face)
        self.assertEqual(lf.reason, 'symmetric about its pads')
        self.assertEqual(lf.marker.face, '+y')

    def test_a_vertical_mount_is_not_asked(self):
        mk = _marker(self.UP, self.LINE_UP)
        lf = mating.mating_face_local(_fp(name='t:Conn_Vertical'),
                                      _geom(mk, body=self.WEAK))
        self.assertIsNone(lf.face)
        self.assertIsNone(lf.marker)


class TheParser(unittest.TestCase):

    def test_the_marker_texts_it_reads(self):
        for t in ('PCB Edge', 'PCB edge', 'Board edge', 'PCB EDGE',
                  'PCB Edges', '^ Suggested board edge for RF ^'):
            self.assertTrue(EDGE_MARKER_TEXT.search(t), t)
        for t in ('PCB connector edge', 'Panel Edge', 'edge', 'PCBA'):
            self.assertFalse(EDGE_MARKER_TEXT.search(t), t)

    def test_kicad_10_and_kicad_5_forms(self):
        k10 = ('(footprint "x" (layer "F.Cu")\n'
               '\t(fp_line\n\t\t(start 5 3.675)\n\t\t(end -5 3.675)\n'
               '\t\t(stroke\n\t\t\t(width 0.1)\n\t\t\t(type solid)\n\t\t)\n'
               '\t\t(layer "Dwgs.User")\n\t)\n'
               '\t(fp_text user "PCB Edge"\n\t\t(at 0 3.1 0)\n'
               '\t\t(unlocked yes)\n\t\t(layer "Dwgs.User")\n\t)\n)')
        mk = edge_marker_of(k10)
        self.assertEqual(mk.texts, ((0.0, 3.1, 0.0, 'Dwgs.User',
                                     'PCB Edge'),))
        self.assertEqual(mk.lines, ((5.0, 3.675, -5.0, 3.675,
                                     'Dwgs.User'),))
        k5 = ('(module x (layer F.Cu)\n'
              '  (fp_text user "PCB edge" (at 2.1 0 90) (layer F.Fab))\n'
              '  (fp_line (start 2.4 3.7) (end 2.4 -3.7) (layer F.Fab) '
              '(width 0.1))\n'
              '  (fp_rect (start -1 -1) (end 1 1) (layer Cmts.User)))')
        mk = edge_marker_of(k5)
        self.assertEqual(mk.texts, ((2.1, 0.0, 90.0, 'F.Fab', 'PCB edge'),))
        self.assertEqual(len(mk.lines), 5)
        self.assertEqual(mk.lines[0], (2.4, 3.7, 2.4, -3.7, 'F.Fab'))

    def test_a_footprint_without_the_marker_reads_none(self):
        self.assertIsNone(edge_marker_of(
            '(footprint "x" (fp_text user "${REFERENCE}" (at 0 0 0) '
            '(layer "F.Fab")) (fp_line (start 0 0) (end 1 0) '
            '(layer "Dwgs.User")))'))


if __name__ == '__main__':
    unittest.main()
