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
    # A vertex that is not a number is a polygon error, not a TypeError the
    # GUI's refusal (which catches ValueError) would let through.
    assert _raises(lambda: nk.load_net_keepouts(
        _write_keepouts([_keepout([[0, 0], [1, "a"], [2, None]])])), "polygon")
    # bool("false") is True: a quoted flag would read as its opposite.
    assert _raises(lambda: nk.load_net_keepouts(
        _write_keepouts([_keepout(D8_BOX, tracks="false")])), "true or false")
    missing = os.path.join(tempfile.mkdtemp(prefix="nko_"), "missing.json")
    assert _raises(lambda: nk.load_net_keepouts(missing), "cannot read")
    fd, bad_json = tempfile.mkstemp(suffix=".json", prefix="nko_")
    with os.fdopen(fd, "w") as f:
        f.write("[{")
    assert _raises(lambda: nk.load_net_keepouts(bad_json), "not valid JSON")


def test_attach_none_clears_a_previous_run():
    """The GUI hands every run the same PCBData: a run with the field emptied
    must not keep stamping the previous run's regions."""
    pcb = parse_kicad_pcb(BOARD)
    d8 = next(i for i, n in pcb.nets.items() if n.name == "Net-(D8-A)")
    cfg = GridRouteConfig()
    cfg.layers = ["F.Cu", "B.Cu"]
    nk.attach(pcb, [nk._validate(_keepout(D8_BOX), 0, "t")])
    m = _Map()
    with nk.stamp(m, pcb, (d8,), cfg):
        assert m.cells, "control: the attached region stamps"
    nk.attach(pcb, None)
    with nk.stamp(m, pcb, (d8,), cfg):
        assert not m.cells and not m.vias, "a cleared run must stamp nothing"


def test_window_cells_do_not_leak_to_the_whole_board():
    """A rescue window is a shallow copy sharing the whole board's cell cache;
    its clipped raster must not be served to a whole-board route."""
    from plane_pad_tap import make_local_window
    pcb = parse_kicad_pcb(BOARD)
    cfg = GridRouteConfig()
    cfg.layers = ["F.Cu", "B.Cu"]
    nk.attach(pcb, [nk._validate(_keepout(D8_BOX), 0, "t")])
    # A window over the box's left edge only, computed FIRST.
    window = make_local_window(pcb, 150.6, 77.45, 0.6)
    assert window.__dict__.get('_net_keepout_cells') is pcb._net_keepout_cells, (
        "precondition: the window shares the board's cache dict")
    w_cells, _ = nk._cells_for(window, (0,), cfg)
    b_cells, _ = nk._cells_for(pcb, (0,), cfg)
    assert 0 < len(w_cells) < len(b_cells), (len(w_cells), len(b_cells))


def test_cli_refuses_a_bad_file_before_routing():
    from run_utils import check
    bad = _write_keepouts([_keepout(D8_BOX, layer=["F.Cu"])])
    out = os.path.join(tempfile.mkdtemp(prefix="nko_"), "out.kicad_pcb")
    for tool in ("route.py", "route_diff.py"):
        check([sys.executable, f"py_router/{tool}", BOARD, out,
               "--nets", "Net-(D8-A)", "--net-keepouts", bad],
              refuse="--net-keepouts: ", code=2)
    assert not os.path.exists(out), "a refused run must write nothing"


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


def test_diff_pair_paths_stamp_their_maps():
    """The diff-pair A* paths outside the standard pair route see the region
    too: the terminal connectors' base map, the hybrid's coupled middle (a
    pair: bound if either member is) and its single-net legs (bound by their
    own net). Each body is replaced by a probe that reads the maps it was
    handed; every map is clean again afterwards."""
    from types import SimpleNamespace
    import diff_pair_routing as dpr
    pcb = parse_kicad_pcb(BOARD)
    nk.attach(pcb, [nk._validate(_keepout(D8_BOX, exemptNets=["Net-(D9-A)"]), 0, "t")])
    d8 = next(i for i, n in pcb.nets.items() if n.name == "Net-(D8-A)")
    d9 = next(i for i, n in pcb.nets.items() if n.name == "Net-(D9-A)")
    cfg = GridRouteConfig()
    cfg.layers = ["F.Cu", "B.Cu"]
    seen = {}

    def probe(tag, *maps):
        def body(*a, **k):
            seen[tag] = tuple(bool(m.cells) for m in maps)
        return body

    leg_args = (None,) * 7    # layer_names .. pair_vias: the probe reads none
    work, base = _Map(), _Map()
    real = (dpr._route_hybrid_leg.__wrapped__,
            dpr._route_direct_coupled_middle.__wrapped__,
            dpr._route_diff_pair_with_obstacles_impl)
    try:
        dpr._route_hybrid_leg.__wrapped__ = probe('leg', work)
        dpr._route_hybrid_leg(pcb, d8, cfg, work, *leg_args)
        assert seen.pop('leg') == (True,), "a bound net's leg map is stamped"
        dpr._route_hybrid_leg(pcb, d9, cfg, work, *leg_args)
        assert seen.pop('leg') == (False,), "an exempt net's leg is not"

        mixed = SimpleNamespace(p_net_id=d9, n_net_id=d8)
        dpr._route_direct_coupled_middle.__wrapped__ = probe('mid', work)
        dpr._route_direct_coupled_middle(pcb, mixed, cfg, work, None)
        assert seen.pop('mid') == (True,), "pair bound through one member"

        dpr._route_diff_pair_with_obstacles_impl = probe('pair', work, base)
        dpr.route_diff_pair_with_obstacles(pcb, mixed, cfg, work, base, None)
        assert seen.pop('pair') == (True, True), (
            "the pair map and the connectors' base map are both stamped")
    finally:
        (dpr._route_hybrid_leg.__wrapped__,
         dpr._route_direct_coupled_middle.__wrapped__,
         dpr._route_diff_pair_with_obstacles_impl) = real
    assert not (work.cells or work.vias or base.cells or base.vias), (
        "every stamp is removed on exit")


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


def _copper_gap(board, net_name, poly):
    """Smallest distance (mm) from `net_name`'s track copper to polygon
    `poly`'s outline; negative when the copper overlaps the polygon. An
    independent measure: plain segment geometry, no grid, no router code."""
    from geometry_utils import segment_to_segment_distance
    from obstacle_map import point_in_polygon
    pcb = parse_kicad_pcb(board)
    nid = next(i for i, n in pcb.nets.items() if n.name == net_name)
    ring = [tuple(v) for v in poly]
    edges = list(zip(ring, ring[1:] + ring[:1]))
    gap = float('inf')
    for s in pcb.segments:
        if s.net_id != nid:
            continue
        if (point_in_polygon(s.start_x, s.start_y, ring)
                or point_in_polygon(s.end_x, s.end_y, ring)):
            return -1.0
        d = min(segment_to_segment_distance(s.start_x, s.start_y, s.end_x, s.end_y,
                                            a[0], a[1], b[0], b[1]) for a, b in edges)
        gap = min(gap, d - s.width / 2)
    return gap


def test_route_diff_member_copper_stays_out():
    """route_diff, a region binding ONE member of /CLK: the pair detours as a
    pair, and neither member's COPPER touches the region. Stamped at the
    plain run geometry the centreline kept its band while /CLK+, (w+gap)/2
    to the side, ended with its copper 0.5 um over the region's edge."""
    board = os.path.join(ROOT, "kicad_files", "lvds_converter_dualclk.kicad_pcb")
    region = box(86.5, 58.0, 88.5, 60.2)
    path = _write_keepouts([_keepout(region, name="mid", appliesToNets=["/CLK+"])])
    env = dict(os.environ, MSYS2_ARG_CONV_EXCL='*')
    gaps = {}
    td = tempfile.mkdtemp(prefix="nko_")
    for arm, extra in (("off", []), ("on", ["--net-keepouts", path])):
        out = os.path.join(td, f"{arm}.kicad_pcb")
        r = subprocess.run([sys.executable, "-X", "utf8", "py_router/route_diff.py",
                            board, out, "--nets", "/CLK+", "/CLK-"] + extra,
                           cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=900)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        summary = json.loads(r.stdout.split("JSON_SUMMARY: ", 1)[1].splitlines()[0])
        assert summary["routed_diff_pairs"] == ["/CLK"], (arm, summary["routed_diff_pairs"])
        gaps[arm] = [_copper_gap(out, n, region) for n in ("/CLK+", "/CLK-")]
    assert min(gaps["off"]) < 0, f"control: the pair must cross the region {gaps}"
    assert min(gaps["on"]) > 0.05, f"a member's copper reached the region {gaps}"


TESTS = [
    test_binds_applies_and_exempt,
    test_loader_refuses_bad_entries,
    test_attach_none_clears_a_previous_run,
    test_window_cells_do_not_leak_to_the_whole_board,
    test_cli_refuses_a_bad_file_before_routing,
    test_stamp_balances_and_skips_exempt_net,
    test_diff_pair_paths_stamp_their_maps,
    test_stamp_reaches_every_via_rung_at_its_own_via,
    test_route_respects_net_filter,
    test_route_diff_member_copper_stays_out,
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
        except Exception as e:                       # noqa: BLE001
            failed += 1
            print(f"  ERROR: {t.__name__}: {type(e).__name__}: {e}")
            continue
        print(f"  PASS: {t.__name__}")
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
