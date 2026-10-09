"""#1201: a pin-1 dot on F.Fab is not a part's body.

`body.body_geometry` accepted the .Fab rung unconditionally while silk had a
tick-mark guard, so an EasyEDA USB-C receptacle whose only F.Fab item is a
0.06 mm pin-1 circle had a 0.06 x 0.06 mm "body" at its inboard corner, and
`rule_edge_connector` graded its seat on that point: CM5_MINIMA_3's J101 and
USB701, declared on the west edge, "seated 8.90mm / 11.71mm from the nearest
edge" while their pads were 3.8 mm from it.

`body.fab_cannot_be_body` rejects a fab bbox under `FAB_MIN_PAD_AREA_FRAC`
(0.05) of the pad bbox's area. The rung falls through to silk, then courtyard,
then pads, and the part records `fab_rejected`. The quench's own .Fab reader
(`QuenchState.fab_rect` / `fab_shape`) honours the same rejection, so the
search and the grader read one body.

Silk containment stays advisory (#896), measured: with the guard on, 13
containment pairs on the tracked corpus and the fa10 designer boards are
exempt only by the silk rule -- tracked esp_prog's R1/U2 among them -- so
letting silk gate would fail designer boards. What changes is that
check_assembly now SAYS so per pair (`gate_exempt`), instead of calling every
non-gating containment "a marker or a board-sized container".
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

from kicad_parser import parse_kicad_pcb                      # noqa: E402
from placement import body, floorplan, legality              # noqa: E402
from run_utils import corpus_boards                          # noqa: E402
from test_1184_container_classification import _board        # noqa: E402

PY = sys.executable
CM5 = os.path.expanduser('~/Documents/prive/git/krt-fa10/wk/fa10/_truth/'
                         'CM5_MINIMA_3/human_stripped.kicad_pcb')
CM5_INTENT = os.path.expanduser('~/Documents/prive/git/krt-fa10/wk/fa10/'
                                '_issues/batch4/work/S8/intent_j101.json')


def _usb_c(x, y, rot):
    """J101's shape: 22 SMD pads over 7.3 x 4.1 mm, no courtyard, a single
    F.Fab item -- a 0.06 mm pin-1 circle at one corner -- and a silk shell
    that runs 2.2 mm past the pads on local +x (the opening)."""
    pads = ''.join(
        f'    (pad "{i}" smd rect (at {round(-3.5 + 0.7 * (i % 11), 2)} '
        f'{-1.55 if i < 11 else 1.55}) (size 0.3 1.0) (layers "F.Cu")'
        f' (net 1 "N1"))\n' for i in range(22))
    return (f'  (footprint "t:USB-C-SMD_TYPEC" (layer "F.Cu") '
            f'(at {x} {y} {rot})\n'
            '    (property "Reference" "J101" (at 0 0) (layer "F.SilkS"))\n'
            '    (fp_rect (start -3.8 -2.6) (end 5.9 2.6) (stroke (width'
            ' 0.12) (type default)) (layer "F.SilkS"))\n'
            '    (fp_circle (center -3.95 -2.23) (end -3.92 -2.23) (stroke'
            ' (width 0.05) (type default)) (layer "F.Fab"))\n'
            + pads + '  )\n')


def _holder_silk():
    """One-Air-Max BAT1's REAL shape (the comment on #1201): clips 40 mm
    apart, a silk outline running PAST the clips in y, and a pin-1 dot on
    F.Fab. (test_1184's `_holder` draws its silk inside the pads, so there
    the silk is a tick and the body falls to the pad box.)"""
    clips = ''.join(
        f'    (pad "{i}" smd rect (at {x} {y}) (size 3 4) (layers "F.Cu")'
        f' (net 1 "N1"))\n'
        for i, (x, y) in enumerate([(-20, -8), (-20, 0), (-20, 8),
                                    (20, -8), (20, 0), (20, 8)], 1))
    return ('  (footprint "t:HOLDER" (layer "F.Cu") (at 22 15)\n'
            '    (property "Reference" "BAT1" (at 0 0) (layer "F.SilkS"))\n'
            '    (fp_rect (start -21 -13) (end 21 13) (stroke (width 0.12)'
            ' (type default)) (layer "F.SilkS"))\n'
            '    (fp_circle (center -18 -9) (end -17.97 -9) (stroke (width'
            ' 0.05) (type default)) (layer "F.Fab"))\n'
            + clips + '  )\n')


def _dc1(x, y):
    return (f'  (footprint "t:P" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "DC1" (at 0 0) (layer "F.SilkS"))\n'
            f'    (fp_rect (start -2 -2) (end 2 2) (stroke (width 0.05)'
            f' (type default)) (layer "F.CrtYd"))\n'
            f'    (fp_rect (start -1 -1) (end 1 1) (stroke (width 0.05)'
            f' (type default)) (layer "F.Fab"))\n'
            f'    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu")'
            f' (net 2 "N2")))\n')


class TheGuard(unittest.TestCase):

    def test_a_pin_one_dot_is_rejected_and_silk_answers(self):
        with tempfile.TemporaryDirectory() as td:
            path = _board(td, _usb_c(6, 15, 180), size=(40, 30))
            pcb = parse_kicad_pcb(path)
            g = body.board_bodies(pcb, path)['J101']
            lb = legality.part_local_bounds(pcb, path)['J101']
        self.assertTrue(g.fab_rejected)
        # ...and the provenance reaches every LocalBounds consumer.
        self.assertTrue(lb.fab_rejected)
        self.assertEqual(lb.source, body.SOURCE_SILK)
        self.assertEqual(g.drawn_source, body.SOURCE_SILK)
        self.assertEqual(g.source, body.SOURCE_SILK)
        self.assertIsNone(g.drawn_shape_local)
        # silk united with the pads, never the dot
        self.assertLess(g.drawn_local[0], -3.7)
        self.assertGreater(g.drawn_local[2], 5.8)

    def test_the_threshold_brackets(self):
        pads = (-2.0, -1.0, 2.0, 1.0)                        # 8 mm2
        f = body.fab_cannot_be_body
        self.assertTrue(f((0, 0, 0.06, 0.06), pads))
        self.assertTrue(f((0, 0, 0.5, 0.75), pads))          # 0.047
        self.assertFalse(f((0, 0, 0.5, 0.85), pads))         # 0.053
        self.assertFalse(f(None, pads))
        self.assertFalse(f((0, 0, 0.06, 0.06), None))

    def test_a_padless_fab_body_is_kept(self):
        """watchy's e-paper display: no pads, a real F.Fab body."""
        disp = ('  (footprint "t:DISPLAY" (layer "F.Cu") (at 20 15)\n'
                '    (property "Reference" "REF**" (at 0 0)'
                ' (layer "F.SilkS"))\n'
                '    (fp_rect (start -10 -8) (end 10 8) (stroke (width'
                ' 0.1) (type default)) (layer "F.Fab")))\n')
        with tempfile.TemporaryDirectory() as td:
            path = _board(td, disp, size=(40, 30))
            g = body.board_bodies(parse_kicad_pcb(path), path)['REF**']
        self.assertFalse(g.fab_rejected)
        self.assertEqual(g.drawn_source, body.SOURCE_FAB)

    def test_the_edge_seat_is_graded_on_the_body_not_the_dot(self):
        """The issue's shape: rot 180 puts the dot at the INBOARD corner
        (x 9.95) and the silk shell at the west edge (x 0.1)."""
        with tempfile.TemporaryDirectory() as td:
            path = _board(td, _usb_c(6, 15, 180), size=(40, 30))
            doc = {'schema': floorplan.SCHEMA_VERSION, 'kind': floorplan.KIND,
                   'units': 'mm',
                   'envelope': {'rect': [0, 0, 40, 30]},
                   'edge_connectors': [{'ref': 'J101', 'edge': 'west',
                                        'class': 'edge_receptacle'}]}
            r = floorplan.grade(
                floorplan.intent_from_dict(doc, source_path='i.json'),
                parse_kicad_pcb(path), path)
        errs = [v.message for v in r.errors if v.rule == 'edge_connector']
        self.assertEqual(errs, [])
        ev = [e for e in r.edge_connector_evidence if e.get('ref') == 'J101']
        self.assertTrue(ev)


class TheCorpus(unittest.TestCase):

    def test_no_tracked_part_is_rejected(self):
        boards = corpus_boards()
        if not boards:
            self.skipTest('git cannot list the tracked boards')
        import measure_1201_fab_and_silk as m
        low, rejected = None, []
        for path in boards:
            pcb = parse_kicad_pcb(path)
            rejected += [(os.path.basename(path), r) for r, g in
                         body.board_bodies(pcb, path).items()
                         if g.fab_rejected]
            for ref, ratio, _f, _p in m.ratio_rows(pcb, path):
                if low is None or ratio < low[0]:
                    low = (ratio, os.path.basename(path), ref)
        self.assertEqual(rejected, [])
        # The change detector the threshold rests on: the least-covering
        # tracked fab body, 0.3114 of its pads (orangecrab J4).
        self.assertEqual((round(low[0], 4), low[1], low[2]),
                         (0.3114, 'orangecrab_ext_pll.kicad_pcb', 'J4'))
        self.assertGreater(low[0], 6 * body.FAB_MIN_PAD_AREA_FRAC)


class TheQuenchReadsTheSameBody(unittest.TestCase):

    def _state(self, td, text):
        import pose_score
        path = _board(td, text, size=(40, 30))
        return pose_score.make_state(parse_kicad_pcb(path), path,
                                     clearance=0.2)

    def test_fab_rect_and_shape_refuse_the_dot(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._state(td, _usb_c(6, 15, 180) + _dc1(30, 15))
            self.assertIsNone(st.fab_rect('J101'))
            self.assertIsNone(st.fab_shape('J101'))
            # A real fab body is still read, exactly as before.
            self.assertIsNotNone(st.fab_rect('DC1'))
            self.assertEqual(st.fab_rect('DC1'), (29.0, 14.0, 31.0, 16.0))


class SilkContainmentIsNamed(unittest.TestCase):

    def test_bat1_shape(self):
        import pose_score
        with tempfile.TemporaryDirectory() as td:
            path = _board(td, _holder_silk() + _dc1(22, 15), size=(44, 30))
            pcb = parse_kicad_pcb(path)
            g = legality.grade_body_overlap(pcb, 0.2, pcb_file=path)
            st = pose_score.make_state(pcb, path, clearance=0.2)
            veto = st.candidate_veto('DC1', 22.0, 15.0, 0.0)
            p = subprocess.run(
                [PY, '-X', 'utf8', 'py_tools/check_assembly.py', path,
                 '--json', os.path.join(td, 'a.json')],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', cwd=ROOT, timeout=600)
            with open(os.path.join(td, 'a.json'), encoding='utf-8') as fh:
                js = json.load(fh)
        self.assertEqual(g['fab_rejected_refs'], ['BAT1'])
        self.assertEqual(g['body_sources'].get('BAT1'), body.SOURCE_SILK)
        pair = [q for q in g['containment_pairs'] if {q.a, q.b} ==
                {'BAT1', 'DC1'}]
        self.assertEqual(len(pair), 1)
        self.assertEqual(pair[0].contained_frac, 1.0)
        self.assertEqual(pair[0].gate_exempt, 'silk_drawn')
        self.assertEqual(g['containment_blocking'], 0)
        self.assertFalse([q for q in g['courtyard_blocking_pairs']
                          if {q.a, q.b} == {'BAT1', 'DC1'}])
        # The search still refuses the pose: the generator stays stricter.
        self.assertEqual((veto or (None,))[0], 'courtyard', veto)
        out = p.stdout + p.stderr
        self.assertIn('does not gate: a silk-drawn body', out)
        self.assertNotIn('a marker or a board-sized container', out)
        self.assertIn('BAT1', out[out.find('.Fab marking'):])
        self.assertEqual(js['fab_rejected_refs'], ['BAT1'])
        self.assertEqual([c['gate_exempt'] for c in js['containments']],
                         ['silk_drawn'])


class TheIssueBoard(unittest.TestCase):

    def test_cm5_receptacles_are_graded_on_their_bodies(self):
        if not (os.path.isfile(CM5) and os.path.isfile(CM5_INTENT)):
            self.skipTest('the fa10 CM5_MINIMA_3 truth board is not on this '
                          'machine (it is not tracked)')
        p = subprocess.run(
            [PY, '-X', 'utf8', 'py_tools/check_floorplan.py', CM5,
             '--intent', CM5_INTENT, '--no-brief', '--no-mechanical'],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', cwd=ROOT, timeout=900)
        out = p.stdout + p.stderr
        self.assertNotIn('body:fab', out)
        self.assertNotIn('seated 8.90mm', out)
        self.assertNotIn('seated 11.71mm', out)


if __name__ == '__main__':
    unittest.main()
