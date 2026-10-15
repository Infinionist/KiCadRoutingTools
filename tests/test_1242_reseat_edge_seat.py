"""#1242 (--reseat half): `place_seed --reseat` seats the scope's edge claims
through the edge seat, mating face and all.

`reseat_scope` drops the scope's own `edge_connectors` declarations before
seeding (stage 1 seats a claim with no legality gate; with the bands of a
damaged board's intent in, it threw parts metres off the board), so the seed
placed a re-seated connector as an ordinary part: no band, no face. On sonde_u
with J2 (a DSUB-9 edge mount, strong face) turned to face into the board,
`--reseat J2` found "no legal pose anywhere on the board", left J2 reversed
and exited 4, where `--repair` turned it 90 -> 270 onto the east band. Each
scope claim is now seated after the seed the way `--repair` seats it --
`seeder._seat_edge`: the band, the declared or facing angles, pads on the
board -- its declaration kept in the graded intent, its `edge_connector`
errors part of the explicit scope's `intent` basis.

  1. the issue's recipe: J2 turned 90 -> 270 onto the east band, exactly
     `--repair`'s pose, exit 0, the declaration graded; blind twin (the edge
     seat patched out): J2 unseated, its band dropped, exit 4.
  2. a reversed J2 inside the board: accepted on the `intent` basis, which
     counts the edge claim's errors (2 -> 0).
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
from placement import floorplan, seeder                      # noqa: E402
from run_utils import check, evidence                        # noqa: E402

RUN_ALL_TIMEOUT = 900
PY = sys.executable
SONDE = os.path.join(ROOT, 'kicad_files', 'sonde_u.kicad_pcb')
CLAIM = {'ref': 'J2', 'edge': 'east', 'class': 'edge_receptacle',
         'overhang_mm': {'min': 0.0, 'max': 15.15}}
#: Where `--repair` seats the reversed J2 (#1242's recipe), and so must this.
REPAIRED = (174.535, 90.119, 270.0)


def _reversed(td, x):
    """sonde_u with J2 at (x, 90.1192), turned to 90 -- facing west, into
    the board -- and the intent declaring it on the east edge."""
    rev = os.path.join(td, 'rev.kicad_pcb')
    check([PY, '-X', 'utf8', os.path.join(ROOT, 'py_placer', 'place_pose.py'),
           SONDE, rev, '--allow-routed', '--force', 'set', 'J2', str(x),
           '90.1192', '--rot', '90'], accept=True)
    ip = os.path.join(td, 'i.json')
    with open(ip, 'w', encoding='utf-8') as fh:
        json.dump({'schema': floorplan.SCHEMA_VERSION, 'kind': floorplan.KIND,
                   'units': 'mm', 'edge_connectors': [CLAIM]}, fh)
    return evidence(rev, 'reversed board'), ip


def _summary(stdout):
    return [json.loads(ln.split(':', 1)[1]) for ln in stdout.splitlines()
            if ln.startswith('JSON_SUMMARY:')][-1]


def _j2(path):
    fp = parse_kicad_pcb(path).footprints['J2']
    return (fp.x, fp.y, fp.rotation or 0.0)


class TheReseatTurnsIt(unittest.TestCase):

    def test_1_the_issue_recipe(self):
        with tempfile.TemporaryDirectory() as td:
            rev, ip = _reversed(td, 181.61)
            out = os.path.join(td, 'reseat.kicad_pcb')
            r = check([PY, '-X', 'utf8',
                       os.path.join(ROOT, 'py_placer', 'place_seed.py'),
                       rev, out, '--intent', ip, '--reseat', 'J2'],
                      accept=True)
            s = _summary(r.stdout)
            pose = _j2(evidence(out, 'reseated board'))
            # The final grade read the declaration: facing east passes.
            g = check([PY, '-X', 'utf8',
                       os.path.join(ROOT, 'py_tools', 'check_floorplan.py'),
                       out, '--intent', ip], accept=True)
        self.assertEqual(pose, REPAIRED)
        self.assertIn('J2: turned 90 -> 270deg on the declared east edge',
                      r.stdout)
        self.assertEqual(s['reseated_refs'], ['J2'])
        self.assertEqual((s['unseated'], s['edge_bands_dropped']), ([], {}))
        self.assertEqual(s['grade_errors'], 0)
        self.assertIn('J2', s['connector_requirements']['declared_refs'])
        # sonde_u J2 draws a "PCB Edge" marker that agrees with its body
        # face, so the basis carries it (#1245).
        self.assertIn('facing east [courtyard+fab+marker, strong] pass',
                      g.stdout)

    def test_1_blind_twin_leaves_it_reversed(self):
        # The edge seat patched out is the old path: the seed alone, which
        # finds J2 no pose, and the declaration dropped.
        with tempfile.TemporaryDirectory() as td:
            rev, ip = _reversed(td, 181.61)
            out = os.path.join(td, 'blind.kicad_pcb')
            buf, err = io.StringIO(), io.StringIO()
            old = sys.argv
            sys.argv = ['place_seed.py', rev, out, '--intent', ip,
                        '--reseat', 'J2']
            try:
                with patch.object(seeder, '_seat_edge',
                                  lambda *a, **k: False), \
                        contextlib.redirect_stdout(buf), \
                        contextlib.redirect_stderr(err):
                    rc = place_seed.main()
            finally:
                sys.argv = old
            s = _summary(buf.getvalue())
            pose = _j2(evidence(out, 'blind board'))
        self.assertEqual(rc, 4)
        self.assertEqual(s['unseated'], ['J2'])
        self.assertIn('J2', s['edge_bands_dropped'])
        self.assertEqual(pose[2], 90.0)

    def test_2_a_reversed_part_inside_is_accepted_on_its_claim(self):
        with tempfile.TemporaryDirectory() as td:
            rev, ip = _reversed(td, 178)
            out = os.path.join(td, 'reseat.kicad_pcb')
            r = check([PY, '-X', 'utf8',
                       os.path.join(ROOT, 'py_placer', 'place_seed.py'),
                       rev, out, '--intent', ip, '--reseat', 'J2'],
                      accept=True)
            s = _summary(r.stdout)
            pose = _j2(evidence(out, 'reseated board'))
        ab = s['accept_basis']
        self.assertEqual(pose, REPAIRED)
        self.assertEqual(ab['fired'], 'intent')
        self.assertIn('edge_connector', ab['intent_rules'])
        (t,) = [t for t in ab['terms'] if t['term'] == 'intent']
        self.assertEqual((t['before'], t['after']), (2, 0))


if __name__ == '__main__':
    unittest.main()
