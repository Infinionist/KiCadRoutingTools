#!/usr/bin/env python3
"""`--net-keepouts` keeps BOUND nets out of a region and lets EXEMPT nets through.

Fixture: kicad_files/flat_hierarchy.kicad_pcb (as tests/test_keepout.py).
Net-(D8-A) and Net-(D9-A) each run a short horizontal path; one box straddles
each path. Both boxes form ONE net-filtered keep-out exempting Net-(D9-A).

Invariants:
1. Unit: `binds` honours appliesToNets (empty = all) and exemptNets globs;
   the loader refuses unknown keys and degenerate polygons.
2. Stamping balances: after `stamp()` exits the obstacle map holds no cell it
   added (refcount restored), and an exempt net stamps nothing.
3. End to end (route.py):
   - control, no flag: Net-(D8-A) cuts through its box;
   - with the flag: Net-(D8-A) routes, connects and has zero cells inside the
     region; Net-(D9-A) (exempt) still cuts straight through its box.

Run:
    python3 tests/test_net_keepouts.py
"""
import json
import os
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
for _p in ('py_router', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import net_keepouts as nk  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402
from routing_config import GridCoord, GridRouteConfig  # noqa: E402
from obstacle_map import _polygon_grid_cells  # noqa: E402

BOARD = os.path.join(ROOT, "kicad_files", "flat_hierarchy.kicad_pcb")
GEOM = ["--track-width", "0.5", "--clearance", "0.4",
        "--via-size", "0.6", "--via-drill", "0.5", "--layers", "F.Cu", "B.Cu"]


class Skip(Exception):
    """A test whose precondition this environment cannot meet."""


def box(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


D8_BOX = box(150.6, 76.4, 152.3, 78.5)
D9_BOX = box(150.6, 86.6, 152.3, 88.7)


def _write_keepouts(entries):
    fd, path = tempfile.mkstemp(suffix=".json", prefix="nko_")
    with os.fdopen(fd, "w") as f:
        json.dump(entries, f)
    return path


def _keepout(polygon, **kw):
    return {"name": kw.pop("name", "corridor"), "polygon": polygon,
            "layers": ["F.Cu", "B.Cu"], "tracks": True, "vias": True, **kw}


def _raises(fn, needle):
    """True when fn() raises ValueError whose message contains `needle`."""
    try:
        fn()
    except ValueError as e:
        return needle in str(e)
    return False


def _route(nets, keepouts_path=None):
    fd, out = tempfile.mkstemp(suffix=".kicad_pcb", prefix="nko_out_")
    os.close(fd)
    cmd = [sys.executable, "py_router/route.py", BOARD, out] + GEOM + ["--nets"] + nets
    if keepouts_path:
        cmd += ["--net-keepouts", keepouts_path]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return out, r.stdout


def _inside(board, net_name, polys, grid_step=0.1):
    from bresenham_utils import walk_line
    pcb = parse_kicad_pcb(board)
    nid = next(i for i, n in pcb.nets.items() if n.name == net_name)
    coord = GridCoord(grid_step)
    cells = set()
    for s in pcb.segments:
        if s.net_id != nid:
            continue
        g1, g2 = coord.to_grid(s.start_x, s.start_y), coord.to_grid(s.end_x, s.end_y)
        cells.update(walk_line(g1[0], g1[1], g2[0], g2[1]))
    region = set()
    for p in polys:
        region |= _polygon_grid_cells([tuple(v) for v in p], coord)
    return len(cells & region), any(s.net_id == nid for s in pcb.segments)


def _connected(board, nets):
    r = subprocess.run([sys.executable, "py_router/check_connected.py", board, "--nets"] + nets,
                       cwd=ROOT, capture_output=True, text=True)
    return "ALL NETS FULLY CONNECTED" in (r.stdout + r.stderr)


def test_binds_applies_and_exempt():
    ko = nk._validate(_keepout(D8_BOX, exemptNets=["GND", "RF_*"]), 0, "t")
    assert nk.binds(ko, "SIG") and not nk.binds(ko, "GND") and not nk.binds(ko, "RF_ANT")
    ko2 = nk._validate(_keepout(D8_BOX, appliesToNets=["HV_*"], exemptNets=["HV_SENSE"]), 0, "t")
    assert nk.binds(ko2, "HV_BUS") and not nk.binds(ko2, "HV_SENSE") and not nk.binds(ko2, "SIG")


def test_loader_refuses_bad_entries():
    assert _raises(lambda: nk.load_net_keepouts(
        _write_keepouts([_keepout(D8_BOX, exempt=["GND"])])), "unknown key")
    assert _raises(lambda: nk.load_net_keepouts(
        _write_keepouts([_keepout([[0, 0], [1, 1]])])), "polygon")


class _Map:
    """Refcounted stand-in for GridObstacleMap's cell/via batches."""

    def __init__(self):
        self.cells, self.vias = {}, {}

    def _bump(self, d, arr, k):
        for row in map(tuple, arr.tolist()):
            d[row] = d.get(row, 0) + k
            if d[row] == 0:
                del d[row]

    def add_blocked_cells_batch(self, a): self._bump(self.cells, a, 1)
    def remove_blocked_cells_batch(self, a): self._bump(self.cells, a, -1)
    def add_blocked_vias_batch(self, a): self._bump(self.vias, a, 1)
    def remove_blocked_vias_batch(self, a): self._bump(self.vias, a, -1)


def test_stamp_balances_and_skips_exempt_net():
    pcb = parse_kicad_pcb(BOARD)
    nk.attach(pcb, nk.load_net_keepouts(
        _write_keepouts([_keepout(D8_BOX, exemptNets=["Net-(D9-A)"])])))
    d8 = next(i for i, n in pcb.nets.items() if n.name == "Net-(D8-A)")
    d9 = next(i for i, n in pcb.nets.items() if n.name == "Net-(D9-A)")
    cfg = GridRouteConfig()
    cfg.layers = ["F.Cu", "B.Cu"]
    m = _Map()
    with nk.stamp(m, pcb, (d8,), cfg):
        assert m.cells and m.vias, "bound net: region must be stamped"
    assert not m.cells and not m.vias, "stamp must remove exactly what it added"
    with nk.stamp(m, pcb, (d9,), cfg):
        assert not m.cells and not m.vias, "exempt net: nothing stamped"


def test_stamp_reaches_every_via_rung_at_its_own_via():
    """On a real map with a #568 small rung and a #530 per-net rung, the region
    blocks vias on every rung while the bound net routes -- the per-net rung
    at ITS via (a cell 0.55 mm off the edge is clear of a 0.6 via's band and
    inside a 0.9 via's) -- and every rung is back as it was afterwards."""
    from obstacle_map import build_base_obstacle_map
    pcb = parse_kicad_pcb(BOARD)
    d8 = next(i for i, n in pcb.nets.items() if n.name == "Net-(D8-A)")
    cfg = GridRouteConfig(layers=["F.Cu", "B.Cu"], track_width=0.25, clearance=0.2,
                          grid_step=0.1, via_size=0.6, via_drill=0.3,
                          net_via_sizes={d8: (0.9, 0.45)})
    work = build_base_obstacle_map(pcb, cfg, [d8]).clone_fresh()
    n = work.rung_count()
    if n < 3:
        raise Skip(f"map carries {n} rung(s); needs the small and a per-net rung")
    snap = lambda: [sorted(work.blocked_via_cells_at_rung(r)) for r in range(n)]
    before = snap()
    nk.attach(pcb, [nk._validate(_keepout(D8_BOX), 0, "t")])
    coord = GridCoord(cfg.grid_step)
    centre = coord.to_grid(151.4, 77.45)
    band = coord.to_grid(150.6 - 0.55, 77.45)
    with nk.stamp(work, pcb, (d8,), cfg):
        assert all(work.is_via_blocked_rung(*centre, r) for r in range(n))
        assert not work.is_via_blocked_rung(*band, 0)
        assert work.is_via_blocked_rung(*band, n - 1), "per-net rung stamped at the run's via"
    assert snap() == before, "stamp must leave every rung as it found it"


def test_route_respects_net_filter():
    nets = ["Net-(D8-A)", "Net-(D9-A)"]
    ctrl, _ = _route(nets)
    inside_ctrl, _ = _inside(ctrl, "Net-(D8-A)", [D8_BOX])
    assert inside_ctrl > 0, "control: without the flag D8 must cut through its box"

    path = _write_keepouts([_keepout(D8_BOX, name="a", exemptNets=["Net-(D9-A)"]),
                            _keepout(D9_BOX, name="b", exemptNets=["Net-(D9-A)"])])
    out, log = _route(nets, path)
    assert "Net-filtered keep-outs: 2 region(s)" in log
    d8_in, d8_has = _inside(out, "Net-(D8-A)", [D8_BOX, D9_BOX])
    d9_in, _ = _inside(out, "Net-(D9-A)", [D9_BOX])
    assert d8_has and _connected(out, ["Net-(D8-A)"])
    assert d8_in == 0, f"bound net entered the keep-out ({d8_in} cells)"
    assert d9_in > 0, "exempt net must still route through its region"


TESTS = [
    test_binds_applies_and_exempt,
    test_loader_refuses_bad_entries,
    test_stamp_balances_and_skips_exempt_net,
    test_stamp_reaches_every_via_rung_at_its_own_via,
    test_route_respects_net_filter,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
        except Skip as e:
            print(f"  SKIP: {t.__name__}: {e}")
            continue
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {t.__name__}: {e}")
            continue
        print(f"  PASS: {t.__name__}")
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
