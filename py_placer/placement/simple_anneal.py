"""The simple path's optimiser: simulated annealing on the exact cost.

The cost is `simple.Model`'s, exactly as it is priced -- each net's Manhattan
spanning tree, crossings among those trees, claims, decaps, each group's gap
tree, the halo, foreign-pad hits and congestion -- plus, while it runs, an
OVERLAP penalty: the area where two parts' courtyards, each grown by half
the clearance, intersect on a face they share, how far a courtyard is
outside the board less its edge margin -- the real outline, cutouts and
notches too -- and how far a part breaks the intent's keep-outs and zones
and the board's rule-area keep-outs. Its weight rises from
`LAM_START` to `LAM_END` as the run cools, so parts may pass through each
other early and a group can cross a crowded board; only an overlap-free
state can become the best one,
and the best one is finally checked by the exact pad legality
(`simple.legal`), a part failing it moved to the nearest legal pose.

Moves, each priced by `Model.local` against the committed state near it:

    shift   one part by up to `reach` mm
    turn    one part to another quarter turn, in place
    swap    two parts of one footprint exchange poses
    jump    one part to its wiring target, at a random turn
    dock    one part against a partner: for one of its nets, the nearest
            partner pad's part, one of its four sides, one of the four
            turns -- the courtyards at minimum clearance, the part slid
            along that side to line its pad up with the partner pad
    gshift  a group's movable members by the same step
    gturn   a group's movable members turned together about their centre
    gswap   two groups with no fixed member exchange places
    escape  a part that overlaps something, to the least-overlap pose of
            ESCAPE_TRIES drawn at random over the board

Group turns and swaps are drawn only in the first quarter of the run
(`BIG_UNTIL`): they move a whole group far at once, and on every board
measured none was kept after the first fifth, while each one costs its
group's size in single moves to price.

A move never leaves the board: the parts it moves are translated together
until their courtyards are inside the board less its edge margin. A part
pushed off the board behind a fixed one (a header on the edge) could
otherwise only come back over it, a jump the overlap weight refuses late in
the run. Only the kinds a board can make are drawn: no group moves on a
board without groups, no swap without two parts of one footprint.

`reach` falls from a sixth of the board as (1 - progress)^2, to 0.1 mm. A
move is kept when it lowers the cost, or with probability exp(-delta / T);
T falls geometrically from where a move changing the cost by the median
amount is taken uphill with probability `ACCEPT`, to `T_END`. The run is a
fixed number of moves from a seeded random stream, so the same board and
seed give the same placement; `seconds` only caps a slow machine. It starts
from `scatter`: every part at a random place on the board.
"""
from __future__ import annotations

import math
import random
import time
from typing import Dict, List, Tuple

import numpy as np

from placement import simple
from placement.legality import BOARD_TOL
from placement.simple import ROTS, legal, nearest_legal, target

MOVES = {'shift': 0.40, 'turn': 0.15, 'swap': 0.10, 'jump': 0.10, 'dock': 0.10,
         'gshift': 0.12, 'gturn': 0.08, 'gswap': 0.05, 'escape': 0.05}
BIG_GROUP_MOVES = ('gturn', 'gswap')
BIG_UNTIL = 0.25    # the share of the run group turns and swaps are drawn in
ESCAPE_TRIES = 48   # random poses an escape move draws, keeping the least overlap
CLEAN = 1e-3        # mm2 of overlap that counts as none
FLOOR = 2 * CLEAN   # the least an outline or keep-out-band breach costs: never clean
ACCEPT = 0.1        # the start: the median cost change taken uphill this often
T_END = 0.02
LAM_START, LAM_END = 1.0, 10000.0


def _place(st, ref, x, y, rot):
    """`QuenchState.apply_move` without its last step: the pose, and the
    legality caches a move makes stale, but not the state's own airwires --
    nothing the annealer prices or tests reads them, and rebuilding them
    was a quarter of each move. `anneal` puts every part through the real
    `apply_move` once at the end."""
    part = st.parts[ref]
    part.x, part.y, part.rot = x, y, rot
    st._inc_violation.clear()
    st._inc_intent.clear()


def _turn(x, y, q):
    """(x, y) turned by q degrees (a multiple of 90), the state's way."""
    q = int(round(q)) % 360
    if q == 0:
        return x, y
    if q == 90:
        return y, -x
    if q == 180:
        return -x, -y
    return -y, x


class Annealer:
    def __init__(self, model, movable: List[str], *, seed: int = 0):
        self.model = model
        self.st = model.state
        st = self.st
        self.mov = sorted(r for r in movable if r in st.parts)
        movset = set(self.mov)
        self.rng = random.Random(seed)
        self.groups = [(n, [r for r in m if r in movset]) for n, m in model.groups]
        self.groups = [(n, m) for n, m in self.groups if len(m) >= 2]
        whole = dict(model.groups)
        self.free_groups = [(n, m) for n, m in self.groups
                            if all(r in movset for r in whole[n])]
        by_fp: Dict[str, List[str]] = {}
        for r in self.mov:
            by_fp.setdefault(st.parts[r].footprint_name, []).append(r)
        self.twins = {r: [o for o in by_fp[st.parts[r].footprint_name] if o != r]
                      for r in self.mov}
        # the board less the margin the legality gate holds a courtyard to
        # (`QuenchState.usable`: the larger of the clearance and the edge
        # clearance), so a pose inside it is never refused for the edge
        self.inner = tuple(st.usable)
        # ... and the two board terms the bbox cannot hold: the real outline
        # (a cutout, a notch: `edge_gate`, None when the bbox is exact) and
        # the rule-area keep-out bands (`legality_ctx.keepouts`, #1031)
        self.gate = st.edge_gate if st.edge_gate.active else None
        ctx = st.legality_ctx
        self.bands = (ctx if ctx is not None and ctx.keepouts is not None
                      and ctx.keepouts.active else None)
        self.span = max(self.inner[2] - self.inner[0], self.inner[3] - self.inner[1])
        self.half = st.clearance / 2.0
        self.names = sorted(st.parts)
        self.everyone = frozenset(st.parts)
        # each part's faces as a bit mask: two parts can overlap when theirs meet
        bit = {}
        self.faces = {r: sum(1 << bit.setdefault(f, len(bit)) for f in set(st.parts[r].sides))
                      for r in self.names}
        self.face_ids = np.asarray([self.faces[r] for r in self.names], dtype=np.int64)
        self.lam = LAM_START

    # -- poses -----------------------------------------------------------------
    def pose(self, r):
        p = self.st.parts[r]
        return (p.x, p.y, p.rot)

    def _centre(self, refs):
        rs = [self.st.parts[r].rect() for r in refs]
        return ((min(q[0] for q in rs) + max(q[2] for q in rs)) / 2.0,
                (min(q[1] for q in rs) + max(q[3] for q in rs)) / 2.0)

    # -- overlap ---------------------------------------------------------------
    def _grown(self, r):
        c, h = self.st.parts[r].rect(), self.half
        return (c[0] - h, c[1] - h, c[2] + h, c[3] + h)

    def _outside(self, r):
        """How far `r`'s courtyard is off the board, as an area: its overhang
        past each side times its size along that side -- the area off while
        it straddles one side, and growing the further off it is, so a part
        wholly off the board is still pulled back.

        Where the outline is not its bbox, plus the real outline's own
        measure (`rect_outside_amount`, what `seeder.pose_ok` refuses on)
        times the courtyard's half-perimeter, and never less than `FLOOR`:
        a pose the gate refuses is never clean. Only a courtyard near a
        cutout, a notch or a rounded corner pays for it; the rest are
        answered by the gate's grid (`rect_bbox_decides`)."""
        c = self.st.parts[r].rect()
        i = self.inner
        w, hh = c[2] - c[0], c[3] - c[1]
        v = (hh * (max(0.0, i[0] - c[0]) + max(0.0, c[2] - i[2]))
             + w * (max(0.0, i[1] - c[1]) + max(0.0, c[3] - i[3])))
        g = self.gate
        if g is not None and not g.rect_bbox_decides(c):
            amt = g.rect_outside_amount(c)
            if amt > BOARD_TOL:
                v += max(FLOOR, amt * (w + hh))
        return v

    def _bands(self, r):
        """How far `r`'s pads reach into the board's rule-area keep-out
        bands past what the gate admits (`keepout_excess`, #1031), times its
        courtyard's half-perimeter, and never less than `FLOOR`; 0 on a board
        without such a keep-out, or for a part whose copper cannot reach one."""
        if self.bands is None:
            return 0.0
        p = self.st.parts[r]
        ex = self.bands.keepout_excess(r, p.x, p.y, p.rot)
        if ex <= 0.0:
            return 0.0
        c = p.rect()
        return max(FLOOR, ex * ((c[2] - c[0]) + (c[3] - c[1])))

    def _intent(self, r):
        """How far `r` breaks the intent's claims on it -- keep-outs and
        exclusive zones (mm2 inside), containment zones (mm outside) --
        each term past its own threshold."""
        spec = self.st.intent_spec_for(r)
        if not spec:
            return 0.0
        p = self.st.parts[r]
        vals = self.st.intent_terms(r, p.grade_rects(p.x, p.y, p.rot))
        return sum(max(0.0, v - t.threshold) for v, t in zip(vals, spec))

    def overlap(self, refs=None) -> float:
        """Overlap (mm2) of every pair with a part in `refs` (all pairs when
        None), each pair once, plus those parts' distance off the board and
        into what the intent and the board's rule areas keep them out of. With
        `refs`, the pairs are summed in Rust (`grid_router.overlap_sum`): the
        moving parts at their live poses, every other part at its committed
        rect (`Model._CR`)."""
        mine = self.mov if refs is None else sorted(refs)
        total = sum(self._outside(r) + self._intent(r) + self._bands(r) for r in mine)
        h = self.half
        if refs is not None:
            P, pid = self.st.parts, self.model._pid
            return simple.grid_router.overlap_sum(
                total, h, [pid(r) for r in mine],
                np.asarray([P[r].rect() for r in mine], dtype=float).reshape(-1, 4),
                self.model._CR, self.face_ids)
        mset = set(mine)
        grown = {}
        for a in mine:
            A = grown.get(a) or grown.setdefault(a, self._grown(a))
            for b in self.names:
                if b == a or (b in mset and b < a) or not self.faces[a] & self.faces[b]:
                    continue
                B = grown.get(b) or grown.setdefault(b, self._grown(b))
                ox = min(A[2], B[2]) - max(A[0], B[0])
                if ox <= 0:
                    continue
                oy = min(A[3], B[3]) - max(A[1], B[1])
                if oy > 0:
                    total += ox * oy
        return total

    # -- proposals: {ref: new pose} or None --------------------------------------
    def kinds(self) -> List[str]:
        """The move kinds this board can make."""
        out = [k for k in MOVES if k not in ('swap', 'gshift', 'gturn', 'gswap')]
        if any(self.twins.values()):
            out.append('swap')
        if self.groups:
            out += ['gshift', 'gturn']
        if len(self.free_groups) >= 2:
            out.append('gswap')
        return [k for k in MOVES if k in out]

    def propose(self, kind, reach):
        """A move of `kind`, its parts translated together onto the board,
        or None."""
        new = self._move(kind, reach)
        if not new:
            return new
        rs = []
        for r, (x, y, rot) in new.items():
            p = self.st.parts[r]
            p.ensure_rotation(rot)
            rs.append(p.rect(x, y, rot))
        u = (min(q[0] for q in rs), min(q[1] for q in rs),
             max(q[2] for q in rs), max(q[3] for q in rs))
        i = self.inner
        if u[2] - u[0] > i[2] - i[0] or u[3] - u[1] > i[3] - i[1]:
            return None
        dx = max(0.0, i[0] - u[0]) - max(0.0, u[2] - i[2])
        dy = max(0.0, i[1] - u[1]) - max(0.0, u[3] - i[3])
        if dx == 0.0 and dy == 0.0:
            return new
        return {r: (x + dx, y + dy, rot) for r, (x, y, rot) in new.items()}

    def _move(self, kind, reach):
        rng, st = self.rng, self.st
        if not self.mov:
            return None
        if kind == 'shift':
            r = rng.choice(self.mov)
            x, y, rot = self.pose(r)
            return {r: (x + rng.uniform(-reach, reach), y + rng.uniform(-reach, reach), rot)}
        if kind == 'turn':
            r = rng.choice(self.mov)
            x, y, rot = self.pose(r)
            return {r: (x, y, rng.choice([q for q in ROTS if abs(q - rot % 360.0) > 1e-6]))}
        if kind == 'swap':
            r = rng.choice(self.mov)
            if not self.twins[r]:
                return None
            o = rng.choice(self.twins[r])
            return {r: self.pose(o), o: self.pose(r)}
        if kind == 'jump':
            r = rng.choice(self.mov)
            tx, ty = target(self.model, r, self.everyone)
            p = st.parts[r]
            rot = rng.choice(ROTS)
            p.ensure_rotation(rot)
            c = p.rect(0.0, 0.0, rot)
            return {r: (tx - (c[0] + c[2]) / 2.0, ty - (c[1] + c[3]) / 2.0, rot)}
        if kind == 'dock':
            return self._dock(rng.choice(self.mov))
        if kind == 'escape':
            return self._escape(rng.choice(self.mov))
        if kind == 'gshift':
            if not self.groups:
                return None
            _n, m = rng.choice(self.groups)
            dx, dy = rng.uniform(-reach, reach), rng.uniform(-reach, reach)
            return {r: (self.pose(r)[0] + dx, self.pose(r)[1] + dy, self.pose(r)[2]) for r in m}
        if kind == 'gturn':
            if not self.groups:
                return None
            _n, m = rng.choice(self.groups)
            q = rng.choice((90.0, 180.0, 270.0))
            cx, cy = self._centre(m)
            out = {}
            for r in m:
                x, y, rot = self.pose(r)
                tx, ty = _turn(x - cx, y - cy, q)
                out[r] = (cx + tx, cy + ty, (rot + q) % 360.0)
            return out
        if kind == 'gswap':
            if len(self.free_groups) < 2:
                return None
            (_a, ma), (_b, mb) = rng.sample(self.free_groups, 2)
            ca, cb = self._centre(ma), self._centre(mb)
            out = {}
            for m, (fx, fy), (tx, ty) in ((ma, ca, cb), (mb, cb, ca)):
                for r in m:
                    x, y, rot = self.pose(r)
                    out[r] = (x + tx - fx, y + ty - fy, rot)
            return out
        return None

    def _escape(self, r):
        """`r` out of an overlap small steps cannot leave -- wedged between
        fixed parts, or past one: when it overlaps anything, the pose of least
        overlap among ESCAPE_TRIES drawn at random over the board. Late in the
        run the overlap weight takes it; early, its wire cost mostly refuses it."""
        if self.overlap([r]) <= CLEAN:
            return None
        st, rng, p = self.st, self.rng, self.st.parts[r]
        was = self.pose(r)
        i = self.inner
        best = None
        for _ in range(ESCAPE_TRIES):
            rot = rng.choice(ROTS)
            p.ensure_rotation(rot)
            c = p.rect(0.0, 0.0, rot)
            w, h = c[2] - c[0], c[3] - c[1]
            cx = rng.uniform(i[0] + w / 2.0, max(i[0] + w / 2.0, i[2] - w / 2.0))
            cy = rng.uniform(i[1] + h / 2.0, max(i[1] + h / 2.0, i[3] - h / 2.0))
            pose = (cx - (c[0] + c[2]) / 2.0, cy - (c[1] + c[3]) / 2.0, rot)
            _place(st, r, *pose)
            ov = self.overlap([r])
            if best is None or ov < best[0]:
                best = (ov, pose)
        p.ensure_rotation(was[2])
        _place(st, r, *was)
        return {r: best[1]}

    def _dock(self, r):
        """`r` against the part of the nearest partner pad on one of its nets."""
        st, rng = self.st, self.rng
        part = st.parts[r]
        nets = [n for n in sorted(set(part.nets)) if n in self.model.signal]
        if not nets:
            return None
        n = rng.choice(nets)
        mine = [(x, y) for x, y, nn in part.pad_globals() if nn == n]
        partners = [(q, x, y) for q in sorted(st.net_refs.get(n, ())) if q != r
                    for x, y, nn in st.parts[q].pad_globals() if nn == n]
        if not mine or not partners:
            return None
        mx, my = mine[0]
        q, qx, qy = min(partners, key=lambda t: abs(t[1] - mx) + abs(t[2] - my))
        Q = st.parts[q].rect()
        rot = rng.choice(ROTS)
        part.ensure_rotation(rot)
        c = part.rect(0.0, 0.0, rot)
        j = next(i for i, (_x, _y, nn) in enumerate(part.pad_globals(0.0, 0.0, rot)) if nn == n)
        px, py, _ = part.pad_globals(0.0, 0.0, rot)[j]
        gap = st.clearance + 1e-3
        side = rng.randrange(4)
        if side == 0:       # to the right of the partner
            x, y = Q[2] + gap - c[0], qy - py
        elif side == 1:     # left
            x, y = Q[0] - gap - c[2], qy - py
        elif side == 2:     # below
            x, y = qx - px, Q[3] + gap - c[1]
        else:               # above
            x, y = qx - px, Q[1] - gap - c[3]
        return {r: (x, y, rot)}

    # -- one trial -------------------------------------------------------------
    def trial(self, new: Dict[str, Tuple], T: float) -> Tuple[bool, float, float]:
        """Price `new` (the cost plus lam x overlap) and keep it by the
        Metropolis rule, committing it to the model if kept. Returns (kept,
        cost delta, overlap delta)."""
        st, model = self.st, self.model
        refs = sorted(new)
        for r, (_x, _y, rot) in new.items():
            st.parts[r].ensure_rotation(rot)
        t = model.touched(refs)
        base, ob = model.local(refs, t, reprice=False), self.overlap(refs)
        was = {r: self.pose(r) for r in refs}
        for r in refs:
            _place(st, r, *new[r])
        do = self.overlap(refs) - ob
        # the moved parts' cost after the move is never below 0, so the move
        # changes the cost by at least lam x do - base: when that is already
        # uphill, the rule's one draw is made now, and a draw that refuses
        # even that change refuses the move unpriced -- the same draw, the
        # same answer as pricing it would give
        u, lb = None, self.lam * do - base
        if lb > 0 and T > 0:
            u = self.rng.random()
            if u >= math.exp(-lb / T):
                for r in refs:
                    _place(st, r, *was[r])
                return False, lb, do
        snap = model.snapshot(t, refs)
        dc = model.local(refs, t) - base
        delta = dc + self.lam * do
        if delta <= 0 or (T > 0 and (self.rng.random() if u is None else u) < math.exp(-delta / T)):
            model.commit(refs, t[0])
            return True, dc, do
        for r in refs:
            _place(st, r, *was[r])
        model.restore(snap)
        return False, dc, do

    def price(self, new: Dict[str, Tuple]) -> float:
        """The cost change `new` would make, the state left as it was."""
        st, model = self.st, self.model
        refs = sorted(new)
        for r, (_x, _y, rot) in new.items():
            st.parts[r].ensure_rotation(rot)
        t = model.touched(refs)
        base = model.local(refs, t, reprice=False)
        snap = model.snapshot(t, refs)
        was = {r: self.pose(r) for r in refs}
        for r in refs:
            _place(st, r, *new[r])
        dc = model.local(refs, t) - base
        for r in refs:
            _place(st, r, *was[r])
        model.restore(snap)
        return dc

    def start_temperature(self, samples: int = 200) -> float:
        """T at which a move changing the cost by the median amount, either
        way, is taken with probability `ACCEPT` uphill. The overlap is left
        out: from a scatter most moves lower it."""
        ds = []
        for _ in range(samples):
            r = self.rng.choice(self.mov)
            x, y, rot = self.pose(r)
            reach = self.span / 6.0
            dc = self.price({r: (x + self.rng.uniform(-reach, reach),
                                 y + self.rng.uniform(-reach, reach), rot)})
            if abs(dc) > 1e-9:
                ds.append(abs(dc))
        if not ds:
            return 1.0
        ds.sort()
        return ds[len(ds) // 2] / -math.log(ACCEPT)


def scatter(model, movable: List[str], seed: int = 0):
    """Every movable part at a random place on the board (less its edge
    margin), at its present turn: the start, a part's neighbourhood local
    from the first move."""
    st = model.state
    rng = random.Random(seed)
    inner = tuple(st.usable)
    for r in movable:
        p = st.parts[r]
        c = p.rect()
        w, h = c[2] - c[0], c[3] - c[1]
        x = rng.uniform(inner[0] + w / 2, max(inner[0] + w / 2, inner[2] - w / 2))
        y = rng.uniform(inner[1] + h / 2, max(inner[1] + h / 2, inner[3] - h / 2))
        _place(st, r, p.x + x - (c[0] + c[2]) / 2.0, p.y + y - (c[1] + c[3]) / 2.0, p.rot)
    model.reprice_all()


def anneal(model, movable: List[str], *, moves: int = 11000, seconds: float = 0.0,
           seed: int = 0, log=None) -> Dict:
    """Anneal from the live poses and leave the best overlap-free placement
    seen applied, each part then held to the exact legality. Returns
    {'moves' (executed), 'seconds', 'loop_seconds', 'start', 'best',
    'tracked_best', 't0', 'stats': {kind: (tried, kept)}, 'capped',
    'reseated' (parts moved to their nearest legal pose afterwards)}."""
    a = Annealer(model, movable, seed=seed)
    st = model.state
    t_start = time.time()
    model.reprice_all()         # parts may have moved outside the model
    model.total()               # ... and the index and grids with them
    t0 = a.start_temperature()
    cur = model.total()['cost']
    ov = a.overlap()
    start = cur
    best, best_poses = (cur, {r: a.pose(r) for r in a.mov}) if ov < CLEAN else (math.inf, None)
    kinds = a.kinds()
    wts = [MOVES[k] for k in kinds]
    stats = {k: [0, 0] for k in kinds}
    reach0 = a.span / 6.0
    window = [0, 0]             # moves tried, kept, since the last log line
    late = int(BIG_UNTIL * moves)       # from here no group turns or swaps
    capped = False
    done = 0            # moves executed (a `seconds` cap stops the loop early)
    t_loop = time.time()
    for k in range(moves):
        if seconds and time.time() - t_start > seconds:
            capped = True
            break
        done = k + 1
        frac = k / moves
        T = t0 * (T_END / t0) ** frac
        a.lam = LAM_START * (LAM_END / LAM_START) ** frac
        reach = max(0.1, reach0 * (1.0 - frac) ** 2)
        if k == late:
            wts = [0.0 if kk in BIG_GROUP_MOVES else w for kk, w in zip(kinds, wts)]
        kind = a.rng.choices(kinds, wts)[0]
        new = a.propose(kind, reach)
        stats[kind][0] += 1
        if not new:
            continue
        kept, dc, do = a.trial(new, T)
        if kept:
            stats[kind][1] += 1
            cur += dc
            ov = max(0.0, ov + do)
            if ov < CLEAN and cur < best - 1e-9:
                best, best_poses = cur, {r: a.pose(r) for r in a.mov}
        window[0] += 1
        window[1] += int(kept)
        if log and moves >= 10 and (k + 1) % (moves // 10) == 0:
            log(f"move {k + 1:6d}: T {T:7.3f} lam {a.lam:7.1f} reach {reach:5.2f} cost {cur:7.1f} "
                f"overlap {ov:6.2f} mm2 best {best:7.1f} accepted {window[1] / max(1, window[0]):.2f}")
            window = [0, 0]
    loop_seconds = time.time() - t_loop
    for r in a.mov:     # the QuenchState's own state, airwires and all
        pose = best_poses[r] if best_poses is not None else a.pose(r)
        st.parts[r].ensure_rotation(pose[2])
        st.apply_move(r, *pose)
    reseated = []
    for r in a.mov:
        p = st.parts[r]
        if legal(st, r, p.x, p.y, p.rot):
            continue
        for rot in (p.rot,) + tuple(q for q in ROTS if q != p.rot):
            got = nearest_legal(st, r, p.x, p.y, rot, frozenset(), reach=6.0)
            if got:
                st.apply_move(r, *got)
                reseated.append(r)
                break
    model.reprice_all()
    final = model.total()['cost']
    return {'moves': done, 'seconds': round(time.time() - t_start, 2), 'start': start,
            'best': final, 'tracked_best': best, 't0': t0, 'capped': capped,
            'reseated': reseated, 'loop_seconds': round(loop_seconds, 3),
            'stats': {kk: tuple(v) for kk, v in stats.items()}}
