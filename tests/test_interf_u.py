#!/usr/bin/env python3
"""
Test routing on the interf_u board.
Creates VCC/GND planes first, fans out U9 (PGA120) nets, routes all signals,
then connects disconnected plane regions.

Every step is ASSERTED (`run_utils.run_step`): it must exit 0 with no
traceback and write the board the next step reads, and the two checkers must
pass. This file used `run_utils.run`, which prints a failure and carries on,
so it was green whatever the chain did.

Outputs go to a fresh temporary directory, not into kicad_files/ (as
test_kit_route does, #426): a stale output there would satisfy the "was it
written" check, and a stale sibling `.kicad_pro` would be read back as the
next step's DRC floor (CLAUDE.md). The input board is read from kicad_files/.
"""

import argparse
import os
import tempfile

from run_utils import ROOT_DIR, run_step

#: A four-step chain on interf_u (25 footprints, 174 nets, 379 pads): planes on
#: both layers, a PGA120 fan-out, a full signal route at --max-iterations
#: 1000000 --max-ripup 10, then plane repair and two checkers. It carried NO
#: budget, so `run_all` gave it the 600 s default and killed it there (#930) --
#: and an undeclared budget cannot tell "this chain is long" from "this chain
#: wedged", so every full-suite run ended on a non-pass a reader had to spend
#: another ten minutes re-running by hand to dismiss.
#:
#: MEASURED: 111.5 s wall, alone, on a fast 2026 arm64 macOS machine
#: (`/usr/bin/time -p`, 2026-09-09). 1200 s is ~10x that -- the suite runs four
#: tests in parallel, and the reporting machine that hit the 600 s wall is a
#: slower Windows box. The figure is a claim about the CHAIN, not the machine:
#: if this file ever times out at 1200 s, something wedged.
RUN_ALL_TIMEOUT = 1200


def main():
    parser = argparse.ArgumentParser(description='Test routing on interf_u board')
    parser.add_argument('-u', '--unbuffered', action='store_true', default=False,
                        help='Accepted for compatibility; step output is '
                             'always echoed live')
    parser.add_argument('--checks', action='store_true', default=True,
                        help='Run DRC and connectivity checks after routing')
    parser.add_argument('--workdir', default=None,
                        help='Directory for the generated boards (default: a '
                             'fresh temp dir, auto-removed). Pass a path to '
                             'keep them for inspection.')
    args = parser.parse_args()

    src = os.path.join(ROOT_DIR, 'kicad_files', 'interf_u_unrouted.kicad_pcb')
    tmp = None
    if args.workdir:
        workdir = os.path.abspath(args.workdir)
        os.makedirs(workdir, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory(prefix='interf_u_')
        workdir = tmp.name
    plane = os.path.join(workdir, 'interf_u_plane.kicad_pcb')
    fanout = os.path.join(workdir, 'interf_u_fanout.kicad_pcb')
    routed = os.path.join(workdir, 'interf_u_routed.kicad_pcb')
    connected = os.path.join(workdir, 'interf_u_connected.kicad_pcb')

    try:
        # Step 1: Create VCC and GND planes on both layers
        run_step(['py_router/route_planes.py', src, plane,
                  '--nets', 'VCC', 'GND', '--plane-layers', 'F.Cu', 'B.Cu'],
                 outputs=[plane])

        # Step 2: Fan out U9 (PGA120) nets
        run_step(['py_router/bga_fanout.py', plane, '--component', 'U9',
                  '--output', fanout, '--nets', '/*'],
                 outputs=[fanout])

        # Step 3: Route all signals
        run_step(['py_router/route.py', fanout, routed, '--no-bga-zone',
                  '--add-teardrops', '--layer-costs', '1', '1',
                  '--max-ripup', '10', '--stub-proximity-radius', '10',
                  '--stub-proximity-cost', '3.0', '--max-iterations', '1000000',
                  '--board-edge-clearance', '0.55'],
                 outputs=[routed])

        # Step 4: Connect disconnected plane regions
        run_step(['py_router/repair_planes.py', routed, connected,
                  '--board-edge-clearance', '0.6'],
                 outputs=[connected])

        if args.checks:
            # Both checkers run to a verdict (a crash fails here and now) and
            # report before either verdict is judged.
            verdicts = {}
            # Check for DRC errors
            verdicts['check_drc'], _ = run_step(
                ['py_router/check_drc.py', connected], findings_ok=True)
            # Check connectivity
            verdicts['check_connected'], _ = run_step(
                ['py_router/check_connected.py', connected], findings_ok=True)
            dirty = sorted(k for k, rc in verdicts.items() if rc)
            if dirty:
                raise AssertionError(
                    f"the chain ran, but {', '.join(dirty)} found problems "
                    f"on the result (output above)")
    finally:
        if tmp is not None:
            tmp.cleanup()

    print("\n=== Test completed ===")


if __name__ == "__main__":
    main()
