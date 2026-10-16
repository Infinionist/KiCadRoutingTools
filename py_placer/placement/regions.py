"""Board regions for grouped placement: one rectangle per functional group.

`grouping` says which parts belong together; this decides WHERE each group
goes. Geometry is computed here; the semantics (which edge a group belongs on,
which groups neighbour, which stay apart) are the agent's hints in the groups
file, and the board's own wiring.

The model:

  * FREE SPACE per face, on a grid (`grid` mm): cells inside the outline,
    inset by the edge clearance, that no FIXED part occupies on that face. A
    fixed part is one the file locks; it occupies its own face by its
    occupancy (courtyard) rect and the other face by its drilled pads' box,
    so a through-hole header blocks both faces and an SMD part one.
  * A SLICING FLOORPLAN of the inset outline's bounding box: a binary tree of
    horizontal and vertical cuts with one group per leaf. Each cut is placed
    so that the FREE area on each side is proportional to the demand on that
    side, so a region that overlaps a fixed part is simply larger. Up to
    `EXHAUSTIVE_MAX` groups every distinct floorplan is evaluated (normalized
    trees: a node's right child never repeats its operator, so each floorplan
    is generated once); beyond that, simulated annealing over the same trees
    with a seeded random stream. Both are deterministic.
  * COST, lower is better:
      - wiring: signal links between groups x Manhattan distance between
        their regions' free-area centroids, plus links from a group to each
        FIXED part's pads (pad-level terminals, so a core lands beside the
        header it talks to);
      - anchor: a group holding fixed members (a connector placed first) is
        pulled to touch them;
      - edge: a group with an `edge` hint pays its distance from that side;
      - near / apart: hinted pairs pay for distance / for adjacency;
      - shape and capacity are HARD: a region that cannot hold its largest
        member at some quarter turn, or whose free area is below its members'
        total occupancy area, is infeasible.

What it returns is the zones, each with the measurements it was chosen on.
`plan_regions.py` writes them into an intent's `blocks[]` (zone, side,
tolerance) where `check_floorplan --plan-only` and every seat search already
read them.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

import numpy as np

from placement.grouping import BoardView, link_matrix
from placement.legality import footprint_side

#: Every floorplan is evaluated up to this many groups (283,680 at 6).
EXHAUSTIVE_MAX = 6
#: Cost weights (mm-equivalents per unit). Wiring is in links x mm.
W_ANCHOR = 6.0       # per mm a group's region stands off its fixed members
W_EDGE = 6.0         # per mm a group's region stands off its hinted edge
W_NEAR = 2.0         # per mm a `near` pair stands apart beyond the gutter
W_APART = 4.0        # per mm a pair `apart` sits closer than APART_MM
APART_MM = 3.0
#: Free space narrower than this is removed (a morphological opening): the
#: 0.3 mm slivers between a back-side header's drilled pads hold no part, and
#: counted as area they made a strip down the header a "region".
MIN_FEATURE_MM = 1.0
#: A group's own fixed members (a connector placed first) are wiring
#: terminals at this multiple of a link, so the group sits by the PADS it
#: wires to -- not merely touching the connector's courtyard at a corner.
OWN_FIXED_WEIGHT = 2.0
W_SPREAD = 1.0       # per internal link x the group cluster's expected span
#: How densely a group's parts pack around their target (courtyard area over
#: the cluster's area), for the cluster-span estimate.
CLUSTER_DENSITY = 0.6
W_ASPECT = 2.0       # per unit of aspect ratio beyond ASPECT_FREE
ASPECT_FREE = 4.0
INFEASIBLE = 1e6


class Group:
    """One group's planning inputs."""

    def __init__(self, name, side, movable, fixed, demand, need_area,
                 dims, edge, near, apart, internal_links=0):
        self.name, self.side = name, side
        self.movable, self.fixed = movable, fixed
        self.internal_links = internal_links
        self.demand = demand        # mm^2 of free area it asks for
        self.need_area = need_area  # sum of member occupancy areas (hard floor)
        self.dims = dims            # (short, long) of its largest member
        self.edge, self.near, self.apart = edge, near, apart


def _outline(pcb, pcb_file):
    """The board outline as a shapely polygon (the bbox when only a plain
    rectangle is drawn)."""
    from shapely.geometry import Polygon, box
    from kicad_parser import extract_board_contours
    try:
        text = open(pcb_file, encoding='utf-8', errors='replace').read()
        outers, cutouts = extract_board_contours(text)
    except Exception:
        outers, cutouts = [], []
    if outers:
        try:
            poly = Polygon(outers[0], holes=list(cutouts))
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.area > 1:
                return poly
        except Exception:
            pass
    bb = pcb.board_info.board_bounds
    return box(*bb) if bb else None


def _occupancy(pcb, pcb_file, ref, cache):
    """`(own-face rect, drilled-pad rect or None)` at the part's pose."""
    from placement import legality
    fp = pcb.footprints[ref]
    pose = (fp.x, fp.y, fp.rotation or 0.0)
    pads = [p for p in fp.pads if getattr(p, 'pad_type', '') != 'np_thru_hole']
    xs = [p.global_x for p in pads] or [fp.x]
    ys = [p.global_y for p in pads] or [fp.y]
    fallback = (min(xs) - 0.5, min(ys) - 0.5, max(xs) + 0.5, max(ys) + 0.5)
    rect = legality.occupancy_rect_at(pcb, ref, pose, fallback=fallback,
                                      pcb_file=pcb_file, cache=cache) or fallback
    drilled = [legality.pad_rect(p) for p in pads if (getattr(p, 'drill', 0) or 0) > 0]
    tht = None
    if drilled:
        tht = (min(r[0] for r in drilled), min(r[1] for r in drilled),
               max(r[2] for r in drilled), max(r[3] for r in drilled))
    return rect, tht


class FreeGrid:
    """Free cells of one face, with prefix sums for O(1) area and centroid."""

    def __init__(self, x0, y0, nx, ny, grid, free):
        self.x0, self.y0, self.nx, self.ny, self.g = x0, y0, nx, ny, grid
        self.free = free.astype(np.float64)
        xs = (np.arange(nx) + 0.5) * grid + x0
        ys = (np.arange(ny) + 0.5) * grid + y0
        X, Y = np.meshgrid(xs, ys)          # [ny, nx]
        self.S = self._ps(self.free)
        self.SX = self._ps(self.free * X)
        self.SY = self._ps(self.free * Y)

    @staticmethod
    def _ps(a):
        s = np.zeros((a.shape[0] + 1, a.shape[1] + 1))
        s[1:, 1:] = a.cumsum(0).cumsum(1)
        return s

    @staticmethod
    def _q(s, i0, j0, i1, j1):
        """Sum over rows i0..i1-1, cols j0..j1-1."""
        return s[i1, j1] - s[i0, j1] - s[i1, j0] + s[i0, j0]

    def cells(self, i0, j0, i1, j1):
        return self._q(self.S, i0, j0, i1, j1)

    def centroid(self, i0, j0, i1, j1):
        n = self.cells(i0, j0, i1, j1)
        if n <= 0:
            return (self.x0 + (j0 + j1) / 2.0 * self.g, self.y0 + (i0 + i1) / 2.0 * self.g)
        return (self._q(self.SX, i0, j0, i1, j1) / n, self._q(self.SY, i0, j0, i1, j1) / n)

    def rect_mm(self, i0, j0, i1, j1):
        return (self.x0 + j0 * self.g, self.y0 + i0 * self.g,
                self.x0 + j1 * self.g, self.y0 + i1 * self.g)


def _window_sum(a, r):
    """Sum of `a` over the (2r+1)^2 window around each cell (zero-padded)."""
    p = np.pad(a.astype(np.int32), r)
    s = np.zeros((p.shape[0] + 1, p.shape[1] + 1), dtype=np.int64)
    s[1:, 1:] = p.cumsum(0).cumsum(1)
    k = 2 * r + 1
    return s[k:, k:] - s[:-k, k:] - s[k:, :-k] + s[:-k, :-k]


def _open(free, r):
    """Morphological opening with a (2r+1)-cell square: keep only free space
    a whole window fits in."""
    if r <= 0:
        return free
    k2 = (2 * r + 1) ** 2
    core = _window_sum(free, r) == k2
    return _window_sum(core, r) > 0


def _free_grid(poly, inset_mm, obstacles, grid):
    from shapely import contains_xy
    inner = poly.buffer(-inset_mm) if inset_mm > 0 else poly
    x0, y0, x1, y1 = inner.bounds
    nx = max(1, int(math.ceil((x1 - x0) / grid)))
    ny = max(1, int(math.ceil((y1 - y0) / grid)))
    xs = (np.arange(nx) + 0.5) * grid + x0
    ys = (np.arange(ny) + 0.5) * grid + y0
    X, Y = np.meshgrid(xs, ys)
    free = contains_xy(inner, X, Y)
    for (a, b, c, d) in obstacles:
        free &= ~((X >= a) & (X <= c) & (Y >= b) & (Y <= d))
    free = _open(free, int(round(MIN_FEATURE_MM / grid / 2.0)))
    return FreeGrid(x0, y0, nx, ny, grid, free)


# ---------------------------------------------------------------------------
# floorplans
# ---------------------------------------------------------------------------

def _subsets(items):
    """Every non-empty proper subset of `items` (a tuple), as tuples."""
    n = len(items)
    for mask in range(1, (1 << n) - 1):
        yield tuple(items[i] for i in range(n) if mask >> i & 1)


def enumerate_trees(items, parent_op=None, right=False):
    """Every distinct slicing floorplan over `items`: ('H'|'V', left, right)
    nodes, leaves are item names. 'H' cuts horizontally (left = top), 'V'
    vertically (left = west). A right child never repeats its parent's
    operator, so each floorplan is generated exactly once."""
    items = tuple(items)
    if len(items) == 1:
        yield items[0]
        return
    for op in ('H', 'V'):
        if right and op == parent_op:
            continue
        for left in _subsets(items):
            rest = tuple(i for i in items if i not in left)
            for tl in enumerate_trees(left, op, False):
                for tr in enumerate_trees(rest, op, True):
                    yield (op, tl, tr)


def _leaves(t):
    return [t] if isinstance(t, str) else _leaves(t[1]) + _leaves(t[2])


def layout(tree, fg: FreeGrid, demand: Dict[str, float], box=None):
    """{group: (i0, j0, i1, j1)} cell boxes for `tree` inside `box` (all cells
    by default): each cut puts free area on each side in proportion to its
    demand, and never leaves a side empty."""
    if box is None:
        box = (0, 0, fg.ny, fg.nx)
    if isinstance(tree, str):
        return {tree: box}
    op, tl, tr = tree
    i0, j0, i1, j1 = box
    dl = sum(demand[g] for g in _leaves(tl))
    dr = sum(demand[g] for g in _leaves(tr))
    frac = dl / max(1e-9, dl + dr)
    total = fg.cells(i0, j0, i1, j1)
    if op == 'H':
        lo, hi = i0 + 1, i1 - 1
        if hi < lo:
            k = (i0 + i1) // 2
        else:
            k = _cut(lambda m: fg.cells(i0, j0, m, j1), lo, hi, frac * total)
        a, b = (i0, j0, k, j1), (k, j0, i1, j1)
    else:
        lo, hi = j0 + 1, j1 - 1
        if hi < lo:
            k = (j0 + j1) // 2
        else:
            k = _cut(lambda m: fg.cells(i0, j0, i1, m), lo, hi, frac * total)
        a, b = (i0, j0, i1, k), (i0, k, i1, j1)
    out = layout(tl, fg, demand, a)
    out.update(layout(tr, fg, demand, b))
    return out


def _cut(area_upto, lo, hi, target):
    """The index in [lo, hi] whose cumulative free area is nearest `target`
    (monotone, so a binary search)."""
    a, b = lo, hi
    while a < b:
        m = (a + b) // 2
        if area_upto(m) < target:
            a = m + 1
        else:
            b = m
    if a > lo and abs(area_upto(a - 1) - target) <= abs(area_upto(a) - target):
        return a - 1
    return a


def _gap(r, s):
    """Edge-to-edge distance between two mm rects (0 when they touch)."""
    dx = max(0.0, max(r[0], s[0]) - min(r[2], s[2]))
    dy = max(0.0, max(r[1], s[1]) - min(r[3], s[3]))
    return math.hypot(dx, dy)


class Planner:
    """Everything the cost needs, built once per face."""

    def __init__(self, groups: Dict[str, Group], fg: FreeGrid, bounds,
                 links: Dict[Tuple[str, str], int], terminals: Dict[str, List],
                 fixed_rects: Dict[str, Tuple], gutter: float):
        self.groups, self.fg, self.bounds = groups, fg, bounds
        self.links, self.terminals = links, terminals
        self.fixed_rects, self.gutter = fixed_rects, gutter
        self.demand = {n: g.demand for n, g in groups.items()}
        # where each group's largest part FITS in the free cells, per quarter
        # turn: fit[i, j] = the (hc x wc) window anchored at cell (i, j) is
        # all free. Its prefix sum answers "does it fit anywhere inside this
        # region" in O(1) -- the region's rectangle alone says nothing when a
        # header's pads take half of it.
        self.fits = {n: [self._fit(g.dims[0], g.dims[1]),
                         self._fit(g.dims[1], g.dims[0])]
                     for n, g in groups.items()}
        self.gut_cells = int(math.ceil(gutter / 2.0 / fg.g))

    def _fit(self, h_mm, w_mm):
        fg = self.fg
        hc = max(1, int(math.ceil(h_mm / fg.g)))
        wc = max(1, int(math.ceil(w_mm / fg.g)))
        if hc > fg.ny or wc > fg.nx:
            return (hc, wc, None)
        S = fg.S
        win = (S[hc:, wc:] - S[:-hc, wc:] - S[hc:, :-wc] + S[:-hc, :-wc])
        ok = (win >= hc * wc - 1e-9).astype(np.float64)   # [ny-hc+1, nx-wc+1]
        return (hc, wc, FreeGrid._ps(ok))

    def _fits_in(self, name, cells):
        """Does the group's largest part fit in the free cells of `cells`
        (shrunk by half a gutter on every side), at some quarter turn?"""
        i0, j0, i1, j1 = cells
        k = self.gut_cells
        i0, j0, i1, j1 = i0 + k, j0 + k, i1 - k, j1 - k
        for hc, wc, ps in self.fits[name]:
            if ps is None:
                continue
            a0, a1 = i0, i1 - hc + 1      # anchor rows [a0, a1)
            b0, b1 = j0, j1 - wc + 1
            if a1 <= a0 or b1 <= b0:
                continue
            if FreeGrid._q(ps, a0, b0, a1, b1) > 0:
                return True
        return False

    def evaluate(self, tree) -> Tuple[float, Dict]:
        cells = layout(tree, self.fg, self.demand)
        fg = self.fg
        rect = {n: fg.rect_mm(*c) for n, c in cells.items()}
        cen = {n: fg.centroid(*c) for n, c in cells.items()}
        free = {n: fg.cells(*c) * fg.g * fg.g for n, c in cells.items()}
        cost = 0.0
        terms = {'wire': 0.0, 'spread': 0.0, 'anchor': 0.0, 'edge': 0.0,
                 'near': 0.0, 'apart': 0.0, 'aspect': 0.0, 'infeasible': 0.0}
        for n, g in self.groups.items():
            x0, y0, x1, y1 = rect[n]
            w, h = x1 - x0 - self.gutter, y1 - y0 - self.gutter
            short, long_ = max(1e-6, min(w, h)), max(w, h)
            if not self._fits_in(n, cells[n]):
                terms['infeasible'] += INFEASIBLE + max(0.0, g.dims[0] - short) \
                    + max(0.0, g.dims[1] - long_)
            # the group's cluster: a square blob when the region is wide
            # enough, else a strip `short` wide -- intra-group wire length
            # grows with its span (mean Manhattan distance ~ (a + b) / 3)
            blob = g.need_area / CLUSTER_DENSITY
            a = min(short, math.sqrt(blob))
            terms['spread'] += W_SPREAD * g.internal_links * (a + blob / a) / 3.0
            if free[n] + 1e-6 < g.need_area:
                terms['infeasible'] += INFEASIBLE + (g.need_area - free[n])
            asp = long_ / max(1e-6, short)
            if asp > ASPECT_FREE:
                terms['aspect'] += W_ASPECT * (asp - ASPECT_FREE)
            for fr in (self.fixed_rects.get(f) for f in g.fixed):
                if fr is not None:
                    terms['anchor'] += W_ANCHOR * _gap(rect[n], fr)
            if g.edge:
                bx0, by0, bx1, by1 = self.bounds
                d = {'N': y0 - by0, 'S': by1 - y1, 'W': x0 - bx0, 'E': bx1 - x1}[g.edge]
                terms['edge'] += W_EDGE * max(0.0, d)
            for (tx, ty, w_) in self.terminals.get(n, ()):
                terms['wire'] += w_ * (abs(cen[n][0] - tx) + abs(cen[n][1] - ty))
        for (a, b), w_ in self.links.items():
            if a in cen and b in cen:
                terms['wire'] += w_ * (abs(cen[a][0] - cen[b][0]) + abs(cen[a][1] - cen[b][1]))
        for n, g in self.groups.items():
            for m in g.near:
                if m in rect and n < m:
                    terms['near'] += W_NEAR * max(0.0, _gap(rect[n], rect[m]) - self.gutter)
            for m in g.apart:
                if m in rect and n < m:
                    terms['apart'] += W_APART * max(0.0, APART_MM - _gap(rect[n], rect[m]))
        terms = {k: float(v) for k, v in terms.items()}
        cost = sum(terms.values())
        return cost, {'cells': cells, 'rect': rect, 'centroid': cen,
                      'free_mm2': free, 'terms': terms}

    def search(self, names, seed=0, iters=None):
        """The best tree over `names`: exhaustive up to EXHAUSTIVE_MAX,
        annealed beyond. Ties keep the first found (deterministic order)."""
        names = sorted(names)
        if len(names) == 1:
            c, info = self.evaluate(names[0])
            return names[0], c, info, 1
        best = None
        n_eval = 0
        if len(names) <= EXHAUSTIVE_MAX:
            for t in enumerate_trees(names):
                c, _ = self.evaluate(t)
                n_eval += 1
                if best is None or c < best[1] - 1e-9:
                    best = (t, c)
        else:
            best, n_eval = self._anneal(names, seed, iters or 20000)
        c, info = self.evaluate(best[0])
        return best[0], c, info, n_eval

    def _anneal(self, names, seed, iters):
        """Simulated annealing over trees: moves swap two leaves, flip an
        operator, or re-split a node. Deterministic for a seed."""
        rng = random.Random(f"regions:{seed}")

        def balanced(items, op='V'):
            if len(items) == 1:
                return items[0]
            k = len(items) // 2
            nop = 'H' if op == 'V' else 'V'
            return (op, balanced(items[:k], nop), balanced(items[k:], nop))

        def nodes(t, path=()):
            if isinstance(t, str):
                return [path]
            return [path] + nodes(t[1], path + (1,)) + nodes(t[2], path + (2,))

        def get(t, path):
            for p in path:
                t = t[p]
            return t

        def put(t, path, v):
            if not path:
                return v
            lst = list(t)
            lst[path[0]] = put(t[path[0]], path[1:], v)
            return tuple(lst)

        cur = balanced(list(names))
        cur_c, _ = self.evaluate(cur)
        best, best_c = cur, cur_c
        temp0 = max(1.0, cur_c * 0.05)
        n_eval = 1
        for it in range(iters):
            temp = temp0 * (1.0 - it / iters) + 1e-3
            kind = rng.random()
            t = cur
            internal = [p for p in nodes(t) if not isinstance(get(t, p), str)]
            if kind < 0.4:      # swap two leaves
                leaves = _leaves(t)
                a, b = rng.sample(leaves, 2)
                t = _swap_leaves(t, a, b)
            elif kind < 0.7:    # flip an operator
                p = rng.choice(internal)
                node = get(t, p)
                t = put(t, p, ('V' if node[0] == 'H' else 'H', node[1], node[2]))
            else:               # swap a node's children
                p = rng.choice(internal)
                node = get(t, p)
                t = put(t, p, (node[0], node[2], node[1]))
            c, _ = self.evaluate(t)
            n_eval += 1
            if c < cur_c or rng.random() < math.exp(-(c - cur_c) / temp):
                cur, cur_c = t, c
                if c < best_c - 1e-9:
                    best, best_c = t, c
        return (best, best_c), n_eval


def _swap_leaves(t, a, b):
    if isinstance(t, str):
        return b if t == a else a if t == b else t
    return (t[0], _swap_leaves(t[1], a, b), _swap_leaves(t[2], a, b))


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------

def plan(pcb, pcb_file: str, doc: Dict, *, grid: float = 0.25,
         util: float = 0.55, gutter: float = 0.6, edge_clearance: float = 0.3,
         seed: int = 0, iters: Optional[int] = None) -> Dict:
    """Plan one region per group with movable parts. Returns
    {'zones': {group: {...}}, 'faces': {...}, 'warnings': [...]}."""
    view = BoardView(pcb)
    poly = _outline(pcb, pcb_file)
    if poly is None:
        raise ValueError("the board has no outline to plan regions in")
    cache: Dict = {}
    fixed = sorted(view.locked)
    occ = {r: _occupancy(pcb, pcb_file, r, cache) for r in fixed}
    warnings: List[str] = []

    # per-group inputs
    groups: Dict[str, Group] = {}
    for g in doc['groups']:
        refs = [r for r in g['refs'] if r in view.parts]
        mov = [r for r in refs if r not in view.locked]
        fix = [r for r in refs if r in view.locked]
        if not mov:
            continue
        areas, dims = [], []
        for r in mov:
            rect, _ = _occupancy(pcb, pcb_file, r, cache)
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            areas.append(w * h)
            dims.append((min(w, h), max(w, h)))
        side = g.get('side') or max(('F', 'B'), key=lambda s: sum(
            1 for r in mov if footprint_side(pcb.footprints[r]) == s))
        need = sum(areas)
        members = set(refs)
        internal = sum(n for r in mov for o, n in view.signal_partners(r).items()
                       if o in members) // 2
        groups[g['name']] = Group(
            g['name'], side, mov, fix, need / max(0.05, util), need,
            max(dims, key=lambda d: (d[0] * d[1], d)), g.get('edge'),
            list(g.get('near') or ()), list(g.get('apart') or ()),
            internal_links=max(internal, len(mov) - 1))

    links = link_matrix(doc, view)
    # pad-level terminals: a group's signal links to FIXED parts
    terminals: Dict[str, List] = {}
    for n, g in groups.items():
        rows = {}
        for r in g.movable:
            for nid in view.nets_of.get(r, ()):
                if nid in view.rails:
                    continue
                for f in view.owners.get(nid, ()):
                    if f in view.locked:
                        pads = [(p.global_x, p.global_y) for p in pcb.footprints[f].pads
                                if p.net_id == nid]
                        if pads:
                            key = (f, nid)
                            px = sum(x for x, _ in pads) / len(pads)
                            py = sum(y for _, y in pads) / len(pads)
                            prev = rows.get(key)
                            rows[key] = (px, py, (prev[2] if prev else 0) + 1)
        # a group's own fixed members count OWN_FIXED_WEIGHT per link
        terminals[n] = [(v[0], v[1], v[2] * (OWN_FIXED_WEIGHT if f in g.fixed else 1.0))
                        for (f, _nid), v in sorted(rows.items())]

    zones: Dict[str, Dict] = {}
    faces: Dict[str, Dict] = {}
    for side in ('F', 'B'):
        names = [n for n, g in groups.items() if g.side == side]
        if not names:
            continue
        obstacles = []
        for r in fixed:
            own, tht = occ[r]
            obstacles.append(own if footprint_side(pcb.footprints[r]) == side else tht)
        fg = _free_grid(poly, edge_clearance, [o for o in obstacles if o], grid)
        bounds = (fg.x0, fg.y0, fg.x0 + fg.nx * grid, fg.y0 + fg.ny * grid)
        fixed_rects = {r: (occ[r][0] if footprint_side(pcb.footprints[r]) == side else occ[r][1])
                       for r in fixed}
        side_links = {k: v for k, v in links.items() if k[0] in names and k[1] in names}
        pl = Planner({n: groups[n] for n in names}, fg, bounds, side_links,
                     terminals, fixed_rects, gutter)
        free_total = fg.cells(0, 0, fg.ny, fg.nx) * grid * grid
        need_total = sum(groups[n].need_area for n in names)
        if need_total > free_total:
            warnings.append(f"face {side}: the groups' parts need {need_total:.0f} mm2 "
                            f"and only {free_total:.0f} mm2 is free -- no plan can hold them")
        tree, cost, info, n_eval = pl.search(names, seed=seed, iters=iters)
        faces[side] = {'tree': tree, 'cost': round(cost, 3), 'evaluated': n_eval,
                       'free_mm2': round(free_total, 1), 'need_mm2': round(need_total, 1),
                       'utilisation': round(need_total / max(1e-9, free_total), 3),
                       'terms': {k: round(v, 2) for k, v in info['terms'].items()}}
        if info['terms']['infeasible'] >= INFEASIBLE:
            warnings.append(f"face {side}: the best plan is still infeasible (a region "
                            f"cannot hold its largest part, or its parts' area)")
        for n in names:
            r = info['rect'][n]
            # gutters: pull each INTERNAL side in by half a gutter; a side on
            # the plan's boundary stays where the free space ends
            x0, y0, x1, y1 = r
            h = gutter / 2.0
            x0 += h if x0 > bounds[0] + 1e-6 else 0.0
            y0 += h if y0 > bounds[1] + 1e-6 else 0.0
            x1 -= h if x1 < bounds[2] - 1e-6 else 0.0
            y1 -= h if y1 < bounds[3] - 1e-6 else 0.0
            zones[n] = {'zone': [round(v, 3) for v in (x0, y0, x1, y1)],
                        'side': side, 'free_mm2': round(info['free_mm2'][n], 1),
                        'need_mm2': round(groups[n].need_area, 1),
                        'centroid': [round(v, 3) for v in info['centroid'][n]],
                        'refs': groups[n].movable, 'fixed': groups[n].fixed}
    return {'zones': zones, 'faces': faces, 'warnings': warnings,
            'params': {'grid': grid, 'util': util, 'gutter': gutter,
                       'edge_clearance': edge_clearance, 'seed': seed}}
