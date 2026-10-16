#!/usr/bin/env python3
"""Every path that lays new copper keeps off a protected sense branch (#1279).

The fixture tests/fixtures/sense_branch/sense_star.kicad_pcb (+ .kicad_pro):
net VDD, join pad C1.1 (3, 5), sense pad U1.1 (27, 5), one 0.2 mm F.Cu sense
trace between them declared by `(group "sense:C1.1" ...)`. The boards below
are built from its text in a temp dir, so each case is a few lines of copper
added to that board. tests/test_1279_sense_branches.py covers the main route;
these cover the OTHER paths that lay copper of the branch's own net:

(a) plane_region_connector.build_base_obstacles -- the map region joins,
    oracle links and the guaranteed join route on -- leaves the routed net's
    copper out, so the branch is stamped back in as foreign copper with the
    join pad cut out.

Run:
    python3 tests/test_1279_sense_branch_paths.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))

import sense_branch as sb  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402
from run_utils import tool, evidence  # noqa: E402

BOARD = os.path.join(TESTS_DIR, 'fixtures', 'sense_branch', 'sense_star.kicad_pcb')
PRO = BOARD[:-len('.kicad_pcb')] + '.kicad_pro'
MEMBER = '11111111-0000-0000-0000-000000000001'
#: The fixture's member trace line; extra copper is inserted before it.
ANCHOR = '  (segment (start 3 5) (end 27 5)'
#: The clearance every case routes at (the fixture's Default class).
CLEARANCE = 0.2

_TMP = []


def _tmpdir():
    d = tempfile.mkdtemp(prefix='t1279p_')
    _TMP.append(d)
    return d


def _src():
    with open(BOARD, encoding='utf-8') as f:
        return f.read()


def _variant(text, name='v'):
    """`text` written as a board in a fresh dir, with the fixture's .kicad_pro."""
    d = _tmpdir()
    path = os.path.join(d, f'{name}.kicad_pcb')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    shutil.copy(PRO, os.path.join(d, f'{name}.kicad_pro'))
    return path


def _seg(x1, y1, x2, y2, w, layer, net, uuid):
    """A KiCad-LOCKED track: the router may not rip it (#521), so it walls."""
    return (f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) '
            f'(layer "{layer}") (net {net}) (locked yes) (uuid "{uuid}"))\n')


def _with_gnd(text, copper):
    """`text` with a GND net and `copper` (locked GND walls) added."""
    text = text.replace('(net 0 "") (net 1 "VDD")', '(net 0 "") (net 1 "VDD") (net 2 "GND")')
    assert ANCHOR in text
    return text.replace(ANCHOR, copper + ANCHOR)


def _run(script, args, timeout=600):
    r = subprocess.run([sys.executable, tool(script)] + args, cwd=ROOT,
                       capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, f'{script} exit {r.returncode}:\n' + r.stdout[-3000:] + r.stderr[-3000:]
    return r.stdout


def _branch(board_or_pcb):
    p = parse_kicad_pcb(board_or_pcb) if isinstance(board_or_pcb, str) else board_or_pcb
    assert len(p.sense_branches) == 1, p.sense_branches
    return p.sense_branches[0]


def _vdd(p):
    return next(i for i, n in p.nets.items() if n.name == 'VDD')


# --- (a) the plane connector's base map --------------------------------------

def test_plane_base_map_stamps_the_branch():
    """The map region joins and oracle links route VDD on: the sense trace
    and U1.1 are blocked for tracks and vias off C1.1, C1.1 itself is not."""
    from plane_region_connector import build_base_obstacles
    from routing_config import GridRouteConfig, GridCoord
    p = parse_kicad_pcb(BOARD)
    assert _branch(p).protected
    vdd = _vdd(p)
    cfg = GridRouteConfig(clearance=CLEARANCE, track_width=0.3, via_size=0.6,
                          via_drill=0.3, grid_step=0.1, layers=['F.Cu', 'B.Cu'])
    obs, layer_map = build_base_obstacles({vdd}, ['F.Cu', 'B.Cu'], p, cfg,
                                          track_width=0.3, track_via_clearance=0.8,
                                          hole_to_hole_clearance=0.3)
    coord = GridCoord(cfg.grid_step)
    f = layer_map['F.Cu']
    for x, y, what in ((15.0, 5.0, 'on the trace'), (15.0, 5.4, 'beside the trace'),
                       (27.0, 5.0, 'on U1.1')):
        gx, gy = coord.to_grid(x, y)
        assert obs.is_blocked(gx, gy, f), f'F.Cu {what} ({x}, {y}) is free to VDD'
        assert obs.is_via_blocked(gx, gy), f'a VDD via {what} ({x}, {y}) is allowed'
    gx, gy = coord.to_grid(3.0, 5.0)
    assert not obs.is_blocked(gx, gy, f), 'the join pad C1.1 is blocked to its own net'
    # Another net's map is unchanged: the branch is ordinary VDD copper to it.
    obs2, _ = build_base_obstacles(set(), ['F.Cu', 'B.Cu'], p, cfg, track_width=0.3,
                                   track_via_clearance=0.8, hole_to_hole_clearance=0.3)
    gx, gy = coord.to_grid(3.0, 5.0)
    assert obs2.is_blocked(gx, gy, f), 'foreign VDD pad C1.1 free to another net'
    # A sense pad never anchors a region (nor carries one across layers).
    from plane_region_connector import _collect_anchor_points
    pts, _ = _collect_anchor_points(vdd, (0, 0, 30, 20), p, coord, {'F.Cu'},
                                    ['F.Cu', 'B.Cu'])
    assert (3.0, 5.0) in pts and (27.0, 5.0) not in pts, pts


TESTS = [
    test_plane_base_map_stamps_the_branch,
]


def main():
    evidence(BOARD, 'the sense_star fixture')
    evidence(PRO, "the sense_star fixture's .kicad_pro")
    failed = []
    try:
        for t in TESTS:
            try:
                t()
                print(f'  PASS  {t.__name__}')
            except Exception:  # noqa: BLE001
                failed.append(t.__name__)
                print(f'  FAIL  {t.__name__}')
                traceback.print_exc()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)
    print(f'{len(TESTS) - len(failed)}/{len(TESTS)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
