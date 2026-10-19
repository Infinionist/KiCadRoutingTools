#!/usr/bin/env python3
"""Make a movie of a placement: place_simple's run, step by step.

    python3 -X utf8 py_placer/place_movie.py board.kicad_pcb placed.kicad_pcb \\
        --groups groups.json --intent intent.json [--seed 0] [--movie placed.mp4]

Places the board exactly as place_simple.py does -- it takes every
place_simple argument (see `place_simple.py --help`) and gives the same
placement for the same seed -- and films the run in the routing movie's style
(`route_movie.py`). Each frame is the board with every part's pads and
courtyard (movable parts tinted by group, fixed parts dim), the airwires the
objective prices (ground left out: it is poured), the parts that moved since
the last frame outlined in white, and a caption. The steps:

  1. input: the board as given;
  2. faces and poses: the parts the intent puts on the other face or at a
     declared pose (when there are any);
  3. scatter: every movable part at a random place;
  4. edge seats: the parts declared on an edge, held to it (when there are
     any);
  5. anneal: the moves, sampled --frames times, with the move count,
     temperature, cost and overlap; a long edge part's half-turn trial and
     a fresh start after a run that left a part illegal are captioned where
     they happen;
  6. best: the best placement the anneal saw, each part moved to its nearest
     legal pose where it needed one;
  7. placed: the board written.

Each step holds a second; the anneal plays at the frame rate. The movie is
an .mp4 through `ffmpeg` when it is on PATH, else a GIF beside it. Exit codes
are place_simple's.
"""
#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement'], 'kind': 'actor'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)

import argparse
import math
import os
import sys

FRAMES = 120        # anneal frames per run


class Film:
    """Draws the placement state as it stands and feeds the frames to the
    movie. `start` is called with place_simple's built state; the anneal's
    hooks call `on_*`."""

    def __init__(self, args):
        self.a = args
        self.movie = None
        self.run = 0

    # -- setup ----------------------------------------------------------------
    def start(self, pcb, board, args, built):
        from kicad_parser import parse_kicad_pcb
        from route_movie import MovieWriter
        from route_render import BoardRenderer
        from placement.grouping import is_ground
        from render_placement import group_colours
        self.state, self.model, self.movable, _notes = built
        self.pcb, self.args = pcb, args
        self.hold = max(1, int(round(self.a.fps)))
        out = self.a.movie or os.path.splitext(args.out)[0] + '.mp4'
        self.movie = MovieWriter(out, self.a.fps)
        self.r = BoardRenderer(pcb, size=self.a.size, supersample=1, theme=self.a.theme,
                               show_pads=False, show_zones=False)
        th = self.r.theme
        body = th.rgb('board_body')
        mix = lambda c, k: tuple(int(body[i] * (1 - k) + c[i] * k) for i in range(3))  # noqa: E731
        try:
            colour_of = group_colours(args.groups, set(self.state.parts))[0]
        except Exception:
            colour_of = {}
        self.fill = {r: mix(colour_of[r], 0.38) for r in colour_of if r in self.movable}
        self.c = {'front': th.rgb('place_court_front'), 'back': th.rgb('place_court_back'),
                  'fixed': th.rgb('place_locked'), 'air': th.rgb('place_airwire'),
                  'moved': th.rgb('event_new'), 'pad': th.rgb('pad'),
                  'pad_back': mix(th.rgb('pad'), 0.55)}
        # the airwires' nets: every net but ground, over each part's pads
        self.nets = {}
        for ref, fp in pcb.footprints.items():
            for i, p in enumerate(fp.pads):
                nm = pcb.nets[p.net_id].name if p.net_id in pcb.nets else ''
                if p.net_id and not is_ground(nm):
                    self.nets.setdefault(p.net_id, []).append((ref, i))
        self.nets = {n: m for n, m in self.nets.items() if len({r for r, _ in m}) > 1}
        self._posed = {}
        fixed = sorted(r for r in self.state.parts if r not in self.movable)
        head = (f"input: {len(self.state.parts)} parts, {len(self.movable)} movable, "
                f"{len(fixed)} fixed")
        if os.path.abspath(board) != os.path.abspath(args.board):
            # the faces and poses the intent declares were written first:
            # show the board as given, then with them
            given = parse_kicad_pcb(args.board)
            moved = sorted(r for r in pcb.footprints
                           if r in given.footprints and _moved(given.footprints[r], pcb.footprints[r]))
            self.shot(head, times=self.hold, given={r: given.footprints[r] for r in moved})
            self.shot(f"faces and poses: {', '.join(moved)} at their declared face or pose",
                      moved=moved, times=self.hold)
        else:
            self.shot(head, times=self.hold)

    # -- drawing --------------------------------------------------------------
    def pads(self, ref):
        from placement.legality import pads_at_pose
        p = self.state.parts.get(ref)
        fp = self.pcb.footprints[ref]
        if p is None:
            return fp.pads
        key = (p.x, p.y, p.rot)
        got = self._posed.get(ref)
        if got is None or got[0] != key:
            got = self._posed[ref] = (key, pads_at_pose(fp, key))
        return got[1]

    def airwires(self, given):
        lines = []
        for members in self.nets.values():
            pts = []
            for ref, i in members:
                pads = given[ref].pads if ref in given else self.pads(ref)
                pts.append((pads[i].global_x, pads[i].global_y))
            lines += _mst(pts)
        return lines

    def shot(self, label, moved=(), times=1, given=None):
        """One frame of the state as it stands; `given` draws those parts
        from a footprint of their own (the board as given) instead."""
        given = given or {}
        r, c, st = self.r, self.c, self.state
        moved = set(moved)

        def draw(d, rr):
            w = max(1, int(round(rr.tf.length(0.08))))
            for ref, p in sorted(st.parts.items()):
                if ref in given:
                    continue
                x0, y0, x1, y1 = p.rect()
                (ax, ay), (bx, by) = rr.tf.pt(x0, y0), rr.tf.pt(x1, y1)
                box = [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]
                if ref not in self.movable:
                    d.rectangle(box, outline=c['fixed'], width=w)
                else:
                    d.rectangle(box, fill=self.fill.get(ref),
                                outline=c['back'] if p.side == 'B' else c['front'], width=w)
            for x0, y0, x1, y1 in self.airwires(given):
                d.line([rr.tf.pt(x0, y0), rr.tf.pt(x1, y1)], fill=c['air'], width=1)
            for ref, fp in self.pcb.footprints.items():
                fpg = given.get(ref)
                pads = fpg.pads if fpg is not None else self.pads(ref)
                back = (fpg or fp).layer == 'B.Cu'
                for pad in pads:
                    rr._draw_pad(d, pad, fill=c['pad_back'] if back else c['pad'])
            for ref in moved & set(st.parts):
                x0, y0, x1, y1 = st.parts[ref].rect()
                (ax, ay), (bx, by) = rr.tf.pt(x0, y0), rr.tf.pt(x1, y1)
                d.rectangle([min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)],
                            outline=c['moved'], width=2 * w)
        self.movie.add(r.frame(segments=[], vias=[], label=label, overlays=[draw]), times)

    # -- the run's hooks ------------------------------------------------------
    def on_scatter(self, seed):
        self.run += 1
        lead = f"run {self.run}, a fresh start (the last left a part with no legal pose): " \
            if self.run > 1 else ''
        self.shot(f"{lead}scatter: every movable part at a random place (seed {seed})",
                  moved=self.movable, times=self.hold)

    def on_anneal(self, moves):
        self.n, self.k, self.T = moves, 0, math.inf
        self.every = max(1, moves // max(1, self.a.frames))
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
            self.k, self.note = 0, ' (long edge parts turned half round)'
        self.T = T
        self.k += 1
        if kept:
            self.cost += dc
            self.ov = max(0.0, self.ov + do)
            self.since |= set(new)
        if self.k % self.every == 0:
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

    def finish(self, rc):
        if self.movie is None:
            return None
        cost = self.model.total()['cost']
        self.shot(f"placed: cost {cost:.0f}" + (' | parts left illegal (see the log)' if rc == 4 else ''),
                  times=1)
        path = self.movie.close(hold=2.0)
        print(f"place_movie: {path} ({self.movie.n} frames, {self.movie.n / self.a.fps:.0f} s)")
        return path


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

    def _scatter(model, movable, seed=0):
        scatter(model, movable, seed=seed)
        film.on_scatter(seed)
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


def main(argv=None):
    from route_movie import FPS, SIZE
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n')[0],
        epilog="Every other argument is place_simple.py's (board, out, --groups, --intent, "
               "--seed and the weights); --seeds is refused, one movie being one seed.")
    ap.add_argument('--movie', help='the movie to write (default: the placed board\'s '
                                    'name, .mp4); .gif writes a GIF')
    ap.add_argument('--frames', type=int, default=FRAMES,
                    help=f'frames of anneal per run (default {FRAMES})')
    ap.add_argument('--size', type=int, default=SIZE, help=f'longest frame side, px (default {SIZE})')
    ap.add_argument('--fps', type=float, default=FPS, help=f'frames per second (default {FPS:g})')
    ap.add_argument('--theme', default='dark', help='render theme: dark (default) or light')
    a, rest = ap.parse_known_args(argv)
    if '--seeds' in rest:
        ap.error('--seeds: a movie is one seed (--seed N)')
    film = Film(a)
    install(film)
    import place_simple
    rc = place_simple.main(rest)
    film.finish(rc)
    return rc


if __name__ == '__main__':
    sys.exit(main())
