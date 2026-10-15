#!/usr/bin/env python3
"""A foreign pad declared `F&B.Cu` is seen by the sampled pad checks (#1046).

KiCad has two copper layer-set tokens for a pad: `*.Cu` (every copper layer)
and `F&B.Cu` (front and back only). The parser now reads `F&B.Cu` as F.Cu +
B.Cu, the set pcbnew loads (`canonical_pad_layers`), but a Pad built in code
can still carry the token, so the consumers keep expanding it.
`_foreign_pad_arrays` kept a pad only if its list held `layer` or `*.Cu`, so an
`F&B.Cu` pad was on no layer at all: `_pt_foreign_pad_dist` /
`_seg_foreign_pad_dist` never saw it, and neither did anything built on them --
the terminal-graze neck, and the cleanup and nudge passes in pcb_modification.
Measured before the fix: a through-hole pad rewritten from `*.Cu` to `F&B.Cu`
on a copy of splitflap_driver dropped out of the F.Cu arrays.

Every check here is a comparison against the same pad declared `*.Cu`, which
the arrays always saw. On F.Cu and B.Cu the two spellings must measure the
same; on an inner layer `F&B.Cu` must stay absent while `*.Cu` does not, so
the fix cannot pass by treating `F&B.Cu` as `*.Cu`.

    python3 tests/test_1046_fb_cu_foreign_pad.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))

from kicad_parser import Pad, Net, PCBData, BoardInfo, Segment
from routing_config import GridRouteConfig
from single_ended_routing import (_seg_foreign_pad_dist, _pt_foreign_pad_dist,
                                  _neck_terminal_grazes)

FAILS = []

#: _custom_pad_min_dist's "nothing near" answer: any distance at or above this
#: means the pad was not seen.
ABSENT = 1e8

# The foreign pad: a round through-hole pad, centre (0.5, 1.0), radius 0.8, so
# its copper edge is at y = 0.2 and a track along y = 0 is 0.2 mm from it.
PAD_X, PAD_Y, PAD_D = 0.5, 1.0, 1.6
EDGE_GAP = PAD_Y - PAD_D / 2.0


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))


def _board(pad_layers, copper=('F.Cu', 'B.Cu')):
    pad = Pad(component_ref='J1', pad_number='1', global_x=PAD_X, global_y=PAD_Y,
              local_x=0.0, local_y=0.0, size_x=PAD_D, size_y=PAD_D, shape='circle',
              layers=list(pad_layers), net_id=2, net_name='/OTHER', drill=1.0,
              pad_type='thru_hole')
    return PCBData(footprints={}, nets={1: Net(1, '/SIG'), 2: Net(2, '/OTHER')},
                   segments=[], vias=[],
                   board_info=BoardInfo(layers={}, copper_layers=list(copper)),
                   pads_by_net={2: [pad]})


def t_front_and_back_see_the_pad():
    for layer in ('F.Cu', 'B.Cu'):
        ctrl = _board(['*.Cu', '*.Mask'])
        fb = _board(['F&B.Cu', '*.Mask'])
        d_ctrl = _pt_foreign_pad_dist(ctrl, 1, PAD_X, 0.0, layer)
        d_fb = _pt_foreign_pad_dist(fb, 1, PAD_X, 0.0, layer)
        check(f'the *.Cu control sees the pad on {layer}',
              abs(d_ctrl - EDGE_GAP) < 1e-6, f'got {d_ctrl:.4f}, want {EDGE_GAP:.4f}')
        check(f'an F&B.Cu pad measures the same as *.Cu on {layer} (point)',
              abs(d_fb - d_ctrl) < 1e-9, f'F&B.Cu {d_fb:.4f} vs *.Cu {d_ctrl:.4f}')
        s_ctrl = _seg_foreign_pad_dist(ctrl, 1, 0.0, 0.0, 1.0, 0.0, layer)
        s_fb = _seg_foreign_pad_dist(fb, 1, 0.0, 0.0, 1.0, 0.0, layer)
        check(f'an F&B.Cu pad measures the same as *.Cu on {layer} (segment)',
              s_ctrl < ABSENT and abs(s_fb - s_ctrl) < 1e-9,
              f'F&B.Cu {s_fb:.4f} vs *.Cu {s_ctrl:.4f}')


def t_the_bare_fb_spelling_is_seen():
    # expand_pad_layers accepts the bare "F&B" too; the arrays inherit it.
    d = _pt_foreign_pad_dist(_board(['F&B', '*.Mask']), 1, PAD_X, 0.0, 'F.Cu')
    check('a bare F&B pad is seen on F.Cu', abs(d - EDGE_GAP) < 1e-6, f'got {d:.4f}')


def t_an_inner_layer_does_not_see_it():
    inner = ('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu')
    d_ctrl = _pt_foreign_pad_dist(_board(['*.Cu', '*.Mask'], inner), 1, PAD_X, 0.0, 'In1.Cu')
    d_fb = _pt_foreign_pad_dist(_board(['F&B.Cu', '*.Mask'], inner), 1, PAD_X, 0.0, 'In1.Cu')
    check('the *.Cu control is on In1.Cu', d_ctrl < ABSENT, f'got {d_ctrl:.4f}')
    check('an F&B.Cu pad has no copper on In1.Cu', d_fb >= ABSENT, f'got {d_fb:.4f}')


def t_the_terminal_neck_sees_the_pad():
    """The consumer the issue names: a full-width terminal grazing the pad."""
    results = {}
    for tag, layers in (('*.Cu', ['*.Cu', '*.Mask']), ('F&B.Cu', ['F&B.Cu', '*.Mask'])):
        pcb = _board(layers)
        cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], track_width=0.25, clearance=0.1)
        seg = Segment(start_x=0.0, start_y=0.0, end_x=1.0, end_y=0.0, width=0.25,
                      layer='F.Cu', net_id=1)
        necked, hard = _neck_terminal_grazes([seg], [(0.0, 0.0)], pcb, 1, cfg)
        results[tag] = (necked, hard, seg.width)
    n_ctrl, h_ctrl, w_ctrl = results['*.Cu']
    n_fb, h_fb, w_fb = results['F&B.Cu']
    # 0.25 wide at 0.2 from the edge leaves 0.075 < the 0.1 clearance, so the
    # control must neck, or the comparison below proves nothing.
    check('the *.Cu control necks the grazing terminal',
          n_ctrl == 1 and not h_ctrl and w_ctrl < 0.25 - 1e-9,
          f'necked={n_ctrl} hard={len(h_ctrl)} width={w_ctrl:.4f}')
    check('an F&B.Cu pad necks the terminal the same way',
          n_fb == n_ctrl and len(h_fb) == len(h_ctrl) and abs(w_fb - w_ctrl) < 1e-9,
          f'F&B.Cu necked={n_fb} width={w_fb:.4f} vs *.Cu necked={n_ctrl} width={w_ctrl:.4f}')


def main():
    t_front_and_back_see_the_pad()
    t_the_bare_fb_spelling_is_seen()
    t_an_inner_layer_does_not_see_it()
    t_the_terminal_neck_sees_the_pad()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S): {', '.join(FAILS)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
