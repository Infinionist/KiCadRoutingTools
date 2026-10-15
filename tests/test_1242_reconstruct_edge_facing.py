"""#1242 (place_reconstruct half): an edge-less receptacle the assignment
seats on an edge faces out of that edge.

`build_candidates` offered translations at the part's own angle, and
`edge_metric` reads the courtyard against ANY edge, so a receptacle whose
input angle faces away from the edge a damage vector pulls it to took a
REVERSED seat at metric 0. The issue's recipe: sonde_u with J2 (DSUB-9 edge
mount, strong face) at (171.45, 90.12) rot 90, R1/R10/R9 stacked onto
R6/R8/R7 at (+10.16, 0) to mint the vector, an intent declaring J2 an
`edge_receptacle` with NO edge (what an emitter gives an implausible pose),
`--lock 'C*' 'D*' J1 P1 'R*' 'U*'`. J2 went to (181.606, 90.119) still at
rot 90: on the east edge, its shell over the board, its face pointing west.

`reconstruct.facing_seats` now drops a translated seat whose strong face
(`seeder._strong_facing_rots` -> `floorplan.edge_facing`) points away from the
edge it sits on, and offers the facing quarter turn at that position as an
`(x, y, rot)` candidate, which both solvers price at its own angle.

  1. the recipe: J2 at (181.6058, 90.1192) turned to 270 -- its home pose --
     facing east, the edge it sits on; the floorplan grade with east
     declared passes (it ERRORed the old seat's facing). Blind twin
     (`facing_seats` patched to identity): the old reversed seat at 90.
  2. the mechanism, on the candidate list itself: the input pose kept as
     index 0, the reversed seat dropped, the turned seat added.
  3. guard: a WEAK face, and an ABSENT one, leave J2 exactly where the old
     code put it -- a face that is not strong says nothing about the turn.
  4. guard: the INPUT pose is never read -- J2 already sitting reversed on
     the east edge stays put, though a turned seat is on offer elsewhere.
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

import place_reconstruct                                     # noqa: E402
from kicad_parser import parse_kicad_pcb                     # noqa: E402
from placement import floorplan, mating, reconstruct         # noqa: E402
from placement.body import board_bodies                      # noqa: E402
from run_utils import check, evidence                        # noqa: E402

RUN_ALL_TIMEOUT = 900
PY = sys.executable
SONDE = os.path.join(ROOT, 'kicad_files', 'sonde_u.kicad_pcb')
ENTRY = {'ref': 'J2', 'class': 'edge_receptacle',
         'overhang_mm': {'min': 0.0, 'max': 15.15}}
LOCKS = ['C*', 'D*', 'J1', 'P1', 'R*', 'U*']
#: The seat the (10.16, 0)-ish vector pulls J2 to: its home, within the
#: derived vector's coarseness (the cluster mean is 10.1558).
SEAT = (181.6058, 90.1192)
#: The three locked stacks the recipe makes are what remains: exit 4.
RESIDUE = 'Final (exact): 3 pad conflict pair(s)'


def _damaged(td, j2_x):
    """sonde_u with J2 at (j2_x, 90.1192) rot 90 (facing west) and R1/R10/R9
    stacked onto R6/R8/R7 at x 132.08 -- the conflicts the (10.16, 0) vector
    is derived from -- plus the edge-less intent."""
    src = parse_kicad_pcb(SONDE).footprints
    ops = ['set', 'J2', str(j2_x), '90.1192', '--rot', '90']
    for r, on in (('R1', 'R6'), ('R10', 'R8'), ('R9', 'R7')):
        ops += ['set', r, '132.08', f'{src[on].y:.4f}', '--rot', '180']
    dmg = os.path.join(td, 'dmg.kicad_pcb')
    check([PY, '-X', 'utf8', os.path.join(ROOT, 'py_placer', 'place_pose.py'),
           SONDE, dmg, '--allow-routed', '--force'] + ops, accept=True)
    ip = os.path.join(td, 'i.json')
    with open(ip, 'w', encoding='utf-8') as fh:
        json.dump({'schema': floorplan.SCHEMA_VERSION, 'kind': floorplan.KIND,
                   'units': 'mm', 'edge_connectors': [ENTRY]}, fh)
    return evidence(dmg, 'damaged board'), ip


def _summary(stdout):
    return [json.loads(ln.split(':', 1)[1]) for ln in stdout.splitlines()
            if ln.startswith('JSON_SUMMARY:')][-1]


def _j2(path):
    """J2's pose, and which edge its mating face points at there, read by
    the grade's own predicate."""
    pcb = parse_kicad_pcb(path)
    fp = pcb.footprints['J2']
    f = floorplan.edge_facing(ENTRY, fp, board_bodies(pcb, path).get('J2'))
    return (round(fp.x, 4), round(fp.y, 4), (fp.rotation or 0.0) % 360), f


def _in_process(dmg, ip, out, *patches):
    """place_reconstruct.main() with `patches` applied; (rc, stdout)."""
    buf, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = ['place_reconstruct.py', dmg, out, '--intent', ip,
                '--lock'] + LOCKS
    try:
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(buf))
            stack.enter_context(contextlib.redirect_stderr(err))
            rc = place_reconstruct.main()
    finally:
        sys.argv = old
    text = buf.getvalue()
    if 'Traceback' in text + err.getvalue():
        raise AssertionError('BROKEN TEST: place_reconstruct raised\n'
                             + (text + err.getvalue())[-1500:])
    return rc, text


class TheSeatFacesOut(unittest.TestCase):

    def test_1_the_issue_recipe(self):
        with tempfile.TemporaryDirectory() as td:
            dmg, ip = _damaged(td, 171.45)
            out = os.path.join(td, 'out.kicad_pcb')
            r = check([PY, '-X', 'utf8',
                       os.path.join(ROOT, 'py_placer', 'place_reconstruct.py'),
                       dmg, out, '--intent', ip, '--lock'] + LOCKS,
                      refuse=RESIDUE, code=4)
            s = _summary(r.stdout)
            pose, face = _j2(evidence(out, 'reconstructed board'))
            # The grade, with the edge J2 now sits on declared.
            ie = os.path.join(td, 'east.json')
            with open(ie, 'w', encoding='utf-8') as fh:
                json.dump({'schema': floorplan.SCHEMA_VERSION,
                           'kind': floorplan.KIND, 'units': 'mm',
                           'edge_connectors': [dict(ENTRY, edge='east')]}, fh)
            g = check([PY, '-X', 'utf8',
                       os.path.join(ROOT, 'py_tools', 'check_floorplan.py'),
                       out, '--intent', ie], accept=True)
        self.assertEqual(pose, SEAT + (270.0,))
        self.assertEqual((face['facing'], face['strong']), ('east', True))
        self.assertEqual(s['edge_seated'], {'J2': list(SEAT)})
        self.assertEqual(s['edge_turned'],
                         {'J2': {'from': 90.0, 'to': 270.0, 'edge': 'east'}})
        self.assertIn('J2: turned 90 -> 270deg, seated on the east edge',
                      r.stdout)
        self.assertIn('facing east [courtyard+fab+marker, strong] pass',
                      g.stdout)
        self.assertNotIn("J2's body leaves its pad row", g.stdout)

    def test_1_blind_twin_seats_it_reversed(self):
        # `facing_seats` patched to identity is the old candidate list.
        with tempfile.TemporaryDirectory() as td:
            dmg, ip = _damaged(td, 171.45)
            out = os.path.join(td, 'blind.kicad_pcb')
            rc, text = _in_process(dmg, ip, out, patch.object(
                reconstruct, 'facing_seats',
                lambda st, ref, e, cands, band, ob: list(cands)))
            pose, face = _j2(evidence(out, 'blind board'))
        self.assertEqual(rc, 4)
        self.assertIn(RESIDUE, text)
        self.assertEqual(pose, SEAT + (90.0,))
        self.assertEqual((face['facing'], face['strong']), ('west', True))
        self.assertNotIn('edge_turned', _summary(text))

    def test_2_the_candidate_list(self):
        from pose_score import make_state
        with tempfile.TemporaryDirectory() as td:
            dmg, _ip = _damaged(td, 171.45)
            st = make_state(parse_kicad_pcb(dmg), dmg, clearance=0.25,
                            board_edge_clearance=0.55)
            v = (10.1558, 0.0)
            tiers = reconstruct.classify(st)
            plain, _ = reconstruct.build_candidates(
                st, tiers, [v], {}, edge_bands={'J2': 15.15})
            faced, _ = reconstruct.build_candidates(
                st, tiers, [v], {}, edge_bands={'J2': 15.15},
                facing={'J2': ENTRY})
            # Both solvers, J2 the only part with a choice: each takes the
            # turned seat, priced and pair-checked at its own angle.
            only = {r: (c if r == 'J2' else c[:1]) for r, c in faced.items()}
            ilp = reconstruct.solve_assignment(st, only, tiers,
                                               edge_pref={'J2': 15.15})
            descent = reconstruct._solve_breakout(st, only, tiers, None,
                                                  edge_pref={'J2': 15.15})
        stay, plus, minus = (171.45, 90.1192), SEAT, (161.2942, 90.1192)
        self.assertEqual(plain['J2'], [stay, plus, minus])
        # The reversed seat at +v is gone, its facing turn offered in its
        # place; the input pose and the interior -v are untouched.
        self.assertEqual(faced['J2'], [stay, plus + (270.0,), minus])
        # Every other part's list is the plain one.
        self.assertEqual({r: c for r, c in faced.items() if r != 'J2'},
                         {r: c for r, c in plain.items() if r != 'J2'})
        self.assertEqual(only['J2'][ilp['J2']], plus + (270.0,))
        self.assertEqual(only['J2'][descent['J2']], plus + (270.0,))

    def test_2b_an_infeasible_ilp_keeps_the_edge_objective(self):
        # The ILP's infeasible fallback (the breakout descent) dropped
        # `edge_pref`, so an edge part there was priced by the net-anchor
        # proxy instead. Make the proxy prefer J2's interior candidate, force
        # the ILP infeasible, and the edge metric must still pick the seat.
        import types
        import scipy.optimize
        from pose_score import make_state
        with tempfile.TemporaryDirectory() as td:
            dmg, _ip = _damaged(td, 171.45)
            st = make_state(parse_kicad_pcb(dmg), dmg, clearance=0.25,
                            board_edge_clearance=0.55)
            tiers = reconstruct.classify(st)
            faced, _ = reconstruct.build_candidates(
                st, tiers, [(10.1558, 0.0)], {}, edge_bands={'J2': 15.15},
                facing={'J2': ENTRY})
            only = {r: (c if r == 'J2' else c[:1]) for r, c in faced.items()}
            minus = (161.2942, 90.1192)
            self.assertIn(minus, only['J2'])

            def proxy(state, ref, x, y, fixed):
                return 0.0 if (ref, round(x, 4)) == ('J2', minus[0]) else 50.0

            def infeasible(*a, **k):
                return types.SimpleNamespace(status=2, x=None,
                                             message='forced (test)')
            notes = []
            # No pair conflicts, so the descent's choice is the objective's
            # alone (its cost compares conflicts first).
            with patch.object(reconstruct, '_net_anchor_cost', proxy), \
                    patch.object(reconstruct, '_pair_conflicts',
                                 lambda *a, **k: False):
                blind = reconstruct._solve_breakout(st, only, tiers, None)
                with patch.object(scipy.optimize, 'milp', infeasible):
                    got = reconstruct.solve_assignment(
                        st, only, tiers, notes=notes,
                        edge_pref={'J2': 15.15})
        # The control: without the edge objective the proxy wins.
        self.assertEqual(only['J2'][blind['J2']], minus)
        self.assertTrue(any('breakout-descent fallback' in n for n in notes),
                        f"BROKEN TEST: the ILP did not fall back: {notes}")
        self.assertEqual(only['J2'][got['J2']], SEAT + (270.0,))

    def test_3_a_weak_or_absent_face_is_left_as_before(self):
        real = mating.mating_face_local

        def weak(fp, geom):
            return real(fp, geom)._replace(strong=False)

        def absent(fp, geom):
            return mating.LocalFace(None, 0.0, '', 'no face (test)')

        for name, fake in (('weak', weak), ('absent', absent)):
            with self.subTest(face=name), \
                    tempfile.TemporaryDirectory() as td:
                dmg, ip = _damaged(td, 171.45)
                out = os.path.join(td, f'{name}.kicad_pcb')
                rc, text = _in_process(dmg, ip, out, patch.object(
                    mating, 'mating_face_local', fake))
                pose = _j2(evidence(out, f'{name}-face board'))[0]
                self.assertEqual(rc, 4)
                self.assertIn(RESIDUE, text)
                self.assertEqual(pose, SEAT + (90.0,))
                self.assertNotIn('edge_turned', _summary(text))

    def test_4_the_input_pose_is_never_read(self):
        # J2 already on the east edge, reversed: its input pose is a seat at
        # metric 0. The turned seat at -v (171.45, rot 270) is on offer, but
        # turning the part where it stands -- or moving it to turn it --
        # would read the input pose as a claim. It stays.
        with tempfile.TemporaryDirectory() as td:
            dmg, ip = _damaged(td, 181.61)
            out = os.path.join(td, 'stay.kicad_pcb')
            r = check([PY, '-X', 'utf8',
                       os.path.join(ROOT, 'py_placer', 'place_reconstruct.py'),
                       dmg, out, '--intent', ip, '--lock'] + LOCKS,
                      refuse=RESIDUE, code=4)
            pose = _j2(evidence(out, 'reconstructed board'))[0]
        self.assertEqual(pose, (181.61, 90.1192, 90.0))
        self.assertNotIn('edge_turned', _summary(r.stdout))


if __name__ == '__main__':
    unittest.main()
