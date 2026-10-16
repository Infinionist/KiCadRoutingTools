"""Protected Kelvin sense branches: the router must not tap sense copper.

A buck regulator's VOS/divider trace, a load switch's CT-cap return: same net as
the high-current distribution, but load current must enter at ONE star pad (the
`join`), never ride the sense copper. The router routes a net by connecting its
pieces to whatever copper the net already has, so on a real board it attached
0.5 mm distribution branches straight onto a 0.2 mm VOS trace -- connected,
DRC-clean, wrong.

`--sense-branches FILE` names each branch by pads:

    [{"name": "buck-vos", "sense": ["U12.5", "R110.1"], "join": "C108.1"}]

`isolate()` runs right after the board is parsed. For each branch it finds the
copper reachable from the sense pads WITHOUT passing through the join pad (the
sense component) and moves that copper -- segments, vias and the sense pads --
onto a private net id of its own. To every routing path the protected branch is
then another net's copper: an obstacle with clearance, never a terminal, never
ripped (it is also marked locked). The join pad stays on the real net, so the
net's loads are routed to the star pad. The board text is never rewritten for
those items: the writer emits only NEW copper, which belongs to the real net, and
`pcb_data._net_alias` maps the private id back for the board-vs-file audits.

A branch is left unprotected, with a printed reason, when its pads are missing or
off-net, when its sense copper does not reach the join pad yet (nothing to
protect -- route the branch first), when one of its sense pads is not joined
to the join pad by that copper yet (protecting it would take the pad off the
net for the run, unrouted and unseen by the run's own grade), when a pour of
the same net covers its copper off the join pad (KiCad's fill floods over
same-net copper, so the pour already taps it whatever the router does), or
when the input already taps it (another pad of the net is in the sense
component).
KiCad DRC cannot see topology, so check the written board in every case.

`restore()` undoes the isolation; `batch_route` / `batch_route_diff_pairs` are
wrapped with `restoring` so a PCBData the caller owns (the GUI keeps one across
runs) comes back on its real nets.
"""
from __future__ import annotations

import json
import math
from typing import Dict, List, Optional, Set, Tuple

#: Contact slack in mm.
_EPS = 0.01

_KEYS = {'name', 'sense', 'join'}


def load_sense_branches(path: str) -> List[dict]:
    """Read and validate a `--sense-branches` JSON file."""
    with open(path, 'r', encoding='utf-8') as f:
        doc = json.load(f)
    if not isinstance(doc, list):
        raise ValueError(f"--sense-branches {path}: expected a JSON array of branches")
    out, names = [], set()
    for i, b in enumerate(doc):
        where = f"--sense-branches {path} entry {i}"
        if not isinstance(b, dict) or set(b) - _KEYS:
            raise ValueError(f"{where}: expected an object with keys {sorted(_KEYS)}")
        sense, join = b.get('sense'), b.get('join')
        if not isinstance(sense, list) or not sense or not isinstance(join, str):
            raise ValueError(f"{where}: needs sense [\"REF.PAD\", ...] and join \"REF.PAD\"")
        name = str(b.get('name') or f'sense_{i}')
        if name in names:
            # The name keys the private net (`<net>~sense:<name>`) and the
            # report; two branches under one name would share both.
            raise ValueError(f"{where}: branch name {name!r} is used twice")
        names.add(name)
        out.append({'name': name,
                    'sense': [_pad_key(s, where) for s in sense],
                    'join': _pad_key(join, where)})
    return out


def _pad_key(s, where: str) -> Tuple[str, str]:
    """'U12.5' -> ('U12', '5')."""
    ref, dot, num = str(s).rpartition('.')
    if not dot or not ref or not num:
        raise ValueError(f"{where}: pad {s!r} is not REF.PAD")
    return ref, num


def _in_pad(pad, x: float, y: float, grow: float) -> bool:
    """True when (x, y) lies on the pad rectangle grown by `grow` mm.

    `size_x`/`size_y` are BOARD-resolved (a 90-degree pad's are already
    swapped), so only the residual tilt `rect_rotation` may turn the point --
    the shared routing_utils test. Rotating by the absolute `pad.rotation`
    turned a 90-degree join pad's rectangle back across itself."""
    from routing_utils import point_in_pad_rect
    return point_in_pad_rect(x, y, pad, grow)


def _dist_seg(px, py, ax, ay, bx, by) -> float:
    """Point to segment-axis distance."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _pad_on(pad, layer: str) -> bool:
    return layer in pad.layers or '*.Cu' in pad.layers


def _touch(a, b, join) -> bool:
    """Copper contact between two items of one net anywhere off the join pad.
    Items are ('pad', Pad) | ('seg', Segment) | ('via', Via)."""
    off = lambda x, y: not _in_pad(join, x, y, _EPS)  # noqa: E731
    order = {'seg': 0, 'via': 1, 'pad': 2}
    if order[a[0]] > order[b[0]]:
        a, b = b, a
    (ka, ia), (kb, ib) = a, b
    if ka == 'seg' and kb == 'seg':
        if ia.layer != ib.layer:
            return False
        reach = ia.width / 2 + ib.width / 2 + _EPS
        return (any(off(x, y) and _dist_seg(x, y, ib.start_x, ib.start_y, ib.end_x, ib.end_y) <= reach
                    for x, y in ((ia.start_x, ia.start_y), (ia.end_x, ia.end_y)))
                or any(off(x, y) and _dist_seg(x, y, ia.start_x, ia.start_y, ia.end_x, ia.end_y) <= reach
                       for x, y in ((ib.start_x, ib.start_y), (ib.end_x, ib.end_y))))
    if ka == 'via' and kb == 'via':
        return off(ia.x, ia.y) and math.hypot(ia.x - ib.x, ia.y - ib.y) <= (ia.size + ib.size) / 2
    if ka == 'seg' and kb == 'via':
        return off(ib.x, ib.y) and _dist_seg(ib.x, ib.y, ia.start_x, ia.start_y, ia.end_x,
                                              ia.end_y) <= ib.size / 2 + ia.width / 2 + _EPS
    if ka == 'seg' and kb == 'pad':
        if not _pad_on(ib, ia.layer):
            return False
        if any(off(x, y) and _in_pad(ib, x, y, ia.width / 2 + _EPS)
               for x, y in ((ia.start_x, ia.start_y), (ia.end_x, ia.end_y))):
            return True
        return off(ib.global_x, ib.global_y) and _dist_seg(
            ib.global_x, ib.global_y, ia.start_x, ia.start_y, ia.end_x, ia.end_y) <= ia.width / 2 + _EPS
    if ka == 'via' and kb == 'pad':
        return off(ia.x, ia.y) and _in_pad(ib, ia.x, ia.y, ia.size / 2 + _EPS)
    return False


def _touches_join(item, join) -> bool:
    """True when the item has copper on the join pad."""
    kind, it = item
    if kind == 'seg':
        return _pad_on(join, it.layer) and any(
            _in_pad(join, x, y, it.width / 2 + _EPS)
            for x, y in ((it.start_x, it.start_y), (it.end_x, it.end_y)))
    if kind == 'via':
        return _in_pad(join, it.x, it.y, it.size / 2 + _EPS)
    return False


def _point_in_poly(x: float, y: float, poly) -> bool:
    """Ray-casting point-in-polygon."""
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        (xi, yi), (xj, yj) = poly[i], poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _zone_tap(pcb_data, items, join) -> Optional[str]:
    """Why a pour of the branch's own net already taps its sense copper, or
    None. A zone floods over same-net copper, so sense copper inside (or
    touching) a same-net pour outline on its layer is joined to that pour --
    and through it to every load the pour feeds -- in the board KiCad fills,
    whatever the router keeps off it. The outline is the conservative proxy
    for the fill; contact on the join pad itself is the join, not a tap."""
    from kicad_parser import pour_zones
    from connectivity import via_copper_layers
    net = join.net_id
    zones = [z for z in pour_zones(getattr(pcb_data, 'zones', None))
             if z.net_id == net and len(z.polygon or ()) >= 3]
    if not zones:
        return None
    copper = list(getattr(pcb_data.board_info, 'copper_layers', None) or ())
    for z in zones:
        poly = z.polygon
        edges = list(zip(poly, poly[1:] + poly[:1]))
        for kind, it in items:
            if kind == 'seg':
                if it.layer != z.layer:
                    continue
                n = max(1, int(math.ceil(math.hypot(it.end_x - it.start_x,
                                                    it.end_y - it.start_y) / 0.1)))
                pts = [(it.start_x + (it.end_x - it.start_x) * k / n,
                        it.start_y + (it.end_y - it.start_y) * k / n) for k in range(n + 1)]
                r, what = it.width / 2, f"its {it.layer} track"
            elif kind == 'via':
                if z.layer not in via_copper_layers(it, copper):
                    continue
                pts, r, what = [(it.x, it.y)], it.size / 2, "its via"
            else:
                if not _pad_on(it, z.layer):
                    continue
                pts, r = [(it.global_x, it.global_y)], 0.0
                what = f"sense pad {it.component_ref}.{it.pad_number}"
            for x, y in pts:
                if _in_pad(join, x, y, r + _EPS):
                    continue
                if _point_in_poly(x, y, poly) or any(
                        _dist_seg(x, y, a[0], a[1], b[0], b[1]) <= r + _EPS for a, b in edges):
                    return (f"a {z.net_name or net} zone on {z.layer} pours over {what} "
                            f"at ({x:.2f}, {y:.2f}) -- its fill already joins the sense "
                            f"copper to the net")
    return None


def _joined_to(items, join) -> Set[int]:
    """Indices of `items` (a sense component) that copper joins to the join
    pad: the items touching it, and everything they reach off the pad."""
    seen: Set[int] = {i for i, it in enumerate(items) if _touches_join(it, join)}
    queue = list(seen)
    while queue:
        i = queue.pop()
        for j, other in enumerate(items):
            if j not in seen and _touch(items[i], other, join):
                seen.add(j)
                queue.append(j)
    return seen


def sense_component(pcb_data, branch: dict):
    """The copper reachable from the branch's sense pads without crossing its join
    pad. Returns (join_pad, sense_pads, items, reason): `items` the component as
    ('pad'|'seg'|'via', obj) tuples, `reason` a string when the branch cannot be
    evaluated (then items is empty)."""
    by_key = {}
    for pads in pcb_data.pads_by_net.values():
        for p in pads:
            by_key[(p.component_ref, p.pad_number)] = p
    join = by_key.get(branch['join'])
    sense = [by_key.get(k) for k in branch['sense']]
    if join is None or any(p is None for p in sense):
        missing = [f"{r}.{n}" for (r, n) in [branch['join']] + branch['sense'] if (r, n) not in by_key]
        return join, [], [], f"pad(s) {', '.join(missing)} not on the board"
    net = join.net_id
    if not net or any(p.net_id != net for p in sense):
        return join, sense, [], "sense pads are not on the join pad's net"
    items = ([('pad', p) for p in pcb_data.pads_by_net.get(net, []) if p is not join]
             + [('seg', s) for s in pcb_data.segments if s.net_id == net]
             + [('via', v) for v in pcb_data.vias if v.net_id == net])
    seen: Set[int] = {i for i, it in enumerate(items) if it[0] == 'pad' and it[1] in sense}
    queue = list(seen)
    while queue:
        i = queue.pop()
        for j, other in enumerate(items):
            if j not in seen and _touch(items[i], other, join):
                seen.add(j)
                queue.append(j)
    return join, sense, [items[i] for i in sorted(seen)], None


def _free_net_id(pcb_data) -> int:
    """An id no item of the board uses. `pads_by_net` can hold a pad-only net
    absent from `pcb_data.nets`, and copper can carry an id the net table
    lacks; `max(pcb_data.nets) + 1` could land on either, merging the sense
    pads into that net's bucket (and `restore` would then drop the bucket)."""
    used = set(pcb_data.nets) | set(getattr(pcb_data, 'pads_by_net', None) or ())
    for attr in ('segments', 'vias', 'zones'):
        used.update(getattr(o, 'net_id', 0) or 0
                    for o in getattr(pcb_data, attr, None) or ())
    return max(used, default=0) + 1


def isolate(pcb_data, branches: Optional[List[dict]]) -> Dict[str, int]:
    """Move every protectable branch's sense copper onto a private net id.
    Returns {branch name: private net id} for the branches protected.

    Idempotent per board: a nested in-memory sub-run (reconcile lap, plane
    finalize repair) handed an already-isolated PCBData keeps the outer
    run's isolation and returns {}. `restore` undoes it."""
    from kicad_parser import Net
    done: Dict[str, int] = {}
    if not branches or getattr(pcb_data, '_sense_isolation', None) is not None:
        return done
    record: List[tuple] = []
    pcb_data._sense_isolation = record
    print(f"Sense branches: {len(branches)} declared")
    for b in branches:
        join, sense, items, why = sense_component(pcb_data, b)
        if why is None:
            extra = sorted(f"{it.component_ref}.{it.pad_number}" for k, it in items
                           if k == 'pad' and it not in sense)
            joined = _joined_to(items, join)
            loose = sorted(f"{it.component_ref}.{it.pad_number}"
                           for i, (k, it) in enumerate(items)
                           if k == 'pad' and i not in joined)
            if extra:
                why = f"input copper already taps it ({', '.join(extra)})"
            elif not joined:
                why = "its sense copper does not reach the join pad yet -- nothing to protect"
            elif loose:
                # Protecting it would take these pads off the net for the
                # run: nothing would route them, and the run's own grade
                # (which no longer sees them) would call the net complete.
                why = (f"sense pad(s) {', '.join(loose)} not joined to "
                       f"{join.component_ref}.{join.pad_number} by copper yet -- "
                       f"route the branch first")
            else:
                why = _zone_tap(pcb_data, items, join)
        if why is not None:
            print(f"  {b['name']}: NOT protected -- {why}")
            continue
        net = join.net_id
        real = pcb_data.nets.get(net)
        pid = _free_net_id(pcb_data)
        name = f"{real.name if real is not None else join.net_name}~sense:{b['name']}"
        moved = Net(net_id=pid, name=name)
        pcb_data.nets[pid] = moved
        # The real net's pad ORDER, so `restore` puts the pads back where
        # they were: pad order reaches routing decisions, and the GUI routes
        # its next step on this same PCBData.
        order = (list(pcb_data.pads_by_net.get(net, ())),
                 list(real.pads) if real is not None else [])
        record.append((pid, net, [(kind, it, getattr(it, 'locked', False),
                                   getattr(it, 'net_name', None)) for kind, it in items],
                       order))
        for kind, it in items:
            it.net_id = pid
            if kind == 'pad':
                it.net_name = name
                pcb_data.pads_by_net[net].remove(it)
                pcb_data.pads_by_net.setdefault(pid, []).append(it)
                if real is not None and it in real.pads:
                    real.pads.remove(it)
                moved.pads.append(it)
            else:
                it.locked = True
        n_seg = sum(1 for k, _ in items if k == 'seg')
        n_via = sum(1 for k, _ in items if k == 'via')
        print(f"  {b['name']}: protected {n_seg} segment(s), {n_via} via(s), "
              f"{len(sense)} sense pad(s); new copper joins only at "
              f"{join.component_ref}.{join.pad_number}")
        done[b['name']] = pid
        pcb_data.__dict__.setdefault('_net_alias', {})[pid] = net
    return done


def restore(pcb_data) -> None:
    """Undo `isolate` on `pcb_data`: every moved item back on its real net
    with its own locked flag, the private nets and aliases gone. A no-op on a
    board that was not isolated. The GUI keeps one PCBData across runs, so
    the engine entries restore it on every exit (`restoring`)."""
    record = getattr(pcb_data, '_sense_isolation', None)
    if record is None:
        return
    for pid, net, items, (order_pbn, order_net) in reversed(record):
        real = pcb_data.nets.get(net)
        for kind, it, locked, net_name in items:
            it.net_id = net
            if kind == 'pad':
                it.net_name = net_name
                bucket = pcb_data.pads_by_net.get(pid, [])
                if it in bucket:
                    bucket.remove(it)
                pcb_data.pads_by_net.setdefault(net, []).append(it)
                if real is not None and it not in real.pads:
                    real.pads.append(it)
            else:
                it.locked = locked
        _reorder(pcb_data.pads_by_net.get(net), order_pbn)
        if real is not None:
            _reorder(real.pads, order_net)
        pcb_data.pads_by_net.pop(pid, None)
        pcb_data.nets.pop(pid, None)
        (getattr(pcb_data, '_net_alias', None) or {}).pop(pid, None)
    pcb_data._sense_isolation = None


def _reorder(lst, order) -> None:
    """Sort `lst` in place into `order` (by identity); items `order` does not
    hold keep their relative place after it."""
    if lst is None:
        return
    rank = {id(p): i for i, p in enumerate(order)}
    lst.sort(key=lambda p: rank.get(id(p), len(rank)))


def restoring(engine):
    """Decorate a batch engine entry so a caller-owned PCBData (the GUI's, a
    parent run's) leaves it as it came in: an isolation THIS call made is
    undone on every exit; one a parent made is left to the parent."""
    import functools

    @functools.wraps(engine)
    def run(*args, **kwargs):
        pcb = kwargs.get('pcb_data')
        outer = pcb is not None and getattr(pcb, '_sense_isolation', None) is None
        try:
            return engine(*args, **kwargs)
        finally:
            if outer:
                restore(pcb)
    return run
