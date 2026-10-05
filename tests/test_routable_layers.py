#!/usr/bin/env python3
"""Relocation, stub switching and layer swaps draw their candidate layers from
GridRouteConfig.routable_layers(): config.layers minus the FORBIDDEN (negative
cost) layers that route.py / route_diff.py append as obstacles. A 6-layer board
routed with --layers F.Cu In2.Cu B.Cu had USB terminals relocated and stubs
swapped onto In1.Cu / In4.Cu, its GND reference planes.

    python3 tests/test_routable_layers.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_router"))
from routing_config import GridRouteConfig  # noqa: E402


class _OpenMap:
    """An obstacle map with nothing blocked on any layer."""
    def is_blocked(self, gx, gy, layer):
        return False


def main():
    cfg = GridRouteConfig(layers=["F.Cu", "In2.Cu", "B.Cu", "In1.Cu", "In3.Cu", "In4.Cu"],
                          layer_costs=[1.0, 1.0, 1.0, -1, -1, -1])
    assert cfg.routable_layers() == ["F.Cu", "In2.Cu", "B.Cu"], cfg.routable_layers()
    assert GridRouteConfig(layers=["F.Cu", "B.Cu"]).routable_layers() == ["F.Cu", "B.Cu"]

    # The setback-failure fallback ranks launch layers by openness; the forbidden
    # inner layers are as open as any, so they used to be offered.
    from layer_swap_fallback import rank_fallback_layers
    ranked = rank_fallback_layers(cfg, SimpleNamespace(zones=[]), _OpenMap(),
                                  10.0, 10.0, "F.Cu")
    assert sorted(ranked) == ["B.Cu", "In2.Cu"], ranked
    print("PASS")


if __name__ == "__main__":
    main()
