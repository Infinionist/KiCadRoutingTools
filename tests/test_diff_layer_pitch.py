#!/usr/bin/env python3
"""A diff pair's P/N centre pitch follows the layer's own width: with per-layer
widths F.Cu 0.197 / In2.Cu 0.154 and a 0.15 mm gap, the pair runs at 0.347 mm
on F.Cu and 0.304 mm on In2.Cu (it kept 0.347 everywhere, a 0.193 mm edge gap
on In2, cm5-io 2026-10-05).

    python3 tests/test_diff_layer_pitch.py
"""
import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_router"))
from diff_pair_routing import create_parallel_path_from_float, layer_pair_spacing  # noqa: E402


def main():
    widths = {"F.Cu": 0.197, "In2.Cu": 0.154, "B.Cu": 0.197}
    cfg = SimpleNamespace(track_width=0.197, diff_pair_gap=0.15, layers=["F.Cu", "In2.Cu", "B.Cu"],
                          layer_widths=widths, get_track_width=lambda l: widths.get(l, 0.197))
    sp = layer_pair_spacing(cfg)
    assert abs(sp(0) - 0.1735) < 1e-9 and abs(sp(1) - 0.152) < 1e-9
    for layer, pitch in ((0, 0.347), (1, 0.304)):
        line = [(0.0, 0.0, layer), (5.0, 0.0, layer)]
        p = create_parallel_path_from_float(line, 1, sp)
        n = create_parallel_path_from_float(line, -1, sp)
        got = math.dist(p[0][:2], n[0][:2])
        assert abs(got - pitch) < 1e-9, (layer, got)
    flat = SimpleNamespace(track_width=0.197, diff_pair_gap=0.15, layers=["F.Cu"], layer_widths={})
    assert layer_pair_spacing(flat) == (0.197 + 0.15) / 2
    print("PASS")


if __name__ == "__main__":
    main()
