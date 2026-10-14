"""#1244: stage 1's width-aware packed starts read what the rungs read.

`seeder._packed_starts` (#1172, opt-in) spreads an edge's claims by their
turned widths when stage 1's equal fractions would overlap them. It used to
read only the claims on that edge, so:

  * a part already on the band -- file-locked, a fixed pose, pad-less
    artwork, a claim of an edge seated earlier -- was not there to it: on a
    60 mm north edge with a locked part at x 27..33, four 13 mm claims were
    packed at equal 1.6 mm gaps and two of them started on the locked part;
  * a claim's own declared zone was not there either: on glasgow_revC's
    south edge J3 (34 mm, a member of a zone at x 85.97..128.47) was packed
    in seat order to x 74.8..108.8, outside it, and `place_seed` on the
    issue's pile exited 4 on `zone_containment` with packing on.

Now the placed parts that share a side with a claim and reach its depth band
are packed around, each claim stays in its zones' span along the edge, and
the equal starts are replaced when they would put two claims on each other,
a claim on a placed part, or a claim outside its zone. With nothing placed
and nothing zoned the packing is #1172's equal gaps, unchanged. The switch
stays an opt-in, off by default.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

from kicad_parser import parse_kicad_pcb                      # noqa: E402
from placement import floorplan, seeder                      # noqa: E402
import pose_score                                            # noqa: E402

KF = os.path.join(ROOT, 'kicad_files')
GLASGOW = os.path.join(KF, 'glasgow_revC.kicad_pcb')
COLDFIRE = os.path.join(KF, 'kit-dev-coldfire-xilinx_5213.kicad_pcb')
SOURCES = ('kicad', 'sheet')
W, H = 60.0, 30.0


def _part(ref, x, y, cy, *, locked=False, layer='F', pads=True):
    """A footprint at (x, y, 0): courtyard `cy` (local x0, y0, x1, y1) on
    `layer`, one 1 x 1 SMD pad at its origin unless `pads` is False."""
    lk = '    (locked yes)\n' if locked else ''
    pad = (f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "{layer}.Cu")'
           f' (net 1 "N1"))\n' if pads else '')
    crt = (f'    (fp_rect (start {cy[0]} {cy[1]}) (end {cy[2]} {cy[3]})'
           f' (stroke (width 0.05) (type default)) (layer "{layer}.CrtYd"))\n'
           if cy is not None else '')
    return (f'  (footprint "t:{ref}" (layer "{layer}.Cu") (at {x} {y})\n' + lk
            + f'    (property "Reference" "{ref}" (at 0 0) '
              f'(layer "{layer}.SilkS"))\n' + crt + pad + '  )\n')


def _board(td, extra, name='b'):
    """A W x H board, four 13 x 4 claims A-D parked off it, and `extra`."""
    parts = [_part(r, 100.0 + 20.0 * i, 100.0, (-6.5, -2.0, 6.5, 2.0))
             for i, r in enumerate('ABCD')]
    path = os.path.join(td, name + '.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('(kicad_pcb (version 20240108) (generator pcbnew)\n'
                 '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal)'
                 ' (44 "Edge.Cuts" user))\n'
                 '  (net 0 "") (net 1 "N1")\n'
                 f'  (gr_rect (start 0 0) (end {W} {H}) (stroke (width 0.1)'
                 ' (type default)) (layer "Edge.Cuts"))\n'
                 + ''.join(parts + list(extra)) + ')\n')
    return path


SPECS = [{'ref': r, 'edge': 'north', 'overhang_mm': {'min': 0.0, 'max': 0.5}}
         for r in 'ABCD']


def _spans(state, out, edge='north'):
    """`{ref: (a, b)}`: each claim's courtyard along the edge at its packed
    start (the start is the courtyard centre's fraction of the edge)."""
    e_lo, e_hi, _ = seeder._declared_edge_span(state, state.board, edge)
    res = {}
    for ref, f in out.items():
        p = state.parts[ref]
        g = p.grade_rect(0.0, 0.0, p.rot)
        w = (g[2] - g[0]) if edge in ('north', 'south') else (g[3] - g[1])
        c = e_lo + f * (e_hi - e_lo)
        res[ref] = (c - w / 2.0, c + w / 2.0)
    return res


def _overlaps(a, b):
    return a[0] < b[1] - 1e-6 and b[0] < a[1] - 1e-6


class PacksAroundPlacedParts(unittest.TestCase):
    """The band half: a locked part on the north band at x 27..33."""

    def _state(self, td, extra):
        b = _board(td, extra)
        return pose_score.make_state(parse_kicad_pcb(b), b)

    def test_the_old_blind_packing_starts_two_claims_on_the_placed_part(self):
        """The falsifier: read without the placed part (what #1172 did),
        the four 13 mm claims are packed at equal 1.6 mm gaps and B and C
        start on L; read with it, two go each side of L and none touches
        it, at the largest common gap that fits (1/3 mm)."""
        with tempfile.TemporaryDirectory() as td:
            st = self._state(td, [_part('L', 30.0, 3.0, (-3, -2, 3, 2),
                                        locked=True)])
            blind = _spans(st, seeder._packed_starts(
                st, SPECS, 'north', st.board, lambda r: None))
            why = []
            seen = _spans(st, seeder._packed_starts(
                st, SPECS, 'north', st.board, lambda r: None,
                placed={'L'}, why=why))
        lband = (27.0, 33.0)
        self.assertEqual(sorted(r for r, s in blind.items()
                                if _overlaps(s, lband)), ['B', 'C'])
        self.assertEqual([r for r, s in seen.items() if _overlaps(s, lband)],
                         [])
        refs = sorted(seen, key=lambda r: seen[r][0])
        self.assertEqual(refs, ['A', 'B', 'C', 'D'])
        for a, b in zip(refs, refs[1:]):
            self.assertFalse(_overlaps(seen[a], seen[b]), (a, b, seen))
        self.assertAlmostEqual(seen['A'][0], 1.0 / 3.0, places=4)
        self.assertAlmostEqual(seen['C'][0], 33.0 + 1.0 / 3.0, places=4)
        # The equal starts (fractions 1/5 .. 4/5) overlap each other and put
        # B on L.
        self.assertEqual(why, ['overlapping', 'on a placed part'])

    def test_equal_starts_on_a_placed_part_are_packed_too(self):
        """Three claims whose equal starts clear each other, one on L: the
        equal starts used to stand there (nothing overlapped). Now they are
        packed, and `why` says the reason."""
        with tempfile.TemporaryDirectory() as td:
            st = self._state(td, [_part('L', 30.0, 3.0, (-3, -2, 3, 2),
                                        locked=True)])
            three = [dict(c) for c in SPECS[:3]]
            for ref in 'ABC':
                p = st.parts[ref]
                p.grade_by_rot[0.0] = (-5.0, -2.0, 5.0, 2.0)
            self.assertEqual(seeder._packed_starts(
                st, three, 'north', st.board, lambda r: None), {})
            why = []
            out = _spans(st, seeder._packed_starts(
                st, three, 'north', st.board, lambda r: None, placed={'L'},
                why=why))
        self.assertEqual(why, ['on a placed part'])
        self.assertEqual(sorted(out), ['A', 'B', 'C'])
        self.assertFalse(any(_overlaps(s, (27.0, 33.0)) for s in out.values()),
                         out)

    def test_a_placed_part_off_the_claims_depth_does_not_block(self):
        """L sits 10..14 mm in from the north edge, deeper than any claim
        reaches (their band ends at 3.75 mm): the packing is the blind one."""
        with tempfile.TemporaryDirectory() as td:
            st = self._state(td, [_part('L', 30.0, 12.0, (-3, -2, 3, 2),
                                        locked=True)])
            blind = seeder._packed_starts(st, SPECS, 'north', st.board,
                                          lambda r: None)
            seen = seeder._packed_starts(st, SPECS, 'north', st.board,
                                         lambda r: None, placed={'L'})
        self.assertTrue(blind)
        self.assertEqual(seen, blind)

    def test_a_back_side_part_does_not_block_a_front_claim(self):
        """An SMD part on B.Cu shares no side with the F.Cu claims."""
        with tempfile.TemporaryDirectory() as td:
            st = self._state(td, [_part('L', 30.0, 3.0, (-3, -2, 3, 2),
                                        locked=True, layer='B')])
            blind = seeder._packed_starts(st, SPECS, 'north', st.board,
                                          lambda r: None)
            seen = seeder._packed_starts(st, SPECS, 'north', st.board,
                                         lambda r: None, placed={'L'})
        self.assertEqual(seen, blind)

    def test_a_padless_part_with_a_courtyard_is_an_obstacle(self):
        """A pad-less footprint that draws a courtyard enters the state as
        a locked static obstacle (`LOCK_PADLESS`) and both rungs price its
        courtyard, so the packing packs around it as around L. (One with no
        courtyard -- make_unplaced leaves logos where they were -- never
        enters the state: there is no geometry to respect.)"""
        with tempfile.TemporaryDirectory() as td:
            st = self._state(td, [_part('K', 30.0, 3.0, (-3, -2, 3, 2),
                                        locked=True, pads=False),
                                  _part('G', 30.0, 8.0, None, locked=True,
                                        pads=False)])
            self.assertNotIn('G', st.parts)
            self.assertEqual([o[0] for o in seeder._band_obstacles(
                st, {'K', 'G'}, set())], ['K'])
            seen = _spans(st, seeder._packed_starts(
                st, SPECS, 'north', st.board, lambda r: None,
                placed={'K', 'G'}))
        self.assertFalse(any(_overlaps(s, (27.0, 33.0))
                             for s in seen.values()), seen)

    def test_a_pin_frame_container_is_not_an_obstacle(self):
        """A board-sized ring of drilled pins with no courtyard (rp2350's
        Teensy frame U8 is one) is a CONTAINER: the courtyard budget leaves
        it out and its box is under every seat on the band, so the packing
        does not read it -- the rungs still price its pins one by one."""
        frame = ('  (footprint "t:Frame" (layer "F.Cu") (at 30 15)\n'
                 '    (locked yes)\n'
                 '    (property "Reference" "U8" (at 0 0) (layer "F.SilkS"))\n'
                 + ''.join(f'    (pad "{i + 1}" thru_hole circle (at {x} {y})'
                           f' (size 1.6 1.6) (drill 1) (layers "*.Cu")'
                           f' (net 1 "N1"))\n'
                           for i, (x, y) in enumerate(((-28, -13), (28, -13),
                                                       (-28, 13), (28, 13))))
                 + '  )\n')
        with tempfile.TemporaryDirectory() as td:
            st = self._state(td, [frame])
            self.assertEqual(seeder._band_obstacles(st, {'U8'}, set()), [])
            blind = seeder._packed_starts(st, SPECS, 'north', st.board,
                                          lambda r: None)
            seen = seeder._packed_starts(st, SPECS, 'north', st.board,
                                         lambda r: None, placed={'U8'})
        self.assertTrue(blind)
        self.assertEqual(seen, blind)

    def test_claims_that_do_not_fit_around_it_keep_the_equal_starts(self):
        """L over x 10..50: four 13 mm claims fit neither side, so the
        equal starts stand ({}), as when they do not fit the edge at all."""
        with tempfile.TemporaryDirectory() as td:
            st = self._state(td, [_part('L', 30.0, 3.0, (-20, -2, 20, 2),
                                        locked=True)])
            self.assertEqual(seeder._packed_starts(
                st, SPECS, 'north', st.board, lambda r: None,
                placed={'L'}), {})


class KeepsAZonedClaimInItsZone(unittest.TestCase):
    """The zone half, on glasgow_revC's south edge and its designer intent:
    J3 is a member of a block zoned at x 85.97..128.47 (tolerance 0.5)."""

    @classmethod
    def setUpClass(cls):
        pcb = parse_kicad_pcb(GLASGOW)
        it = floorplan.intent_from_dict(
            floorplan.emit_intent(pcb, GLASGOW, group_sources=SOURCES),
            source_path='e')
        cls.st = pose_score.make_state(pcb, GLASGOW)
        blocks, _ = floorplan.resolve_blocks(it, pcb, SOURCES)
        cls.zones = {}
        for z in it.blocks:
            if z.rect is None:
                continue
            for r in blocks.get(z.name, ()):
                cls.zones.setdefault(r, []).append(
                    (tuple(z.rect), it.zone_tolerance(z)))
        cls.specs = sorted((c for c in it.edge_claims()
                            if c.get('edge') == 'south'),
                           key=lambda c: c['ref'])

    def test_the_designer_zone_is_the_one_the_issue_names(self):
        self.assertEqual([c['ref'] for c in self.specs],
                         ['FID1', 'FID3', 'FID4', 'FID6', 'J3', 'J4', 'SW1'])
        self.assertEqual(self.zones['J3'],
                         [((85.97, 94.5, 128.47, 120.0), 0.5)])

    def test_j3_starts_inside_its_zone(self):
        """The falsifier: packed blind to zones, in seat order, J3 starts at
        x 74.8..108.8 -- outside its zone. With its zone it is ordered after
        the free claims and starts inside it, and no two claims overlap."""
        blind = _spans(self.st, seeder._packed_starts(
            self.st, self.specs, 'south', self.st.board, lambda r: None),
            'south')
        why = []
        seen = _spans(self.st, seeder._packed_starts(
            self.st, self.specs, 'south', self.st.board, lambda r: None,
            zones=self.zones, why=why), 'south')
        lo, hi = 85.97 - 0.5, 128.47 + 0.5
        self.assertLess(blind['J3'][0], lo)
        self.assertGreaterEqual(seen['J3'][0], lo - 1e-6)
        self.assertLessEqual(seen['J3'][1], hi + 1e-6)
        self.assertEqual(max(seen, key=lambda r: seen[r][0]), 'J3')
        refs = sorted(seen, key=lambda r: seen[r][0])
        for a, b in zip(refs, refs[1:]):
            self.assertFalse(_overlaps(seen[a], seen[b]), (a, b, seen))
        self.assertEqual(why, ['overlapping', 'outside a zone'])


class UnchangedWithNothingPlacedOrZoned(unittest.TestCase):
    """#1172's packing is what remains when nothing is placed and nothing
    zoned: kit-dev-coldfire's five north claims at EQUAL gaps, the ends
    included, in seat order."""

    def test_equal_gaps_in_seat_order(self):
        pcb = parse_kicad_pcb(COLDFIRE)
        it = floorplan.intent_from_dict(floorplan.emit_intent(pcb, COLDFIRE),
                                        source_path='e')
        specs = sorted((c for c in it.edge_claims()
                        if c.get('edge') == 'north'), key=lambda c: c['ref'])
        st = pose_score.make_state(pcb, COLDFIRE)
        out = seeder._packed_starts(st, specs, 'north', st.board,
                                    lambda r: None)
        sp = _spans(st, out)
        e_lo, e_hi, _ = seeder._declared_edge_span(st, st.board, 'north')
        refs = [c['ref'] for c in specs]
        self.assertEqual(sorted(sp, key=lambda r: sp[r][0]), refs)
        gaps = ([sp[refs[0]][0] - e_lo]
                + [sp[b][0] - sp[a][1] for a, b in zip(refs, refs[1:])]
                + [e_hi - sp[refs[-1]][1]])
        self.assertGreater(min(gaps), 0.0)
        self.assertAlmostEqual(max(gaps) - min(gaps), 0.0, places=6)


class TheSwitchStaysOff(unittest.TestCase):
    def test_it_is_opt_in(self):
        """An opt-in: off by default."""
        self.assertIs(seeder._stage1_packed_starts, False)


if __name__ == '__main__':
    unittest.main()
