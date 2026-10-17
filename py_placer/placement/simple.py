"""The simple placement path's cost model: groups, one objective.

There are no regions. A GROUP is a set of parts optimised together and kept
together; the shape it takes is whatever the objective and the free space
make of it. Only parts the file locks stay put. `simple_anneal.py` lowers
this cost by simulated annealing; `place_simple.py` is the CLI.

    cost = sum over signal nets of the Manhattan MST of their pads
         + rail_weight / ground_weight x each rail's MST (below)
         + crossing_weight x crossings among the signal trees' edges
         + claim_weight x each claimed pad's distance to its partner's pad
         + decap_weight x each decap's distance to the nearest supply pin
           of its rail on an IC of its own group (any IC's when its group
           has none)
         + group_weight x each group's spanning tree over its members'
           courtyards, an edge priced by the gap between the two courtyards
           -- nothing while the group is one contiguous piece, whatever its
           shape
         + the halo: each pair of parts sharing a side and NO signal net,
           closer than the sum of their halos (base + coef x sqrt(pins)),
           pays halo_weight x the shortfall squared (QuenchState's own
           `_halo_pair_penalty`) -- strong when too close, nothing once far
           enough. A wired pair (a decap at its pin, a crystal at its IC, a
           series resistor) is meant to touch and pays nothing.
         + pad_weight x each tree edge's crossings of foreign pads: a pad of
           a part other than the edge's two ends, grown by the clearance, a
           through-hole pad counting twice (it blocks both layers)
         + congestion_weight x the sum over a grid of each cell's routing
           overflow squared: every net spreads its tree's length evenly over
           its bounding box (the demand, mm of track); a cell holds its area
           free of pad copper, per copper layer, over the track pitch (the
           supply). Ground, poured as a plane, is left out.

Rails -- ground and nets reaching more than a quarter of the parts
(`grouping.rail_net_ids`) -- carry a fraction of a signal's cost per mm of
their tree: a supply `rail_weight` (wide or poured, but not nothing, or
nothing pulls a regulator toward the parts it feeds), ground `ground_weight`
(a plane: each pad only drops a via, so almost nothing). They take no part
in crossings.

A move is priced LOCALLY: `local` re-prices only the moved parts and the
nets they are on, against the committed state near them (`SpatialIndex`),
the "before" read from cached breakdowns and the "after" held as pending
until the caller `commit`s or `restore`s -- so a move costs about the same
on a board of any size. `total` prices everything from scratch and is the
reference the incremental pricing is checked against.
"""
from __future__ import annotations

import math
import os
import sys as _sys
from typing import Dict, List, Sequence, Set, Tuple

import numpy as np

_sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..',
                                                 'rust_router')))
import grid_router  # noqa: E402  the kernels: mst_l1, seg_rect_hits, seg_crossings (0.23.3)

ROTS = (0.0, 90.0, 180.0, 270.0)


# --------------------------------------------------------------------------
# geometry

def mst_l1(pts: Sequence[Tuple[float, float]]):
    """`(length, edges)` of the Manhattan minimum spanning tree of `pts`
    (Prim from point 0, the lowest index among equal keys, a key lowered
    only by a strictly shorter edge); edges as `(x0, y0, x1, y1)`. The Rust
    kernel `grid_router.mst_l1` from three points; `_mst_l1_py` is its
    reference."""
    n = len(pts)
    if n < 2:
        return 0.0, []
    if n == 2:
        (ax, ay), (bx, by) = pts
        return abs(bx - ax) + abs(by - ay), [(ax, ay, bx, by)]
    total, ij = grid_router.mst_l1(np.fromiter((p[0] for p in pts), float, n),
                                   np.fromiter((p[1] for p in pts), float, n))
    return total, [(pts[i][0], pts[i][1], pts[j][0], pts[j][1]) for i, j in ij]


def _mst_l1_py(pts: Sequence[Tuple[float, float]]):
    """`mst_l1` in Python: plain loops up to 16 points, a distance matrix
    above -- the same tree either way."""
    n = len(pts)
    if n < 2:
        return 0.0, []
    if n == 2:      # most nets: no tree to search
        (ax, ay), (bx, by) = pts
        return abs(bx - ax) + abs(by - ay), [(ax, ay, bx, by)]
    if n <= 16:
        inf = float('inf')
        x0, y0 = pts[0]
        best = [abs(x0 - x) + abs(y0 - y) for x, y in pts]
        frm = [0] * n
        done = [False] * n
        done[0] = True
        best[0] = inf
        total, edges = 0.0, []
        for _ in range(n - 1):
            k, bk = -1, inf
            for i in range(n):
                if not done[i] and best[i] < bk:
                    k, bk = i, best[i]
            done[k] = True
            total += bk
            a = pts[frm[k]]
            px, py = pts[k]
            edges.append((a[0], a[1], px, py))
            for i in range(n):
                if not done[i]:
                    d = abs(px - pts[i][0]) + abs(py - pts[i][1])
                    if d < best[i]:
                        best[i], frm[i] = d, k
        return total, edges
    P = np.asarray(pts, dtype=float)
    D = np.abs(P[:, None, :] - P[None, :, :]).sum(axis=2)
    best = D[0].copy()
    frm = np.zeros(n, dtype=int)
    done = np.zeros(n, dtype=bool)
    done[0] = True
    best[0] = np.inf
    total, edges = 0.0, []
    for _ in range(n - 1):
        k = int(np.argmin(best))
        total += float(best[k])
        a = P[frm[k]]
        edges.append((a[0], a[1], P[k][0], P[k][1]))
        done[k] = True
        best[k] = np.inf
        row = D[k]
        upd = (~done) & (row < best)
        best[upd] = row[upd]
        frm[upd] = k
    return total, edges


def rect_gap(a, b) -> float:
    """Manhattan gap between two rects `(x0, y0, x1, y1)`; 0 when they touch
    or overlap."""
    return (max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
            + max(0.0, max(a[1], b[1]) - min(a[3], b[3])))


def gap_tree(rects) -> Tuple[float, List[Tuple[int, int, float]]]:
    """`(length, edges)` of the minimum spanning tree over `rects`, each edge
    weighted by `rect_gap`; edges as `(i, j, gap)`."""
    n = len(rects)
    if n < 2:
        return 0.0, []
    done = [False] * n
    best = [rect_gap(rects[0], r) for r in rects]
    frm = [0] * n
    done[0] = True
    total, edges = 0.0, []
    for _ in range(n - 1):
        k = min((i for i in range(n) if not done[i]), key=lambda i: best[i])
        done[k] = True
        total += best[k]
        edges.append((frm[k], k, best[k]))
        for i in range(n):
            if not done[i]:
                g = rect_gap(rects[k], rects[i])
                if g < best[i]:
                    best[i], frm[i] = g, k
    return total, edges


def _orient(ax, ay, bx, by, cx, cy):
    return np.sign((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))


def crossings(A: np.ndarray, B: np.ndarray, same: bool = False) -> int:
    """Proper crossings between segment arrays A (n x 4) and B (m x 4);
    `same` counts each pair of one array once (i < j). The Rust kernel
    `grid_router.seg_crossings`; `_crossings_py` is its reference."""
    if len(A) == 0 or len(B) == 0:
        return 0
    A = np.ascontiguousarray(A, dtype=float)
    if same:
        return int(grid_router.seg_crossings(A))
    return int(grid_router.seg_crossings(A, np.ascontiguousarray(B, dtype=float)))


def _crossings_py(A: np.ndarray, B: np.ndarray, same: bool = False) -> int:
    """`crossings` in numpy."""
    if len(A) == 0 or len(B) == 0:
        return 0
    a0x, a0y, a1x, a1y = (A[:, None, i] for i in range(4))
    b0x, b0y, b1x, b1y = (B[None, :, i] for i in range(4))
    o1 = _orient(a0x, a0y, a1x, a1y, b0x, b0y)
    o2 = _orient(a0x, a0y, a1x, a1y, b1x, b1y)
    o3 = _orient(b0x, b0y, b1x, b1y, a0x, a0y)
    o4 = _orient(b0x, b0y, b1x, b1y, a1x, a1y)
    m = (o1 * o2 < 0) & (o3 * o4 < 0)
    if same:
        m = np.triu(m, 1)
    return int(m.sum())


# --------------------------------------------------------------------------
# a spatial hash, so a move is priced against what is near it

class SpatialIndex:
    """Keys with rectangles, bucketed on a square grid: `query(rect)` gives
    the keys whose rectangles may touch `rect` (their buckets do), in time
    that depends on what is near, not on how much there is."""

    def __init__(self, cell: float = 2.0):
        self.cell = cell
        self.buckets: Dict[Tuple[int, int], Set] = {}
        self.where: Dict = {}

    def _cells(self, r):
        c = self.cell
        i0, i1 = int(math.floor(r[0] / c)), int(math.floor(r[2] / c))
        j0, j1 = int(math.floor(r[1] / c)), int(math.floor(r[3] / c))
        return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]

    def insert(self, key, rect):
        cells = self._cells(rect)
        self.where[key] = cells
        for k in cells:
            self.buckets.setdefault(k, set()).add(key)

    def remove(self, key):
        for k in self.where.pop(key, ()):
            b = self.buckets.get(k)
            if b is not None:
                b.discard(key)

    def query_many(self, rects) -> Set:
        """`query` over several rects at once: each bucket merged once."""
        c = self.cell
        cells = set()
        for r in rects:
            i0, i1 = int(math.floor(r[0] / c)), int(math.floor(r[2] / c))
            j0, j1 = int(math.floor(r[1] / c)), int(math.floor(r[3] / c))
            if i0 == i1 and j0 == j1:
                cells.add((i0, j0))
            else:
                cells.update((i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1))
        out: Set = set()
        get = self.buckets.get
        for k in cells:
            b = get(k)
            if b:
                out |= b
        return out

    def query(self, rect) -> Set:
        c = self.cell
        i0, i1 = int(math.floor(rect[0] / c)), int(math.floor(rect[2] / c))
        j0, j1 = int(math.floor(rect[1] / c)), int(math.floor(rect[3] / c))
        if i0 == i1 and j0 == j1:       # most queries: one bucket
            return set(self.buckets.get((i0, j0), ()))
        out: Set = set()
        get = self.buckets.get
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                b = get((i, j))
                if b:
                    out |= b
        return out


def _bbox(e):
    return (min(e[0], e[2]), min(e[1], e[3]), max(e[0], e[2]), max(e[1], e[3]))


# --------------------------------------------------------------------------
# the cost model

class Model:
    """The objective over a QuenchState's live poses, priced net by net so a
    move re-prices only what it touches."""

    def __init__(self, state, pcb, *, rails: Set[int], claims: List,
                 decaps: List[Tuple[str, int]], supply: Dict[int, List[Tuple[str, str]]],
                 decap_pins: Dict[str, List[Tuple[str, str]]],
                 groups: List[Tuple[str, List[str]]],
                 crossing_weight: float, claim_weight: float, decap_weight: float,
                 group_weight: float, congestion_weight: float = 0.0,
                 track_pitch: float = 0.33, cell: float = 0.5,
                 congestion_nets=None, rail_weight: float = 0.0,
                 ground_nets=(), ground_weight: float = 0.0, pad_weight: float = 0.0):
        self.state, self.pcb = state, pcb
        self.cw, self.kw, self.dw, self.gw = (crossing_weight, claim_weight,
                                              decap_weight, group_weight)
        self.nets = sorted(n for n, refs in state.net_refs.items()
                           if n and n not in rails and len(refs) >= 1)
        self.signal = set(self.nets)
        self.rw, self.grw = rail_weight, ground_weight
        self.pw_pad = pad_weight
        self.ground = set(ground_nets)
        self.rnets = sorted(n for n in rails if n and state.net_refs.get(n)
                            and (ground_weight if n in self.ground else rail_weight) > 0)
        self.qw, self.pitch, self.cell = congestion_weight, track_pitch, cell
        # the nets that need channels: every signal net and every rail but
        # the ones poured as planes (the caller's `congestion_nets`)
        self.cnets = sorted(congestion_nets if congestion_nets is not None else self.nets)
        b = pcb.board_info.board_bounds
        self._gx0, self._gy0 = b[0], b[1]
        self._nx = max(1, int(math.ceil((b[2] - b[0]) / cell)))
        self._ny = max(1, int(math.ceil((b[3] - b[1]) / cell)))
        self.layers = list(pcb.board_info.copper_layers or ['F.Cu', 'B.Cu'])
        self.claims = claims            # [(subject, partner, [(spad, ppads)])]
        self.decaps = decaps            # [(cap ref, rail net)]
        self.supply = supply            # rail net -> [(ic ref, pad number)]
        self.decap_pins = decap_pins    # cap -> the supply pins it serves
        self.groups = groups            # [(name, [members])], two or more each
        self.net_len: Dict[int, float] = {}
        self.net_edges: Dict[int, np.ndarray] = {}
        self.reprice_all()

    def reprice_all(self):
        """Every priced net's tree, from the live poses."""
        for n in self.nets:
            self._price_net(n)
        for n in self.rnets:
            self._price_net(n)

    def wire_weight(self, n):
        if n in self.signal:
            return 1.0
        return self.grw if n in self.ground else self.rw

    # -- pads ---------------------------------------------------------------
    def _pads(self, ref):
        return self.state.parts[ref].pad_globals()

    def _net_points(self, n):
        pts = []
        for r in self.state.net_refs.get(n, ()):
            pts += [(x, y) for x, y, nn in self._pads(r) if nn == n]
        return pts

    def _pad_xy(self, ref, num):
        part = self.state.parts[ref]
        fp = self.pcb.footprints[ref]
        # pad_globals keeps pad order of the footprint's connected pads
        pg = part.pad_globals()
        conn = [p for p in fp.pads if p.net_id > 0]
        return [(pg[i][0], pg[i][1]) for i, p in enumerate(conn)
                if str(p.pad_number) == str(num) and i < len(pg)]

    # -- terms --------------------------------------------------------------
    def _pid(self, ref):
        ids = getattr(self, '_pids', None)
        if ids is None:
            ids = self._pids = {r: i for i, r in enumerate(sorted(self.state.parts))}
        return ids[ref]

    def _price_net(self, n):
        """Net `n`'s tree: its length, its edges, and each edge's end parts
        (as `_pid` ids, for the foreign-pad test)."""
        st = self.state
        where = {}
        for r in st.net_refs.get(n, ()):
            for x, y, nn in st.parts[r].pad_globals():
                if nn == n:
                    where.setdefault((x, y), r)
        L, E = mst_l1(sorted(where))
        self.net_len[n] = L
        self.net_edges[n] = np.asarray(E, dtype=float).reshape(-1, 4)
        ends = getattr(self, 'net_ends', None)
        if ends is None:
            ends = self.net_ends = {}
        ends[n] = np.asarray([(self._pid(where[(e[0], e[1])]), self._pid(where[(e[2], e[3])]))
                              for e in E], dtype=int).reshape(-1, 2)

    def claim_cost(self, c):
        subj, part, legs = c
        d = 0.0
        for spad, ppads in legs:
            sp = self._pad_xy(subj, spad)
            pp = [q for p in ppads for q in self._pad_xy(part, p)]
            if sp and pp:
                d += min(math.dist(sp[0], q) for q in pp)
        return d

    def pins_for(self, cap, net):
        """The supply pins `cap` decouples: its group's, else its rail's."""
        return self.decap_pins.get(cap) or self.supply.get(net, ())

    def decap_cost(self, cap, net):
        pins = [q for ic, num in self.pins_for(cap, net) for q in self._pad_xy(ic, num)]
        mine = [(x, y) for x, y, nn in self._pads(cap) if nn == net]
        if not pins or not mine:
            return 0.0
        return min(math.dist(mine[0], q) for q in pins)

    def group_cost(self, members):
        """The group's gap tree: 0 while its courtyards form one piece."""
        return gap_tree([self.state.parts[r].rect() for r in members])[0]

    def wired(self, a, b):
        """Are parts `a` and `b` meant to touch: a shared signal net, or a
        decap and an IC it serves? (Fixed by the netlist: computed once.)"""
        w = getattr(self, '_wired', None)
        if w is None:
            w = self._wired = {}
        key = (a, b) if a < b else (b, a)
        got = w.get(key)
        if got is None:
            got = w[key] = self._wired_now(a, b)
        return got

    def _wired_now(self, a, b):
        st = self.state
        if set(st.parts[a].nets) & set(st.parts[b].nets) & self.signal:
            return True
        for cap, ic in ((a, b), (b, a)):
            if any(i == ic for i, _n in self.decap_pins.get(cap, ())):
                return True
            if any(c == cap for c, _n in self.decaps) and any(
                    i == ic for c, n in self.decaps if c == cap
                    for i, _num in self.pins_for(cap, n)):
                return True
        return False

    def halo_pair(self, a, b):
        st = self.state
        if st.parts[a].locked and st.parts[b].locked:
            return 0.0      # two fixed parts: nothing to move, overlap by design
        if self.wired(a, b):
            return 0.0
        pa, pb = st.parts[a], st.parts[b]
        return st._halo_pair_penalty(pa, pa.rect(), pb, pb.rect())

    def halo_cost(self, refs=None):
        """The halo penalty over every unwired pair of parts with one in
        `refs` (every pair when None), each pair once."""
        st = self.state
        names = sorted(st.parts)
        mine = names if refs is None else sorted(refs)
        mset = set(mine)
        rect = {r: st.parts[r].rect() for r in names}
        total = 0.0
        for a in mine:
            ra, ha = rect[a], st.parts[a].halo
            for b in names:
                if b == a or (b in mset and b < a):
                    continue
                rb, need = rect[b], ha + st.parts[b].halo
                # farther apart than both halos on some axis: nothing to pay
                if (ra[2] + need <= rb[0] or rb[2] + need <= ra[0]
                        or ra[3] + need <= rb[1] or rb[3] + need <= ra[1]):
                    continue
                total += self.halo_pair(a, b)
        return total

    def _grid_edges(self):
        g = getattr(self, '_ge', None)
        if g is None or g[0] != (self._nx, self._ny, self.cell):
            c = self.cell
            g = self._ge = ((self._nx, self._ny, self.cell),
                            self._gx0 + c * np.arange(self._nx + 1),
                            self._gy0 + c * np.arange(self._ny + 1))
        return g[1], g[2]

    def _cover(self, x0, x1, y0, y1):
        """The area of rect [x0, x1] x [y0, y1] in each grid cell (ny x nx)."""
        ex, ey = self._grid_edges()
        cx = np.clip(np.minimum(ex[1:], x1) - np.maximum(ex[:-1], x0), 0.0, None)
        cy = np.clip(np.minimum(ey[1:], y1) - np.maximum(ey[:-1], y0), 0.0, None)
        return np.outer(cy, cx)

    def _pad_shapes(self, ref):
        """`[(x, y, w, h, layers)]` of `ref`'s pads at its live pose."""
        part = self.state.parts[ref]
        fp = self.pcb.footprints[ref]
        g = part.pad_globals()
        conn = [p for p in fp.pads if p.net_id > 0]
        out = []
        turn = int(round((part.rot - fp.rotation) % 180.0)) == 90
        for i, p in enumerate(conn):
            if i >= len(g):
                break
            w, hh = (p.size_y, p.size_x) if turn else (p.size_x, p.size_y)
            layers = list(p.layers) if p.drill == 0 else ['*.Cu']
            out.append((g[i][0], g[i][1], w, hh, layers))
        return out

    def _win(self, x0, x1, y0, y1):
        """Grid rows i0:i1 and columns j0:j1 a rect [x0, x1] x [y0, y1]
        touches, clipped to the grid."""
        c = self.cell
        j0 = max(0, min(self._nx, int(math.floor((x0 - self._gx0) / c))))
        j1 = max(0, min(self._nx, int(math.floor((x1 - self._gx0) / c)) + 1))
        i0 = max(0, min(self._ny, int(math.floor((y0 - self._gy0) / c))))
        i1 = max(0, min(self._ny, int(math.floor((y1 - self._gy0) / c)) + 1))
        return i0, i1, j0, j1

    def _net_demand(self, n):
        """Net `n`'s share of `demand` as `(i0, i1, j0, j1, block)`, the block
        covering only the cells its box touches; None (fewer than two pads)."""
        h = self.pitch / 2.0
        E = self.net_edges.get(n)
        if E is not None:       # the tree the wire term just priced: its
            if not len(E):      # edges span its points' box exactly
                return None
            L = self.net_len[n]
            x0, x1 = float(min(E[:, 0].min(), E[:, 2].min())) - h, float(max(E[:, 0].max(), E[:, 2].max())) + h
            y0, y1 = float(min(E[:, 1].min(), E[:, 3].min())) - h, float(max(E[:, 1].max(), E[:, 3].max())) + h
        else:
            pts = sorted(set(self._net_points(n)))
            if len(pts) < 2:
                return None
            L = mst_l1(pts)[0]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            x0, x1, y0, y1 = min(xs) - h, max(xs) + h, min(ys) - h, max(ys) + h
        i0, i1, j0, j1 = self._win(x0, x1, y0, y1)
        if i1 <= i0 or j1 <= j0:
            return None
        ex, ey = self._grid_edges()
        cx = np.clip(np.minimum(ex[j0 + 1:j1 + 1], x1) - np.maximum(ex[j0:j1], x0), 0.0, None)
        cy = np.clip(np.minimum(ey[i0 + 1:i1 + 1], y1) - np.maximum(ey[i0:i1], y0), 0.0, None)
        return (i0, i1, j0, j1, np.outer(cy, cx) * (L / ((x1 - x0) * (y1 - y0))))

    def _part_use(self, ref):
        """`ref`'s pad copper (grown by half the clearance) per copper layer,
        `{layer: (i0, i1, j0, j1, block)}` over the cells its pads touch: its
        share of what `capacity` takes off each cell."""
        h = self.state.clearance / 2.0
        shapes = self._pad_shapes(ref)
        if not shapes:
            return {}
        A = np.asarray([(px - sx / 2 - h, px + sx / 2 + h, py - sy / 2 - h, py + sy / 2 + h)
                        for px, py, sx, sy, _l in shapes])
        i0, i1, j0, j1 = self._win(A[:, 0].min(), A[:, 1].max(), A[:, 2].min(), A[:, 3].max())
        if i1 <= i0 or j1 <= j0:
            return {}
        ex, ey = self._grid_edges()
        cx = np.clip(np.minimum(ex[None, j0 + 1:j1 + 1], A[:, 1:2])
                     - np.maximum(ex[None, j0:j1], A[:, 0:1]), 0.0, None)
        cy = np.clip(np.minimum(ey[None, i0 + 1:i1 + 1], A[:, 3:4])
                     - np.maximum(ey[None, i0:i1], A[:, 2:3]), 0.0, None)
        out = {}
        for layer in self.layers:
            on = np.asarray([layer in l or '*.Cu' in l for *_r, l in shapes])
            if on.any():
                out[layer] = (i0, i1, j0, j1, cy[on].T @ cx[on])
        return out

    def _over2(self, i0, i1, j0, j1):
        """Overflow squared summed over grid window [i0:i1, j0:j1]."""
        if i1 <= i0 or j1 <= j0:
            return 0.0
        cap = np.zeros((i1 - i0, j1 - j0))
        for L in self.layers:
            cap += np.clip(self.cell * self.cell - self._U[L][i0:i1, j0:j1], 0.0, None)
        over = np.clip(self._D[i0:i1, j0:j1] - cap / self.pitch, 0.0, None)
        return float((over * over).sum())

    def _cong_init(self):
        """Every net's demand and every part's pad use, from scratch, their
        sums, and the overflow total -- what a move then corrects window by
        window (`_cong_price`, `_cong_commit`)."""
        self._cset = set(self.cnets)
        self._dem = {n: self._net_demand(n) for n in self.cnets}
        self._use = {r: self._part_use(r) for r in sorted(self.state.parts)}
        self._D = np.zeros((self._ny, self._nx))
        for v in self._dem.values():
            if v is not None:
                self._D[v[0]:v[1], v[2]:v[3]] += v[4]
        self._U = {L: np.zeros((self._ny, self._nx)) for L in self.layers}
        for u in self._use.values():
            for L, (i0, i1, j0, j1, blk) in u.items():
                self._U[L][i0:i1, j0:j1] += blk
        self._S = self._over2(0, self._ny, 0, self._nx)
        self._cong_pending = None

    def _cong_nets(self, refs):
        st = self.state
        return sorted({n for r in refs for n in st.parts[r].nets if n in self._cset})

    def _cong_price(self, refs):
        """Price the congestion a move of `refs` makes, without touching the
        grids: the demand of the nets `refs` are on and the pad use of `refs`
        re-priced, swapped into COPIES of the window they touch, the overflow
        total corrected there. The change is kept as pending, written by
        `_cong_commit` if the move is kept and dropped if it is not -- a
        refused move (most of them) costs one pass and no restore."""
        dem = []
        for n in self._cong_nets(refs):
            dem.append((n, self._dem[n], self._net_demand(n)))
        use = [(r, self._use[r], self._part_use(r)) for r in refs]
        wins = [v[:4] for _n, old, new in dem for v in (old, new) if v is not None]
        wins += [b[:4] for _r, old, new in use for u in (old, new) for b in u.values()]
        if not wins:
            self._cong_pending = (dem, use, None, None, None, self._S)
            return self._S
        i0 = min(w[0] for w in wins)
        i1 = max(w[1] for w in wins)
        j0 = min(w[2] for w in wins)
        j1 = max(w[3] for w in wins)
        Dw = self._D[i0:i1, j0:j1].copy()
        Uw = {L: self._U[L][i0:i1, j0:j1].copy() for L in self.layers}
        before = self._over2_on(Dw, Uw)
        for _n, old, new in dem:
            if old is not None:
                Dw[old[0] - i0:old[1] - i0, old[2] - j0:old[3] - j0] -= old[4]
            if new is not None:
                Dw[new[0] - i0:new[1] - i0, new[2] - j0:new[3] - j0] += new[4]
        for _r, old, new in use:
            for L, (a0, a1, b0, b1, blk) in old.items():
                Uw[L][a0 - i0:a1 - i0, b0 - j0:b1 - j0] -= blk
            for L, (a0, a1, b0, b1, blk) in new.items():
                Uw[L][a0 - i0:a1 - i0, b0 - j0:b1 - j0] += blk
        S = self._S + self._over2_on(Dw, Uw) - before
        self._cong_pending = (dem, use, (i0, i1, j0, j1), Dw, Uw, S)
        return S

    def _over2_on(self, Dw, Uw):
        cap = np.zeros(Dw.shape)
        for L in self.layers:
            cap += np.clip(self.cell * self.cell - Uw[L], 0.0, None)
        over = np.clip(Dw - cap / self.pitch, 0.0, None)
        return float((over * over).sum())

    def _cong_commit(self):
        """Write the pending congestion of the move just kept."""
        pend = getattr(self, '_cong_pending', None)
        if pend is None:
            return
        dem, use, win, Dw, Uw, S = pend
        for n, _old, new in dem:
            self._dem[n] = new
        for r, _old, new in use:
            self._use[r] = new
        if win is not None:
            i0, i1, j0, j1 = win
            self._D[i0:i1, j0:j1] = Dw
            for L in self.layers:
                self._U[L][i0:i1, j0:j1] = Uw[L]
        self._S = S
        self._cong_pending = None

    def congestion(self):
        """Sum over cells of the overflow squared (mm^2 of track): the
        pending move's when one is being priced, else the committed one."""
        if self.qw <= 0:
            return 0.0
        if getattr(self, '_D', None) is None:
            self._cong_init()
        pend = getattr(self, '_cong_pending', None)
        return pend[5] if pend is not None else self._S

    def snapshot(self, touched, refs):
        """What pricing a move of `refs` changes, to put back if the move is
        refused: the touched nets' trees (congestion is only pending)."""
        return {'nets': {n: (self.net_len[n], self.net_edges[n], self.net_ends[n])
                         for n in touched[0]}}

    def restore(self, snap):
        for n, (L, E, ends) in snap['nets'].items():
            self.net_len[n], self.net_edges[n], self.net_ends[n] = L, E, ends
        self._cong_pending = None
        self._pend = None

    def total(self):
        self.sync()
        wire = sum(self.net_len[n] for n in self.nets) + sum(
            self.wire_weight(n) * self.net_len[n] for n in self.rnets)
        allE = []
        cross = 0
        for n in self.nets:
            E = self.net_edges[n]
            if len(E) and allE:
                cross += crossings(E, np.vstack(allE))
            if len(E):
                allE.append(E)
        claims = sum(self.claim_cost(c) for c in self.claims)
        dec = sum(self.decap_cost(c, n) for c, n in self.decaps)
        per_group = {name: self.group_cost(m) for name, m in self.groups}
        grp = sum(per_group.values())
        halo = self.halo_cost()
        if self.qw > 0:
            self._cong_init()       # from scratch: total() is the reference
        cong = self.congestion()
        pads = self.pad_crossings() if self.pw_pad > 0 else 0.0
        return {'wire': wire, 'crossings': cross, 'claims': claims, 'decaps': dec,
                'groups': grp, 'per_group': per_group, 'halo': halo, 'congestion': cong,
                'pad_hits': pads,
                'cost': (wire + self.cw * cross + self.kw * claims + self.dw * dec
                         + self.gw * grp + halo + self.qw * cong + self.pw_pad * pads)}

    # -- local pricing of a set of moved parts --------------------------------
    # -- airwires over foreign pads -------------------------------------------
    def _pad_rows(self, ref):
        """`(rects k x 4 grown by the clearance, weights, nets, owner ids)` of
        `ref`'s pads at its live pose, memoised on the pose."""
        part = self.state.parts[ref]
        key = (part.x, part.y, part.rot)
        memo = getattr(self, '_padmemo', None)
        if memo is None:
            memo = self._padmemo = {}
        got = memo.get(ref)
        if got is not None and got[0] == key:
            return got[1]
        h = self.state.clearance
        rows = [((x - w / 2 - h, y - hh / 2 - h, x + w / 2 + h, y + hh / 2 + h),
                 2.0 if '*.Cu' in layers else 1.0, n)
                for (x, y, w, hh, layers), (_gx, _gy, n) in zip(self._pad_shapes(ref),
                                                                part.pad_globals())]
        val = (np.asarray([r[0] for r in rows], dtype=float).reshape(-1, 4),
               np.asarray([r[1] for r in rows], dtype=float),
               np.asarray([r[2] for r in rows], dtype=int),
               np.full(len(rows), self._pid(ref), dtype=int))
        memo[ref] = (key, val)
        return val

    def _all_edges(self):
        """Every signal net's tree edges, stacked, with end parts and nets --
        rebuilt only when a net's tree is not the array it was built from
        (a refused move puts the old arrays back, so it stays valid)."""
        c = getattr(self, '_ecache', None)
        if c is not None and all(self.net_edges[n] is a for n, a in c[0]):
            return c[1]
        nets = [n for n in self.nets if len(self.net_edges[n])]
        val = self._edges_of(nets)
        self._ecache = ([(n, self.net_edges[n]) for n in self.nets], val)
        return val

    def _all_pads(self):
        """Every part's pad rows, stacked -- rebuilt only when a part's rows
        are not the ones it was built from."""
        names = sorted(self.state.parts)
        c = getattr(self, '_pcache', None)
        rows = [(r, self._pad_rows(r)) for r in names]
        if c is not None and all(a is b for (_r, a), b in zip(rows, c[0])):
            return c[1]
        val = tuple(np.concatenate([v[k] for _r, v in rows]) for k in range(4))
        self._pcache = ([v for _r, v in rows], val)
        return val

    def _pads_of(self, refs):
        rows = [self._pad_rows(r) for r in refs]
        if not rows:
            return (np.zeros((0, 4)), np.zeros(0), np.zeros(0, dtype=int), np.zeros(0, dtype=int))
        return tuple(np.concatenate([r[k] for r in rows]) for k in range(4))

    def _edges_of(self, nets):
        nets = [n for n in nets if len(self.net_edges[n])]
        if not nets:
            return np.zeros((0, 4)), np.zeros((0, 2), dtype=int), np.zeros(0, dtype=int)
        return (np.concatenate([self.net_edges[n] for n in nets]),
                np.concatenate([self.net_ends[n] for n in nets]),
                np.concatenate([np.full(len(self.net_edges[n]), n, dtype=int) for n in nets]))

    @staticmethod
    def _seg_hits(S, P):
        """`(i, j)` index arrays of segment i touching rect j: the Rust kernel
        `grid_router.seg_rect_hits`; `_seg_hits_py` is its reference."""
        if len(S) == 0 or len(P) == 0:
            return np.zeros(0, dtype=int), np.zeros(0, dtype=int)
        return grid_router.seg_rect_hits(np.ascontiguousarray(S, dtype=float),
                                         np.ascontiguousarray(P, dtype=float))

    @staticmethod
    def _seg_hits_py(S, P):
        """`_seg_hits` in numpy: a bounding-box prefilter, then Liang-Barsky
        on the pairs that pass it."""
        if len(S) == 0 or len(P) == 0:
            return np.zeros(0, dtype=int), np.zeros(0, dtype=int)
        sx0, sx1 = np.minimum(S[:, 0], S[:, 2]), np.maximum(S[:, 0], S[:, 2])
        sy0, sy1 = np.minimum(S[:, 1], S[:, 3]), np.maximum(S[:, 1], S[:, 3])
        bb = ((sx0[:, None] <= P[None, :, 2]) & (sx1[:, None] >= P[None, :, 0])
              & (sy0[:, None] <= P[None, :, 3]) & (sy1[:, None] >= P[None, :, 1]))
        i, j = np.nonzero(bb)
        if len(i) == 0:
            return i, j
        x0, y0 = S[i, 0], S[i, 1]
        dx, dy = S[i, 2] - x0, S[i, 3] - y0
        lo = np.zeros(len(i))
        hi = np.ones(len(i))
        ok = np.ones(len(i), dtype=bool)
        for d, q in ((-dx, x0 - P[j, 0]), (dx, P[j, 2] - x0),
                     (-dy, y0 - P[j, 1]), (dy, P[j, 3] - y0)):
            zero = np.abs(d) < 1e-12
            ok &= ~(zero & (q < 0))
            t = q / np.where(zero, 1.0, d)
            lo = np.where(~zero & (d < 0), np.maximum(lo, t), lo)
            hi = np.where(~zero & (d > 0), np.minimum(hi, t), hi)
        keep = ok & (lo <= hi)
        return i[keep], j[keep]

    def _pad_hits(self, edges, pads):
        """Weighted hits of tree edges on pads foreign to them: another net,
        and neither of the edge's end parts."""
        S, ends, labs = edges
        P, wts, pnets, owners = pads
        i, j = self._seg_hits(S, P)
        if len(i) == 0:
            return 0.0
        foreign = (labs[i] != pnets[j]) & (ends[i, 0] != owners[j]) & (ends[i, 1] != owners[j])
        return float(wts[j][foreign].sum())

    def pad_crossings(self, nets=None, refs=None):
        """Weighted foreign-pad hits: every signal edge on every pad (both
        None), or what `nets` and `refs` can change -- `nets`' edges on every
        pad, plus every other net's edges on the pads of `refs`."""
        if nets is None and refs is None:
            return self._pad_hits(self._all_edges(), self._all_pads())
        mine = [n for n in (nets or ()) if n in self.signal]
        E, ends, labs = self._all_edges()
        keep = ~np.isin(labs, mine)
        total = self._pad_hits(self._edges_of(mine), self._all_pads())
        return total + self._pad_hits((E[keep], ends[keep], labs[keep]),
                                      self._pads_of(sorted(refs or ())))

    def _names(self):
        return sorted(self.state.parts)

    def sync(self):
        """Index the live poses -- parts, pads, signal tree edges -- and the
        by-part lookups: the COMMITTED state every move is priced against.
        A caller that keeps a move tells it (`commit`); one that moves parts
        some other way calls this, as `total` does."""
        st = self.state
        self._ixp, self._ixd, self._ixe = SpatialIndex(), SpatialIndex(), SpatialIndex()
        self._edge_geo, self._edge_keys, self._npads = {}, {}, {}
        # pads as flat rows, an index key -> row (rows of a part are reused)
        self._PR = np.zeros((0, 4))
        self._PW = np.zeros(0)
        self._PN = np.zeros(0, dtype=int)
        self._PO = np.zeros(0, dtype=int)
        self._prow: Dict = {}
        names = self._names()
        self._maxhalo = max((st.parts[r].halo for r in names), default=0.0)
        for r in names:
            self._index_part(r)
        for n in self.nets:
            self._index_net(n)
        self._claims_by: Dict[str, List] = {}
        self._decaps_by: Dict[str, List] = {}
        self._groups_by: Dict[str, List] = {}
        for c in self.claims:
            for r in {c[0], c[1]}:
                self._claims_by.setdefault(r, []).append(c)
        for cap, net in self.decaps:
            for r in {cap} | {ic for ic, _num in self.pins_for(cap, net)}:
                self._decaps_by.setdefault(r, []).append((cap, net))
        for name, m in self.groups:
            for r in m:
                self._groups_by.setdefault(r, []).append((name, m))
        self._synced = True
        self._build_breakdowns()

    # -- the committed state's breakdowns: a move's "before" is read from these
    def _build_breakdowns(self):
        """From scratch: each nonzero halo pair, each group's cost, crossings
        between each pair of signal nets, and foreign-pad hits for each
        (net, part)."""
        names = self._names()
        self._hp, self._hnb = {}, {r: set() for r in names}
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                v = self.halo_pair_fast(a, b)
                if v:
                    self._hp[(a, b)] = v
                    self._hnb[a].add(b)
                    self._hnb[b].add(a)
        self._gc = {name: self.group_cost(m) for name, m in self.groups}
        self._xc = {n: {} for n in self.nets}
        E, ends, labs = self._edges_of(self.nets)
        if len(E):
            ii, jj = grid_router.seg_cross_pairs(np.ascontiguousarray(E, dtype=float), None,
                                                 labs.astype(np.int64))
            for i, j in zip(ii, jj):
                a, b = int(labs[i]), int(labs[j])
                self._xc[a][b] = self._xc[a].get(b, 0) + 1
                self._xc[b][a] = self._xc[b].get(a, 0) + 1
        self._pn: Dict[int, Dict[int, float]] = {n: {} for n in self.nets}
        self._po: Dict[int, Dict[int, float]] = {}
        for (n, pid), w in self._pad_breakdown((E, ends, labs), self._pads_of(names)).items():
            self._pn.setdefault(n, {})[pid] = w
            self._po.setdefault(pid, {})[n] = w
        self._pend = None

    def halo_pair_fast(self, a, b):
        """`halo_pair` behind the rect prefilter `_local_halo` uses."""
        st = self.state
        ra, rb = st.parts[a].rect(), st.parts[b].rect()
        need = st.parts[a].halo + st.parts[b].halo
        if (ra[2] + need <= rb[0] or rb[2] + need <= ra[0]
                or ra[3] + need <= rb[1] or rb[3] + need <= ra[1]):
            return 0.0
        return self.halo_pair(a, b)

    def _pad_breakdown(self, edges, pads):
        """Weighted foreign hits of `edges` on `pads`, as {(net, owner pid): w}."""
        S, ends, labs = edges
        P, wts, pnets, owners = pads
        out: Dict = {}
        if not len(S) or not len(P):
            return out
        i, j = self._seg_hits(S, P)
        if not len(i):
            return out
        f = (labs[i] != pnets[j]) & (ends[i, 0] != owners[j]) & (ends[i, 1] != owners[j])
        for n, o, w in zip(labs[i][f], owners[j][f], wts[j][f]):
            key = (int(n), int(o))
            out[key] = out.get(key, 0.0) + float(w)
        return out

    def _index_part(self, r):
        st = self.state
        self._ixp.remove(r)
        self._ixp.insert(r, st.parts[r].rect())
        for i in range(self._npads.get(r, 0)):
            self._ixd.remove((r, i))
        rects, wts, nets, pids = self._pad_rows(r)
        k = len(rects)
        base = self._prow.get((r, 0))
        if base is None or self._npads.get(r, 0) != k:
            base = len(self._PR)
            self._PR = np.vstack([self._PR, rects]) if k else self._PR
            self._PW = np.concatenate([self._PW, wts])
            self._PN = np.concatenate([self._PN, nets])
            self._PO = np.concatenate([self._PO, pids])
        else:
            self._PR[base:base + k] = rects
            self._PW[base:base + k] = wts
            self._PN[base:base + k] = nets
            self._PO[base:base + k] = pids
        for i in range(k):
            self._ixd.insert(base + i, tuple(rects[i]))
            self._prow[(r, i)] = base + i
        self._npads[r] = k

    def _index_net(self, n):
        for key in self._edge_keys.get(n, ()):
            self._ixe.remove(key)
            self._edge_geo.pop(key, None)
        keys = []
        for k, (e, en) in enumerate(zip(self.net_edges[n], self.net_ends[n])):
            key = (n, k)
            self._ixe.insert(key, _bbox(e))
            self._edge_geo[key] = (e[0], e[1], e[2], e[3], en[0], en[1])
            keys.append(key)
        self._edge_keys[n] = keys

    def commit(self, refs, nets):
        """A move of `refs` was kept: index their new poses and the trees of
        `nets` (the nets it touched)."""
        if not getattr(self, '_synced', False):
            self.sync()
            return
        if self.qw > 0:
            self._cong_commit()
        pend = getattr(self, '_pend', None)
        if pend is not None:
            self._commit_breakdowns(pend)
        for r in refs:
            self._index_part(r)
        for n in nets:
            if n in self.signal:
                self._index_net(n)

    def _commit_breakdowns(self, pend):
        R, T = pend['R'], pend['T']
        for r in R:                                     # halo: r's pairs anew
            for b in list(self._hnb.get(r, ())):
                self._hp.pop((r, b) if r < b else (b, r), None)
                self._hnb[b].discard(r)
            self._hnb[r] = set()
        for (a, b), v in pend['halo'].items():
            if v:
                self._hp[(a, b) if a < b else (b, a)] = v
                self._hnb.setdefault(a, set()).add(b)
                self._hnb.setdefault(b, set()).add(a)
        self._gc.update(pend['groups'])
        for t in T:                                     # crossings: T's rows anew
            for m in list(self._xc.get(t, {})):
                self._xc[m].pop(t, None)
            self._xc[t] = {}
        for (a, b), c in pend['cross'].items():
            self._xc[a][b] = self._xc[a].get(b, 0) + c
            self._xc[b][a] = self._xc[b].get(a, 0) + c
        Rp = {self._pid(r) for r in R}
        for n in T:                                     # pads: T's rows, R's columns
            for pid in list(self._pn.get(n, {})):
                self._po.get(pid, {}).pop(n, None)
            self._pn[n] = {}
        for pid in Rp:
            for n in list(self._po.get(pid, {})):
                self._pn.get(n, {}).pop(pid, None)
            self._po[pid] = {}
        for (n, pid), w in pend['pads'].items():
            self._pn.setdefault(n, {})[pid] = self._pn.get(n, {}).get(pid, 0.0) + w
            self._po.setdefault(pid, {})[n] = self._po.get(pid, {}).get(n, 0.0) + w
        self._pend = None

    def _near_edges(self, rects, skip_nets):
        """Committed edges near `rects`, not of `skip_nets`: `(segs, ends,
        nets)`."""
        cand = self._ixe.query_many(rects)
        keys = sorted(k for k in cand if k[0] not in skip_nets)
        if not keys:
            return np.zeros((0, 4)), np.zeros((0, 2), dtype=int), np.zeros(0, dtype=int)
        g = np.asarray([self._edge_geo[k] for k in keys], dtype=float)
        return g[:, :4], g[:, 4:6].astype(int), np.asarray([k[0] for k in keys], dtype=int)

    def touched(self, refs):
        if not getattr(self, '_synced', False):
            self.sync()
        st = self.state
        nets = sorted({n for r in refs for n in st.parts[r].nets if n in self.net_len})
        cl = list({id(c): c for r in refs for c in self._claims_by.get(r, ())}.values())
        dc = sorted({d for r in refs for d in self._decaps_by.get(r, ())})
        gr = list({name: m for r in refs for name, m in self._groups_by.get(r, ())}.values())
        return nets, cl, dc, gr

    def local(self, refs, touched=None, reprice=True):
        """The part of the cost `refs` can change. `reprice` False: the
        committed state's, read from the breakdowns (`_build_breakdowns`)
        and the cached trees -- a move's "before". True: re-priced at the
        live poses against the committed state near them, its breakdown
        held as pending for `commit` -- a move's "after"."""
        nets, cl, dc, gr = touched or self.touched(refs)
        R = sorted(refs)
        T = [n for n in nets if n in self.signal]
        claims = sum(self.claim_cost(c) for c in cl)
        dec = sum(self.decap_cost(c, n) for c, n in dc)
        if not reprice:
            self._cong_pending = None
            self._pend = None
            wire = sum(self.wire_weight(n) * self.net_len[n] for n in nets)
            tset, rset = set(T), set(R)
            cross = 0
            for t in T:
                for m, c in self._xc.get(t, {}).items():
                    if m not in tset or m > t:
                        cross += c
            Rp = {self._pid(r) for r in R}
            pads = sum(w for t in T for w in self._pn.get(t, {}).values())
            pads += sum(w for pid in Rp for n, w in self._po.get(pid, {}).items() if n not in tset)
            halo = 0.0
            for r in R:
                for b in self._hnb.get(r, ()):
                    if b not in rset or b > r:
                        halo += self._hp[(r, b) if r < b else (b, r)]
            grp = sum(self._gc[name] for name in self._group_names(gr))
            return (wire + self.cw * cross + self.kw * claims + self.dw * dec
                    + self.gw * grp + halo + self.qw * self.congestion() + self.pw_pad * pads)
        for n in nets:
            self._price_net(n)
        if self.qw > 0:
            if getattr(self, '_D', None) is None:
                self._cong_init()
            else:
                self._cong_price(refs)
        wire = sum(self.wire_weight(n) * self.net_len[n] for n in nets)
        pend = {'R': R, 'T': T}
        cross, pend['cross'] = self._local_crossings_bd(T)
        groups = {name: self.group_cost(m) for name, m in zip(self._group_names(gr), gr)}
        pend['groups'] = groups
        if self.pw_pad > 0:
            pend['pads'] = self._local_pads_bd(T, R)
            pads = sum(pend['pads'].values())
        else:
            pend['pads'], pads = {}, 0.0
        pend['halo'] = self._local_halo_bd(R)
        self._pend = pend
        return (wire + self.cw * cross + self.kw * claims + self.dw * dec
                + self.gw * sum(groups.values()) + sum(pend['halo'].values())
                + self.qw * self.congestion() + self.pw_pad * pads)

    def _group_names(self, gr):
        names = {id(m): name for name, m in self.groups}
        return [names[id(m)] for m in gr]

    def _local_crossings_bd(self, T):
        """`_local_crossings`, with its breakdown {(net, net): count}."""
        out: Dict = {}
        T = [n for n in T if len(self.net_edges[n])]
        if not T:
            return 0, out
        M = np.ascontiguousarray(np.vstack([self.net_edges[n] for n in T]), dtype=float)
        lab = np.concatenate([np.full(len(self.net_edges[n]), n) for n in T]).astype(np.int64)
        O, _e, olab = self._near_edges([_bbox(e) for e in M], set(T))
        total = 0
        if len(O):
            ii, jj = grid_router.seg_cross_pairs(M, np.ascontiguousarray(O, dtype=float))
            for i, j in zip(ii, jj):
                key = (int(lab[i]), int(olab[j]))
                out[key] = out.get(key, 0) + 1
            total += len(ii)
        if len(T) > 1:
            ii, jj = grid_router.seg_cross_pairs(M, None, lab)
            for i, j in zip(ii, jj):
                key = (int(lab[i]), int(lab[j]))
                out[key] = out.get(key, 0) + 1
            total += len(ii)
        return total, out

    def _local_pads_bd(self, T, R):
        """`_local_pads` as a breakdown {(net, owner pid): weight}."""
        rset = set(R)
        mine = self._pads_of(R)
        S = self._edges_of(T)
        out: Dict = {}
        if len(S[0]):
            rows = np.fromiter(self._ixd.query_many([_bbox(e) for e in S[0]]), dtype=int)
            if len(rows):
                rows.sort()
                rows = rows[~np.isin(self._PO[rows], [self._pid(r) for r in rset])]
            pads = mine if not len(rows) else (
                np.concatenate([self._PR[rows], mine[0]]), np.concatenate([self._PW[rows], mine[1]]),
                np.concatenate([self._PN[rows], mine[2]]), np.concatenate([self._PO[rows], mine[3]]))
            for k, w in self._pad_breakdown(S, pads).items():
                out[k] = out.get(k, 0.0) + w
        if len(mine[0]):
            near = self._near_edges([tuple(r) for r in mine[0]], set(T))
            for k, w in self._pad_breakdown(near, mine).items():
                out[k] = out.get(k, 0.0) + w
        return out

    def _local_halo_bd(self, R):
        """`_local_halo` as a breakdown {(a, b): value}, nonzero pairs only."""
        st = self.state
        rset = set(R)
        out: Dict = {}
        for a in R:
            ra, ha = st.parts[a].rect(), st.parts[a].halo
            m = ha + self._maxhalo
            for b in self._ixp.query((ra[0] - m, ra[1] - m, ra[2] + m, ra[3] + m)) | (rset - {a}):
                if b == a or (b in rset and b < a):
                    continue
                v = self.halo_pair_fast(a, b)
                if v:
                    out[(a, b)] = v
        return out


# --------------------------------------------------------------------------
# blame: the cost, part by part

TERMS = ('wire', 'cross', 'claims', 'decaps', 'group', 'halo')


def blame(model) -> Dict[str, Dict[str, float]]:
    """The cost split onto parts, each term in cost units, summing to
    `model.total()['cost']`: a net tree edge's length half to each end's
    part; a crossing a quarter to each of its two edges' four end parts; a
    claim to its subject; a decap term to the cap; a group tree edge's gap
    half to each member; a halo pair half to each part."""
    st = model.state
    out = {r: dict.fromkeys(TERMS, 0.0) for r in st.parts}
    seg, own = [], []
    for n in list(model.nets) + list(model.rnets):
        w = model.wire_weight(n)
        where = {}
        for r in st.net_refs.get(n, ()):
            for x, y, nn in model._pads(r):
                if nn == n:
                    where.setdefault((x, y), r)
        _L, E = mst_l1(sorted(where))
        for x0, y0, x1, y1 in E:
            a, b = where[(x0, y0)], where[(x1, y1)]
            d = w * (abs(x1 - x0) + abs(y1 - y0))
            out[a]['wire'] += d / 2.0
            out[b]['wire'] += d / 2.0
            if n in model.signal:       # rails take no part in crossings
                seg.append((x0, y0, x1, y1))
                own.append((a, b, n))
    if seg:
        S = np.asarray(seg, dtype=float)
        a0x, a0y, a1x, a1y = (S[:, None, i] for i in range(4))
        b0x, b0y, b1x, b1y = (S[None, :, i] for i in range(4))
        o1 = _orient(a0x, a0y, a1x, a1y, b0x, b0y)
        o2 = _orient(a0x, a0y, a1x, a1y, b1x, b1y)
        o3 = _orient(b0x, b0y, b1x, b1y, a0x, a0y)
        o4 = _orient(b0x, b0y, b1x, b1y, a1x, a1y)
        hit = np.triu((o1 * o2 < 0) & (o3 * o4 < 0), 1)
        for i, j in zip(*np.nonzero(hit)):
            if own[i][2] == own[j][2]:
                continue        # one net's own tree edges are not priced
            for r in (own[i][0], own[i][1], own[j][0], own[j][1]):
                out[r]['cross'] += model.cw / 4.0
    for c in model.claims:
        out[c[0]]['claims'] += model.kw * model.claim_cost(c)
    for cap, n in model.decaps:
        out[cap]['decaps'] += model.dw * model.decap_cost(cap, n)
    for _name, members in model.groups:
        _L, E = gap_tree([st.parts[r].rect() for r in members])
        for i, j, g in E:
            out[members[i]]['group'] += model.gw * g / 2.0
            out[members[j]]['group'] += model.gw * g / 2.0
    names = sorted(st.parts)
    for i, a in enumerate(names):
        pa = st.parts[a]
        for b in names[i + 1:]:
            pb = st.parts[b]
            h = st._halo_pair_penalty(pa, pa.rect(), pb, pb.rect())
            if h:
                out[a]['halo'] += h / 2.0
                out[b]['halo'] += h / 2.0
    for r in out:
        out[r]['total'] = sum(out[r][k] for k in TERMS)
    return out


# --------------------------------------------------------------------------
# legality

def _rot_ok(state, ref, rot):
    state.parts[ref].ensure_rotation(rot)


def legal(state, ref, x, y, rot, exclude=frozenset()):
    from placement.seeder import pose_ok
    return pose_ok(state, ref, x, y, rot, set(exclude))


# --------------------------------------------------------------------------
# targets

def target(model, ref, placed):
    """Where `ref` wants to be: its claim partner's pins, else a decap's
    supply pin, else its wiring target (seeder._wiring_target), else the
    board's centre."""
    from placement.seeder import _wiring_target
    st = model.state
    t = None
    for subj, part, legs in model.claims:
        if subj == ref and part in placed:
            pts = [q for _s, pp in legs for p in pp for q in model._pad_xy(part, p)]
            if pts:
                t = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
            break
    if t is None:
        for cap, net in model.decaps:
            if cap == ref:
                pins = [q for ic, num in model.pins_for(cap, net) if ic in placed
                        for q in model._pad_xy(ic, num)]
                if pins:
                    t = min(pins, key=lambda q: math.dist(q, (st.parts[ref].x, st.parts[ref].y)))
                break
    if t is None:
        t = _wiring_target(st, ref, placed)
    if t is None:
        b = model.pcb.board_info.board_bounds
        t = ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)
    return t


def nearest_legal(state, ref, tx, ty, rot, exclude, reach=12.0, step=0.25):
    """The legal pose nearest (tx, ty) at `rot`, walking square rings out to
    `reach` mm, or None."""
    _rot_ok(state, ref, rot)
    if legal(state, ref, tx, ty, rot, exclude):
        return (tx, ty, rot)
    k = 1
    while k * step <= reach:
        best = None
        for i in range(-k, k + 1):
            for x, y in ((tx + i * step, ty - k * step), (tx + i * step, ty + k * step),
                         (tx - k * step, ty + i * step), (tx + k * step, ty + i * step)):
                if legal(state, ref, x, y, rot, exclude):
                    d = (x - tx) ** 2 + (y - ty) ** 2
                    if best is None or d < best[0]:
                        best = (d, x, y)
        if best is not None:
            return (round(best[1], 4), round(best[2], 4), rot)
        k += 1
    return None

