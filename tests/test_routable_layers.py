#!/usr/bin/env python3
"""Relocation, stub switching and layer swaps draw their candidate layers from
GridRouteConfig.routable_layers(): config.layers minus the FORBIDDEN (negative
cost) layers that route.py / route_diff.py append as obstacles. cm5-io
(2026-10-05) had USB terminals relocated and stubs swapped onto In1.Cu / In4.Cu,
its GND reference planes, with --layers F.Cu In2.Cu B.Cu.

    python3 tests/test_routable_layers.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_router"))
from routing_config import GridRouteConfig  # noqa: E402


def main():
    cfg = GridRouteConfig(layers=["F.Cu", "In2.Cu", "B.Cu", "In1.Cu", "In3.Cu", "In4.Cu"],
                          layer_costs=[1.0, 1.0, 1.0, -1, -1, -1])
    assert cfg.routable_layers() == ["F.Cu", "In2.Cu", "B.Cu"], cfg.routable_layers()
    assert GridRouteConfig(layers=["F.Cu", "B.Cu"]).routable_layers() == ["F.Cu", "B.Cu"]
    print("PASS")


if __name__ == "__main__":
    main()
