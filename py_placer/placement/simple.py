"""The simple placement path's cost model: groups, one objective.

A GROUP is a set of parts optimised together and kept together; the shape
it takes is whatever the objective and the free space make of it. Only the
parts the file locks or the intent seats stay put. `simple_anneal.py` lowers
this cost by simulated annealing; `place_simple.py` is the CLI.

    cost = sum over signal nets of the Manhattan MST of their pads
         + rail_weight / ground_weight x each rail's MST (below)
         + crossing_weight x crossings among the signal trees' edges
         + claim_weight x each claimed pad's distance to its partner's pad
         + decap_weight x each decap's distance to the supply-pin site it
           is matched to: the decaps of one rail and one set of pins (its own
           group's IC's, any IC's when its group has none) are matched to
           that rail's pin SITES (pins of one IC within SITE_MM of each
           other) at the least total distance, a site taking one decap
           before any takes two -- so they spread over the pins rather than
           crowding the nearest
         + group_weight x each group's spanning tree over its members'
           courtyards, an edge priced by the gap between the two courtyards
           -- nothing while the group is one contiguous piece, whatever its
           shape
         + the halo, in front of fine-pitch parts' pads only: a part with
           HALO_MIN_PADS pads or more at a pitch of HALO_PITCH_MM or less (a
           QFN, a QFP, a BGA, a fine-pitch connector) keeps, on each side its
           pads face (`pad_fronts`), a strip `base + coef x the nets that
           leave by it` deep (ground, dropping a via to its plane, leaves
           none), spanning those pads, for its tracks to fan out in; a
           part sharing a board side with it, where a strip reaches it,
           pays halo_weight x the shortfall squared on the axis that
           separates the two best. Everything else packs as
           tight as legality lets it, and a part off the end of a pad row (a
           chip's corner) pays nothing. A small part wired to it (a decap at
           its pin, a crystal at its oscillator pins: HALO_SMALL_PADS pads or
           fewer) is meant to touch and pays nothing; a bigger one wired to
           it (a header its signals go to) leaves the fan-out room all the
           same.
         + pad_weight x each tree edge's crossings of foreign pads: a pad of
           a part other than the edge's two ends, grown by the clearance, a
           through-hole pad counting twice (it blocks both layers)
         + the groups file's hints, each weighted: `near`, the gap between
           two groups' nearest courtyards; `apart`, how far that gap falls
           short of APART_MM; `edge`, how far a group is from its board edge
         + congestion_weight x the sum over a grid of each cell's routing
           overflow squared: every net spreads its tree's length evenly over
           its bounding box (the demand, mm of track); a cell holds its area
           free of pad copper, per copper layer, over the track pitch (the
           supply). Ground, poured as a plane, is left out.

Rails -- ground, and nets reaching more than a quarter of the parts and
more than six of them (`grouping.rail_net_ids`) -- carry a fraction of a signal's cost per mm of
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

import bisect
import math
import os
import sys as _sys
from typing import Dict, List, Sequence, Set, Tuple

import numpy as np
from scipy.optimize import linear_sum_assignment

_sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..',
                                                 'rust_router')))
import grid_router  # noqa: E402  the placement kernels (rust_router/src/placement_kernels.rs)

ROTS = (0.0, 90.0, 180.0, 270.0)
#: mm two groups the groups file keeps `apart` must leave between them.
APART_MM = 3.0
#: mm between two supply pins of one IC that make them one decap site.
SITE_MM = 1.5
#: a part keeps a halo only when it is fine pitch with many pins (a QFN,
#: a QFP, a BGA, a fine-pitch connector): at least this many connected pads
HALO_MIN_PADS = 8
#: ... at a pad pitch (the median distance from a pad to its nearest
#: neighbour) of at most this many mm
HALO_PITCH_MM = 1.0
#: mm a halo strip reaches past the end pads of its side
FAN_MM = 0.25
#: a part with at most this many pads, wired to a fine-pitch part, may sit
#: in its halo (a decap at its pin, a crystal at its oscillator pins)
HALO_SMALL_PADS = 4


def pad_pitch(offs: Sequence[Tuple[float, float]]) -> float:
    """The median distance from each pad to its nearest neighbour; inf
    for fewer than two pads."""
    if len(offs) < 2:
        return float('inf')
    P = np.asarray(offs, dtype=float)
    D = np.hypot(P[:, None, 0] - P[None, :, 0], P[:, None, 1] - P[None, :, 1])
    np.fill_diagonal(D, np.inf)
    return float(np.median(D.min(axis=1)))


def pad_fronts(pads: Sequence[Tuple[float, float, int]], coef: float,
               skip=frozenset()):
    """The sides of a part its pads face, from its pads' `(dx, dy, net)`,
    offsets from the part's origin in the board frame: for W, E, N, S (-x,
    +x, -y, +y), `(h, lo, hi)` -- the halo depth in front of that side,
    `coef x the nets that leave by it` (each a track, and tracks
    turning along a side stack one pitch deep each: a net on several pads
    is one track, a net in `skip` -- ground, dropping a via to its plane --
    none), and the span of its facing pads along it (y for W and E, x for N
    and S, as offsets, FAN_MM past the end pads) -- or None for a side no
    such net leaves by.

    A pad faces the side its offset from the pads' centre points to, each
    axis scaled by the pads' half-extent: a two-pad part faces along the
    line through its pads, a chip's pin rows face out. A pad in the inner
    half (an exposed pad, an inner ball) faces nothing. A corner pad faces
    the side whose line of pads (its row or column) is longer, both on a
    tie. A single line of three or more pads (a pin header) faces both
    sides across the line."""
    eps = 0.05
    face: List[List[Tuple[float, float, int]]] = [[], [], [], []]
    if pads:
        xs, ys = [p[0] for p in pads], [p[1] for p in pads]
        cx, cy = (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0
        hw, hh = (max(xs) - min(xs)) / 2.0, (max(ys) - min(ys)) / 2.0
        if len(pads) >= 3 and hh < eps <= hw:
            face[2] = face[3] = list(pads)
        elif len(pads) >= 3 and hw < eps <= hh:
            face[0] = face[1] = list(pads)
        else:
            for x, y, n in pads:
                u = (x - cx) / hw if hw >= eps else 0.0
                v = (y - cy) / hh if hh >= eps else 0.0
                if max(abs(u), abs(v)) < 0.5:
                    continue
                sx, sy = (0 if u < 0 else 1), (2 if v < 0 else 3)
                if abs(u) > abs(v) + eps:
                    face[sx].append((x, y, n))
                elif abs(v) > abs(u) + eps:
                    face[sy].append((x, y, n))
                else:
                    col = sum(1 for q in pads if abs(q[0] - x) < eps)
                    row = sum(1 for q in pads if abs(q[1] - y) < eps)
                    if col >= row:
                        face[sx].append((x, y, n))
                    if row >= col:
                        face[sy].append((x, y, n))
    out = []
    for k, f in enumerate(face):
        nets = {q[2] for q in f} - set(skip)
        if not nets:
            out.append(None)
            continue
        along = [q[1] for q in f] if k < 2 else [q[0] for q in f]
        out.append((coef * len(nets), min(along) - FAN_MM, max(along) + FAN_MM))
    return tuple(out)


# --------------------------------------------------------------------------
# geometry

def mst_l1(pts: Sequence[Tuple[float, float]]):
    """`(length, edges)` of the Manhattan minimum spanning tree of `pts`
    (Prim from point 0, the lowest index among equal keys, a key lowered
    only by a strictly shorter edge); edges as `(x0, y0, x1, y1)`. The Rust
    kernel `grid_router.mst_l1` from three points."""
    n = len(pts)
    if n < 2:
        return 0.0, []
    if n == 2:
        (ax, ay), (bx, by) = pts
        return abs(bx - ax) + abs(by - ay), [(ax, ay, bx, by)]
    total, ij = grid_router.mst_l1(np.fromiter((p[0] for p in pts), float, n),
                                   np.fromiter((p[1] for p in pts), float, n))
    return total, [(pts[i][0], pts[i][1], pts[j][0], pts[j][1]) for i, j in ij]


def rect_gap(a, b) -> float:
    """Manhattan gap between two rects `(x0, y0, x1, y1)`; 0 when they touch
    or overlap."""
    return (max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
            + max(0.0, max(a[1], b[1]) - min(a[3], b[3])))


def _gaps(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """`rect_gap` of every row of A (k x 4) with every row of B (m x 4), k x m."""
    gx = np.maximum(A[:, None, 0], B[None, :, 0]) - np.minimum(A[:, None, 2], B[None, :, 2])
    gy = np.maximum(A[:, None, 1], B[None, :, 1]) - np.minimum(A[:, None, 3], B[None, :, 3])
    return np.maximum(gx, 0.0) + np.maximum(gy, 0.0)


def _at_add(at, p, order, ref):
    """One more pad of `ref` at position `p` in a net's `at` map (a new list,
    never the one the map held): `[(order, ref, count)]` sorted, so its first
    entry is the owner. Returns the owner."""
    rows = list(at.get(p, ()))
    for k, (o, r, c) in enumerate(rows):
        if r == ref:
            rows[k] = (o, r, c + 1)
            break
    else:
        rows.append((order, ref, 1))
        rows.sort()
    at[p] = rows
    return rows[0][1]


def _at_remove(at, p, ref):
    """One pad of `ref` fewer at `p`; returns the position's owner after, or
    None when no pad is left there (and `p` leaves the map)."""
    rows = []
    for o, r, c in at[p]:
        if r == ref:
            if c > 1:
                rows.append((o, r, c - 1))
        else:
            rows.append((o, r, c))
    if not rows:
        del at[p]
        return None
    at[p] = rows
    return rows[0][1]


def gap_tree(rects) -> Tuple[float, List[Tuple[int, int, float]]]:
    """`(length, edges)` of the minimum spanning tree over `rects`, each edge
    weighted by `rect_gap`; edges as `(i, j, gap)`, in Prim's order from
    `rects[0]`, ties to the lower index. The Rust kernel
    `grid_router.gap_tree`."""
    if len(rects) < 2:
        return 0.0, []
    return grid_router.gap_tree(np.asarray(rects, dtype=float).reshape(-1, 4))


def _orient(ax, ay, bx, by, cx, cy):
    return np.sign((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))


def crossings(A: np.ndarray, B: np.ndarray) -> int:
    """Proper crossings between segment arrays A (n x 4) and B (m x 4), in
    the Rust kernel `grid_router.seg_crossings`."""
    if len(A) == 0 or len(B) == 0:
        return 0
    return int(grid_router.seg_crossings(np.ascontiguousarray(A, dtype=float),
                                         np.ascontiguousarray(B, dtype=float)))


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
                 ground_nets=(), ground_weight: float = 0.0, pad_weight: float = 0.0,
                 hints: Sequence[Dict] = (), halo_coef: float = 0.25,
                 halo_weight: float = 10.0):
        self.state, self.pcb = state, pcb
        self.halo_coef, self.halo_weight = halo_coef, halo_weight
        self.hints = [h for h in hints if h['weight'] > 0]
        self.inner = tuple(state.usable)
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
        self._pidx: Dict[Tuple[str, str], List[int]] = {}
        self._fr: Dict = {}
        self._sites()
        # the parts that keep a halo (fine pitch, many pins), and how far
        # each one's reaches: its deepest front, the same at every turn (the
        # prefilter for pairs worth pricing)
        self._fine, self._reach = set(), {}
        for r, part in state.parts.items():
            offs = [(x - part.x, y - part.y) for x, y, _n in part.pad_globals()]
            if len(offs) >= HALO_MIN_PADS and pad_pitch(offs) <= HALO_PITCH_MM + 1e-6:
                self._fine.add(r)
            fr = self.fronts(r)
            self._reach[r] = max((f[0] for f in fr if f), default=0.0)
        self.net_len: Dict[int, float] = {}
        self.net_edges: Dict[int, np.ndarray] = {}
        self._moving = frozenset()
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
        key = (ref, str(num))
        idx = self._pidx.get(key)
        if idx is None:
            # pad_globals keeps pad order of the footprint's connected pads
            conn = [p for p in self.pcb.footprints[ref].pads if p.net_id > 0]
            idx = self._pidx[key] = [i for i, p in enumerate(conn)
                                     if str(p.pad_number) == str(num)]
        pg = self.state.parts[ref].pad_globals()
        return [(pg[i][0], pg[i][1]) for i in idx if i < len(pg)]

    # -- terms --------------------------------------------------------------
    def _pid(self, ref):
        ids = getattr(self, '_pids', None)
        if ids is None:
            ids = self._pids = {r: i for i, r in enumerate(sorted(self.state.parts))}
        return ids[ref]

    def _price_net(self, n, moved=None):
        """Net `n`'s tree: its length, its edges, and each edge's end parts
        (as `_pid` ids, for the foreign-pad test). Its points are the net's
        pad positions in sorted order, each owned by the first part in
        `net_refs` order with a pad there. They are kept per net (`_wh`) and,
        given `moved` (the parts a move has put at new poses since the net was
        last priced), updated from those parts' pads alone, so a net with
        many parts costs its moved pads to re-gather, not all of them."""
        wh = getattr(self, '_wh', None)
        if wh is None:
            wh = self._wh = {}
        W = wh.get(n) if moved is not None else None
        W = self._where_full(n) if W is None else self._where_moved(n, W, moved)
        wh[n] = W
        _order, _contrib, _at, _pts, xs, ys, own = W
        L, E, ends = grid_router.mst_l1_tree(np.array(xs, dtype=float), np.array(ys, dtype=float),
                                             np.array(own, dtype=np.int64))
        self.net_len[n] = L
        self.net_edges[n] = E
        ne = getattr(self, 'net_ends', None)
        if ne is None:
            ne = self.net_ends = {}
        ne[n] = ends

    def _where_full(self, n):
        """Net `n`'s points from scratch: `(order, contrib, at, pts, xs, ys,
        own)` -- each part's place in `net_refs`, each part's pad positions on
        the net, each position's parts as sorted `(order, ref, count)`, the
        positions sorted, their coordinates, and their owners' ids."""
        st = self.state
        refs = st.net_refs.get(n, ())
        order = {r: i for i, r in enumerate(refs)}
        contrib, at = {}, {}
        for r in refs:
            ps = [(x, y) for x, y, nn in st.parts[r].pad_globals() if nn == n]
            contrib[r] = ps
            for p in ps:
                _at_add(at, p, order[r], r)
        pts = sorted(at)
        own = [self._pid(at[p][0][1]) for p in pts]
        return (order, contrib, at, pts, [p[0] for p in pts], [p[1] for p in pts], own)

    def _where_moved(self, n, W, moved):
        """`W` with the parts of `moved` on net `n` at their live pads, built
        as a new value: `W` itself is the committed state a refused move
        returns to (`snapshot`, `restore`)."""
        order, contrib, at, pts, xs, ys, own = W
        st = self.state
        mv = [r for r in moved if r in order]
        changes = []
        for r in mv:
            new = [(x, y) for x, y, nn in st.parts[r].pad_globals() if nn == n]
            if new != contrib[r]:
                changes.append((r, new))
        if not changes:
            return W
        contrib, at = dict(contrib), dict(at)
        pts, xs, ys, own = list(pts), list(xs), list(ys), list(own)
        for r, new in changes:
            for p in contrib[r]:
                left = _at_remove(at, p, r)
                i = bisect.bisect_left(pts, p)
                if left is None:
                    del pts[i], xs[i], ys[i], own[i]
                else:
                    own[i] = self._pid(left)
            for p in new:
                fresh = p not in at
                first = _at_add(at, p, order[r], r)
                if fresh:
                    i = bisect.bisect_left(pts, p)
                    pts.insert(i, p)
                    xs.insert(i, p[0])
                    ys.insert(i, p[1])
                    own.insert(i, self._pid(first))
                else:
                    own[bisect.bisect_left(pts, p)] = self._pid(first)
            contrib[r] = new
        return (order, contrib, at, pts, xs, ys, own)

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

    def _sites(self):
        """The decap clusters, `[(net, caps, sites)]`: the decaps of one
        rail that serve one set of pins, and those pins' SITES -- a site the
        pins of one IC within SITE_MM of each other, chained (measured once:
        an IC is rigid)."""
        by: Dict = {}
        for cap, net in self.decaps:
            by.setdefault((net, tuple(sorted(self.pins_for(cap, net)))), []).append(cap)
        self.clusters = []
        for (net, pins), caps in sorted(by.items()):
            up = list(range(len(pins)))

            def root(i):
                while up[i] != i:
                    up[i] = up[up[i]]
                    i = up[i]
                return i
            xy = [self._pad_xy(ic, num) for ic, num in pins]
            for i in range(len(pins)):
                for j in range(i):
                    if pins[i][0] == pins[j][0] and any(
                            math.dist(p, q) <= SITE_MM for p in xy[i] for q in xy[j]):
                        up[root(i)] = root(j)
            sites: Dict[int, List] = {}
            for i, pin in enumerate(pins):
                sites.setdefault(root(i), []).append(pin)
            self.clusters.append((net, sorted(caps), [sites[k] for k in sorted(sites)]))
        self._cluster_of = {c: k for k, (_n, caps, _s) in enumerate(self.clusters) for c in caps}

    def cluster_match(self, k):
        """Cluster `k`'s decaps matched to its sites at the least total
        distance (a cap's rail pad to the site's nearest pin), every site
        taking one decap before any takes two: `(cost, {cap: site index})`."""
        net, caps, sites = self.clusters[k]
        if not sites:
            return 0.0, {}
        D = np.zeros((len(caps), len(sites)))
        pts = [[q for ic, num in site for q in self._pad_xy(ic, num)] for site in sites]
        for i, cap in enumerate(caps):
            mine = [(x, y) for x, y, nn in self._pads(cap) if nn == net]
            if mine:
                for j, P in enumerate(pts):
                    if P:
                        D[i, j] = min(math.dist(mine[0], q) for q in P)
        if len(caps) == 1:
            j = int(np.argmin(D[0]))
            return float(D[0, j]), {caps[0]: j}
        ns = len(sites)
        # a site's second decap costs 1e6 more than its first, so a matching
        # of least cost fills every site before doubling any
        C = np.hstack([D + 1e6 * t for t in range(-(-len(caps) // ns))])
        ri, ci = linear_sum_assignment(C)
        return float(D[ri, ci % ns].sum()), {caps[i]: int(c % ns) for i, c in zip(ri, ci)}

    def cluster_cost(self, k):
        return self.cluster_match(k)[0]

    def decap_cost(self, cap, net):
        """`cap`'s distance to the site it is matched to."""
        k = self._cluster_of.get(cap)
        if k is None:
            return 0.0
        _c, match = self.cluster_match(k)
        site = self.clusters[k][2][match[cap]]
        mine = [(x, y) for x, y, nn in self._pads(cap) if nn == net]
        pts = [q for ic, num in site for q in self._pad_xy(ic, num)]
        return min(math.dist(mine[0], q) for q in pts) if mine and pts else 0.0

    def group_cost(self, members):
        """The group's gap tree: 0 while its courtyards form one piece."""
        return gap_tree(self._rects(members))[0]

    def _rects(self, refs, moving=None):
        """`[part.rect()]` of `refs` as a k x 4 array: the committed rows
        kept per list (`_rc`, written by `_rects_commit`), the rows of
        `moving` (every part a priced move put at a new pose) read live."""
        rc = self.__dict__.setdefault('_rc', {})
        key = id(refs)
        got = rc.get(key)
        P = self.state.parts
        if got is None or got[0] is not refs:
            arr = np.asarray([P[r].rect() for r in refs], dtype=float).reshape(-1, 4)
            idx = {}
            for i, r in enumerate(refs):
                idx.setdefault(r, []).append(i)
            got = rc[key] = (refs, arr, idx)
            if moving is None:
                return arr
        _refs, arr, idx = got
        mv = self._moving if moving is None else moving
        live = [(i, r) for r in mv for i in idx.get(r, ())]
        if not live:
            return arr
        arr = arr.copy()
        for i, r in live:
            arr[i] = P[r].rect()
        return arr

    def _rects_commit(self, refs):
        """The committed rows of every cached list holding one of `refs`."""
        P = self.state.parts
        for _key, (_refs, arr, idx) in self.__dict__.get('_rc', {}).items():
            for r in refs:
                for i in idx.get(r, ()):
                    arr[i] = P[r].rect()

    def hint_cost(self, h):
        """A groups-file hint, weighted. `near`: the gap between the two
        groups' nearest courtyards (nothing once they touch). `apart`: how
        far that gap falls short of APART_MM. `edge`: how far the group's
        nearest courtyard is from its board edge, less the edge margin."""
        return self._hint_value(h, self._hint_full(h)[0])

    def _hint_value(self, h, gap):
        if h['kind'] == 'edge':
            return h['weight'] * max(0.0, gap)
        if h['kind'] == 'near':
            return h['weight'] * gap
        return h['weight'] * max(0.0, APART_MM - gap)

    def _edge_side(self, h):
        i = self.inner
        return {'N': lambda q: q[1] - i[1], 'S': lambda q: i[3] - q[3],
                'W': lambda q: q[0] - i[0], 'E': lambda q: i[2] - q[2]}[h['edge']]

    def _hint_full(self, h, seed=None):
        """`(gap, witness)` at the live poses: an `edge` hint's least side
        distance and the member at it, or the least gap between the two
        groups and a pair at it. `seed`, a pair to start from, bounds the
        search: B sits on a grid and each A part looks no further than the
        best gap so far (every pair, when there are few)."""
        P = self.state.parts
        if h['kind'] == 'edge':
            side = self._edge_side(h)
            return min((side(P[r].rect()), (r,)) for r in h['a'])
        A, B = h['a'], h['b']
        best, wit = math.inf, None
        if seed is not None:
            best, wit = rect_gap(P[seed[0]].rect(), P[seed[1]].rect()), seed
        if not A or not B:
            return best, wit
        G = _gaps(self._rects(A), self._rects(B))
        k = int(G.argmin())
        if G.flat[k] < best:        # the first pair at the least gap
            best, wit = float(G.flat[k]), (A[k // len(B)], B[k % len(B)])
        return best, wit

    def _hint_local(self, h, moving):
        """`(gap, witness)` with `moving` at their live poses and every other
        part where it is committed: the committed gap and its witness stand
        unless the witness moved, and only the moving parts' own pairs can
        lower it, so only they are looked at -- through the committed index,
        no further away than the committed gap. A witness that moved needs
        the full search."""
        gap, wit = self._hw[h['key']]
        if any(r in moving for r in wit):
            return self._hint_full(h, wit if h['kind'] != 'edge' else None)
        P = self.state.parts
        if h['kind'] == 'edge':
            side = self._edge_side(h)
            for r in h['a']:
                if r in moving:
                    s = side(P[r].rect())
                    if s < gap:
                        gap, wit = s, (r,)
            return gap, wit
        for side, other, a_side in ((h['a'], h['b'], True), (h['b'], h['a'], False)):
            mine = [d for d in side if d in moving]
            if not mine or not other:
                continue
            G = _gaps(np.asarray([P[d].rect() for d in mine]), self._rects(other))
            k = int(G.argmin())
            if G.flat[k] < gap:
                d, c = mine[k // len(other)], other[k % len(other)]
                gap, wit = float(G.flat[k]), ((d, c) if a_side else (c, d))
        return gap, wit

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

    def fronts(self, ref):
        """`pad_fronts` of `ref` at its live turn; none for a part that is
        not fine pitch with many pins."""
        if ref not in self._fine:
            return (None, None, None, None)
        part = self.state.parts[ref]
        key = (ref, part.rot)
        fr = self._fr.get(key)
        if fr is None:
            fr = self._fr[key] = pad_fronts(
                [(x - part.x, y - part.y, n) for x, y, n in part.pad_globals()],
                self.halo_coef, self.ground)
        return fr

    def halo_pair(self, a, b):
        """The pair's halo: on each axis, the depth of the fronts facing
        across it whose span reaches the other part, less the gap there;
        the smaller of the two shortfalls (the axis that separates them
        best), squared, times the halo weight. Nothing when no front of
        either reaches the other."""
        st = self.state
        pa, pb = st.parts[a], st.parts[b]
        if pa.locked and pb.locked:
            return 0.0      # two fixed parts: nothing to move, overlap by design
        if a in st.container_refs or b in st.container_refs:
            return 0.0      # a container is a frame, not a body
        if not set(pa.sides) & set(pb.sides):
            return 0.0      # on opposite faces, neither drilled
        if min(pa.pin_count, pb.pin_count) <= HALO_SMALL_PADS and self.wired(a, b):
            return 0.0
        ra, rb = pa.rect(), pb.rect()
        fa, fb = self.fronts(a), self.fronts(b)
        short, any_need = float('inf'), False
        # x: the W/E fronts, spanning y from the part's y; y: N/S, spanning x
        for lo, hi, olo, ohi, ca, cb, k in ((0, 2, 1, 3, pa.y, pb.y, 0),
                                            (1, 3, 0, 2, pa.x, pb.x, 2)):
            if ra[lo] + ra[hi] <= rb[lo] + rb[hi]:      # a first on this axis
                gap, sa, sb = rb[lo] - ra[hi], fa[k + 1], fb[k]
            else:
                gap, sa, sb = ra[lo] - rb[hi], fa[k], fb[k + 1]
            need = 0.0
            if sa is not None and sa[1] + ca < rb[ohi] and sa[2] + ca > rb[olo]:
                need += sa[0]
            if sb is not None and sb[1] + cb < ra[ohi] and sb[2] + cb > ra[olo]:
                need += sb[0]
            any_need = any_need or need > 0
            short = min(short, need - gap)
        if not any_need or short <= 0:
            return 0.0
        return self.halo_weight * short * short

    def halo_cost(self):
        """The halo penalty over every pair of parts, each pair once."""
        st = self.state
        names = sorted(st.parts)
        rect = {r: st.parts[r].rect() for r in names}
        total = 0.0
        for a in names:
            ra, ha = rect[a], self._reach[a]
            for b in names:
                if b <= a:
                    continue
                rb, need = rect[b], ha + self._reach[b]
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
        """Net `n`'s share of `demand` as `(i0, i1, j0, j1, cy, cx, s)`: s *
        outer(cy, cx) over the cells its box touches, cy and cx each cell's
        overlap with the box, s its tree's length over the box's area; None
        (fewer than two pads)."""
        h = self.pitch / 2.0
        E = self.net_edges.get(n)
        if E is not None:       # the tree the wire term just priced: its
            ex, ey = self._grid_edges()     # edges span its points' box exactly
            return grid_router.net_box(np.ascontiguousarray(E, dtype=float), self.net_len[n], h,
                                       ex, ey, self._gx0, self._gy0, self.cell, self._nx, self._ny)
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
        return (i0, i1, j0, j1, cy, cx, L / ((x1 - x0) * (y1 - y0)))

    @staticmethod
    def _dem_block(v):
        """A `_net_demand` value as its block."""
        return np.outer(v[4], v[5]) * v[6]

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
        on = np.asarray([[layer in l or '*.Cu' in l for layer in self.layers]
                         for *_r, l in shapes], dtype=bool)
        ex, ey = self._grid_edges()
        got = grid_router.pad_use(A, on, ex, ey, self._gx0, self._gy0, self.cell, self._nx, self._ny)
        if got is None:
            return {}
        i0, i1, j0, j1, blocks = got
        return {self.layers[L]: (i0, i1, j0, j1, blk) for L, blk in blocks}

    def _over2(self, i0, i1, j0, j1):
        """Overflow squared summed over grid window [i0:i1, j0:j1]."""
        if i1 <= i0 or j1 <= j0:
            return 0.0
        over = np.clip(self._D[i0:i1, j0:j1] - self._capacity(i0, i1, j0, j1) / self.pitch, 0.0, None)
        return float((over * over).sum())

    def _cong_init(self):
        """Every net's demand and every part's pad use, from scratch, their
        sums, the capacity they leave each cell, and the overflow total --
        what a move then corrects (`_cong_price`, `_cong_commit`)."""
        self._cset = set(self.cnets)
        self._dem = {n: self._net_demand(n) for n in self.cnets}
        self._use = {r: self._part_use(r) for r in sorted(self.state.parts)}
        self._D = np.zeros((self._ny, self._nx))
        for v in self._dem.values():
            if v is not None:
                self._D[v[0]:v[1], v[2]:v[3]] += self._dem_block(v)
        self._U = {L: np.zeros((self._ny, self._nx)) for L in self.layers}
        for u in self._use.values():
            for L, (i0, i1, j0, j1, blk) in u.items():
                self._U[L][i0:i1, j0:j1] += blk
        self._C = self._capacity(0, self._ny, 0, self._nx)
        self._S = self._over2(0, self._ny, 0, self._nx)
        self._cong_pending = None

    def _capacity(self, i0, i1, j0, j1):
        """Over grid window [i0:i1, j0:j1], the area each cell has left for
        tracks: clip(cell area - pad use, 0) summed over the layers."""
        cap = np.zeros((i1 - i0, j1 - j0))
        for L in self.layers:
            cap += np.clip(self.cell * self.cell - self._U[L][i0:i1, j0:j1], 0.0, None)
        return cap

    def _cong_nets(self, refs):
        st = self.state
        return sorted({n for r in refs for n in st.parts[r].nets if n in self._cset})

    def _cong_price(self, refs):
        """Price the congestion a move of `refs` makes, without touching the
        grids: the demand of the nets `refs` are on and the pad use of `refs`
        re-priced, and the overflow total corrected over the cells they reach
        (the Rust kernel `grid_router.cong_delta`). The change is kept as
        pending, written by `_cong_commit` if the move is kept and dropped if
        it is not -- a refused move (most of them) writes nothing."""
        dem = [(n, self._dem[n], self._net_demand(n)) for n in self._cong_nets(refs)]
        use = [(r, self._use[r], self._part_use(r)) for r in refs]
        wins = [v[:4] for _n, old, new in dem for v in (old, new) if v is not None]
        wins += [b[:4] for _r, old, new in use for u in (old, new) for b in u.values()]
        if not wins:
            self._cong_pending = (dem, use, self._S)
            return self._S
        win = (min(w[0] for w in wins), max(w[1] for w in wins),
               min(w[2] for w in wins), max(w[3] for w in wins))
        dterms = []
        for _n, old, new in dem:
            if (old is not None and new is not None and old[:4] == new[:4]
                    and np.array_equal(old[4], new[4]) and np.array_equal(old[5], new[5])):
                if new[6] != old[6]:    # the same box: only its density changes
                    dterms.append((new[6] - old[6], new[0], new[2], new[4], new[5]))
                continue
            dterms += [(sign * v[6], v[0], v[2], v[4], v[5])
                       for sign, v in ((-1.0, old), (1.0, new)) if v is not None]
        li = {L: k for k, L in enumerate(self.layers)}
        uterms = [(li[L], sign, b[0], b[2], b[4]) for _r, old, new in use
                  for sign, u in ((-1.0, old), (1.0, new)) for L, b in u.items()]
        S = self._S + grid_router.cong_delta(self._D, self._C, [self._U[L] for L in self.layers],
                                             win, dterms, uterms, self.cell * self.cell, self.pitch)
        self._cong_pending = (dem, use, S)
        return S

    def _cong_commit(self):
        """Write the pending congestion of the move just kept: its nets'
        demand and its parts' pad use into the grids, and the capacity again
        under the pad use it changed."""
        pend = getattr(self, '_cong_pending', None)
        if pend is None:
            return
        dem, use, S = pend
        for n, old, new in dem:
            for sign, v in ((-1.0, old), (1.0, new)):
                if v is not None:
                    self._D[v[0]:v[1], v[2]:v[3]] += sign * self._dem_block(v)
            self._dem[n] = new
        boxes = []
        for r, old, new in use:
            for sign, u in ((-1.0, old), (1.0, new)):
                for L, (i0, i1, j0, j1, blk) in u.items():
                    if sign < 0:
                        self._U[L][i0:i1, j0:j1] -= blk
                    else:
                        self._U[L][i0:i1, j0:j1] += blk
                    boxes.append((i0, i1, j0, j1))
            self._use[r] = new
        if boxes:
            i0, i1 = min(b[0] for b in boxes), max(b[1] for b in boxes)
            j0, j1 = min(b[2] for b in boxes), max(b[3] for b in boxes)
            self._C[i0:i1, j0:j1] = self._capacity(i0, i1, j0, j1)
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
        return pend[2] if pend is not None else self._S

    def snapshot(self, touched, refs):
        """What pricing a move of `refs` changes, to put back if the move is
        refused: the touched nets' trees (congestion is only pending)."""
        wh = getattr(self, '_wh', {})
        return {'nets': {n: (self.net_len[n], self.net_edges[n], self.net_ends[n], wh.get(n))
                         for n in touched[0]}}

    def restore(self, snap):
        for n, (L, E, ends, W) in snap['nets'].items():
            self.net_len[n], self.net_edges[n], self.net_ends[n] = L, E, ends
            if W is not None:
                self._wh[n] = W
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
        dec = sum(self.cluster_cost(k) for k in range(len(self.clusters)))
        per_group = {name: self.group_cost(m) for name, m in self.groups}
        grp = sum(per_group.values())
        per_hint = {h['key']: self.hint_cost(h) for h in self.hints}
        hint = sum(per_hint.values())
        halo = self.halo_cost()
        if self.qw > 0:
            self._cong_init()       # from scratch: total() is the reference
        cong = self.congestion()
        pads = self.pad_crossings() if self.pw_pad > 0 else 0.0
        return {'wire': wire, 'crossings': cross, 'claims': claims, 'decaps': dec,
                'groups': grp, 'per_group': per_group, 'hints': hint, 'per_hint': per_hint,
                'halo': halo, 'congestion': cong, 'pad_hits': pads,
                'cost': (wire + self.cw * cross + self.kw * claims + self.dw * dec
                         + self.gw * grp + hint + halo + self.qw * cong + self.pw_pad * pads)}

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
        `grid_router.seg_rect_hits`."""
        if len(S) == 0 or len(P) == 0:
            return np.zeros(0, dtype=int), np.zeros(0, dtype=int)
        return grid_router.seg_rect_hits(np.ascontiguousarray(S, dtype=float),
                                         np.ascontiguousarray(P, dtype=float))

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

    def pad_crossings(self):
        """Weighted foreign-pad hits: every signal edge on every pad."""
        return self._pad_hits(self._all_edges(), self._all_pads())

    def _names(self):
        return sorted(self.state.parts)

    def sync(self):
        """Index the live poses -- the parts' rects, every pad row, every
        signal tree edge (`_cedges`) -- and the by-part lookups: the COMMITTED
        state every move is priced against.
        A caller that keeps a move tells it (`commit`); one that moves parts
        some other way calls this, as `total` does."""
        self._ixp = SpatialIndex()
        self._npads = {}
        # pads as flat rows, an index key -> row (rows of a part are reused)
        self._PR = np.zeros((0, 4))
        self._PW = np.zeros(0)
        self._PN = np.zeros(0, dtype=int)
        self._PO = np.zeros(0, dtype=int)
        self._prow: Dict = {}
        names = self._names()
        self._maxhalo = max((self._reach[r] for r in names), default=0.0)
        for r in names:
            self._index_part(r)
        self._cedges = self._edges_of(self.nets)
        self._claims_by: Dict[str, List] = {}
        self._decaps_by: Dict[str, List] = {}
        self._groups_by: Dict[str, List] = {}
        for c in self.claims:
            for r in {c[0], c[1]}:
                self._claims_by.setdefault(r, []).append(c)
        for k, (_net, caps, sites) in enumerate(self.clusters):
            for r in set(caps) | {ic for site in sites for ic, _num in site}:
                self._decaps_by.setdefault(r, []).append(k)
        for name, m in self.groups:
            for r in m:
                self._groups_by.setdefault(r, []).append((name, m))
        self._hints_by: Dict[str, List[Dict]] = {}
        for h in self.hints:
            for r in set(h['a']) | set(h['b'] or ()):
                self._hints_by.setdefault(r, []).append(h)
        self._synced = True
        self._rc = {}
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
        self._clc = {id(c): self.claim_cost(c) for c in self.claims}
        self._dcc = {k: self.cluster_cost(k) for k in range(len(self.clusters))}
        self._hw = {h['key']: self._hint_full(h) for h in self.hints}
        self._hc = {h['key']: self._hint_value(h, self._hw[h['key']][0]) for h in self.hints}
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
        """`halo_pair`, or 0 at once when the two rects are farther apart
        than their reaches."""
        st = self.state
        ra, rb = st.parts[a].rect(), st.parts[b].rect()
        need = self._reach[a] + self._reach[b]
        if (ra[2] + need <= rb[0] or rb[2] + need <= ra[0]
                or ra[3] + need <= rb[1] or rb[3] + need <= ra[1]):
            return 0.0
        return self.halo_pair(a, b)

    def _pad_breakdown(self, edges, pads):
        """Weighted foreign hits of `edges` on `pads`, as {(net, owner pid): w}."""
        S, ends, labs = edges
        P, wts, pnets, owners = pads
        if not len(S) or not len(P):
            return {}
        c = np.ascontiguousarray
        rows = grid_router.pad_hit_sums(c(S, dtype=float), c(ends, dtype=np.int64),
                                        c(labs, dtype=np.int64), c(P, dtype=float),
                                        c(wts, dtype=float), c(pnets, dtype=np.int64),
                                        c(owners, dtype=np.int64))
        return {(a, b): w for a, b, w in rows}

    def _index_part(self, r):
        st = self.state
        self._ixp.remove(r)
        self._ixp.insert(r, st.parts[r].rect())
        rects, wts, nets, pids = self._pad_rows(r)
        k = len(rects)
        base = self._prow.get((r, 0))
        if base is None or self._npads.get(r, 0) != k:
            if base is not None:        # rows it no longer has: out of every test
                self._PR[base:base + self._npads[r]] = np.nan
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
            self._prow[(r, i)] = base + i
        self._npads[r] = k

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
        self._rects_commit(refs)
        if any(n in self.signal for n in nets):
            self._cedges = self._edges_of(self.nets)

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
        self._clc.update(pend['claims'])
        self._dcc.update(pend['decaps'])
        self._hc.update(pend['hints'])
        self._hw.update(pend['hint_w'])
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

    # -- local pricing of a set of moved parts --------------------------------
    def touched(self, refs):
        if not getattr(self, '_synced', False):
            self.sync()
        st = self.state
        nets = sorted({n for r in refs for n in st.parts[r].nets if n in self.net_len})
        cl = list({id(c): c for r in refs for c in self._claims_by.get(r, ())}.values())
        dc = sorted({d for r in refs for d in self._decaps_by.get(r, ())})
        gr = list({name: m for r in refs for name, m in self._groups_by.get(r, ())}.values())
        hs = list({h['key']: h for r in refs for h in self._hints_by.get(r, ())}.values())
        return nets, cl, dc, gr, hs

    def local(self, refs, touched=None, reprice=True):
        """The part of the cost `refs` can change. `reprice` False: the
        committed state's, read from the breakdowns (`_build_breakdowns`)
        and the cached trees -- a move's "before". True: re-priced at the
        live poses against the committed state near them, its breakdown
        held as pending for `commit` -- a move's "after"."""
        nets, cl, dc, gr, hs = touched or self.touched(refs)
        R = sorted(refs)
        T = [n for n in nets if n in self.signal]
        if not reprice:
            claims = sum(self._clc[id(c)] for c in cl)
            dec = sum(self._dcc[k] for k in dc)
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
            hint = sum(self._hc[h['key']] for h in hs)
            return (wire + self.cw * cross + self.kw * claims + self.dw * dec
                    + self.gw * grp + hint + halo + self.qw * self.congestion()
                    + self.pw_pad * pads)
        ccl = {id(c): self.claim_cost(c) for c in cl}
        cdc = {k: self.cluster_cost(k) for k in dc}
        claims = sum(ccl[id(c)] for c in cl)
        dec = sum(cdc[k] for k in dc)
        self._moving = frozenset(R)
        for n in nets:
            self._price_net(n, R)
        if self.qw > 0:
            if getattr(self, '_D', None) is None:
                self._cong_init()
            else:
                self._cong_price(refs)
        wire = sum(self.wire_weight(n) * self.net_len[n] for n in nets)
        pend = {'R': R, 'T': T, 'claims': ccl, 'decaps': cdc}
        cross, pend['cross'] = self._local_crossings_bd(T)
        groups = {name: self.group_cost(m) for name, m in zip(self._group_names(gr), gr)}
        pend['groups'] = groups
        moving = set(R)
        hw = {h['key']: self._hint_local(h, moving) for h in hs}
        pend['hint_w'] = hw
        pend['hints'] = {h['key']: self._hint_value(h, hw[h['key']][0]) for h in hs}
        if self.pw_pad > 0:
            pend['pads'] = self._local_pads_bd(T, R)
            pads = sum(pend['pads'].values())
        else:
            pend['pads'], pads = {}, 0.0
        pend['halo'] = self._local_halo_bd(R)
        self._pend = pend
        self._moving = frozenset()
        return (wire + self.cw * cross + self.kw * claims + self.dw * dec
                + self.gw * sum(groups.values()) + sum(pend['hints'].values())
                + sum(pend['halo'].values()) + self.qw * self.congestion()
                + self.pw_pad * pads)

    def _group_names(self, gr):
        names = {id(m): name for name, m in self.groups}
        return [names[id(m)] for m in gr]

    def _local_crossings_bd(self, T):
        """The crossings of `T`'s edges and their breakdown {(net, net):
        count}: against every committed edge of another net and among
        themselves, in the Rust kernel `grid_router.seg_cross_counts`."""
        T = [n for n in T if len(self.net_edges[n])]
        if not T:
            return 0, {}
        M = np.ascontiguousarray(np.vstack([self.net_edges[n] for n in T]), dtype=float)
        lab = np.concatenate([np.full(len(self.net_edges[n]), n) for n in T]).astype(np.int64)
        O, _ends, olab = self._cedges
        total, rows = grid_router.seg_cross_counts(M, lab, np.ascontiguousarray(O, dtype=float),
                                                   olab.astype(np.int64), np.asarray(T, dtype=np.int64))
        return total, {(a, b): c for a, b, c in rows}

    def _local_pads_bd(self, T, R):
        """A move's airwires over foreign pads as a breakdown {(net, owner
        pid): weight}: the edges of `T` over every committed pad but those of
        `R`, and over `R`'s pads where they are now; and every committed edge
        of another net over `R`'s pads (the Rust kernel
        `grid_router.pad_hit_sums`)."""
        c = np.ascontiguousarray
        mine = self._pads_of(R)
        mine = (c(mine[0], dtype=float), c(mine[1], dtype=float),
                c(mine[2], dtype=np.int64), c(mine[3], dtype=np.int64))
        S = self._edges_of(T)
        out: Dict = {}

        def add(rows):
            for a, b, w in rows:
                out[(a, b)] = out.get((a, b), 0.0) + w
        if len(S[0]):
            S = (c(S[0], dtype=float), c(S[1], dtype=np.int64), c(S[2], dtype=np.int64))
            add(grid_router.pad_hit_sums(
                *S, c(self._PR, dtype=float), c(self._PW, dtype=float),
                c(self._PN, dtype=np.int64), c(self._PO, dtype=np.int64),
                skip_owners=np.asarray([self._pid(r) for r in R], dtype=np.int64)))
            if len(mine[0]):
                add(grid_router.pad_hit_sums(*S, *mine))
        if len(mine[0]):
            E, ends, labs = self._cedges
            add(grid_router.pad_hit_sums(c(E, dtype=float), c(ends, dtype=np.int64),
                                         c(labs, dtype=np.int64), *mine,
                                         skip_nets=np.asarray(list(T), dtype=np.int64)))
        return out

    def _local_halo_bd(self, R):
        """The halo of `R`'s parts against their neighbours as a breakdown
        {(a, b): value}, nonzero pairs only."""
        st = self.state
        rset = set(R)
        out: Dict = {}
        for a in R:
            ra, ha = st.parts[a].rect(), self._reach[a]
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
    """Six of the cost's terms (`TERMS`) split onto parts, in cost units: a
    net tree edge's length half to each end's part; a crossing a quarter to
    each of its two edges' four end parts; a claim to its subject; a decap's
    distance to its matched site to the cap; a group tree edge's gap half to
    each member; a halo pair half to each part. Each part's `total` is the
    sum of its six. The hint, congestion and pad-hit terms are not split, so
    the parts' totals fall short of `model.total()['cost']` by those three.
    A debugging view: nothing in the placer calls it."""
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
        for b in names[i + 1:]:
            h = model.halo_pair(a, b)
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
    k = model._cluster_of.get(ref)
    if t is None and k is not None:     # a decap: the site it is matched to
        _c, match = model.cluster_match(k)
        sites = model.clusters[k][2]
        for site in [sites[match[ref]]] + sites:
            pins = [q for ic, num in site if ic in placed for q in model._pad_xy(ic, num)]
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

