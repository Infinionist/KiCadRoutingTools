#!/usr/bin/env python3
"""escape_plan.py -- the ESCAPE fanout (FANOUT_ESCAPE=1): the bus's ends planned and laid as a human escapes a
DOG-BONED ball field (Andy 1010), the ends model choosing among the escapes the field has.

    python3 escape_plan.py OUT.kicad_pcb K --board=BASE      (fanout_from_plan.py hands over to it, same arguments)

THE BASE has every ball of the two arrays dog-boned already (route_bus --escape: dogbone_arrays): a short stub on the
balls' layer to a via on one of its four diagonal gap points, every ball's via on a gap point -- so the BALL LINES, the
rows and columns through the balls, are free on every layer the balls are not on. A lane leaves its ball by its via:

  * half a pitch along its gap row onto a ball line beside the via, out along the line to the face, and a pitch (PAST)
    past the outer balls -- its EXIT, beyond the outer ring's dog-bone vias, where the whole route takes it on;
  * or a SIDESTEP: from the line beside the via along the ball row half a pitch out, to a line up to SIDE pitches
    over, then out along that one -- a deeper leg past its neighbours' vias, so a pair can take either hand;
  * or along a STREET: a pitch along its gap column, between two balls, onto the next GAP line -- the row of gap
    points between two ball rows, free of vias where the dog-bones of the rows either side point away from it (the
    human's U5: the field's two halves dog-boned out of the middle, four B.Cu lanes through it) -- and out along it;
  * on any routing layer the balls are not on (a via-in-pad -- the dog-bone that found no gap -- runs out along its
    ball's own line).

Every such escape clear of the board's copper (every pad on its layer, every via, every segment on its layer, by the
fanout's own track and clearance) is a MOVE (escape_moves.Move): kind 'dogbone' (or 'via_in_pad'), its legs the exact
path, `climb` set so the selector prices its axis-aligned runs as lanes (select_moves._lane_spans). The menus feed the
ends model unchanged (whole_ends.choose, a plan state of the same keys as fanout_from_plan.plan_state's): it chooses
each lane's tooth and berth among them -- crossings round both arrays, vias, pair hands, conflicts -- with no tooth laid
before it (pages_first.current_tooth: none on an escape state). A pair's two legs are one option only on two
adjacent lines of one face and layer (whole_ends.combos), and never where its own legs pass within the rule of each
other (banned as a pair). The chosen legs are LAID AS THEY ARE -- the dog-bone stubs and vias stand on the base, so only
the runs are new copper -- and the sidecar (OUT.plan.json) records the ends as laid, the ends model's cut round the
destination, and each berth's move for an incremental round's hold. The fanout audit then refuses a board the whole
route could not take, as fanout_from_plan's does.

An INCREMENTAL round (INCREMENTAL= the previous round's sidecar, FEEDBACK= its findings) holds every berth the feedback
does not name at the move it was laid by; every tooth is free (no tooth stands on the base to hold)."""
KRT_TOOL = {'scope': [], 'kind': 'actor'}   # #937: a research tool (awx), catalogued, shown at no door

import collections
import contextlib
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'py_router'))
sys.path.insert(0, HERE)
import awx_settings  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402
from kicad_writer import add_tracks_and_vias_to_pcb  # noqa: E402
import escape_moves as em  # noqa: E402
import route_layers  # noqa: E402
import rules as _rules  # noqa: E402

SIDE = 2          # a sidestep: up to this many pitches past the lines beside the via
PAST = 1.0        # the exit: this many pitches past the outer balls
DIRS = em.DIRS


def _pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 < 1e-18 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _cross(a, b, c, d):
    o = lambda p, q, r: (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return (o(a, b, c) * o(a, b, d) < 0) and (o(c, d, a) * o(c, d, b) < 0)


def seg_seg(a, b, c, d):
    """the least distance between segments ab and cd"""
    if _cross(a, b, c, d):
        return 0.0
    return min(_pt_seg(a, c, d), _pt_seg(b, c, d), _pt_seg(c, a, b), _pt_seg(d, a, b))


class Copper:
    """The board's copper, binned per millimetre, for one question: does a track from p to q on a layer keep the
    fanout's clearance off every other net's pad on that layer (and every plated hole and NPTH drill), every via, and
    every segment on that layer"""
    CELL = 1.0

    def __init__(self, pcb, track, clear):
        self.track, self.clear = track, clear
        self.bins = collections.defaultdict(list)
        cell = lambda x: int(math.floor(x / self.CELL))
        for fp in pcb.footprints.values():
            for p in fp.pads:
                if p.pad_type == 'np_thru_hole' and not (p.drill or 0) > 0:
                    continue
                r = max(p.size_x, p.size_y) / 2 + 0.01
                for cx in range(cell(p.global_x - r), cell(p.global_x + r) + 1):
                    for cy in range(cell(p.global_y - r), cell(p.global_y + r) + 1):
                        self.bins[(cx, cy)].append(('pad', p))
        for v in pcb.vias:
            r = v.size / 2
            for cx in range(cell(v.x - r), cell(v.x + r) + 1):
                for cy in range(cell(v.y - r), cell(v.y + r) + 1):
                    self.bins[(cx, cy)].append(('via', v))
        for s in pcb.segments:
            x0, x1 = sorted((s.start_x, s.end_x))
            y0, y1 = sorted((s.start_y, s.end_y))
            r = s.width / 2
            for cx in range(cell(x0 - r), cell(x1 + r) + 1):
                for cy in range(cell(y0 - r), cell(y1 + r) + 1):
                    self.bins[(cx, cy)].append(('seg', s))
        self.cell = cell

    @staticmethod
    def _pad_dist(x, y, p):
        if p.shape == 'circle':
            return max(0.0, math.hypot(p.global_x - x, p.global_y - y) - p.size_x / 2)
        from routing_utils import point_to_pad_rect_dist
        return point_to_pad_rect_dist(x, y, p)

    def seg_clear(self, a, b, layer, nid):
        """(clear, what stopped it)"""
        g = self.track / 2 + self.clear
        reach = 1.0
        seen = set()
        cands = []
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for cx in range(self.cell(x0 - reach), self.cell(x1 + reach) + 1):
            for cy in range(self.cell(y0 - reach), self.cell(y1 + reach) + 1):
                for kind, o in self.bins.get((cx, cy), ()):
                    if id(o) not in seen:
                        seen.add(id(o))
                        cands.append((kind, o))
        n = max(2, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.05))
        for kind, o in cands:
            if getattr(o, 'net_id', None) == nid and nid:
                continue
            if kind == 'via':
                if _pt_seg((o.x, o.y), a, b) < o.size / 2 + g:
                    return False, f'via {o.net_id} ({o.x:.2f},{o.y:.2f})'
            elif kind == 'seg':
                if o.layer == layer and seg_seg(a, b, (o.start_x, o.start_y), (o.end_x, o.end_y)) < o.width / 2 + g:
                    return False, f'segment {o.net_id} on {o.layer}'
            else:
                p = o
                if p.pad_type == 'np_thru_hole':
                    if _pt_seg((p.global_x, p.global_y), a, b) < (p.drill or 0) / 2 + g:
                        return False, f'hole {p.component_ref}.{p.pad_number}'
                    continue
                on = layer in p.layers or '*.Cu' in p.layers or (p.drill or 0) > 0
                if not on:
                    continue
                if _pt_seg((p.global_x, p.global_y), a, b) > max(p.size_x, p.size_y) + g:
                    continue
                for k_ in range(n + 1):
                    q = (a[0] + (b[0] - a[0]) * k_ / n, a[1] + (b[1] - a[1]) * k_ / n)
                    if self._pad_dist(q[0], q[1], p) < g:
                        return False, f'pad {p.component_ref}.{p.pad_number}'
        return True, ''

    def path_clear(self, pts, layer, nid):
        for a, b in zip(pts, pts[1:]):
            ok, _why = self.seg_clear(a, b, layer, nid)
            if not ok:
                return False
        return True


def dedupe(pts):
    return [q for j, q in enumerate(pts) if j == 0 or math.hypot(q[0] - pts[j - 1][0], q[1] - pts[j - 1][1]) > 1e-6]


def own_via(pcb, pad, nid, grid):
    """the ball's dog-bone via: the net's via nearest the ball within a pitch (its gap point, or the ball itself for a
    via in its pad), or None"""
    reach = max(grid.pitch_x, grid.pitch_y) * 0.75
    vs = [v for v in pcb.vias if v.net_id == nid and math.hypot(v.x - pad.global_x, v.y - pad.global_y) <= reach]
    v = min(vs, key=lambda v: math.hypot(v.x - pad.global_x, v.y - pad.global_y)) if vs else None
    return (v.x, v.y) if v is not None else None


def escape_menu(pad, site, grid, faces, layers, copper, side=SIDE):
    """every escape the ball at `pad` has from its via at `site`, by each of `faces`, on each of `layers` (the module's
    docstring): [Move]"""
    net = (getattr(pad, 'net_name', '') or '').split('/')[-1]
    nid = pad.net_id
    home = next((L for L in pad.layers if L.endswith('.Cu')), 'F.Cu')
    x0, y0, x1, y1 = grid.bbox
    hx, hy = grid.pitch_x / 2.0, grid.pitch_y / 2.0
    ball = (pad.global_x, pad.global_y)
    in_pad = math.hypot(site[0] - ball[0], site[1] - ball[1]) < 0.05
    out = []
    for d in faces:
        dx, dy = DIRS[d]
        k = 0 if dx else 1                      # the axis normal to the face
        a_ = 1 - k                              # the axis along it
        s = dx if dx else dy
        h = (hy if k == 0 else hx)              # half a pitch along the face
        hk = (hx if k == 0 else hy)             # half a pitch across it
        # (the exit a full pitch past the outer balls, PAST: the outer ring's own dog-bones stand on the gap points half
        # a pitch out, between every two ball lines -- a pair's tips there had a via between them, a split)
        e = (x0 - PAST * 2 * hx if dx < 0 else x1 + PAST * 2 * hx) if k == 0 else \
            (y0 - PAST * 2 * hy if dy < 0 else y1 + PAST * 2 * hy)
        P = lambda along, normal, a_=a_: (along, normal) if a_ == 0 else (normal, along)
        if s * (e - site[k]) < -1e-6:
            continue                            # (the via stands past this face's exit line)
        lo, hi = (y0, y1) if a_ == 1 else (x0, x1)
        if in_pad:
            lines = [(ball[a_], [site, P(ball[a_], e)])]
        else:
            lines = []
            for sg in (-1, 1):
                ln0 = site[a_] + sg * h
                lines.append((ln0, [site, P(ln0, site[k]), P(ln0, e)]))
                # (the gap line a pitch over: a street where no via stands on it -- path_clear finds which)
                ls = site[a_] + sg * 2 * h
                lines.append((ls, [site, P(ls, site[k]), P(ls, e)]))
                row = site[k] + s * hk
                if s * (e - row) < 1e-6:
                    continue                    # (no ball row between the via and the exit line to sidestep along)
                for m in range(1, side + 1):
                    ln = ln0 + sg * m * 2 * h
                    lines.append((ln, [site, P(ln0, site[k]), P(ln0, row), P(ln, row), P(ln, e)]))
        for ln, path in lines:
            if not (lo - 1e-6 <= ln <= hi + 1e-6):
                continue
            path = dedupe(path)
            for L in layers:
                if L == home:
                    continue
                if not copper.path_clear(path, L, nid):
                    continue
                legs = ([] if in_pad else [(ball, site, home)]) + [(p, q, L) for p, q in zip(path, path[1:])]
                mv = em.Move(net, 'via_in_pad' if in_pad else 'dogbone', d, L, P(ln, e), 1, legs, site=site,
                             climb=1)
                mv.esc_side = int(round(abs(ln - site[a_]) / (2 * h) - 0.5)) if not in_pad else 0
                mv.street = abs(abs(ln - site[a_]) - 2 * h) < 1e-6
                out.append(mv)
    return out


def _pair_legs_clash(ma, mb, track, clear):
    """two legs of one pair whose runs come within the rule of each other on their layer"""
    ra = [(p, q) for p, q, L in ma.legs if L == ma.layer]
    rb = [(p, q) for p, q, L in mb.legs if L == mb.layer]
    return ma.layer == mb.layer and any(seg_seg(p, q, u, v) < track + clear - 1e-6 for p, q in ra for u, v in rb)


def plan_state(pcb, names, sref, dref, banned=frozenset(), log=print):
    """the ends model's plan state (whole_ends.Ends' keys, fanout_from_plan.plan_state's shape) on a dog-boned base:
    the escape menus at both arrays, no tooth laid"""
    import select_moves as sm
    import pairs as _pairs
    r = _rules.active()
    track, clear = r.fan_track, r.fan_clear
    copper = Copper(pcb, track, clear)
    byname = {n.name.split('/')[-1]: (i, n) for i, n in pcb.nets.items()}
    rlayers = list(route_layers.layers())
    sfp, dfp = pcb.footprints[sref], pcb.footprints[dref]
    sgrid, dgrid = em.grid_of(sfp), em.grid_of(dfp)
    cs = ((sgrid.bbox[0] + sgrid.bbox[2]) / 2, (sgrid.bbox[1] + sgrid.bbox[3]) / 2)
    cd = ((dgrid.bbox[0] + dgrid.bbox[2]) / 2, (dgrid.bbox[1] + dgrid.bbox[3]) / 2)
    toward_d = max(DIRS, key=lambda d: DIRS[d][0] * (cd[0] - cs[0]) + DIRS[d][1] * (cd[1] - cs[1]))   # source's near face
    far_s = min(DIRS, key=lambda d: DIRS[d][0] * (cd[0] - cs[0]) + DIRS[d][1] * (cd[1] - cs[1]))
    far_d = max(DIRS, key=lambda d: DIRS[d][0] * (cd[0] - cs[0]) + DIRS[d][1] * (cd[1] - cs[1]))     # dest's far face

    def half(pad, grid, d):
        ax = 0 if DIRS[d][0] else 1
        b = grid.bbox
        return ((pad.global_x, pad.global_y)[ax] - (b[ax] + b[ax + 2]) / 2) * DIRS[d][ax] >= -1e-6
    smenu, dmenu, src_pad, dst_pad, launch, none = {}, {}, {}, {}, {}, []
    for nm in names:
        nid, net = byname[nm]
        sp = next((p for p in net.pads if p.component_ref == sref), None)
        dp = next((p for p in net.pads if p.component_ref == dref), None)
        src_pad[nm], dst_pad[nm] = sp, dp
        if sp is None or dp is None:
            none.append(nm)
            continue
        launch[nm] = (sp.global_x, sp.global_y)
        for pad, grid, menus, faces in (
                (sp, sgrid, smenu, [d for d in DIRS if d != far_s and (d == toward_d or half(sp, sgrid, d))]),
                (dp, dgrid, dmenu, [d for d in DIRS if d != far_d or half(dp, dgrid, d)])):
            site = own_via(pcb, pad, nid, grid)
            if site is None:
                menus[nm] = []
                continue
            import source_realize as sr
            menus[nm] = [m for m in escape_menu(pad, site, grid, faces, rlayers, copper)
                         if (nm, sr.move_sig(m)) not in banned]
    # a PAIR's two legs never within the rule of each other: their joint options so banned (whole_ends: unbanned)
    import source_realize as sr
    banned = set(banned)
    prs = _pairs.pair_names(list(names)) if int(awx_settings.get('PLAN_PAIRS', awx_settings.get('BRAID_PAIRS', '0'))
                                                or 0) else {}
    nclash = 0
    for _b, (pn, nn) in prs.items():
        for menus in (smenu, dmenu):
            for ma in menus.get(pn, ()):
                for mb in menus.get(nn, ()):
                    if ma.direction == mb.direction and ma.layer == mb.layer and _pair_legs_clash(ma, mb, track, clear):
                        banned.add(('pair', pn, nn, sr.move_sig(ma), sr.move_sig(mb)))
                        banned.add(('pair', nn, pn, sr.move_sig(mb), sr.move_sig(ma)))
                        nclash += 1
    chi = sm.pair_chirality({nm: (src_pad[nm].global_x, src_pad[nm].global_y) for nm in launch},
                            {nm: (dst_pad[nm].global_x, dst_pad[nm].global_y) for nm in launch},
                            sgrid.bbox, dgrid.bbox)
    log(f'  escape menus: {sum(len(v) for v in smenu.values())} tooth moves, {sum(len(v) for v in dmenu.values())} '
        f'berth moves for {len(launch)} nets ({sum(1 for v in list(smenu.values()) + list(dmenu.values()) for m in v
                                                 if getattr(m, "street", False))} along streets); '
        f'{nclash} pair leg combinations clash'
        + (f'; no ball at both arrays: {none}' if none else '')
        + (f'; no tooth escape: {[n for n in launch if not smenu.get(n)]}' if any(not smenu.get(n) for n in launch)
           else '')
        + (f'; no berth escape: {[n for n in launch if not dmenu.get(n)]}' if any(not dmenu.get(n) for n in launch)
           else ''))
    import braid as te
    cache = {}

    def obs(nid, layer, own_only=False):
        key = (nid, layer, own_only)
        if key not in cache:
            cache[key] = te.build_obstacles(pcb, nid, {nid}, layer)
        return cache[key]
    return {'banned': banned, 'byname': byname, 'dmenu': dmenu, 'smenu': smenu, 'sblock': {}, 'launch': launch,
            'feedback': json.load(open(awx_settings.req('FEEDBACK'))) if awx_settings.get('FEEDBACK') else None,
            'incr': None, 'tooth0': {nm: 'F.Cu' for nm in launch}, 'tooth_vias': {nm: 1 for nm in launch},
            'src_pad': src_pad, 'dst_pad': dst_pad, 'sref': sref, 'dref': dref, 'sgrid': sgrid,
            'bundle_layer': 'F.Cu', 'chi': chi, 'dgrid': dgrid, 'dboxes': dgrid.bbox, 'buses': [list(launch)],
            'obs': obs, 'pcb': pcb, 'escape': True, 'held': {},
            'pads_of': {ref: [(p.global_x, p.global_y) for p in fp.pads] for ref, fp in pcb.footprints.items()}}


def incremental_fixed(st, names):
    """(INCREMENTAL= the previous round's sidecar, FEEDBACK= its findings) {net: move signature}: every berth the
    feedback does not name, held at the move it was laid by"""
    import pairs as _pairs
    import source_realize as sr
    if not (awx_settings.get('INCREMENTAL') and awx_settings.get('FEEDBACK')):
        return {}
    prev = json.load(open(awx_settings.req('INCREMENTAL')))
    fb = json.load(open(awx_settings.req('FEEDBACK')))
    items = [e for pr in fb.get('pairs', ()) for e in pr] + list(fb.get('avoid', ()))
    over = set(fb.get('over') or {})
    free_b = {e['lane'] for e in items if e['end'] == 1} | over
    pr_i = _pairs.pair_names(list(names))
    legs_b = {l_ for ln in free_b for l_ in (pr_i.get(ln) or (ln,))}
    fixed = {}
    for nm in names:
        if nm in legs_b or nm not in st['dmenu']:
            continue
        sig = (prev.get('berth_sig') or {}).get(nm)
        if sig is None:
            continue
        m = next((m for m in st['dmenu'][nm] if json.dumps(sr.move_sig(m)) == json.dumps(sig)), None)
        if m is not None:
            fixed[nm] = sr.move_sig(m)
    return fixed


def lay(base, out, moves, byname, log=print):
    """the chosen moves' runs laid on `base` as they are (their stubs and vias stand on it): OUT, and the clearance
    faults between the runs laid (two nets' runs on one layer within the rule), which the selector's lane test should
    have left none of"""
    r = _rules.active()
    tracks = []
    with contextlib.redirect_stdout(sys.stderr):
        pcb = parse_kicad_pcb(base)
    for nm, m in moves:
        nid = byname[nm][0]
        for p, q, L in m.legs:
            if L == m.layer and math.hypot(q[0] - p[0], q[1] - p[1]) > 1e-6:
                tracks.append({'start': tuple(p), 'end': tuple(q), 'width': r.fan_track, 'layer': L, 'net_id': nid})
    faults = []
    for i, t in enumerate(tracks):
        for u in tracks[i + 1:]:
            if t['layer'] == u['layer'] and t['net_id'] != u['net_id'] and \
                    seg_seg(t['start'], t['end'], u['start'], u['end']) < r.fan_track + r.fan_clear - 1e-6:
                faults.append((t['net_id'], u['net_id'], t['layer']))
    pcb_nets = {i: n.name for i, n in pcb.nets.items()}
    with contextlib.redirect_stdout(sys.stderr):
        ok = add_tracks_and_vias_to_pcb(base, out, tracks, [], net_id_to_name=pcb_nets)
    import fanout_from_plan as fp_
    fp_.copy_pro(base, out)
    log(f'  escapes laid: {len(moves)} ends, {len(tracks)} run segment(s)' + ('' if ok else ' (writer FAILED)')
        + (f'; {len(faults)} clearance fault(s) between runs: {sorted(set(faults))[:8]}' if faults else ''))
    return ok, faults


def sidecar(out, st, names, teeth, berths, cut, read=None):
    """OUT.plan.json, the plan sidecar braid.setup reads (fanout_from_plan.braid_plan_of's keys): each net's tooth and
    berth as laid (the chosen moves' exits: their runs end there), the layers there, the escape directions (out of
    each array, its face's), each berth's move, the pair frame, the ends model's cut round the destination, and its
    reading of each lane (READ, choose.last: its nets over two, its load on the trunk, its crossings, its blocked
    fronts -- the lanes a round the whole route lays nothing on frees next, whole_feedback --name)"""
    import source_realize as sr
    plan = {'nets': list(names), 'ends': {}, 'tooth_layer': {}, 'dest_layer': {}, 'tooth_dir': {}, 'stub_dir': {},
            'berth_sig': {}, 'chi': int(st['chi']), 'pages_first': False,
            'ends_model': {k: ((read or {}).get('parts') or {}).get('lane_' + k, {}) for k in ('over', 'load', 'x', 'front')},
            'escape': True}
    for nm in names:
        t, b = teeth.get(nm), berths.get(nm)
        if t is None or b is None:
            continue
        plan['ends'][nm] = [list(t.exit_pt), list(b.exit_pt)]
        plan['tooth_layer'][nm], plan['dest_layer'][nm] = t.layer, b.layer
        plan['tooth_dir'][nm] = list(DIRS[t.direction])
        plan['stub_dir'][nm] = list(DIRS[b.direction])
        plan['berth_sig'][nm] = sr.move_sig(b)
    if cut is not None:
        plan['dest_cut'] = list(cut) if isinstance(cut, (list, tuple)) else cut
    with open(out[:-len('.kicad_pcb')] + '.plan.json', 'w', encoding='utf-8') as f:
        json.dump(plan, f, indent=1)
    return plan


def run(out_path, K, base, log=print):
    import whole_ends
    from coherent_nets import coherent_nets
    _rules.install_defaults()
    run_nets = [n for n in (awx_settings.get('FANOUT_NETS') or '').split(',') if n]
    names = run_nets or coherent_nets(K, base)
    with contextlib.redirect_stdout(sys.stderr):
        pcb = parse_kicad_pcb(base)
    dref = awx_settings.get('DEST') or 'U5'
    byname = {n.name.split('/')[-1]: (i, n) for i, n in pcb.nets.items()}
    refs = collections.Counter(p.component_ref for nm in names if nm in byname for p in byname[nm][1].pads
                               if p.component_ref != dref)
    sref = refs.most_common(1)[0][0]
    log(f'escape fanout: {len(names)} nets, {sref} -> {dref}, on {os.path.basename(base)}')
    st = plan_state(pcb, names, sref, dref, log=log)
    st['base'] = base                    # (the board the ends are laid on: whole_ends.solve_check reads its floors)
    fixed = incremental_fixed(st, names)
    if fixed:
        log(f'  incremental: {len(fixed)} berth(s) held as laid')
    berths, teeth = whole_ends.choose(st, log=log, src_free=True, fixed=fixed or None)
    read = getattr(whole_ends.choose, 'last', None) or {}
    cut = read.get('cut')
    missing = [nm for nm in st['launch'] if nm not in berths or nm not in teeth]
    if missing:
        log(f'  no escape chosen for {missing}')
    moves = [(nm, teeth[nm]) for nm in st['launch'] if nm in teeth] + \
            [(nm, berths[nm]) for nm in st['launch'] if nm in berths]
    ok, faults = lay(base, out_path, moves, byname, log=log)
    sidecar(out_path, st, [n for n in names if n in st['launch']], teeth, berths, cut, read)
    by_face = collections.Counter((m.direction, m.layer) for _n, m in moves)
    log(f'  ends by face and layer: {dict(sorted(by_face.items()))}; sidesteps '
        f'{sum(1 for _n, m in moves if getattr(m, "esc_side", 0))}, streets '
        f'{sum(1 for _n, m in moves if getattr(m, "street", False))}')
    if not ok:
        return 2
    # THE FANOUT AUDIT, as fanout_from_plan's: a board the whole route would refuse is set aside and named
    import fanout_from_plan as fp_
    sp, far, stops = fp_.fanout_audit(out_path, names, dref)
    if sp or far or stops:
        held = out_path[:-len('.kicad_pcb')] + '.refused.kicad_pcb'
        os.replace(out_path, held)
        with open(out_path[:-len('.kicad_pcb')] + '.refused.json', 'w', encoding='utf-8') as f_:
            json.dump({'splits': [[pr_, end_, list(ls_)] for pr_, end_, ls_ in sp], 'far': list(far),
                       'stops': stops}, f_, indent=1)
        log(f'REFUSED by the fanout audit: {fp_._audit_line(sp, far, stops)} -- the board set aside as '
            f'{os.path.basename(held)}')
        return 3
    log('fanout audit: clean')
    return 0


def main():
    if sys.argv[1:2] in (['-h'], ['--help']):
        print('usage: escape_plan.py OUT.kicad_pcb K --board=BASE.kicad_pcb\n')
        print(__doc__.strip())
        return
    if len(sys.argv) < 2 or sys.argv[1].startswith('-'):
        print(__doc__.strip().splitlines()[0])
        sys.exit(2)
    out_path = sys.argv[1]
    rest = [a for a in sys.argv[2:] if not a.startswith('-')]
    K = int(rest[0]) if rest else 21
    base = next((a.split('=', 1)[1] for a in sys.argv if a.startswith('--board=')), None)
    if base is None:
        sys.exit('escape_plan: --board=BASE is needed')
    sys.exit(run(out_path, K, base))


if __name__ == '__main__':
    main()
