#!/usr/bin/env python3
"""An existing via blocks tracks only on the copper it spans.

A through via (F.Cu..B.Cu) occupies every layer. A blind or micro via occupies only
the layers between its two end layers: an F.Cu->In1.Cu GND microvia in a pad leaves
In2..B.Cu free for tracks. Every via used to be rasterized on all layers, so
via-in-pad microvias freed no routing space at all. Add and remove must stay
mirror images, or rip-up leaks ref-counts.

    python3 tests/test_via_layer_span.py
"""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_tools'))  # #522

from geometry_utils import via_span_layer_indices  # noqa: E402
from kicad_parser import Via  # noqa: E402
from obstacle_map import (GridObstacleMap, add_vias_list_as_obstacles,  # noqa: E402
                          remove_vias_list_from_obstacles)
from routing_config import GridCoord, GridRouteConfig  # noqa: E402

LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu', 'B.Cu']
FAILS = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        FAILS.append(label)


def _config():
    cfg = GridRouteConfig()
    cfg.layers = list(LAYERS)
    cfg.grid_step = 0.05
    cfg.clearance = 0.1
    cfg.track_width = 0.1
    cfg.via_size = 0.25
    cfg.via_drill = 0.15
    return cfg


def _blocked_layers(obstacles, gx, gy):
    return [i for i in range(len(LAYERS)) if obstacles.is_blocked(gx, gy, i)]


def test_span_indices():
    class V:
        layers = None
    v = V()
    v.layers = ['F.Cu', 'In1.Cu']
    check("F.Cu->In1.Cu microvia spans layers 0..1", list(via_span_layer_indices(v, LAYERS, 6)) == [0, 1])
    v.layers = ['B.Cu', 'In4.Cu']
    check("B.Cu->In4.Cu microvia spans layers 4..5 (end order irrelevant)",
          list(via_span_layer_indices(v, LAYERS, 6)) == [4, 5])
    v.layers = ['F.Cu', 'B.Cu']
    check("through via spans every layer", list(via_span_layer_indices(v, LAYERS, 6)) == list(range(6)))
    v.layers = ['In1.Cu', 'In2.Cu']
    check("buried via In1..In2 on a 4-layer routing set maps by name",
          list(via_span_layer_indices(v, ['F.Cu', 'In2.Cu', 'In3.Cu', 'B.Cu'], 4)) == [1])
    v.layers = None
    check("unknown layers fall back to every layer", list(via_span_layer_indices(v, LAYERS, 6)) == list(range(6)))


def test_microvia_frees_inner_layers_and_balances():
    cfg = _config()
    coord = GridCoord(cfg.grid_step)
    micro = Via(x=5.0, y=5.0, size=0.25, drill=0.1, layers=['F.Cu', 'In1.Cu'], net_id=1)
    through = Via(x=8.0, y=5.0, size=0.25, drill=0.15, layers=['F.Cu', 'B.Cu'], net_id=1)
    obstacles = GridObstacleMap(len(LAYERS))
    add_vias_list_as_obstacles(obstacles, [micro, through], cfg)
    gx, gy = coord.to_grid(micro.x, micro.y)
    check("microvia blocks tracks on F.Cu and In1.Cu only", _blocked_layers(obstacles, gx, gy) == [0, 1])
    tx, ty = coord.to_grid(through.x, through.y)
    check("through via still blocks every layer", _blocked_layers(obstacles, tx, ty) == list(range(6)))
    check("microvia still blocks a new via at its spot", obstacles.is_via_blocked(gx, gy))
    remove_vias_list_from_obstacles(obstacles, [micro, through], cfg)
    clean = all(not obstacles.is_blocked(x, y, layer)
                for x, y in ((gx, gy), (tx, ty)) for layer in range(len(LAYERS)))
    check("remove mirrors add: no cell left blocked", clean)
    check("remove mirrors add: via map clear", not obstacles.is_via_blocked(gx, gy)
          and not obstacles.is_via_blocked(tx, ty))


def main():
    test_span_indices()
    test_microvia_frees_inner_layers_and_balances()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED")
        return 1
    print("\nall passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
