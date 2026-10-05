#!/usr/bin/env python3
"""check_via_segment_overlap grades a via only on the layers it spans: an
F.Cu->In1.Cu GND microvia under a segment on In2.Cu or B.Cu is no overlap (it
vetoed every In2 diff-pair candidate under a GND pad on cm5-io), a through via
is an overlap on every layer.

    python3 tests/test_via_segment_span.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_router"))
from check_drc import check_via_segment_overlap  # noqa: E402


def seg(layer):
    return SimpleNamespace(start_x=0.0, start_y=0.0, end_x=2.0, end_y=0.0, width=0.2, layer=layer)


def main():
    micro = SimpleNamespace(x=1.0, y=0.1, size=0.25, layers=["F.Cu", "In1.Cu"])
    through = SimpleNamespace(x=1.0, y=0.1, size=0.25, layers=["F.Cu", "B.Cu"])
    assert check_via_segment_overlap(micro, seg("F.Cu"), 0.1)[0]
    assert check_via_segment_overlap(micro, seg("In1.Cu"), 0.1)[0]
    assert not check_via_segment_overlap(micro, seg("In2.Cu"), 0.1)[0]
    assert not check_via_segment_overlap(micro, seg("B.Cu"), 0.1)[0]
    for layer in ("F.Cu", "In2.Cu", "B.Cu"):
        assert check_via_segment_overlap(through, seg(layer), 0.1)[0], layer
    print("PASS")


if __name__ == "__main__":
    main()
