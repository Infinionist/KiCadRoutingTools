#!/usr/bin/env python3
"""Make a movie of a placement: place_simple's run, step by step, and with
--route the placement skill's whole flow.

    python3 -X utf8 py_placer/place_movie.py board.kicad_pcb placed.kicad_pcb \\
        --groups groups.json --intent intent.json [--seed 0] [--movie placed.mp4]
    python3 -X utf8 py_placer/place_movie.py board.kicad_pcb placed.kicad_pcb \\
        --groups groups.json --intent intent.json --route ch32v003_usb:6 [--seeds 0-11]

BOARD and OUT come first; every other argument but this tool's own is
place_simple.py's (see `place_simple.py --help`). The placement is exactly
place_simple's for the same seed.

Each placement frame is the board with every part's pads and courtyard
(movable parts tinted by group, fixed parts dim), the airwires the objective
prices (ground left out: it is poured), the parts that moved since the last
frame outlined in white, and a caption, in the routing movie's style
(`route_movie.py`). The steps:

  1. input: the board's outline, the movable parts in their pile and the
     fixed parts in a pile of their own, each off an edge, in view; then the
     fixed parts fly to their places, one at a time, biggest first;
  2. faces and poses: the parts the intent puts on the other face or at a
     declared pose (when there are any);
  3. scatter: the pile glides onto the board, each part to the random place
     it is seeded at, and the view closes in on the board;
  4. edge seats: the parts declared on an edge, held to it (when any);
  5. anneal: the moves, with the move count, temperature, cost and overlap,
     playing faster as it goes (--speedup times faster by its end); a long
     edge part's half-turn trial and a fresh start after a run that left a
     part illegal are captioned where they happen;
  6. best: the best placement the anneal saw, each part moved to its nearest
     legal pose where it needed one;
  7. placed.

With --route BOARD:SET (a stress-corpus board whose recorded chain routes
this one: `tests/stress/placement_ab/route_placed.py`), it runs the placement
skill's flow first and films it after: every seed of --seeds is placed (by
default the skill's round, seeds 0-11 with 6-11 started clustered), the
--judge legal seeds the objective rates cheapest are fast-routed (the chain
with `--quick 2`) and scored by quick_score, the best (fewest blocking, then
vias, copper, segments) is routed in full. The movie films that seed's
placement -- the one routed in full -- and goes on:

  8. its fast route, played as the routing movie plays a run;
  9. a zoom out to the judged seeds' fast routes side by side, a box round
     the best, and a zoom into it;
 10. its fast route cleared, and its full route played.

OUT is then the best seed's placement; the work (placed seeds, routes,
scores) is in --work. The movie is an .mp4 through `ffmpeg` when it is on
PATH, else a GIF beside it. Exit codes are place_simple's (2 also when a
--route step fails).
"""
#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement'], 'kind': 'actor'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

FRAMES = 120        # anneal frames per run
SPEEDUP = 4.0       # the anneal plays this much faster at its end than at its start
GLIDE = 10          # frames the pile takes to reach the places it is seeded at
PILE_GAP = 2.0      # mm off the board's edge the pile is drawn, at most
FLY = 3             # frames each fixed part takes to fly from its pile to its place
ZOOM = 12           # frames of a zoom between the board and the judged seeds
LABEL_PX = 16       # the line under each judged seed's picture
STEP_HOLD = 0.5     # seconds a step's frame holds
END_HOLD = 1.0      # seconds on the last frame


def _ease(t):
    return t * t * (3 - 2 * t)


def _lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


class Film:
    """Draws the placement state as it stands (or at the poses a frame
    overrides) and feeds the frames to `movie`. `start` is called with
    place_simple's built state; the anneal's hooks call `on_*`."""

    def __init__(self, args, movie):
        self.a, self.movie = args, movie
        self.run = 0
        self.wide = None        # the view with the pile in it, when it is off the board

    # -- setup ----------------------------------------------------------------
    def start(self, pcb, board, args, built):
        from kicad_parser import parse_kicad_pcb
        from route_render import BoardRenderer
        from placement.grouping import is_ground, load_groups
        self.state, self.model, self.movable, _notes = built
        self.pcb, self.args = pcb, args
        self.hold = max(1, int(round(STEP_HOLD * self.a.fps)))
        self.r = BoardRenderer(pcb, size=self.a.size, supersample=1, theme=self.a.theme,
                               show_pads=False, show_zones=False)
        th = self.r.theme
        body = th.rgb('board_body')
        mix = lambda c, k: tuple(int(body[i] * (1 - k) + c[i] * k) for i in range(3))  # noqa: E731
        self.c = {'front': th.rgb('place_court_front'), 'back': th.rgb('place_court_back'),
                  'fixed': th.rgb('place_locked'), 'air': th.rgb('place_airwire'),
                  'moved': th.rgb('event_new'), 'pad': th.rgb('pad'),
                  'pad_back': mix(th.rgb('pad'), 0.55)}
        # each group its own colour (outline full, fill toward the board), named in the key
        # (a group of fixed parts only shows no colour, so it takes none)
        groups = [g for g in load_groups(args.groups)['groups']
                  if any(r in self.movable for r in g['refs'])]
        from render_placement import group_palette
        colours = group_palette(len(groups))
        self.line, self.fill, self.key = {}, {}, []
        for g, rgb in zip(groups, colours):
            role = g.get('role')
            self.key.append((rgb, 'solid', g['name'] + (f" ({role})" if role and role != g['name'] else '')))
            for ref in g['refs']:
                if ref in self.movable:
                    self.line[ref], self.fill[ref] = rgb, mix(rgb, 0.55)
        if any(r not in self.line for r in self.movable):
            self.key.append((self.c['front'], 'line', 'in no group'))
        if len(self.movable) < len(self.state.parts):
            self.key.append((self.c['fixed'], 'hatch', 'fixed'))
        self.key.append((self.c['moved'], 'ring', 'just moved'))
        self._key_layout()
        # the airwires' nets: every net but ground, over each part's pads
        self.nets = {}
        for ref, fp in pcb.footprints.items():
            for i, p in enumerate(fp.pads):
                nm = pcb.nets[p.net_id].name if p.net_id in pcb.nets else ''
                if p.net_id and not is_ground(nm):
                    self.nets.setdefault(p.net_id, []).append((ref, i))
        self.nets = {n: m for n, m in self.nets.items() if len({r for r, _ in m}) > 1}
        self._posed = {}
        # the piles: the movable parts where they lie before the scatter, and
        # the fixed parts in a pile of their own, each drawn PILE_GAP mm off an
        # edge of the board (parking places, not poses): the fixed parts fly to
        # their places first, one at a time, then the movable pile glides in
        st = self.state
        bx0, by0, bx1, by1 = self.r.bounds
        self.pile = {r: (p.x, p.y, p.rot) for r, p in st.parts.items() if r in self.movable}
        rects = [st.parts[r].rect() for r in self.pile]
        dx = dy = 0.0
        mside = None
        if rects:
            px0, py0 = min(q[0] for q in rects), min(q[1] for q in rects)
            px1, py1 = max(q[2] for q in rects), max(q[3] for q in rects)
            gaps = {'below': py0 - by1, 'above': by0 - py1, 'right': px0 - bx1, 'left': bx0 - px1}
            side, gap = max(gaps.items(), key=lambda kv: kv[1])
            if gap > PILE_GAP:
                move, mside = gap - PILE_GAP, side
                dx = {'right': -move, 'left': move}.get(side, 0.0)
                dy = {'below': -move, 'above': move}.get(side, 0.0)
                self.pile = {r: (x + dx, y + dy, q) for r, (x, y, q) in self.pile.items()}
        # the faces and poses the intent declares were written first: the
        # board as given shows those parts where they were
        self.given, declared = {}, []
        if os.path.abspath(board) != os.path.abspath(args.board):
            given = parse_kicad_pcb(args.board)
            declared = sorted(r for r in pcb.footprints
                              if r in given.footprints and _moved(given.footprints[r], pcb.footprints[r]))
            self.given = {r: given.footprints[r] for r in declared}
        fixed = sorted(r for r in st.parts if r not in self.movable)
        self.fixed_pile = self._fixed_pile([r for r in fixed if r not in self.given], mside)
        # ... and both in view, with the board
        x0, y0, x1, y1 = _union([self.r.bounds] + list(self._rects_at({**self.pile,
                                                                      **self.fixed_pile}).values())
                                 + [p.rect() for p in st.parts.values()])
        # the key's band along the bottom never covers a part: each view is
        # grown below what it shows by the band
        self.home = self._fit(self.r.bounds)
        if min(x0 - bx0, y0 - by0, bx1 - x1, by1 - y1) < -2.0:
            self.wide = self._fit((x0 - 1.0, y0 - 1.0, x1 + 1.0, y1 + 1.0))
        self.r.set_view(self.wide or self.home)
        poses = {**self.pile, **self.fixed_pile}
        self.shot(f"input: {len(st.parts)} parts, {len(self.movable)} movable, {len(fixed)} fixed",
                  times=self.hold, given=self.given, poses=poses)
        # the fixed parts to their places, biggest first
        size = {r: (lambda q: (q[2] - q[0]) * (q[3] - q[1]))(st.parts[r].rect()) for r in self.fixed_pile}
        order = sorted(self.fixed_pile, key=lambda r: (-size[r], r))
        for k, ref in enumerate(order, 1):
            home = (st.parts[ref].x, st.parts[ref].y, st.parts[ref].rot)
            for i in range(1, FLY + 1):
                poses[ref] = _lerp(self.fixed_pile[ref], home, _ease(i / FLY))
                self.shot(f"fixed parts: {ref} to its place ({k}/{len(order)})", moved=[ref],
                          given=self.given, poses=poses)
            del poses[ref]
        if declared:
            self.shot(f"faces and poses: {', '.join(declared)} at their declared face or pose",
                      moved=declared, times=self.hold, poses=self.pile)

    def _rects_at(self, poses):
        """{ref: courtyard rect} of each part at its pose in `poses` (its turn
        unchanged: the rect moves with it)."""
        out = {}
        for r, (x, y, _q) in poses.items():
            p = self.state.parts[r]
            a, b, c, d = p.rect()
            out[r] = (a + x - p.x, b + y - p.y, c + x - p.x, d + y - p.y)
        return out

    def _fixed_pile(self, refs, taken):
        """The fixed parts piled PILE_GAP mm off the board's edge -- the edge,
        other than `taken` (the movable pile's), that leaves the board biggest
        in the frame -- each a little off the pile's centre."""
        import random
        if not refs:
            return {}
        st = self.state
        bx0, by0, bx1, by1 = self.r.bounds
        rs = {r: st.parts[r].rect() for r in refs}
        mw = max(q[2] - q[0] for q in rs.values())
        mh = max(q[3] - q[1] for q in rs.values())
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        anchors = {'above': (cx, by0 - PILE_GAP - mh / 2), 'below': (cx, by1 + PILE_GAP + mh / 2),
                   'left': (bx0 - PILE_GAP - mw / 2, cy), 'right': (bx1 + PILE_GAP + mw / 2, cy)}
        rng = random.Random(0)
        jit = {r: (rng.uniform(-1, 1), rng.uniform(-1, 1)) for r in sorted(refs)}
        reach = min(1.0, 0.15 * max(mw, mh))
        best = None
        for side, (ax, ay) in anchors.items():
            if side == taken:
                continue
            piled = {}
            for r, q in rs.items():
                p = st.parts[r]
                tx, ty = ax + jit[r][0] * reach, ay + jit[r][1] * reach
                piled[r] = (p.x + tx - (q[0] + q[2]) / 2, p.y + ty - (q[1] + q[3]) / 2, p.rot)
            u = _union([self.r.bounds] + list(self._rects_at({**self.pile, **piled}).values()))
            scale = min(self.r.W / (u[2] - u[0]), self.r.H / (u[3] - u[1]))
            if best is None or scale > best[0]:
                best = (scale, piled)
        return best[1]

    # -- the key -------------------------------------------------------------
    def _key_layout(self):
        """The key as a band along the frame's bottom: its entries in evenly
        spaced columns across the whole width, in as few rows as fit."""
        from PIL import Image, ImageDraw
        from route_render import load_font
        W, H = self.r.W, self.r.H
        self.kfont = load_font(max(11, H // 75))
        d = ImageDraw.Draw(Image.new('RGB', (1, 1)))
        self.ksw = max(9, H // 90)
        widths = [self.ksw + 6 + d.textlength(t, font=self.kfont) for _, _, t in self.key]
        cols = max(1, min(len(widths), int((W - 16) // (max(widths) + 14))))
        rows = math.ceil(len(widths) / cols)
        self.kcols = math.ceil(len(widths) / rows)      # no emptier last row than needed
        self.klh = self.ksw + 7
        self.band = rows * self.klh + 8

    def _fit(self, view):
        """`view` grown downward until what it shows clears the key's band."""
        r = self.r
        x0, y0, x1, y1 = view
        floor = r.H - self.band - 2

        def clears(e):
            r.set_view((x0, y0, x1, y1 + e))
            return r.tf.pt(x0, y1)[1] <= floor
        if clears(0.0):
            return view
        lo, hi = 0.0, y1 - y0
        for _ in range(20):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if clears(mid) else (mid, hi)
        return (x0, y0, x1, y1 + hi)

    def _draw_key(self, d, rr):
        from render_chrome import draw_mark
        y0 = rr.H - self.band
        d.rectangle([0, y0, rr.W, rr.H], fill=rr.theme.rgb('chrome_band'))
        slot = (rr.W - 16) / self.kcols
        for i, (rgb, mark, text) in enumerate(self.key):
            x = int(8 + (i % self.kcols) * slot)
            y = int(y0 + 5 + (i // self.kcols) * self.klh)
            draw_mark(d, [x, y, x + self.ksw, y + self.ksw], rgb, mark)
            d.text((x + self.ksw + 6, y - 2), text, fill=rr.theme.rgb('chrome_text'), font=self.kfont)

    # -- drawing --------------------------------------------------------------
    def pads(self, ref, pose=None):
        from placement.legality import pads_at_pose
        p = self.state.parts.get(ref)
        fp = self.pcb.footprints[ref]
        if p is None:
            return fp.pads
        key = pose or (p.x, p.y, p.rot)
        got = self._posed.get(ref)
        if got is None or got[0] != key:
            got = self._posed[ref] = (key, pads_at_pose(fp, key))
        return got[1]

    def airwires(self, given, poses):
        lines = []
        for members in self.nets.values():
            pts = []
            for ref, i in members:
                pads = given[ref].pads if ref in given else self.pads(ref, poses.get(ref))
                pts.append((pads[i].global_x, pads[i].global_y))
            lines += _mst(pts)
        return lines

    def shot(self, label, moved=(), times=1, given=None, poses=None, key=True):
        """One frame of the state as it stands. `poses` draws those parts at
        another pose (the glide); `given` draws those parts from a footprint
        of their own (the board as given)."""
        given, poses = given or {}, poses or {}
        r, c, st = self.r, self.c, self.state
        moved = set(moved)

        def rect(ref, p):
            x0, y0, x1, y1 = p.rect()
            if ref in poses:
                dx, dy = poses[ref][0] - p.x, poses[ref][1] - p.y
                x0, y0, x1, y1 = x0 + dx, y0 + dy, x1 + dx, y1 + dy
            (ax, ay), (bx, by) = r.tf.pt(x0, y0), r.tf.pt(x1, y1)
            return [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]

        def draw(d, rr):
            w = max(1, int(round(rr.tf.length(0.08))))
            for ref, p in sorted(st.parts.items()):
                if ref in given:
                    continue
                if ref not in self.movable:
                    d.rectangle(rect(ref, p), outline=c['fixed'], width=w)
                else:
                    d.rectangle(rect(ref, p), fill=self.fill.get(ref),
                                outline=self.line.get(ref, c['back'] if p.side == 'B' else c['front']),
                                width=w)
            for x0, y0, x1, y1 in self.airwires(given, poses):
                d.line([rr.tf.pt(x0, y0), rr.tf.pt(x1, y1)], fill=c['air'], width=1)
            for ref, fp in self.pcb.footprints.items():
                fpg = given.get(ref)
                pads = fpg.pads if fpg is not None else self.pads(ref, poses.get(ref))
                back = (fpg or fp).layer == 'B.Cu'
                for pad in pads:
                    rr._draw_pad(d, pad, fill=c['pad_back'] if back else c['pad'])
            for ref in moved & set(st.parts):
                d.rectangle(rect(ref, st.parts[ref]), outline=c['moved'], width=2 * w)
            if key:
                self._draw_key(d, rr)
        self.movie.add(r.frame(segments=[], vias=[], label=label, overlays=[draw]), times)

    # -- the run's hooks ------------------------------------------------------
    def on_scatter(self, seed, clustered=False):
        self.run += 1
        if self.run > 1:
            self.shot(f"run {self.run}, a fresh start (the last left a part with no legal "
                      f"pose): scatter (seed {seed})", moved=self.movable, times=self.hold)
            return
        # the pile glides onto the board, each part to the place it is seeded at
        label = (f"scatter: each group's parts round one random place (seed {seed})" if clustered
                 else f"scatter: each movable part to a random place (seed {seed})")
        seeded = {r: (p.x, p.y, p.rot) for r, p in self.state.parts.items() if r in self.movable}
        for i in range(1, GLIDE + 1):
            t = _ease(i / GLIDE)
            self.shot(label, moved=self.movable,
                      poses={r: _lerp(self.pile[r], seeded[r], t) for r in seeded})
        if self.wide:           # ... and the view closes in on the board
            for i in range(1, 7):
                self.r.set_view(_lerp(self.wide, self.home, _ease(i / 6)))
                self.shot(label)
            self.r.set_view(self.home)
        self.shot(label, times=self.hold // 2)

    def on_anneal(self, moves):
        # frame i of F is taken at move N g(i/F): g' grows linearly to
        # SPEEDUP times its start, so the play speeds up that much
        f = max(1, self.a.frames)
        sp = max(1.0, self.a.speedup)
        g = lambda u: (u + (sp - 1) / 2 * u * u) / ((sp + 1) / 2)  # noqa: E731
        self.marks = [max(1, math.ceil(moves * g(i / f))) for i in range(1, f + 1)]
        self.n, self.k, self.mi, self.T = moves, 0, 0, math.inf
        self.since, self.note = set(), ''

    def on_seats(self, a):
        if a.seats:
            self.shot(f"edge seats: {', '.join(sorted(a.seats))} held to their declared edges",
                      moved=a.seats, times=self.hold)

    def on_total(self, cost):
        self.cost = cost

    def on_overlap(self, ov):
        self.ov = ov

    def on_trial(self, a, new, T, res):
        kept, dc, do = res
        if T > self.T * 1.5:          # the second start of the half-turn trial
            self.k, self.mi, self.note = 0, 0, ' (long edge parts turned half round)'
        self.T = T
        self.k += 1
        if kept:
            self.cost += dc
            self.ov = max(0.0, self.ov + do)
            self.since |= set(new)
        if a.shoving:
            due = self.k % 5 == 0
        else:
            due = self.mi < len(self.marks) and self.k >= self.marks[self.mi]
            while self.mi < len(self.marks) and self.k >= self.marks[self.mi]:
                self.mi += 1
        if due:
            what = 'repair: shoving the overlaps apart' if a.shoving else \
                f"anneal{self.note}: move {self.k}/{self.n}"
            self.shot(f"{what} | T {T:.3g} | cost {self.cost:.0f} | overlap {self.ov:.2f} mm2",
                      moved=self.since)
            self.since = set()

    def on_annealed(self, res):
        legal = res.get('reseated') or []
        self.shot(f"best placement, cost {res['best']:.0f}"
                  + (f" | to the nearest legal pose: {', '.join(legal)}" if legal else ''),
                  moved=legal, times=self.hold)

    def placed(self, rc, times, handoff=False):
        """The placed board; with `handoff`, the view then opens to the
        whole frame, the key gone, as the routing frames show the board."""
        cost = self.model.total()['cost']
        label = f"placed: cost {cost:.0f}" + (' | parts left illegal (see the log)' if rc == 4 else '')
        self.shot(label, times=times)
        if handoff and self.home != tuple(self.r.bounds):
            for i in range(1, 5):
                self.r.set_view(_lerp(self.home, self.r.bounds, _ease(i / 4)))
                self.shot(label, key=False)


def _union(rects):
    rects = list(rects)
    return (min(q[0] for q in rects), min(q[1] for q in rects),
            max(q[2] for q in rects), max(q[3] for q in rects))


def _moved(a, b):
    return (abs(a.x - b.x) > 1e-6 or abs(a.y - b.y) > 1e-6
            or abs(((a.rotation or 0) - (b.rotation or 0) + 180) % 360 - 180) > 1e-6
            or a.layer != b.layer)


def _mst(pts):
    """Prim's tree over `pts`, as (x0, y0, x1, y1) lines."""
    if len(pts) < 2:
        return []
    inf = float('inf')
    best = {i: (inf, 0) for i in range(1, len(pts))}
    at, out = 0, []
    while best:
        ax, ay = pts[at]
        for i in best:
            dd = abs(pts[i][0] - ax) + abs(pts[i][1] - ay)
            if dd < best[i][0]:
                best[i] = (dd, at)
        at = min(best, key=lambda i: best[i][0])
        j = best.pop(at)[1]
        out.append((pts[j][0], pts[j][1], pts[at][0], pts[at][1]))
    return out


def install(film):
    """Hook the film into place_simple's run. The hooks only read the state
    and pass every call through, so the placement is the one place_simple
    makes."""
    import place_simple
    from placement import simple_anneal
    from placement.simple_anneal import Annealer

    build = place_simple.build

    def _build(pcb, board, doc, base, args, *k, **kw):
        out = build(pcb, board, doc, base, args, *k, **kw)
        model = out[1]
        total = model.total

        def _total(*a, **kk):
            t = total(*a, **kk)
            film.on_total(t['cost'])
            return t
        model.total = _total
        film.start(pcb, board, args, out)
        return out
    place_simple.build = _build

    scatter, anneal = simple_anneal.scatter, simple_anneal.anneal

    def _scatter(model, movable, seed=0, clustered=False):
        scatter(model, movable, seed=seed, clustered=clustered)
        film.on_scatter(seed, clustered)
    simple_anneal.scatter = _scatter

    def _anneal(model, movable, **kw):
        film.on_anneal(kw.get('moves', 11000))
        res = anneal(model, movable, **kw)
        film.on_annealed(res)
        return res
    simple_anneal.anneal = _anneal

    start_t, trial, overlap = Annealer.start_temperature, Annealer.trial, Annealer.overlap

    def _start_t(self, *a, **kw):
        film.on_seats(self)
        return start_t(self, *a, **kw)

    def _trial(self, new, T):
        res = trial(self, new, T)
        film.on_trial(self, new, T, res)
        return res

    def _overlap(self, refs=None):
        ov = overlap(self, refs)
        if refs is None:
            film.on_overlap(ov)
        return ov
    Annealer.start_temperature, Annealer.trial, Annealer.overlap = _start_t, _trial, _overlap


# -- the flow (--route) ---------------------------------------------------------

def _strip(opts, *flags):
    """`opts` without each of `flags` and its value."""
    out, skip = [], False
    for x in opts:
        if skip:
            skip = False
        elif x in flags:
            skip = True
        elif not any(x.startswith(f + '=') for f in flags):
            out.append(x)
    return out


def _score_key(sc):
    q = sc.get('quality') or {}
    return (sc.get('blocking', 1e9), q.get('vias', 1e9), q.get('copper_mm', 1e9),
            q.get('segments', 1e9))


def _route(placed, name, set_, out, quick):
    cmd = [sys.executable, '-X', 'utf8', os.path.join(REPO, 'tests', 'stress', 'placement_ab',
                                                       'route_placed.py'),
           placed, name, set_, out] + (['--quick', '2'] if quick else [])
    r = subprocess.run(cmd, capture_output=True, text=True,
                       env=dict(os.environ, KICAD_ROUTE_TRACE='1'))
    print('  ' + ((r.stdout.strip().splitlines() or ['(no output)'])[-1]))
    sc = os.path.join(out, 'score.json')
    return json.load(open(sc)) if r.returncode == 0 and os.path.isfile(sc) else None


def _seed_list(spec):
    out = []
    for part in spec.split(','):
        lo, _, hi = part.partition('-')
        out += range(int(lo), int(hi or lo) + 1)
    return out


def _done(work, key):
    """The score of a route route_placed.py already finished in `work/key`."""
    p = os.path.join(work, key, 'score.json')
    return json.load(open(p)) if os.path.isfile(p) else None


def run_flow(a, board, out, opts, work):
    """Place every seed, fast-route the judged ones, route the best in full,
    reusing what `work` already holds (a placed seed, a finished route), so a
    second run only films. Returns (judged, scores, best, full score) or
    None."""
    name, _, set_ = a.route.partition(':')
    if a.fresh:
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    seeds = _seed_list(a.seeds)
    todo = [s for s in seeds if not (os.path.isfile(os.path.join(work, f's{s}.json'))
                                     and os.path.isfile(os.path.join(work, f's{s}.kicad_pcb')))]
    if todo:
        print(f"place_movie: placing seeds {','.join(map(str, todo))} ({a.jobs} at a time)")
        rc = subprocess.run([sys.executable, '-X', 'utf8', os.path.join(HERE, 'place_simple.py'),
                             board, os.path.join(work, 's{seed}.kicad_pcb'), *opts,
                             '--seeds', ','.join(map(str, todo)), '--jobs', str(a.jobs),
                             '--json', os.path.join(work, 's{seed}.json')]).returncode
        if rc == 2:
            return None
    reps = {s: json.load(open(os.path.join(work, f's{s}.json'))) for s in seeds
            if os.path.isfile(os.path.join(work, f's{s}.json'))}
    legal = sorted((r['final']['cost'], s) for s, r in reps.items() if not r.get('illegal'))
    judged = [s for _c, s in legal[:a.judge]]
    if not judged:
        print("place_movie: no seed placed every part legally; nothing to route", file=sys.stderr)
        return None
    print(f"place_movie: fast-routing the {len(judged)} cheapest legal seeds: {judged}")
    with ThreadPoolExecutor(max_workers=max(1, a.route_jobs or a.jobs)) as pool:
        got = list(pool.map(lambda s: _done(work, f'fast_s{s}') or _route(
            os.path.join(work, f's{s}.kicad_pcb'), name, set_, os.path.join(work, f'fast_s{s}'), True),
            judged))
    scores = {s: sc for s, sc in zip(judged, got) if sc}
    if not scores:
        print("place_movie: no fast route finished", file=sys.stderr)
        return None
    best = min(scores, key=lambda s: _score_key(scores[s]))
    print(f"place_movie: the best fast route is seed {best}; routing it in full")
    full = _done(work, f'full_s{best}') or _route(os.path.join(work, f's{best}.kicad_pcb'), name,
                                                    set_, os.path.join(work, f'full_s{best}'), False)
    if full is None:
        return None
    return judged, scores, best, full


def _chain(work, key):
    """(the board the chain started from, its boards in order) of a route
    route_placed.py left in `work/key`."""
    from route_movie import run_boards
    d = os.path.join(work, key)
    first = [os.path.join(d, 'in', f) for f in os.listdir(os.path.join(d, 'in'))
             if f.endswith('.kicad_pcb')][0]
    boards = [b for b in run_boards(os.path.join(d, 'out'),
                                    os.path.join(d, 'manifest', 'redo_commands.sh'))
              if os.path.basename(b) != os.path.basename(first)]
    return first, boards


def _score_text(sc):
    q = sc.get('quality') or {}
    mm = q.get('copper_mm')
    return f"blocking {sc.get('blocking')} | {q.get('vias')} vias" + (f" | {mm:.0f} mm" if mm else '')


def _grid_dims(n, W, H):
    """(cols, rows) giving the n cells the biggest board-shaped picture."""
    best = None
    for cols in range(1, n + 1):
        rows = math.ceil(n / cols)
        k = min(W / cols / W, (H / rows - LABEL_PX) / H)
        if best is None or k > best[0]:
            best = (k, cols, rows)
    return best[1], best[2]


def film_routes(a, movie, work, judged, scores, best, full, filmed):
    """The routing half of the movie, played with route_movie's own player:
    the filmed seed's fast route, the judged seeds side by side, the best
    one boxed and zoomed into, its fast route cleared and its full route."""
    from PIL import Image, ImageDraw
    from kicad_parser import parse_kicad_pcb
    from route_movie import ZONE_TINT, _board_copper, play_chain, shoot
    from route_render import BoardRenderer, load_font
    kw = dict(size=a.size, theme=a.theme)
    rkw = dict(kw, zone_tint=ZONE_TINT)
    hold = max(1, int(round(STEP_HOLD * a.fps)))
    # 8. the filmed seed's fast route
    first, boards = _chain(work, f'fast_s{filmed}')
    r, live_s, live_v = play_chain(movie, boards, first, prefix='fast route | ',
                                   first_label=f'placed, seed {filmed}', **kw)
    shoot(movie, r, live_s, live_v, f"fast route, seed {filmed}: {_score_text(scores[filmed])}",
          times=hold)
    # 9. the judged seeds side by side
    W, H = movie.size
    cols, rows = _grid_dims(len(judged), W, H)
    cw, ch = W // cols, H // rows
    th = r.theme
    full_img = {}
    G = Image.new('RGB', (W, H), th.rgb('ground'))
    d = ImageDraw.Draw(G)
    font = load_font(LABEL_PX - 4)
    cells = {}
    for i, s in enumerate(sorted(scores)):
        _f, bs = _chain(work, f'fast_s{s}')
        rr = BoardRenderer(parse_kicad_pcb(bs[-1]), supersample=1, **rkw)
        img = rr.render()
        if img.size != (W, H):
            img = img.resize((W, H))
        full_img[s] = img
        k = min(cw / W, (ch - LABEL_PX) / H)
        w, h = int(W * k), int(H * k)
        x = (i % cols) * cw + (cw - w) // 2
        y = (i // cols) * ch + (ch - LABEL_PX - h) // 2
        cells[s] = (x, y, x + w, y + h)
        G.paste(img.resize((w, h), Image.LANCZOS), (x, y))
        sc = scores[s]
        d.text((x, y + h + 1), f"s{s}  blk {sc.get('blocking')}  "
               f"{(sc.get('quality') or {}).get('vias')} v", fill=th.rgb('chrome_text'), font=font)

    def zoom(img, cell, t, label):
        """The grid seen through a camera at t (0: `cell` fills the frame, 1:
        the whole grid), with `img` drawn sharp over its cell."""
        cam = _lerp(cell, (0, 0, W, H), t)
        sx, sy = W / (cam[2] - cam[0]), H / (cam[3] - cam[1])
        f = G.crop(tuple(int(round(v)) for v in cam)).resize((W, H), Image.BILINEAR)
        box = ((cell[0] - cam[0]) * sx, (cell[1] - cam[1]) * sy,
               (cell[2] - cam[0]) * sx, (cell[3] - cam[1]) * sy)
        bw, bh = int(round(box[2] - box[0])), int(round(box[3] - box[1]))
        if bw > 0 and bh > 0:
            f.paste(img.resize((bw, bh), Image.BILINEAR), (int(round(box[0])), int(round(box[1]))))
        r._label(f, label)
        movie.add(f)
    title = f"the {len(scores)} cheapest seeds by the objective, fast-routed"
    for i in range(1, ZOOM + 1):
        zoom(full_img[filmed], cells[filmed], _ease(i / ZOOM), title)
    grid = G.copy()
    r._label(grid, title)
    movie.add(grid, hold)
    x0, y0, x1, y1 = cells[best]
    pick = f"the best: seed {best} ({_score_text(scores[best])})"
    boxed = G.copy()
    ImageDraw.Draw(boxed).rectangle([x0 - 3, y0 - 3, x1 + 3, y1 + LABEL_PX + 2],
                                    outline=th.rgb('status_best'), width=4)
    G = boxed
    for blink in range(3):
        f = (boxed if blink % 2 == 0 else grid).copy()
        r._label(f, pick)
        movie.add(f, hold // 2)
    f = boxed.copy()
    r._label(f, pick)
    movie.add(f, hold)
    for i in range(ZOOM - 1, -1, -1):
        zoom(full_img[best], cells[best], _ease(i / ZOOM), pick)
    # 10. its fast route cleared, net by net, then its full route
    _f, bs = _chain(work, f'fast_s{best}')
    pcb = parse_kicad_pcb(bs[-1])
    r = BoardRenderer(pcb, supersample=1, **rkw)
    live_s, live_v = _board_copper(pcb)
    nets = sorted({o.net_id for o in list(live_s.values()) + list(live_v.values())})
    step = max(1, math.ceil(len(nets) / 6))
    for j in range(0, len(nets), step):
        part = set(nets[j:j + step])
        gone = [o for o in live_s.values() if o.net_id in part]
        shoot(movie, r, live_s, live_v, f"clearing the fast route of seed {best}", rips=gone)
        live_s = {k: o for k, o in live_s.items() if o.net_id not in part}
        live_v = {k: o for k, o in live_v.items() if o.net_id not in part}
    first, boards = _chain(work, f'full_s{best}')
    r, live_s, live_v = play_chain(movie, boards, first, prefix='full route | ',
                                   first_label=f'seed {best}, cleared', **kw)
    shoot(movie, r, live_s, live_v, f"full route, seed {best}: {_score_text(full)}", times=hold)


def main(argv=None):
    from route_movie import FPS, SIZE, MovieWriter
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n')[0], allow_abbrev=False,   # --seed is place_simple's
        epilog="BOARD and OUT come first; every other argument is place_simple.py's "
               "(--groups, --intent, --seed and the weights).")
    ap.add_argument('--movie', help="the movie to write (default: OUT's name, .mp4); "
                                    ".gif writes a GIF")
    ap.add_argument('--frames', type=int, default=FRAMES,
                    help=f'frames of anneal per run (default {FRAMES})')
    ap.add_argument('--speedup', type=float, default=SPEEDUP,
                    help=f'how much faster the anneal plays at its end than its start '
                         f'(default {SPEEDUP:g})')
    ap.add_argument('--size', type=int, default=SIZE, help=f'longest frame side, px (default {SIZE})')
    ap.add_argument('--fps', type=float, default=FPS, help=f'frames per second (default {FPS:g})')
    ap.add_argument('--theme', default='dark', help='render theme: dark (default) or light')
    ap.add_argument('--route', metavar='BOARD:SET',
                    help='run and film the placement flow, routing with this stress-corpus '
                         "board's recorded chain (e.g. ch32v003_usb:6)")
    ap.add_argument('--seeds', help="with --route, the seeds placed (default: the skill's "
                                    'round, 0-11 with --clustered-seeds 6-11)')
    ap.add_argument('--judge', type=int, default=6,
                    help='with --route, how many of the cheapest legal seeds are fast-routed '
                         '(default 6)')
    ap.add_argument('--jobs', type=int, default=4,
                    help='with --route, placements and fast routes run at once (default 4)')
    ap.add_argument('--route-jobs', type=int, default=0,
                    help='with --route, fast routes run at once, when it should differ from '
                         '--jobs (a big board\'s route can take 5 GB: 1 runs them one at a time)')
    ap.add_argument('--fresh', action='store_true',
                    help='with --route, empty --work first: place and route every seed again '
                         '(otherwise a placed seed or finished route there is reused, so a '
                         'second run only films)')
    ap.add_argument('--work', help="with --route, the work directory (default: OUT's name + _work)")
    a, rest = ap.parse_known_args(argv)
    if len(rest) < 2 or rest[0].startswith('-') or rest[1].startswith('-'):
        ap.error('BOARD and OUT come first')
    board, out, opts = rest[0], rest[1], rest[2:]
    movie_path = a.movie or os.path.splitext(out)[0] + '.mp4'
    if not a.route:
        if a.seeds:
            ap.error('--seeds needs --route: a placement movie is one seed (--seed N)')
        movie = MovieWriter(movie_path, a.fps)
        film = Film(a, movie)
        install(film)
        import place_simple
        rc = place_simple.main(rest)
        if hasattr(film, 'state'):
            film.placed(rc, 1)
        path = movie.close(hold=END_HOLD)
        print(f"place_movie: {path} ({movie.n} frames, {movie.n / a.fps:.0f} s)")
        return rc
    if ':' not in a.route:
        ap.error('--route BOARD:SET, e.g. ch32v003_usb:6')
    work = os.path.abspath(a.work or os.path.splitext(out)[0] + '_work')
    opts = _strip(opts, '--seed', '--json', '--seeds', '--jobs')
    if not a.seeds:             # the skill's round
        a.seeds = '0-11'
        if not any(x == '--clustered-seeds' or x.startswith('--clustered-seeds=') for x in opts):
            opts += ['--clustered-seeds', '6-11']
    got = run_flow(a, board, out, opts, work)
    if got is None:
        return 2
    judged, scores, best, full = got
    filmed = best
    # film: the chosen seed's placement again (the same placement), then its routes
    movie = MovieWriter(movie_path, a.fps)
    film = Film(a, movie)
    install(film)
    import place_simple
    rc = place_simple.main([board, os.path.join(work, f'film_s{filmed}.kicad_pcb'), *opts,
                            '--seed', str(filmed)])
    film.placed(rc, film.hold, handoff=True)
    film_routes(a, movie, work, judged, scores, best, full, filmed)
    path = movie.close(hold=END_HOLD)
    subprocess.run([sys.executable, os.path.join(REPO, 'py_router', 'copy_board.py'),
                    os.path.join(work, f's{best}.kicad_pcb'), out], check=False, capture_output=True)
    print(f"place_movie: {path} ({movie.n} frames, {movie.n / a.fps:.0f} s); "
          f"{out} = seed {best}; full route in {os.path.join(work, f'full_s{best}', 'out')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
