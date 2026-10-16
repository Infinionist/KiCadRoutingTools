#!/usr/bin/env python3
"""The quench's scaling machinery answers what the full computation answers.

The polish prices a candidate pose by the airwire length and crossings of the
nets the part touches. Four pieces of `placement/quench.py` make that cost
grow with what is NEAR a part instead of with the board, and each is held
here to the computation it replaces:

1. `_box_pairs_grid` -- the (a, b) airwire pairs whose boxes meet, found
   through a uniform grid -- returns `np.nonzero` of the full box matrix,
   element for element and in the same order (so weighted sums add the same
   values in the same order).
2. `_mst_edges_sparse` -- Prim over the Delaunay triangulation's edges --
   returns `compute_mst_edges`' tree exactly: the same edges, in the same
   order, by the same floats, ties included (full pad grids, coincident
   pads); None where it cannot (collinear pads), and the caller falls back.
3. `_TreeJoin` -- a net's tree with one part's pads joined to the tree of
   its other pads -- gives the same answer on its one-pad fast path (range
   queries on the Kruskal reconstruction tree) as on its Kruskal loop,
   on random pad sets full of ties and on glasgow's own nets, and a tree as
   short as Prim's.
4. `_PosePricer` -- the pose's cost from the edges it changes -- equals a
   full recount of the same trees (length, crossings with the rest of the
   board, crossings among the part's own nets), weighted and unweighted.
   Among equally short trees it may pick another than Prim's, so it is held
   to a recount of ITS trees, and to Prim's length.

`airwires_excluding`, which now joins cached per-net arrays, is held to the
list it used to build.

    python3 tests/test_quench_pricer.py
"""
import math
import os
import random
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from connectivity import compute_mst_edges  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import quench as Q  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'glasgow_revC.kicad_pcb')
FAILS = []


def check(ok, what):
    if not ok:
        FAILS.append(what)
        print(f"  FAIL {what}")


def full_pairs(a, b):
    m = 1e-6
    ax0 = np.minimum(a[:, 0], a[:, 2])[:, None] - m
    ax1 = np.maximum(a[:, 0], a[:, 2])[:, None] + m
    ay0 = np.minimum(a[:, 1], a[:, 3])[:, None] - m
    ay1 = np.maximum(a[:, 1], a[:, 3])[:, None] + m
    bx0 = np.minimum(b[:, 0], b[:, 2])[None, :]
    bx1 = np.maximum(b[:, 0], b[:, 2])[None, :]
    by0 = np.minimum(b[:, 1], b[:, 3])[None, :]
    by1 = np.maximum(b[:, 1], b[:, 3])[None, :]
    return np.nonzero((ax1 >= bx0) & (ax0 <= bx1) & (ay1 >= by0) & (ay0 <= by1))


def grid_pairs(a, b):
    m = 1e-6
    return Q._box_pairs_grid(
        np.minimum(a[:, 0], a[:, 2]) - m, np.minimum(a[:, 1], a[:, 3]) - m,
        np.maximum(a[:, 0], a[:, 2]) + m, np.maximum(a[:, 1], a[:, 3]) + m,
        np.minimum(b[:, 0], b[:, 2]), np.minimum(b[:, 1], b[:, 3]),
        np.maximum(b[:, 0], b[:, 2]), np.maximum(b[:, 1], b[:, 3]))


def segments(rng, n, long, snap):
    x = rng.uniform(0, 80, n)
    y = rng.uniform(0, 50, n)
    L = rng.exponential(30 if long else 3, n)
    t = rng.uniform(0, 2 * np.pi, n)
    a = np.column_stack([x, y, x + L * np.cos(t), y + L * np.sin(t),
                         rng.integers(0, 40, n)])
    if snap:
        a[:, :4] = np.round(a[:, :4] / snap) * snap
    return a


def test_grid_pairs():
    rng = np.random.default_rng(7)
    bad = 0
    for trial in range(120):
        a = segments(rng, int(rng.integers(1, 300)), trial % 3 == 0,
                     [None, 0.5, 1.27][trial % 3])
        b = segments(rng, int(rng.integers(1, 900)), trial % 2 == 0,
                     [None, 0.5][trial % 2])
        if trial % 7 == 0:
            b[: len(b) // 3] = b[0]                  # many identical boxes
        if trial % 11 == 0:
            a[:, 2], a[:, 3] = a[:, 0], a[:, 1]      # zero-length segments
        if trial % 13 == 0:
            b[0, 2] = np.inf                         # back to the matrix
        f, g = full_pairs(a, b), grid_pairs(a, b)
        if not (np.array_equal(f[0], g[0]) and np.array_equal(f[1], g[1])):
            bad += 1
    check(bad == 0, f"grid pairing differs from the box matrix in {bad}/120 cases")
    # The crossing counter itself, both branches against each other.
    a = segments(rng, 400, False, 0.5)
    b = segments(rng, 900, True, None)
    w = np.linspace(1.0, 3.0, 64)
    big = Q._count_crossings_np(a, b, w)
    save = Q._GRID_MIN_PAIRS
    try:
        Q._GRID_MIN_PAIRS = 1 << 62
        ref = Q._count_crossings_np(a, b, w)
    finally:
        Q._GRID_MIN_PAIRS = save
    check(big == ref, f"_count_crossings_np grid {big} != matrix {ref}")
    check(ref[0] > 0, "the crossing check counted nothing (vacuous)")
    print("  grid pairing: 120 random cases + the crossing counter")


def test_sparse_prim(pcb):
    rng = np.random.default_rng(11)
    verdict = {}
    for trial in range(150):
        n = int(rng.integers(3, 700))
        kind = trial % 5
        if kind == 0:
            pts = [tuple(p) for p in rng.uniform(0, 100, (n, 2))]
        elif kind == 1:      # a full 1.27 mm grid: ties everywhere
            pts = [(float(i % 30) * 1.27, float(i // 30) * 1.27)
                   for i in rng.permutation(n)]
        elif kind == 2:      # snapped to 0.5 mm: ties and coincident pads
            pts = [tuple(np.round(p / 0.5) * 0.5)
                   for p in rng.uniform(0, 30, (n, 2))]
        elif kind == 3:      # collinear: no triangulation
            pts = [(float(x), 5.0) for x in rng.uniform(0, 50, n)]
        else:                # every pad doubled
            base = rng.uniform(0, 60, (n // 2 + 2, 2))
            pts = [tuple(base[i % len(base)]) for i in range(n)]
        s = Q._mst_edges_sparse(pts)
        v = 'fallback' if s is None else (
            'same' if s == compute_mst_edges(pts, use_manhattan=False) else 'DIFF')
        verdict[(kind, v)] = verdict.get((kind, v), 0) + 1
    check(not any(v == 'DIFF' for _, v in verdict),
          f"sparse Prim differs from compute_mst_edges: {verdict}")
    check(verdict.get((3, 'fallback')) == 30, "collinear pads did not fall back")
    check(sum(c for (k, v), c in verdict.items() if v == 'same') >= 110,
          f"too few sparse trees were compared: {verdict}")
    by = {}
    for fp in pcb.footprints.values():
        for pd in fp.pads:
            if pd.net_id:
                by.setdefault(pd.net_id, []).append((pd.global_x, pd.global_y))
    big = [p for p in by.values() if len(p) >= 30]
    check(bool(big), "glasgow has no net of 30+ pads (vacuous)")
    for pts in big:
        check(Q._mst_edges_sparse(pts) == compute_mst_edges(pts),
              f"sparse Prim differs on a real net of {len(pts)} pads")
    print(f"  sparse Prim: 150 random point sets, {len(big)} real nets")


def join_agrees(j, q):
    """`_TreeJoin._join_one` (called directly, whatever `join` would route
    to) and the Kruskal loop give the same tree at moved pad `q`."""
    dx = j.F[None, :, 0] - q[:, 0, None]
    dy = j.F[None, :, 1] - q[:, 1, None]
    D = np.sqrt(dx * dx + dy * dy)
    thr = np.minimum.reduceat(np.maximum(j._adj_w[None, :], D[:, j._adj_far]),
                              j._adj_start, axis=1)
    keep = D <= thr
    d1, r1 = j._join_one(q[0], D[0], keep[0])
    d2, r2 = j._join_kruskal(q, D, keep)
    return sorted(d1.tolist()) == sorted(d2.tolist()) and sorted(r1) == sorted(r2)


def test_join_random():
    """Random fixed pads -- uniform, a full 1.27 mm grid, a 0.5 mm snap, every
    pad doubled -- and moved pads on and off their lattice (ties)."""
    rng = np.random.default_rng(5)
    bad = n = 0
    for trial in range(80):
        m = int(rng.integers(2, 900))
        kind = trial % 4
        if kind == 0:
            F = rng.uniform(0, 100, (m, 2))
        elif kind == 1:
            F = np.array([[float(i % 40) * 1.27, float(i // 40) * 1.27]
                          for i in rng.permutation(m)])
        elif kind == 2:
            F = np.round(rng.uniform(0, 40, (m, 2)) / 0.5) * 0.5
        else:
            F = np.repeat(rng.uniform(0, 60, (m // 2 + 1, 2)), 2, axis=0)[:m]
        j = Q._TreeJoin([tuple(p) for p in F], 7, 1)
        if not j.ok or j.m < 2:
            continue
        for _ in range(6):
            if trial % 3 == 0:
                q = F[rng.integers(0, len(F))] + rng.choice([0, 0.635, 1.27], 2)
            else:
                q = rng.uniform(-5, 105, 2)
            n += 1
            bad += not join_agrees(j, np.asarray([q], dtype=float))
    check(bad == 0 and n >= 400,
          f"one-pad join differs from Kruskal in {bad} of {n} random joins")
    print(f"  one-pad join: {n} random joins (grids, snaps, doubled pads)")


def _state(pcb, weighted=False):
    st = Q.QuenchState(pcb, BOARD, 0.2, 0.3, 30.0, 0.3, 0.15, 1.0, 0.5, 1.0,
                       0.5, 0.3)
    if weighted:
        rng = random.Random(5)
        st.net_weights = {n: rng.choice([1.0, 1.5, 2.0, 3.0]) for n in st.net_refs}
        st._net_w = np.ones(max(st.net_airwires) + 1)
        for n, w in st.net_weights.items():
            st._net_w[n] = w
    return st


def test_pricer(pcb, weighted):
    st = _state(pcb, weighted)
    rng = random.Random(1)
    refs = [r for r, p in st.parts.items() if not p.locked and p.nets]
    rng.shuffle(refs)
    priced = poses = joins = 0
    for ref in refs[:50]:
        part = st.parts[ref]
        involved = set(part.nets)
        other = st.airwires_excluding(involved)
        pr = Q._PosePricer.for_part(st, ref, involved, other)
        if pr is None:
            continue
        priced += 1
        for n, j in pr.joins.items():
            if j.k != 1 or j.m < 2:
                continue
            for _ in range(4):
                q = np.array([[part.x + rng.uniform(-4, 4),
                               part.y + rng.uniform(-4, 4)]])
                joins += 1
                check(join_agrees(j, q),
                      f"{ref} net {n}: one-pad join differs from Kruskal")
        for _ in range(5):
            x = part.x + rng.choice([-2, -1, 0, 1, 2])
            y = part.y + rng.choice([-2, -1, 0, 1, 2])
            rot = rng.choice([0, 90, 180, 270])
            got = pr.net_cost(x, y, rot)
            D, R = pr.tree_rows(x, y, rot)
            keep = np.ones(len(pr.T), dtype=bool)
            keep[R] = False
            own = np.vstack([pr.T[keep], D]) if len(D) else pr.T[keep]
            _, w_out = Q._count_crossings_np(own, other, st._net_w)
            _, w_in = Q._count_crossings_within(own, st._net_w)
            want = (st.length_weight * st._weighted_length(own)
                    + st.crossing_penalty * (w_out + w_in))
            poses += 1
            check(abs(got - want) <= 1e-7 * max(1.0, abs(want)),
                  f"{ref} at ({x}, {y}, {rot}): pricer {got} != recount {want}")
            pads = part.pad_globals(x, y, rot)
            prim = sum(math.hypot(a[2] - a[0], a[3] - a[1]) for n in involved
                       for a in st._build_net_airwires(n, override_ref=ref,
                                                       override_pads=pads))
            mine = float(np.hypot(own[:, 2] - own[:, 0], own[:, 3] - own[:, 1]).sum())
            check(abs(prim - mine) < 1e-6,
                  f"{ref}: pricer tree {mine} mm, Prim's {prim} mm")
    check(priced >= 20 and poses >= 100 and joins >= 40,
          f"too little was priced to mean anything: {priced} parts, "
          f"{poses} poses, {joins} joins")
    print(f"  pricer ({'weighted' if weighted else 'unweighted'}): {priced} "
          f"parts, {poses} poses, {joins} joins")


def test_airwires_excluding(pcb):
    st = _state(pcb)
    nets = set(list(st.net_refs)[::3])
    old = []
    for net_id, lst in st.net_airwires.items():
        if net_id not in nets:
            old.extend(lst)
    check(np.array_equal(st.airwires_excluding(nets), Q._aw_array(old)),
          "airwires_excluding differs from the list it replaced")
    ref = next(r for r, p in st.parts.items() if not p.locked and p.nets)
    p = st.parts[ref]
    st.apply_move(ref, p.x + 1.0, p.y, p.rot)
    old = []
    for net_id, lst in st.net_airwires.items():
        if net_id not in nets:
            old.extend(lst)
    check(np.array_equal(st.airwires_excluding(nets), Q._aw_array(old)),
          "airwires_excluding is stale after a move")
    print("  airwires_excluding: before and after a move")


def main():
    pcb = parse_kicad_pcb(BOARD)
    test_grid_pairs()
    test_sparse_prim(pcb)
    test_join_random()
    test_pricer(pcb, weighted=False)
    test_pricer(pcb, weighted=True)
    test_airwires_excluding(pcb)
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
