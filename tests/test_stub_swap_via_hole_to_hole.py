"""A stub layer-switch pad via must respect hole-to-hole clearance
against EVERY existing hole -- including the same net's own via.

Wavelet RevB attempt-004: a STUB-SWAP RESCUE moved GNSS_V_BCKP's F.Cu stub to
B.Cu and drilled a pad via at R202.2 (-25.7,-14.59), 0.16 mm from the net's
existing 0.45/0.20 via at (-25.7,-14.75): overlapping drills. The rescue
paths (single_ended_loop stub-swap rescue, switch_boxed_stub_near, the
diff-pair multipoint switch) applied the swap without the via-fit funnel
(_swap_vias_fit_or_shrink) every layer_swap_optimization site runs.

    python3 tests/test_stub_swap_via_hole_to_hole.py
"""
import math
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, 'py_router'), os.path.join(_ROOT, 'py_tools')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kicad_parser import PCBData, BoardInfo, Segment, Pad, Via  # noqa: E402
from routing_config import GridRouteConfig  # noqa: E402
from stub_layer_switching import switch_boxed_stub_near  # noqa: E402

NET = 12
H2H = 0.25


def _board():
    pad = Pad(component_ref='R202', pad_number='2', global_x=-25.7, global_y=-14.59,
              local_x=0, local_y=0, size_x=0.64, size_y=0.54, shape='roundrect',
              layers=['F.Cu', 'F.Mask', 'F.Paste'], net_id=NET, net_name='GNSS_V_BCKP')
    old_via = Via(x=-25.7, y=-14.75, size=0.45, drill=0.2,
                  layers=['F.Cu', 'B.Cu'], net_id=NET)
    tp = Pad(component_ref='TP206', pad_number='1', global_x=-25.7, global_y=-17.5,
             local_x=0, local_y=0, size_x=1.0, size_y=1.0, shape='circle',
             layers=['F.Cu', 'F.Mask'], net_id=NET, net_name='GNSS_V_BCKP')
    # The escape stub off R202.2 (free end at y=-12.59) and an unconnected
    # second island, so the net has two copper groups and a stub free end.
    stub = [Segment(start_x=-25.7, start_y=-14.59, end_x=-25.7, end_y=-13.59,
                    width=0.2, layer='F.Cu', net_id=NET),
            Segment(start_x=-25.7, start_y=-13.59, end_x=-25.7, end_y=-12.59,
                    width=0.2, layer='F.Cu', net_id=NET)]
    island = Segment(start_x=-25.7, start_y=-17.5, end_x=-25.7, end_y=-16.8,
                     width=0.2, layer='F.Cu', net_id=NET)
    pcb = PCBData(board_info=BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu'],
                                       board_bounds=None),
                  nets={}, footprints={}, vias=[old_via], segments=stub + [island],
                  pads_by_net={NET: [pad, tp]})
    cfg = GridRouteConfig(grid_step=0.05, clearance=0.2, via_size=0.45,
                          via_drill=0.2, track_width=0.2,
                          hole_to_hole_clearance=H2H, layers=['F.Cu', 'B.Cu'])
    return pcb, cfg, old_via


def test_boxed_stub_switch_never_drills_within_hole_to_hole():
    pcb, cfg, old_via = _board()
    res = switch_boxed_stub_near(pcb, NET, cfg, -25.7, -12.59)
    new_vias = [v for v in pcb.vias if v is not old_via]
    for v in new_vias:
        gap = math.hypot(v.x - old_via.x, v.y - old_via.y) - v.drill / 2 - old_via.drill / 2
        assert gap >= H2H - 1e-9, (
            f"new via at ({v.x},{v.y}) drill gap {gap:.3f} mm < {H2H} mm "
            f"to same-net via at ({old_via.x},{old_via.y})")
    if res is None:
        # Refused: the board is exactly as it was (no relayered stub left behind).
        assert new_vias == []
        assert all(s.layer == 'F.Cu' for s in pcb.segments)


if __name__ == '__main__':
    test_boxed_stub_switch_never_drills_within_hole_to_hole()
    print("PASS")
