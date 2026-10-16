#!/usr/bin/env python3
"""
Regression test: --no-stub-layer-swap must reach GridRouteConfig.stub_layer_swap.

The flag became batch_route(enable_layer_switch=False), which gated only the
up-front stub pass. The in-loop and reconciliation STUB-SWAP RESCUE read
config.stub_layer_swap (single_ended_loop.py, stub_layer_switching.py), which
was never set and defaulted to True, so a run asked to keep input stubs still
moved them: on cm5-io stage 3 a 0.8 mm CHG_CC_5V stub swapped onto F.Cu shorted
a fine-pitch U1002 pad.

Checks:
  1. batch_route builds its config with stub_layer_swap ==
     enable_layer_switch (stopped at GridRouteConfig).
  2. route.py on the #1261 STUB-SWAP RESCUE board (its stub left unlocked):
     the rescue switches NET_A's stub by default and not under
     --no-stub-layer-swap, which ships the input stub where it was.

Run:
    python3 tests/test_no_stub_swap_reaches_config.py
"""

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "py_router"))

import route  # noqa: E402
from run_utils import check as run_check, evidence, tool  # noqa: E402

BOARD = os.path.join(ROOT, "kicad_files", "cap_chain.kicad_pcb")
RESCUE = "STUB-SWAP RESCUE: NET_A"
failures = []


def check(label, ok, detail=""):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + str(detail)) if detail else ''}")
    if not ok:
        failures.append(label)


class _Stop(Exception):
    pass


def captured_stub_swap(module, engine, enable):
    real = module.GridRouteConfig
    seen = {}

    def capture(**kwargs):
        seen.update(kwargs)
        raise _Stop

    module.GridRouteConfig = capture
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            getattr(module, engine)(BOARD, os.devnull, [], enable_layer_switch=enable)
    except _Stop:
        pass
    finally:
        module.GridRouteConfig = real
    return seen.get("stub_layer_swap", "<not set>")


def config_reach():
    print("1. the flag reaches config.stub_layer_swap")
    for module, engine in ((route, "batch_route"),):
        got = (captured_stub_swap(module, engine, False),
               captured_stub_swap(module, engine, True))
        check(f"{engine}: enable_layer_switch False/True -> stub_layer_swap",
              got == (False, True), got)


def rescue_route():
    print("2. route.py STUB-SWAP RESCUE")
    from test_1261_locked_stub_never_switches import rescue_board
    text, args = rescue_board(lock=False)
    tmp = tempfile.mkdtemp(prefix="t1270_")
    try:
        inp = os.path.join(tmp, "in.kicad_pcb")
        with open(inp, "w", encoding="utf-8") as fh:
            fh.write(text)
        evidence(inp, "synthetic input board")
        env = dict(os.environ, KICAD_PLANE_FINALIZE="0")
        outs = {}
        for tag, extra in (("default", []), ("no_swap", ["--no-stub-layer-swap"])):
            out = os.path.join(tmp, f"{tag}.kicad_pcb")   # fresh path per run
            r = run_check([sys.executable, "-X", "utf8", tool("route.py"), inp, out]
                          + args + ["--keep-input-copper", "--escalation", "off",
                                    "--no-fix-drc-settings"] + extra,
                          accept=True, env=env)
            evidence(out, "routed output")
            with open(out, encoding="utf-8") as fh:
                outs[tag] = (r.stdout + r.stderr, fh.read())
        con, _ = outs["default"]
        if RESCUE not in con:
            check("BROKEN FIXTURE: the default run takes the stub-swap rescue", False)
            return
        check("control: the default run switches NET_A's stub", True)
        con, board = outs["no_swap"]
        hit = [l.strip() for l in con.splitlines() if RESCUE in l]
        check("--no-stub-layer-swap: no rescue switch", not hit, "; ".join(hit))
        stub = re.findall(r'\(segment \(start 10\.05 15\.05\) \(end 14\.05 15\.05\)'
                          r'[^\n]*\(layer "([^"]+)"\)', board)
        check("and the input stub ships on F.Cu", stub == ["F.Cu"], stub)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    config_reach()
    rescue_route()
    print("FAILED: " + ", ".join(failures) if failures else
          "PASS: --no-stub-layer-swap reaches config.stub_layer_swap")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
