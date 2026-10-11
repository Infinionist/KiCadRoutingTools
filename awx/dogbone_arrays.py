#!/usr/bin/env python3
"""dogbone_arrays.py IN.kicad_pcb OUT.kicad_pcb REF[,REF..] -- every netted ball of the arrays DOG-BONED the human's way
(the ESCAPE fanout's base, route_bus --escape): a short stub on the ball's layer to a through via on one of its four
diagonal gap points, so every ball's via stands on a gap point and the BALL LINES -- the rows and columns through the
balls -- are free on every other layer, for the escapes (escape_plan.py) to run out along.

The gap point: toward the other array on the axis it lies along most, outward from the array's middle on the other
(the human's zynq: U1's dog-bones all toward U5, U5's toward U1 and out of its two halves); else the next of the four
clear; else a via in the ball's own pad. A site is clear where its via ring keeps the clearance off every other pad's
copper (any footprint, any layer: a via is through) and every via, and its drill the hole-to-hole off every other drill;
the stub the clearance off every other pad on its layer. A movable two-pad passive on the arrays' other face beneath one
of them (an unlocked C/R/L/FB) is no obstacle: the cap step after this (place_fanout_clearance --beneath-only) moves it
off the vias, as the chain's cap step moves it off a fanout's. The outermost balls first: an edge ball has the most room.
Sizes: the run's rules (rules.active: the fanout track and clearance, the via, the hole-to-hole floor). Prints per array
how many balls were dog-boned, in their pads, and left without one."""
KRT_TOOL = {'scope': [], 'kind': 'actor'}   # #937: a research tool (awx), catalogued, shown at no door

import contextlib
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'py_router'))
sys.path.insert(0, HERE)
from kicad_parser import parse_kicad_pcb  # noqa: E402
from kicad_writer import add_tracks_and_vias_to_pcb  # noqa: E402
import rules as _rules  # noqa: E402


def _pad_dist(x, y, p):
    """(x, y) to pad p's copper -- a round pad by its circle (point_to_pad_rect_dist reads it as its square: a diagonal
    gap site at 0.8 mm pitch measured 0.314 from a 0.356 ball, its circle 0.388)"""
    if p.shape == 'circle':
        return max(0.0, math.hypot(p.global_x - x, p.global_y - y) - p.size_x / 2)
    from routing_utils import point_to_pad_rect_dist
    return point_to_pad_rect_dist(x, y, p)


def beneath(pcb, fp, refs):
    """a movable passive BENEATH one of the arrays: an unlocked two-pad C/R/L/FB part on the arrays' other face, its
    centre inside an array's pad box -- the cap step moves it off the vias"""
    if fp.reference in refs or len(fp.pads) != 2 or getattr(fp, 'locked', False):
        return False
    if fp.reference.rstrip('0123456789').upper() not in ('C', 'R', 'L', 'FB', 'RX', 'CX'):
        return False
    for r in refs:
        a = pcb.footprints[r]
        if fp.layer == a.layer:
            continue
        xs = [q.global_x for q in a.pads]
        ys = [q.global_y for q in a.pads]
        if min(xs) <= fp.x <= max(xs) and min(ys) <= fp.y <= max(ys):
            return True
    return False


def dogbone(pcb, refs, log=print):
    """(tracks, vias) the dog-bones of every netted ball of `refs`, and per array (dog-boned, in pad, left)"""
    r = _rules.active()
    VIA, DRILL, STUB, CLEAR = r.via_size, r.via_drill, r.fan_track, r.fan_clear
    H2H = getattr(r, 'hole_to_hole', None) or 0.25
    moved = {fp.reference for fp in pcb.footprints.values() if beneath(pcb, fp, refs)}
    allpads = [p for fp in pcb.footprints.values() if fp.reference not in moved for p in fp.pads
               if any(L.endswith('.Cu') for L in p.layers) or (p.drill or 0) > 0]
    vias = [(v.x, v.y, v.size, v.drill) for v in pcb.vias]
    holes = [(p.global_x, p.global_y, p.drill) for p in allpads if (p.drill or 0) > 0]
    near = lambda x, y, R=1.6: [p for p in allpads if abs(p.global_x - x) < R and abs(p.global_y - y) < R]

    def site_ok(x, y, own):
        for p in near(x, y):
            if p is own or (p.net_id and p.net_id == own.net_id):
                continue
            if p.pad_type == 'np_thru_hole':
                if math.hypot(p.global_x - x, p.global_y - y) < (p.drill or 0) / 2 + DRILL / 2 + H2H:
                    return False
                continue
            if _pad_dist(x, y, p) < VIA / 2 + CLEAR:
                return False
        for vx, vy, vs, vd in vias:
            if math.hypot(vx - x, vy - y) < vs / 2 + VIA / 2 + CLEAR or math.hypot(vx - x, vy - y) < vd / 2 + DRILL / 2 + H2H:
                return False
        return all(math.hypot(hx - x, hy - y) >= hd / 2 + DRILL / 2 + H2H for hx, hy, hd in holes)

    def stub_ok(a, b, own, home):
        for k in range(13):
            x, y = a[0] + (b[0] - a[0]) * k / 12, a[1] + (b[1] - a[1]) * k / 12
            for p in near(x, y, 1.0):
                if p is own or (p.net_id and p.net_id == own.net_id) or p.pad_type == 'np_thru_hole':
                    continue
                if home not in p.layers and '*.Cu' not in p.layers and not (p.drill or 0) > 0:
                    continue
                if _pad_dist(x, y, p) < STUB / 2 + CLEAR:
                    return False
        return True
    tracks, new_vias, stats = [], [], {}
    for ref in refs:
        fp = pcb.footprints[ref]
        pads = [p for p in fp.pads if p.net_id and any(L.endswith('.Cu') for L in p.layers)]
        xs = sorted({round(p.global_x, 3) for p in pads})
        ys = sorted({round(p.global_y, 3) for p in pads})
        px = min((b - a for a, b in zip(xs, xs[1:]) if b - a > 0.05), default=None)
        py = min((b - a for a, b in zip(ys, ys[1:]) if b - a > 0.05), default=None)
        if px is None and py is None:
            # (no array: one ball, or none netted -- nothing to dog-bone a gap of)
            log(f'  {ref}: no ball array ({len(pads)} netted ball(s)) -- not dog-boned')
            stats[ref] = (0, 0, [])
            continue
        px, py = px or py, py or px              # (one row or one column: the pitch along it, both ways)
        cx, cy = (xs[0] + xs[-1]) / 2, (ys[0] + ys[-1]) / 2
        other = next((pcb.footprints[o] for o in refs if o != ref), None)
        ox, oy = (other.x - cx, other.y - cy) if other is not None else (0.0, 0.0)
        prim_x = abs(ox) >= abs(oy)
        pads.sort(key=lambda p: -max(abs(p.global_x - cx) / px, abs(p.global_y - cy) / py))
        n_in, left = 0, []
        for p in pads:
            home = next((L for L in p.layers if L.endswith('.Cu')), 'F.Cu')
            sx = 1 if p.global_x >= cx else -1
            sy = 1 if p.global_y >= cy else -1
            if other is not None:
                if prim_x:
                    sx = 1 if ox > 0 else -1
                else:
                    sy = 1 if oy > 0 else -1
            done = False
            for dx, dy in ((sx, sy), (sx, -sy), (-sx, sy), (-sx, -sy)):
                x, y = p.global_x + dx * px / 2, p.global_y + dy * py / 2
                if site_ok(x, y, p) and stub_ok((p.global_x, p.global_y), (x, y), p, home):
                    tracks.append({'start': (p.global_x, p.global_y), 'end': (x, y), 'width': STUB, 'layer': home,
                                   'net_id': p.net_id})
                    new_vias.append({'x': x, 'y': y, 'size': VIA, 'drill': DRILL, 'layers': ['F.Cu', 'B.Cu'],
                                     'net_id': p.net_id})
                    vias.append((x, y, VIA, DRILL))
                    done = True
                    break
            if not done and site_ok(p.global_x, p.global_y, p):
                new_vias.append({'x': p.global_x, 'y': p.global_y, 'size': VIA, 'drill': DRILL,
                                 'layers': ['F.Cu', 'B.Cu'], 'net_id': p.net_id})
                vias.append((p.global_x, p.global_y, VIA, DRILL))
                n_in += 1
                done = True
            if not done:
                left.append(f'{ref}.{p.pad_number}')
        stats[ref] = (len(pads) - len(left), n_in, left)
        log(f'  {ref}: {len(pads) - len(left)} of {len(pads)} balls dog-boned ({n_in} via in pad)'
            + (f'; none for {len(left)}: {left[:12]}' if left else ''))
    if moved:
        log(f'  beneath the arrays, left to the cap step: {len(moved)} part(s)')
    return tracks, new_vias, stats


def dogbone_file(src, out, refs, log=print):
    with contextlib.redirect_stdout(sys.stderr):
        pcb = parse_kicad_pcb(src)
    tracks, new_vias, stats = dogbone(pcb, refs, log=log)
    nmap = {i: n.name for i, n in pcb.nets.items()}
    with contextlib.redirect_stdout(sys.stderr):
        ok = add_tracks_and_vias_to_pcb(src, out, tracks, new_vias, net_id_to_name=nmap)
    import fanout_from_plan as fp_
    fp_.copy_pro(src, out)
    return ok, stats


def main():
    if len(sys.argv) < 4:
        print(__doc__.strip().splitlines()[0])
        sys.exit(2)
    _rules.install_defaults()
    ok, _st = dogbone_file(sys.argv[1], sys.argv[2], sys.argv[3].split(','))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
