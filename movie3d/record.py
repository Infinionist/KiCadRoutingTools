#!/usr/bin/env python3
"""Record a place_simple run for the 3D placement movie (prototype).

    python3 -X utf8 movie3d/record.py BOARD OUT.kicad_pcb --work DIR \\
        --groups groups.json --intent intent.json --seed 0

BOARD and OUT come first; every argument but this tool's own (--work,
--frames, --speedup, --fps) is place_simple.py's.

place_simple runs IN-PROCESS with hooks that only read its state and pass
every call through (the idea of py_placer/place_movie.py's `install`), so the
placement is place_simple's own for the same seed and OUT is its output.
Nothing in the repo is changed: the hooks patch the imported modules in this
process only.

Writes two JSON files into --work, which the page (page/movie3d.mjs) draws:

  scene.json     the board (outline, cutouts, thickness), every part in its
                 OWN frame (pads, courtyard, a body box with a height by part
                 kind), its side and group colour, the nets the airwires join,
                 the key and the colours;
  timeline.json  one entry per movie frame: every part's pose [x, y, rot,
                 height of its mounting face], the parts that just moved, the
                 caption, the box the camera frames and the camera's azimuth
                 and elevation.

The story is place_movie's: the input with its piles, the fixed parts flying
to their places, the scatter, the anneal (played faster as it goes), the
best placement, placed. In 3D, parts off the board lie on a table below it,
and a part travelling to its place arcs up and over.
"""
import argparse
import json
import math
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, 'py_placer'))
import _path  # noqa: E402,F401  (py_router and py_tools on sys.path, as for every py_placer script)

FRAMES = 120        # anneal frames per run (place_movie's default): the budget the tweens share
TWEEN = 3           # frames each anneal step takes: FRAMES / TWEEN snapshots, each played as a move
HOP = 1.2           # mm a part hops at most on an anneal step (a shorter step hops less)
SPEEDUP = 4.0       # the anneal plays this much faster at its end than at its start
FPS = 12.0
FLY = 8             # frames a fixed part takes to fly to its place (at most)
GLIDE = 20          # frames the pile takes to reach the places it is seeded at
ZOOM_FROM = 0.3     # how far into the glide the camera starts closing in on the board
PILE_GAP = 2.0      # mm off the board's edge each pile lies, at most
HOLD_S = 0.6        # seconds a step's frame holds
END_S = 3.0         # seconds on the placed board (the camera keeps orbiting)
TABLE = -3.0        # the table under the board (y of its top); the board stands above it
#: The board is DRAWN this thick at most: at its real 1.6 mm the slab read as
#: a block next to 0402 parts (Andy, on the first stills).
DRAWN_THICKNESS = 0.8
SWEEP = 20.0        # the camera orbits this far either side of its base azimuth over the movie
ELEV = 46.0         # camera elevation, deg
TALL = 0.9          # mm: a body this tall or taller can hide the parts behind it

#: Body heights (mm) by footprint name, then by reference prefix. A stand-in:
#: the board file carries no heights the parser reads.
NAME_HEIGHTS = (('usb', 3.2), ('pinheader', 2.5), ('pin_header', 2.5), ('b3u', 1.6),
                ('sot-23', 1.1), ('sot23', 1.1), ('qfn', 0.85), ('dfn', 0.85),
                ('soic', 1.6), ('tssop', 1.1), ('qfp', 1.4), ('bga', 1.2),
                ('crystal_smd_2016', 0.55), ('crystal_smd_3225', 0.8), ('hc49', 3.5))
PREFIX_HEIGHTS = {'R': 0.35, 'C': 0.5, 'L': 0.8, 'FB': 0.5, 'D': 0.45, 'LED': 0.45,
                  'Q': 1.0, 'U': 1.0, 'Y': 0.8, 'X': 0.8, 'SW': 2.0, 'S': 2.0, 'J': 3.0,
                  'P': 3.0, 'BT': 3.0, 'F': 1.0}
#: Parts drawn by their courtyard (mechanical bodies), not by their pad field.
COURT_BODY = ('J', 'P', 'SW', 'S', 'BT')


def _ease(t):
    return t * t * (3 - 2 * t)


def _lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def _union(rects):
    rects = list(rects)
    return (min(q[0] for q in rects), min(q[1] for q in rects),
            max(q[2] for q in rects), max(q[3] for q in rects))


def _prefix(ref):
    m = re.match(r'[A-Za-z]+', ref)
    return m.group(0).upper() if m else ''


def body_height(ref, name):
    n = (name or '').lower()
    for key, h in NAME_HEIGHTS:
        if key in n:
            return h
    return PREFIX_HEIGHTS.get(_prefix(ref), 1.0)


def _not_assembled(fp):
    """No body to draw: no 3D model AND nothing placed there (a test pad,
    a solder jumper) -- stage3d's rule (#1098)."""
    return (not getattr(fp, 'has_model', False)
            and set(getattr(fp, 'attrs', ()) or ()) & {'board_only', 'exclude_from_pos_files'})


def part_scene(ref, fp, part, colour, fixed):
    """One part in its own frame (its pose (0, 0, 0)): pads, courtyard, body."""
    from placement.legality import pads_at_pose
    posed = pads_at_pose(fp, (0.0, 0.0, 0.0))
    rot = math.radians(fp.rotation or 0.0)

    def to_local(x, y):
        dx, dy = x - fp.x, y - fp.y
        return (dx * math.cos(rot) - dy * math.sin(rot), dx * math.sin(rot) + dy * math.cos(rot))
    pads = []
    for q, src in zip(posed, fp.pads):
        layers = [str(x) for x in (src.layers or ())]
        drill = float(getattr(src, 'drill', 0.0) or 0.0)
        if getattr(src, 'pad_type', '') == 'np_thru_hole':
            face = 'N'                      # no copper: its hole only
        elif drill > 0:
            face = 'T'
        elif any(x.startswith('B.') for x in layers) and not any(
                x.startswith('F.') or x.startswith('*') for x in layers):
            face = 'B'
        else:
            face = 'F'
        hx = hy = None
        if drill > 0 and getattr(src, 'hole_x', None) is not None:
            hx, hy = to_local(src.hole_x, src.hole_y)
        pads.append([round(q.global_x, 4), round(q.global_y, 4), round(float(q.size_x), 4),
                     round(float(q.size_y), 4), round(float(q.rect_rotation or 0.0), 3),
                     (q.shape or 'rect').lower(), face, round(drill, 4),
                     None if hx is None else round(hx, 4), None if hy is None else round(hy, 4)])
    if part is not None:
        court = part.rect(0.0, 0.0, 0.0)
    elif pads:
        court = (min(p[0] - p[2] / 2 for p in pads), min(p[1] - p[3] / 2 for p in pads),
                 max(p[0] + p[2] / 2 for p in pads), max(p[1] + p[3] / 2 for p in pads))
    else:
        court = (-0.5, -0.5, 0.5, 0.5)
    court = [round(v, 4) for v in court]
    body = None
    copper = [p for p in pads if p[6] != 'N']
    if not _not_assembled(fp) and copper:
        h = body_height(ref, fp.footprint_name)
        if _prefix(ref) in COURT_BODY:
            x0, y0, x1, y1 = court
            ix = iy = 0.25
        else:   # the body sits INSIDE the pad field, as a package does (stage3d's rule)
            x0 = min(p[0] - p[2] / 2 for p in copper)
            x1 = max(p[0] + p[2] / 2 for p in copper)
            y0 = min(p[1] - p[3] / 2 for p in copper)
            y1 = max(p[1] + p[3] / 2 for p in copper)
            ix, iy = min(0.15 * (x1 - x0), 0.4), min(0.15 * (y1 - y0), 0.4)
        body = [round(x0 + ix, 4), round(y0 + iy, 4), round(x1 - ix, 4), round(y1 - iy, 4), h]
    side = 'B' if (fp.layer or '').startswith('B') else 'F'
    return {'ref': ref, 'side': side, 'pads': pads, 'court': court, 'body': body,
            'color': list(colour) if colour else None, 'fixed': fixed}


class Recorder:
    """Records the placement state as frames. `start` is called with
    place_simple's built state; the anneal's hooks call `on_*`."""

    def __init__(self, a):
        self.a = a
        self.frames = []
        self.run = 0
        self.scene = None

    # -- setup ----------------------------------------------------------------
    def start(self, pcb, board, args, built):
        from shapely.geometry import Point, Polygon
        from placement.grouping import is_ground, load_groups
        from render_placement import group_palette
        import render_theme
        self.state, self.model, self.movable, _notes = built
        st = self.state
        self.pcb = pcb
        # the board the parts' models are exported from: the one placed (the
        # input, or place_simple's copy with the declared faces written), its
        # model paths resolved against the ORIGINAL board's directory
        with open(board, encoding='utf-8') as f:
            self.model_src = (f.read(), os.path.dirname(os.path.abspath(args.board)))
        self.hold = max(1, int(round(HOLD_S * self.a.fps)))
        th = render_theme.theme('dark', strict=False)
        # each group with a movable member its own colour, as place_movie
        groups = [g for g in load_groups(args.groups)['groups']
                  if any(r in self.movable for r in g['refs'])] if args.groups else []
        colours = group_palette(len(groups))
        colour, key = {}, []
        for g, rgb in zip(groups, colours):
            role = g.get('role')
            key.append({'rgb': list(rgb), 'mark': 'solid',
                        'text': g['name'] + (f" ({role})" if role and role != g['name'] else '')})
            for ref in g['refs']:
                if ref in self.movable:
                    colour[ref] = rgb
        if any(r not in colour for r in self.movable):
            key.append({'rgb': list(th.rgb('place_court_front')), 'mark': 'solid', 'text': 'in no group'})
            for r in self.movable:
                colour.setdefault(r, th.rgb('place_court_front'))
        fixed_rgb = (120, 118, 108)
        if len(self.movable) < len(st.parts):
            key.append({'rgb': list(fixed_rgb), 'mark': 'solid', 'text': 'fixed'})
        key.append({'rgb': [250, 250, 250], 'mark': 'ring', 'text': 'just moved'})
        # every part place_simple models, and any other footprint with pads
        # (a pad-less logo it does not model is left out)
        self.refs = sorted(r for r in pcb.footprints if not r.startswith('#')
                           and (r in st.parts or pcb.footprints[r].pads))
        self.idx = {r: i for i, r in enumerate(self.refs)}
        parts = [part_scene(r, pcb.footprints[r], st.parts.get(r), colour.get(r, fixed_rgb),
                            r not in self.movable) for r in self.refs]
        self.side = {p['ref']: p['side'] for p in parts}
        self.bodyh = {p['ref']: (p['body'][4] if p['body'] else 0.0) for p in parts}
        self.court = {p['ref']: p['court'] for p in parts}
        self.fixed = {p['ref'] for p in parts if p['fixed']}
        # the airwires' nets: every net but ground, joining two parts or more
        nets = {}
        for i, ref in enumerate(self.refs):
            for k, p in enumerate(pcb.footprints[ref].pads):
                nm = pcb.nets[p.net_id].name if p.net_id in pcb.nets else ''
                if p.net_id and not is_ground(nm):
                    nets.setdefault(p.net_id, []).append([i, k])
        nets = [m for _n, m in sorted(nets.items()) if len({i for i, _k in m}) > 1]
        bi = pcb.board_info
        bx0, by0, bx1, by1 = bi.board_bounds
        outline = [[float(x), float(y)] for x, y in (bi.board_outline or [])]
        if len(outline) < 3:
            outline = [[bx0, by0], [bx1, by0], [bx1, by1], [bx0, by1]]
        cutouts = [[[float(x), float(y)] for x, y in ring]
                   for ring in (getattr(bi, 'board_cutouts', None) or []) if len(ring) >= 3]
        self.board_poly = Polygon(outline).buffer(0)
        self._Point = Point
        st_total = sum(float(getattr(s, 'thickness', 0) or 0) for s in (bi.stackup or []))
        real = st_total if 0.2 <= st_total <= 10 else 1.6
        self.d = min(real, DRAWN_THICKNESS)
        self.scene = {
            'thickness': round(self.d, 4), 'real_thickness': round(real, 4), 'table': TABLE, 'bounds': [bx0, by0, bx1, by1],
            'outline': outline, 'cutouts': cutouts, 'parts': parts, 'nets': nets, 'key': key,
            'glb': None,
            # the poses the models are exported at: the page re-poses each by
            # F(now) F(export)^-1
            'export_poses': {r: [pcb.footprints[r].x, pcb.footprints[r].y,
                                 pcb.footprints[r].rotation or 0.0, self.side[r]] for r in self.refs},
            'colors': {'ground': list(th.rgb('ground')), 'board': [24, 74, 48],
                       'edge': list(th.rgb('board_edge')), 'pad': list(th.rgb('pad')),
                       'hole': list(th.rgb('pad_hole')), 'air': [150, 162, 185],
                       'table': [34, 36, 40], 'moved': [255, 255, 255],
                       'band': list(th.rgb('chrome_band')), 'text': list(th.rgb('chrome_text'))}}
        diag = math.hypot(bx1 - bx0, by1 - by0)
        self.arc = min(15.0, max(2.5, 0.15 * diag))
        # the piles, as place_movie lays them out: the movable parts where
        # they lie before the scatter, moved to PILE_GAP mm off the board's
        # nearest edge, and the fixed parts in a pile of their own off
        # another edge
        self.pile = {r: (p.x, p.y, p.rot) for r, p in st.parts.items() if r in self.movable}
        mside = None
        if self.pile:
            px0, py0, px1, py1 = _union(st.parts[r].rect() for r in self.pile)
            gaps = {'below': py0 - by1, 'above': by0 - py1, 'right': px0 - bx1, 'left': bx0 - px1}
            side, gap = max(gaps.items(), key=lambda kv: kv[1])
            if gap > PILE_GAP:
                move, mside = gap - PILE_GAP, side
                dx = {'right': -move, 'left': move}.get(side, 0.0)
                dy = {'below': -move, 'above': move}.get(side, 0.0)
                self.pile = {r: (x + dx, y + dy, q) for r, (x, y, q) in self.pile.items()}
        fixed = sorted(r for r in st.parts if r not in self.movable)
        self.fixed_pile = self._fixed_pile(fixed, mside)
        # each view is [x0, y0, x1, y1, lowest, highest]: the board's box, and
        # with the piles in view, the table's height too
        top = self.d + 2.5
        self.home = (bx0 - 1.0, by0 - 1.0, bx1 + 1.0, by1 + 1.0, 0.0, top)
        piled = list(self._rects_at({**self.pile, **self.fixed_pile}).values())
        u = _union([self.home[:4]] + piled)
        self.wide = ((u[0] - 1.0, u[1] - 1.0, u[2] + 1.0, u[3] + 1.0, TABLE, top)
                     if u != self.home[:4] else None)
        self.view = self.wide or self.home
        poses = {**self.pile, **self.fixed_pile}
        self.shot(f"input: {len(st.parts)} parts, {len(self.movable)} movable, {len(fixed)} fixed",
                  times=2 * self.hold, poses=poses)
        # the fixed parts fly to their places, biggest first, each arcing over
        size = {r: (lambda q: (q[2] - q[0]) * (q[3] - q[1]))(st.parts[r].rect()) for r in self.fixed_pile}
        order = sorted(self.fixed_pile, key=lambda r: (-size[r], r))
        fly = max(3, min(FLY, 48 // max(1, len(order))))
        for k, ref in enumerate(order, 1):
            home = (st.parts[ref].x, st.parts[ref].y, st.parts[ref].rot)
            for i in range(1, fly + 1):
                t = _ease(i / fly)
                poses[ref] = _lerp(self.fixed_pile[ref], home, t)
                lifts = {ref: self._arc(ref, self.fixed_pile[ref], home, t)}
                self.shot(f"fixed parts: {ref} to its place ({k}/{len(order)})", moved=[ref],
                          poses=poses, lifts=lifts)
            del poses[ref]
        self.shot("fixed parts in place", times=self.hold, poses=poses)

    def _rects_at(self, poses):
        out = {}
        for r, (x, y, _q) in poses.items():
            p = self.state.parts[r]
            a, b, c, d = p.rect()
            out[r] = (a + x - p.x, b + y - p.y, c + x - p.x, d + y - p.y)
        return out

    def _fixed_pile(self, refs, taken):
        """place_movie's fixed pile: PILE_GAP mm off the edge (not `taken`)
        that leaves the board biggest in a 16:9 frame."""
        if not refs:
            return {}
        st = self.state
        bx0, by0, bx1, by1 = self.pcb.board_info.board_bounds
        rs = {r: st.parts[r].rect() for r in refs}
        mw = max(q[2] - q[0] for q in rs.values())
        mh = max(q[3] - q[1] for q in rs.values())
        cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
        anchors = {'above': (cx, by0 - PILE_GAP - mh / 2), 'below': (cx, by1 + PILE_GAP + mh / 2),
                   'left': (bx0 - PILE_GAP - mw / 2, cy), 'right': (bx1 + PILE_GAP + mw / 2, cy)}
        rng = random.Random(0)
        jit = {r: (rng.uniform(-1, 1), rng.uniform(-1, 1)) for r in sorted(refs)}
        reach = min(1.0, 0.15 * max(mw, mh))
        W, H = self.a.width, self.a.height
        best = None
        for side, (ax, ay) in anchors.items():
            if side == taken:
                continue
            piled = {}
            for r, q in rs.items():
                p = st.parts[r]
                tx, ty = ax + jit[r][0] * reach, ay + jit[r][1] * reach
                piled[r] = (p.x + tx - (q[0] + q[2]) / 2, p.y + ty - (q[1] + q[3]) / 2, p.rot)
            u = _union([(bx0, by0, bx1, by1)] + list(self._rects_at({**self.pile, **piled}).values()))
            # the camera looks across the board's long side: fit either way round
            s = max(min(W / (u[2] - u[0]), H / (u[3] - u[1])), min(W / (u[3] - u[1]), H / (u[2] - u[0])))
            if best is None or s > best[0]:
                best = (s, piled)
        return best[1]

    # -- heights ----------------------------------------------------------------
    def base(self, ref, x, y):
        """Height of the part's mounting face: the board's face when the part
        stands on the board, else the table (a back-side part lies body down)."""
        on = self.board_poly.contains(self._Point(x, y))
        if self.side.get(ref) == 'B':
            return 0.0 if on else TABLE + self.bodyh.get(ref, 0.0)
        return self.d if on else TABLE

    def _arc(self, ref, a, b, t):
        """Height on a hop from pose `a` to pose `b`, at `t` in [0, 1]."""
        h0, h1 = self.base(ref, a[0], a[1]), self.base(ref, b[0], b[1])
        dist = math.hypot(b[0] - a[0], b[1] - a[1])
        return h0 + (h1 - h0) * t + min(self.arc, 0.5 * dist + 0.5) * math.sin(math.pi * t)

    # -- frames -----------------------------------------------------------------
    def shot(self, label, moved=(), times=1, poses=None, lifts=None):
        """One frame of the state as it stands; `poses` draws those parts at
        another pose (a glide), `lifts` at another height (a hop)."""
        poses, lifts = poses or {}, lifts or {}
        st = self.state
        row = []
        for r in self.refs:
            if r in poses:
                x, y, q = poses[r]
            elif r in st.parts:
                p = st.parts[r]
                x, y, q = p.x, p.y, p.rot
            else:
                fp = self.pcb.footprints[r]
                x, y, q = fp.x, fp.y, fp.rotation or 0.0
            h = lifts[r] if r in lifts else self.base(r, x, y)
            row.append([round(x, 4), round(y, 4), round(q, 3), round(h, 3)])
        rec = {'p': row, 'label': label, 'view': [round(v, 3) for v in self.view],
               'moved': sorted(self.idx[r] for r in set(moved) if r in self.idx)}
        self.frames.extend([rec] * times)

    # -- the run's hooks (place_movie's) ---------------------------------------
    def on_scatter(self, seed, clustered=False):
        self.run += 1
        if self.run > 1:
            self.shot(f"run {self.run}, a fresh start (the last left a part with no legal "
                      f"pose): scatter (seed {seed})", moved=self.movable, times=self.hold)
            return
        label = (f"scatter: each group's parts round one random place (seed {seed})" if clustered
                 else f"scatter: each movable part to a random place (seed {seed})")
        seeded = {r: (p.x, p.y, p.rot) for r, p in self.state.parts.items() if r in self.movable}
        # the pile takes off part by part: each starts a little after the last
        rng = random.Random(1)
        start = {r: rng.uniform(0.0, 0.45) for r in sorted(seeded)}
        span = 1.0 - 0.45
        for i in range(1, GLIDE + 1):
            poses, lifts = {}, {}
            for r in seeded:
                t = _ease(min(1.0, max(0.0, (i / GLIDE - start[r]) / span)))
                poses[r] = _lerp(self.pile[r], seeded[r], t)
                lifts[r] = self._arc(r, self.pile[r], seeded[r], t)
            if self.wide:       # ... the camera closing in on the board as they land
                self.view = _lerp(self.wide, self.home,
                                  _ease(min(1.0, max(0.0, (i / GLIDE - ZOOM_FROM) / (1 - ZOOM_FROM)))))
            self.shot(label, moved=self.movable, poses=poses, lifts=lifts)
        self.view = self.home
        self.shot(label, times=self.hold)

    def on_anneal(self, moves):
        # snapshot i of F is taken at move N g(i/F): g' grows linearly to
        # SPEEDUP times its start, so the play speeds up that much. The frame
        # budget is shared: F = frames / tween snapshots, each played as a
        # move of `tween` frames
        self.tween = max(1, self.a.tween)
        f = max(1, self.a.frames // self.tween)
        sp = max(1.0, self.a.speedup)
        g = lambda u: (u + (sp - 1) / 2 * u * u) / ((sp + 1) / 2)  # noqa: E731
        self.marks = [max(1, math.ceil(moves * g(i / f))) for i in range(1, f + 1)]
        self.n, self.k, self.mi, self.T = moves, 0, 0, math.inf
        self.since, self.note = set(), ''
        self.prev = self._poses()
        if getattr(self, 'anneal_from', None) is None:
            self.anneal_from = len(self.frames)

    def _poses(self):
        return {r: (p.x, p.y, p.rot) for r, p in self.state.parts.items() if r in self.movable}

    def play(self, label, moved, frames):
        """The parts that moved since the last snapshot, played from where
        they were to where they are over `frames` frames, each with a small
        hop (a turn takes the short way round)."""
        cur = self._poses()
        went = [r for r in cur if cur[r] != self.prev.get(r, cur[r])]
        for j in range(1, frames + 1):
            t = _ease(j / frames)
            poses, lifts = {}, {}
            for r in went:
                a, b = self.prev[r], cur[r]
                turn = (b[2] - a[2] + 180.0) % 360.0 - 180.0
                poses[r] = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, (a[2] + turn * t) % 360.0)
                dist = math.hypot(b[0] - a[0], b[1] - a[1])
                h0, h1 = self.base(r, a[0], a[1]), self.base(r, b[0], b[1])
                lifts[r] = h0 + (h1 - h0) * t + min(HOP, 0.3 + 0.25 * dist) * math.sin(math.pi * t)
            self.shot(label, moved=moved, poses=poses, lifts=lifts)
        self.prev = cur

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
            self.play(f"{what} | T {T:.3g} | cost {self.cost:.0f} | overlap {self.ov:.2f} mm2",
                      self.since, 1 if a.shoving else self.tween)
            self.since = set()

    def on_annealed(self, res):
        legal = res.get('reseated') or []
        label = (f"best placement, cost {res['best']:.0f}"
                 + (f" | to the nearest legal pose: {', '.join(legal)}" if legal else ''))
        # the anneal ends on the best placement it saw: the parts go back to it
        back = [r for r, q in self._poses().items() if q != self.prev.get(r, q)]
        self.play(label, set(back) | set(legal), 2 * self.tween)
        self.shot(label, moved=legal, times=self.hold)

    def placed(self, rc):
        cost = self.model.total()['cost']
        label = f"placed: cost {cost:.0f}" + (' | parts left illegal (see the log)' if rc == 4 else '')
        self.shot(label, times=max(1, int(round(END_S * self.a.fps))))

    def _hidden(self, base, step=4):
        """How much the tall parts hide of the parts that move, over the
        film, with the camera orbiting SWEEP either side of `base`: for every
        sampled frame, each part whose centre lies in the ground a tall body
        hides from the camera (up to its height / tan(elevation) behind it)
        costs that body's height, double when the part is moving. A fixed
        part is drawn see-through, so it hides at half. Only the frames from
        the anneal on count: before it the parts lie piled, hiding each other
        from every side."""
        n = len(self.frames)
        cot = 1.0 / math.tan(math.radians(ELEV))
        tall = [i for i, r in enumerate(self.refs)
                if self.bodyh[r] >= TALL and self.side[r] == 'F']
        half = {i: 0.5 * max(c[2] - c[0], c[3] - c[1])
                for i, c in enumerate(self.court[r] for r in self.refs)}
        cost = 0.0
        for k in range(getattr(self, 'anneal_from', None) or 0, n, step):
            f = self.frames[k]
            az = math.radians(base + SWEEP * (2.0 * k / max(1, n - 1) - 1.0))
            ux, uy = math.sin(az), math.cos(az)        # board plane, toward the camera
            p = f['p']
            moving = set(f['moved'])
            for t in tall:
                tx, ty, ht = p[t][0], p[t][1], self.bodyh[self.refs[t]]
                w = ht / 2.0 if self.refs[t] in self.fixed else ht
                for i, q in enumerate(p):
                    if i == t:
                        continue
                    vx, vy = q[0] - tx, q[1] - ty
                    behind = -(vx * ux + vy * uy)
                    side = abs(vx * uy - vy * ux)
                    if 0.0 < behind < ht * cot + half[t] and side < half[t] + half[i]:
                        cost += w * (2.0 if i in moving else 1.0)
        return cost

    def finish(self):
        """The timeline: each frame with the camera's azimuth and
        elevation. The camera orbits SWEEP either side of a base azimuth,
        chosen so that the tall parts hide the least of the parts that move;
        a view across the board's long side wins a near tie."""
        n = len(self.frames)
        bx0, by0, bx1, by1 = self.pcb.board_info.board_bounds
        long_x = (bx1 - bx0) >= (by1 - by0)
        scores = []
        for base in range(0, 360, 10):
            # 0: the camera south of the board; across the long side is
            # 90/270 for a tall board, 0/180 for a wide one
            across = abs(math.cos(math.radians(base))) if long_x else abs(math.sin(math.radians(base)))
            scores.append((self._hidden(base) * (1.0 + 0.25 * (1.0 - across)) + 0.5 * (1.0 - across),
                           base))
        self.base_az = min(scores)[1]
        out = []
        for i, f in enumerate(self.frames):
            g = dict(f)
            g['az'] = round(self.base_az + SWEEP * (2.0 * i / max(1, n - 1) - 1.0), 4)
            g['el'] = ELEV
            out.append(g)
        return {'fps': self.a.fps, 'width': self.a.width, 'height': self.a.height,
                'refs': self.refs, 'base_az': self.base_az, 'frames': out}


def install(rec):
    """Hook the recorder into place_simple's run (place_movie's `install`).
    The hooks only read the state and pass every call through."""
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
            rec.on_total(t['cost'])
            return t
        model.total = _total
        rec.start(pcb, board, args, out)
        return out
    place_simple.build = _build

    scatter, anneal = simple_anneal.scatter, simple_anneal.anneal

    def _scatter(model, movable, seed=0, clustered=False):
        scatter(model, movable, seed=seed, clustered=clustered)
        rec.on_scatter(seed, clustered)
    simple_anneal.scatter = _scatter

    def _anneal(model, movable, **kw):
        rec.on_anneal(kw.get('moves', 11000))
        res = anneal(model, movable, **kw)
        rec.on_annealed(res)
        return res
    simple_anneal.anneal = _anneal

    start_t, trial, overlap = Annealer.start_temperature, Annealer.trial, Annealer.overlap

    def _start_t(self, *a, **kw):
        rec.on_seats(self)
        return start_t(self, *a, **kw)

    def _trial(self, new, T):
        res = trial(self, new, T)
        rec.on_trial(self, new, T, res)
        return res

    def _overlap(self, refs=None):
        ov = overlap(self, refs)
        if refs is None:
            rec.on_overlap(ov)
        return ov
    Annealer.start_temperature, Annealer.trial, Annealer.overlap = _start_t, _trial, _overlap


def attach_models(a, rec):
    """The parts' KiCad models (models.py), named in the scene; a part
    without one keeps its box. Prints what fell back."""
    sc = rec.scene
    sc['tint'] = max(0.0, min(1.0, a.tint))
    if a.no_models:
        print('record: models off (--no-models): every part is a box')
        return
    import models
    t0 = time.time()
    info, why = models.export(rec.model_src[0], rec.model_src[1], rec.refs, cli=a.kicad_cli)
    if info is None:
        print(f'record: no part models ({why}); every part is a box')
        return
    matched = set(info['matched'])
    for p in sc['parts']:
        p['model'] = p['ref'] in matched
    boxed = sorted(p['ref'] for p in sc['parts'] if not p['model'] and p['body'])
    bare = sorted(p['ref'] for p in sc['parts'] if not p['model'] and not p['body'])
    sc['glb'] = {'path': info['path'], 'matched': sorted(matched)}
    rec.model_report = {'models': len(matched), 'parts': len(sc['parts']), 'boxes': boxed,
                        'bare': bare, 'cached': info['cached']}
    print(f"record: {len(matched)} of {len(sc['parts'])} parts drawn from their KiCad model "
          f"({'cached' if info['cached'] else f'exported in {time.time() - t0:.1f} s'}"
          + (f"; {info['note']}" if info['note'] else '') + "); "
          + (f"{len(boxed)} fell back to a box ({', '.join(boxed)})" if boxed else 'none fell back to a box')
          + (f"; {len(bare)} have no model and nothing assembled ({', '.join(bare)})" if bare else ''))


def own_args(ap, work_required=True):
    ap.add_argument('--work', required=work_required,
                    help='directory for scene.json and timeline.json' + ('' if work_required else
                         " (default: OUT's name + _movie3d)"))
    ap.add_argument('--frames', type=int, default=FRAMES,
                    help=f'anneal frames, tweens included (default {FRAMES})')
    ap.add_argument('--tween', type=int, default=TWEEN,
                    help=f'frames per anneal step: --frames / --tween snapshots, each played '
                         f'as a move (default {TWEEN}; 1 = cuts, as the 2D movie)')
    ap.add_argument('--speedup', type=float, default=SPEEDUP,
                    help=f'how much faster the anneal plays at its end (default {SPEEDUP:g})')
    ap.add_argument('--fps', type=float, default=FPS, help=f'frames per second (default {FPS:g})')
    ap.add_argument('--no-models', action='store_true',
                    help="draw every part as a box (default: each part's KiCad 3D model, "
                         'exported by kicad-cli and cached in movie3d/cache/)')
    ap.add_argument('--kicad-cli', help='the kicad-cli to export the models with (default: '
                                        '$KICAD_CLI, else the installed KiCad)')
    ap.add_argument('--tint', type=float, default=0.7,
                    help="how far each model's colours are tinted toward its group's colour, "
                         '0-1 (default 0.7)')
    ap.add_argument('--width', type=int, default=1000, help='frame width, px (default 1000)')
    ap.add_argument('--height', type=int, default=560, help='frame height, px (default 560)')


def record(a, rest):
    """Run place_simple with the recorder; write scene.json and
    timeline.json into a.work. Returns (place_simple's exit code, paths)."""
    rec = Recorder(a)
    install(rec)
    import place_simple
    os.makedirs(a.work, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(rest[1])), exist_ok=True)
    rc = place_simple.main(rest)
    if rec.scene is None:
        return rc, None
    rec.placed(rc)
    attach_models(a, rec)
    sp, tp = os.path.join(a.work, 'scene.json'), os.path.join(a.work, 'timeline.json')
    with open(sp, 'w', encoding='utf-8') as f:
        json.dump(rec.scene, f)
    with open(tp, 'w', encoding='utf-8') as f:
        json.dump(rec.finish(), f)
    return rc, (sp, tp, len(rec.frames))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], allow_abbrev=False)
    own_args(ap)
    a, rest = ap.parse_known_args(argv)
    if len(rest) < 2 or rest[0].startswith('-') or rest[1].startswith('-'):
        ap.error('BOARD and OUT come first')
    rc, got = record(a, rest)
    if got:
        print(f"record: {got[2]} frames -> {got[0]}, {got[1]}")
    return rc


if __name__ == '__main__':
    sys.exit(main())
