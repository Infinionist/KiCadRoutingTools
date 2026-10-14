#!/usr/bin/env python3
"""#1251: a differential pair that runs through per-leg series parts.

`diff_xnet.find_ac_coupled_xnets` (#196) joins two name-detected pairs into
one end-to-end signal when BOTH polarities are bridged by a 2-pad, non-DNP
part. It counted `fp.pads`, so a 0201 resistor drawn with two paste-only
windows (pad `""` on `F.Paste` alone) beside its copper pads had four "pads"
and was rejected: rp2350_fpga_eensy_prePlane's R4/R5 and orangecrab_ext_pll's
R21/R29, so neither board's USB XNet was found. It now counts
`kicad_parser.non_aperture_pads` (#1143/#1148's rule).

Pinned here:
  * the tracked boards resolve the issue's XNets: rp2350 R4/R5, orangecrab
    R21/R29, and ulx3s's six unchanged;
  * a synthetic part with two copper pads and two paste-only windows bridges;
    one with three copper pads still does not.

Run: python3 -X utf8 tests/test_1251_series_part_pairs.py
"""
import contextlib
import io
import os
import sys
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in ('py_placer', 'py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

from kicad_parser import parse_kicad_pcb                       # noqa: E402
from diff_xnet import find_ac_coupled_xnets                    # noqa: E402
from net_queries import find_differential_pairs                # noqa: E402
from routing_config import DiffPairNet                         # noqa: E402
from run_utils import evidence                                 # noqa: E402

BOARDS = os.path.join(ROOT, 'kicad_files')
RP = os.path.join(BOARDS, 'rp2350_fpga_eensy_prePlane.kicad_pcb')
OC = os.path.join(BOARDS, 'orangecrab_ext_pll.kicad_pcb')
ULX = os.path.join(BOARDS, 'ulx3s.kicad_pcb')
CAP = os.path.join(BOARDS, 'cap_chain.kicad_pcb')

_PCB = {}


def _pcb(path):
    if path not in _PCB:
        evidence(path, 'tracked board')
        with contextlib.redirect_stdout(io.StringIO()):
            _PCB[path] = parse_kicad_pcb(path)
    return _PCB[path]


def _xnet_refs(path):
    pcb = _pcb(path)
    dp = find_differential_pairs(pcb, ['*'])
    xnets, _warnings = find_ac_coupled_xnets(pcb, dp)
    return sorted(x.bridge_refs for x in xnets)


class TheRoutersJunctionRule(unittest.TestCase):

    def test_paste_only_windows_no_longer_hide_a_series_resistor(self):
        self.assertEqual(_xnet_refs(RP), [['R4', 'R5']])
        self.assertEqual(_xnet_refs(OC), [['R21', 'R29']])

    def test_the_boards_that_already_resolved_are_unchanged(self):
        self.assertEqual(_xnet_refs(ULX), [
            ['C38', 'C39'], ['C40', 'C41'], ['C42', 'C43'], ['C44', 'C45'],
            ['R49', 'R50'], ['R52', 'R53']])
        self.assertEqual(_xnet_refs(CAP), [['C1', 'C2']])

    def test_the_count_is_copper_pads_not_apertures(self):
        def pad(net, layers=('F.Cu', 'F.Mask')):
            return SimpleNamespace(net_id=net, layers=list(layers),
                                   pad_type='smd', drill=0.0)

        def part(*pads):
            return SimpleNamespace(pads=list(pads), dnp=False)

        paste = ('F.Paste',)
        pairs = {b: DiffPairNet(base_name=b, p_net_id=p, n_net_id=n,
                                p_net_name=b + '_P', n_net_name=b + '_N')
                 for b, p, n in (('A', 1, 2), ('B', 3, 4))}
        windows = part(pad(0, paste), pad(0, paste), pad(1), pad(3))
        board = SimpleNamespace(footprints={
            'R1': windows, 'R2': part(pad(0, paste), pad(0, paste),
                                      pad(2), pad(4))})
        xnets, _w = find_ac_coupled_xnets(board, pairs)
        self.assertEqual([x.bridge_refs for x in xnets], [['R1', 'R2']])
        # Three COPPER pads is not a series part, windows or not.
        board.footprints['R2'] = part(pad(0, paste), pad(2), pad(4), pad(9))
        xnets, _w = find_ac_coupled_xnets(board, pairs)
        self.assertEqual(xnets, [])


if __name__ == '__main__':
    unittest.main()
