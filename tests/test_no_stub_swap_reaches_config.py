#!/usr/bin/env python3
"""
Regression test: --no-stub-layer-swap must reach GridRouteConfig.stub_layer_swap.

The flag became batch_route(enable_layer_switch=False), which gated only the
up-front stub pass. The in-loop and reconciliation STUB-SWAP RESCUE read
config.stub_layer_swap (single_ended_loop.py, stub_layer_switching.py), which
was never set and defaulted to True, so a run asked to keep input stubs still
moved them: on cm5-io stage 3 a 0.8 mm CHG_CC_5V stub swapped onto F.Cu shorted
a fine-pitch U1002 pad.

The test stops batch_route at config construction and checks both settings.

Run:
    python3 tests/test_no_stub_swap_reaches_config.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "py_router"))

import route  # noqa: E402

BOARD = os.path.join(HERE, "..", "kicad_files", "cap_chain.kicad_pcb")


class _Stop(Exception):
    pass


def captured_stub_swap(enable: bool) -> bool:
    real = route.GridRouteConfig
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        raise _Stop

    route.GridRouteConfig = capture
    try:
        route.batch_route(BOARD, os.devnull, [], enable_layer_switch=enable)
    except _Stop:
        pass
    finally:
        route.GridRouteConfig = real
    assert "stub_layer_swap" in seen, "config built without stub_layer_swap"
    return seen["stub_layer_swap"]


def main() -> int:
    assert captured_stub_swap(False) is False
    assert captured_stub_swap(True) is True
    print("PASS: --no-stub-layer-swap reaches config.stub_layer_swap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
