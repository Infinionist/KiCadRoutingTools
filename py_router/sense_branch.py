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

import numpy as np

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


# ---------------------------------------------------------------------------
# The declaration: a KiCad group named `sense:<REF>.<PAD>`.
# ---------------------------------------------------------------------------

#: A group whose name starts with this declares a protected sense branch; the
#: rest of the name is the JOIN (star) pad, `REF.PAD`.
GROUP_PREFIX = 'sense:'


class SenseBranch:
    """One `sense:<REF>.<PAD>` group, resolved against a parsed board.

    `segments` / `vias` are the member copper, `sense_pads` the pads of the
    same net that member copper touches (the join pad excepted), `join` the
    join Pad. `reason` is None for a protected branch, else why it is not."""

    def __init__(self, name: str, group_uuid: str, member_uuids: List[str]):
        self.name = name
        self.group_uuid = group_uuid
        self.member_uuids = sorted(member_uuids)
        self.join_key: Optional[Tuple[str, str]] = None
        self.join = None
        self.net_id = 0
        self.segments: list = []
        self.vias: list = []
        self.sense_pads: list = []
        self.reason: Optional[str] = None

    @property
    def protected(self) -> bool:
        return self.reason is None

    def join_label(self) -> str:
        return f"{self.join_key[0]}.{self.join_key[1]}" if self.join_key else '?'

    def summary(self, pcb_data=None) -> dict:
        """The JSON_SUMMARY.sense_branches entry."""
        net = ''
        if pcb_data is not None and self.net_id in getattr(pcb_data, 'nets', {}):
            net = pcb_data.nets[self.net_id].name
        out = {'group': self.name, 'join': self.join_label(), 'net': net,
               'protected': self.protected,
               'segments': len(self.segments), 'vias': len(self.vias),
               'sense_pads': sorted(f"{p.component_ref}.{p.pad_number}"
                                    for p in self.sense_pads)}
        if self.reason is not None:
            out['reason'] = self.reason
        return out


def branch_signature(b: SenseBranch) -> tuple:
    """Order-free identity of a resolved branch, for comparing parse paths."""
    return (b.name, b.join_label(), tuple(b.member_uuids), b.reason or '',
            tuple(sorted(f"{p.component_ref}.{p.pad_number}" for p in b.sense_pads)),
            len(b.segments), len(b.vias))


_UUID_RE = r'"?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"?'


def sense_groups_from_text(content: str) -> List[Tuple[str, str, List[str]]]:
    """[(group name, group uuid, [member uuid, ...])] for every
    `(group "sense:..." ...)` block of a board file. The group's own
    `(uuid ...)` is its identity, not a member (read `(members ...)` only)."""
    import re
    from kicad_parser import find_matching_paren
    out = []
    if GROUP_PREFIX not in content:
        return out
    for m in re.finditer(r'\(group\s+"(' + re.escape(GROUP_PREFIX) + r'[^"]*)"', content):
        block = content[m.start():find_matching_paren(content, m.start())]
        u = re.search(r'\((?:uuid|id)\s+' + _UUID_RE, block)
        mem = re.search(r'\(members\b', block)
        members = (re.findall(_UUID_RE, block[mem.start():find_matching_paren(block, mem.start())])
                   if mem else [])
        out.append((m.group(1), u.group(1) if u else '', members))
    return out


def sense_groups_from_board(board) -> List[Tuple[str, str, List[str]]]:
    """`sense_groups_from_text` for a live pcbnew BOARD (board.Groups())."""
    out = []
    try:
        groups = list(board.Groups())
    except Exception as e:  # noqa: BLE001 -- say so rather than protect nothing silently
        print(f"  WARNING: could not read the board's groups from pcbnew "
              f"({type(e).__name__}: {e}); sense branches (#1279) are not protected")
        return out
    for g in groups:
        name = str(g.GetName())
        if not name.startswith(GROUP_PREFIX):
            continue
        members = []
        for it in g.GetItems():
            try:
                members.append(str(it.m_Uuid.AsString()))
            except Exception:  # noqa: BLE001
                members.append('?')
        try:
            guuid = str(g.m_Uuid.AsString())
        except Exception:  # noqa: BLE001
            guuid = ''
        out.append((name, guuid, members))
    return out


def _item_label(kind: str, it) -> str:
    if kind == 'seg':
        return (f"track ({it.start_x:g}, {it.start_y:g})-({it.end_x:g}, {it.end_y:g}) "
                f"on {it.layer}")
    if kind == 'via':
        return f"via at ({it.x:g}, {it.y:g})"
    return f"pad {it.component_ref}.{it.pad_number}"


def _resolve_one(pcb_data, b: SenseBranch, seg_by_uuid, via_by_uuid) -> Optional[str]:
    """Fill `b` from the board; return why it cannot be protected, or None."""
    ref, dot, num = b.name[len(GROUP_PREFIX):].rpartition('.')
    if not dot or not ref or not num:
        return (f"group name {b.name!r} does not name the join pad as "
                f"{GROUP_PREFIX}REF.PAD")
    b.join_key = (ref, num)
    fp = pcb_data.footprints.get(ref)
    join = next((p for p in (fp.pads if fp is not None else ()) if p.pad_number == num), None)
    if join is None:
        return f"join pad {ref}.{num} is not on the board"
    b.join = join
    if not join.net_id:
        return f"join pad {ref}.{num} has no net"
    b.net_id = join.net_id
    if not b.member_uuids:
        return "the group has no member tracks or vias"
    unknown = []
    for u in b.member_uuids:
        segs, via = seg_by_uuid.get(u), via_by_uuid.get(u)
        if segs:
            b.segments.extend(segs)
        elif via is not None:
            b.vias.append(via)
        else:
            unknown.append(u)
    if unknown:
        return (f"member(s) {', '.join(unknown[:4])}{' ...' if len(unknown) > 4 else ''} "
                f"are not tracks or vias on the board")
    items = [('seg', s) for s in b.segments] + [('via', v) for v in b.vias]
    off = sorted({it.net_id for _k, it in items if it.net_id != join.net_id})
    if off:
        names = [pcb_data.nets[n].name if n in pcb_data.nets else str(n) for n in off]
        return f"member copper is on {', '.join(names)}, not the join pad's net"
    # Sense pads: every pad of the net the member copper touches off the join.
    b.sense_pads = [p for p in pcb_data.pads_by_net.get(join.net_id, ())
                    if p is not join and any(_touch(it, ('pad', p), join) for it in items)]
    allitems = items + [('pad', p) for p in b.sense_pads]
    joined = _joined_to(allitems, join)
    if not joined:
        return f"member copper does not touch the join pad {ref}.{num}"
    loose = [_item_label(*allitems[i]) for i in range(len(allitems)) if i not in joined]
    if loose:
        return (f"{', '.join(loose[:3])}{' ...' if len(loose) > 3 else ''} not joined to "
                f"{ref}.{num} by member copper -- route the branch first")
    # Input already taps it: other copper of the net touching the branch off
    # the join pad would carry load current onto the sense copper.
    ids = {id(it) for _k, it in allitems}
    others = ([('seg', s) for s in pcb_data.segments
               if s.net_id == join.net_id and id(s) not in ids]
              + [('via', v) for v in pcb_data.vias
                 if v.net_id == join.net_id and id(v) not in ids])
    taps = [_item_label(*o) for o in others if any(_touch(o, it, join) for it in allitems)]
    if taps:
        return (f"input copper already taps it ({', '.join(taps[:3])}"
                f"{' ...' if len(taps) > 3 else ''})")
    return _zone_tap(pcb_data, allitems, join)


def resolve_sense_branches(pcb_data, raw) -> List[SenseBranch]:
    """Resolve `sense_groups_from_*` output against a parsed board. Called by
    BOTH parse paths, so every step of a chain and every leg that parses the
    file sees the same branches. A protected branch's member copper and sense
    pads are tagged `sense = <group name>`."""
    out: List[SenseBranch] = []
    if not raw:
        return out
    seg_by_uuid: Dict[str, list] = {}
    for s in pcb_data.segments:
        if s.uuid and not getattr(s, 'graphic', False):
            seg_by_uuid.setdefault(s.uuid, []).append(s)
    via_by_uuid = {v.uuid: v for v in pcb_data.vias if v.uuid}
    for name, guuid, members in raw:
        b = SenseBranch(name, guuid, members)
        b.reason = _resolve_one(pcb_data, b, seg_by_uuid, via_by_uuid)
        out.append(b)
    for b in out:
        if not b.protected:
            continue
        for it in b.segments + b.vias + b.sense_pads:
            it.sense = b.name
        # Immutable for the run, IN MEMORY only (the file is never given a
        # lock token): every rip, prune, smoothing and layer-switch pass
        # already leaves KiCad-locked copper alone (#1261).
        for it in b.segments + b.vias:
            it.locked = True
    return out


def protected_branches(pcb_data, net_id: Optional[int] = None) -> List[SenseBranch]:
    """The board's protected branches (of `net_id` when given)."""
    return [b for b in (getattr(pcb_data, 'sense_branches', None) or ())
            if b.protected and (net_id is None or b.net_id == net_id)]


def report(pcb_data) -> None:
    """Print one line per declared branch: protected, or why not."""
    branches = getattr(pcb_data, 'sense_branches', None) or []
    if not branches:
        return
    print(f"Sense branches (#1279): {len(branches)} declared by `{GROUP_PREFIX}` group(s)")
    for b in branches:
        if b.protected:
            pads = ', '.join(sorted(f"{p.component_ref}.{p.pad_number}" for p in b.sense_pads))
            print(f"  {b.name}: protected {len(b.segments)} track(s), {len(b.vias)} via(s), "
                  f"sense pad(s) {pads or 'none'}; new copper joins only at {b.join_label()}")
        else:
            print(f"  {b.name}: NOT protected -- {b.reason}")


def summary_entries(pcb_data) -> List[dict]:
    """JSON_SUMMARY.sense_branches: one entry per declared branch."""
    return [b.summary(pcb_data) for b in (getattr(pcb_data, 'sense_branches', None) or [])]


# ---------------------------------------------------------------------------
# The rule: member copper and sense pads are FOREIGN to their own net, except
# on the join pad.
# ---------------------------------------------------------------------------

def _join_shrink(join, half: float) -> float:
    """How far inside the join pad's edge copper of half-width `half` must be
    centred to lie on the pad -- never more than the pad's own half-extent,
    so even a pad narrower than the track keeps its centre landable."""
    return min(half, min(join.size_x, join.size_y) / 2.0)


def _outside_join(seg, join) -> list:
    """The parts of `seg` (a member track) off the join pad rectangle, as
    (x1, y1, x2, y2) tuples. Member copper ON the pad is the pad."""
    from routing_utils import _to_pad_frame
    if not _pad_on(join, seg.layer):
        return [(seg.start_x, seg.start_y, seg.end_x, seg.end_y)]
    ax, ay = _to_pad_frame(seg.start_x, seg.start_y, join)
    bx, by = _to_pad_frame(seg.end_x, seg.end_y, join)
    hx, hy = join.size_x / 2.0, join.size_y / 2.0
    t0, t1 = 0.0, 1.0
    dx, dy = bx - ax, by - ay
    for p, q in ((-dx, ax + hx), (dx, hx - ax), (-dy, ay + hy), (dy, hy - ay)):
        if abs(p) < 1e-12:
            if q < 0:
                t0, t1 = 1.0, 0.0          # parallel and outside: no overlap
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
    full = (seg.start_x, seg.start_y, seg.end_x, seg.end_y)
    if t0 >= t1:
        return [full]                       # never crosses the pad
    ex, ey = seg.end_x - seg.start_x, seg.end_y - seg.start_y
    out = []
    if t0 > 1e-9:
        out.append((seg.start_x, seg.start_y, seg.start_x + ex * t0, seg.start_y + ey * t0))
    if t1 < 1.0 - 1e-9:
        out.append((seg.start_x + ex * t1, seg.start_y + ey * t1, seg.end_x, seg.end_y))
    return out


class _Piece:
    """A member track's off-pad part, shaped like a Segment for the stamp
    helpers."""
    __slots__ = ('start_x', 'start_y', 'end_x', 'end_y', 'width', 'layer', 'net_id')

    def __init__(self, xy, seg):
        self.start_x, self.start_y, self.end_x, self.end_y = xy
        self.width, self.layer, self.net_id = seg.width, seg.layer, seg.net_id


class _Collector:
    """Stands in for a GridObstacleMap while the obstacle_map stamp helpers
    draw the sense copper, so the join pad can be cut out of what they drew
    before it reaches the real map."""

    def __init__(self):
        self.cells, self.vias = [], []

    def add_blocked_cells_batch(self, cells):
        self.cells.append(np.asarray(cells, dtype=np.int32).reshape(-1, 3))

    add_static_blocked_cells_batch = add_blocked_cells_batch

    def add_blocked_vias_batch(self, vias):
        self.vias.append(np.asarray(vias, dtype=np.int32).reshape(-1, 2))

    def add_blocked_cell(self, gx, gy, layer):
        self.add_blocked_cells_batch([[gx, gy, layer]])

    def add_blocked_via(self, gx, gy):
        self.add_blocked_vias_batch([[gx, gy]])


def _off_join_mask(join, xs, ys, shrink: float):
    """Boolean mask of board points NOT on the join pad shrunk by `shrink`."""
    dx, dy = xs - join.global_x, ys - join.global_y
    rr = getattr(join, 'rect_rotation', 0.0) or 0.0
    if rr:
        c, s = math.cos(math.radians(rr)), math.sin(math.radians(rr))
        dx, dy = dx * c + dy * s, -dx * s + dy * c
    return ~((np.abs(dx) <= join.size_x / 2.0 - shrink)
             & (np.abs(dy) <= join.size_y / 2.0 - shrink))


def sense_obstacle_rows(b: SenseBranch, config, coord, layer_map, clearance: float,
                        extra_clearance: float = 0.0):
    """(cells (N, 3), vias (M, 2)): branch `b`'s member copper and sense pads
    stamped as foreign copper at `clearance` -- the same keep-outs
    build_base_obstacle_map draws for another net's copper -- minus every
    cell where new copper of `b`'s net would lie on the join pad."""
    from obstacle_map import (_add_segment_obstacle, _add_via_obstacle,
                              _add_pad_obstacle, _via_track_expansion_per_layer)
    from routing_utils import segment_blocked_cells_array
    import routing_defaults as defaults
    col = _Collector()
    join = b.join
    names = sorted(layer_map, key=layer_map.get)
    for s in b.segments:
        seg_w = s.width if s.width > 0 else config.get_track_width(s.layer)
        seg_clr = config.layer_clearance(s.layer, clearance)
        trk = config.track_obstacle_clearance(b.net_id, seg_clr)
        exp = config.route_reserve_width(s.layer) / 2 + seg_w / 2 + trk + extra_clearance
        vblk = config.via_size / 2 + seg_w / 2 + seg_clr + extra_clearance
        li = layer_map.get(s.layer)
        for xy in _outside_join(s, join):
            if li is None:              # a layer this run cannot route: vias only
                col.add_blocked_vias_batch(segment_blocked_cells_array(
                    *xy, vblk, coord.grid_step))
                continue
            _add_segment_obstacle(col, _Piece(xy, s), coord, li, exp, vblk)
    for v in b.vias:
        if _in_pad(join, v.x, v.y, 0.0):
            continue                    # a via on the join pad is the join
        vs = v.size if v.size > 0 else config.via_size
        full = _via_track_expansion_per_layer(vs, config, coord, clearance, extra_clearance)
        # Per layer of THIS map (a plane tap map has one layer).
        vte = [full[config.layers.index(n)] if n in config.layers else max(full)
               for n in names]
        vv = max(1.0, (vs / 2 + config.via_size / 2
                       + config.stack_clearance(clearance)) * coord.inv_step)
        _add_via_obstacle(col, v, coord, len(names), vte, vv,
                          diagonal_margin=defaults.DIAGONAL_MARGIN)
    for p in b.sense_pads:
        _add_pad_obstacle(col, p, coord, layer_map, config, extra_clearance,
                          clearance_override=clearance)
    cells = (np.concatenate(col.cells) if col.cells else np.empty((0, 3), np.int32))
    vias = (np.concatenate(col.vias) if col.vias else np.empty((0, 2), np.int32))
    if len(cells):
        keep = np.ones(len(cells), dtype=bool)
        for lname, li in layer_map.items():
            if not _pad_on(join, lname):
                continue
            on = cells[:, 2] == li
            if on.any():
                half = config.get_net_track_width(b.net_id, lname) / 2.0
                keep[on] = _off_join_mask(join, cells[on, 0] * coord.grid_step,
                                          cells[on, 1] * coord.grid_step,
                                          _join_shrink(join, half))
        cells = np.unique(cells[keep], axis=0)
    if len(vias):
        keep = _off_join_mask(join, vias[:, 0] * coord.grid_step, vias[:, 1] * coord.grid_step,
                              _join_shrink(join, config.via_size / 2.0))
        vias = np.unique(vias[keep], axis=0)
    return np.ascontiguousarray(cells, dtype=np.int32), np.ascontiguousarray(vias, dtype=np.int32)


def stamp_base_obstacles(obstacles, pcb_data, config, nets_to_route, coord, layer_map,
                         obstacle_clearance, extra_clearance: float = 0.0) -> None:
    """build_base_obstacle_map's half of the rule. The base map leaves out the
    copper of every net it is built to route (those nets' caches carry it and
    each is lifted for its own route), so a protected branch of such a net is
    stamped here as foreign copper, with the join pad cut out. Every map the
    router derives from a base -- the working map, a Phase-3 clone, a rescue
    window's pristine map, each per-net via rung (whose sub-builds run this
    body again) -- then holds it. A net NOT routed already sees the branch as
    that net's ordinary copper."""
    for b in protected_branches(pcb_data):
        if b.net_id not in nets_to_route:
            continue
        cells, vias = sense_obstacle_rows(b, config, coord, layer_map,
                                          obstacle_clearance(b.net_id), extra_clearance)
        if len(cells):
            obstacles.add_blocked_cells_batch(cells)
        if len(vias):
            obstacles.add_blocked_vias_batch(vias)


def stamp_plane_obstacles(obstacles, pcb_data, config, net_id: int, layers,
                          tracks: bool) -> None:
    """The plane path's half (plane_obstacle_builder): the via-placement map
    (`tracks` False) and the single-layer tap-routing map (`tracks` True,
    `layers` its one layer) built for `net_id` leave that net's copper out,
    so its protected branches are stamped in here, join pad cut out."""
    from routing_config import GridCoord
    branches = protected_branches(pcb_data, net_id)
    if not branches:
        return
    coord = GridCoord(config.grid_step)
    layer_map = {name: i for i, name in enumerate(layers)}
    clearance = (config.obstacle_clearance(net_id)
                 if hasattr(config, 'obstacle_clearance') else config.clearance)
    for b in branches:
        cells, vias = sense_obstacle_rows(b, config, coord, layer_map, clearance)
        if tracks and len(cells):
            obstacles.add_blocked_cells_batch(cells)
        if not tracks and len(vias):
            obstacles.add_blocked_vias_batch(vias)


def terminal_sense_dist(pcb_data, net_id: int, x1: float, y1: float, x2: float, y2: float,
                        layer: str, half_width: float) -> float:
    """The terminal-overlap gate's half of the rule: the smallest distance
    from the centreline of new copper (x1, y1)-(x2, y2) of `net_id` on
    `layer` to the EDGE of that net's protected sense copper, measured only
    where the new copper is off the join pad (contact on the join pad is the
    join). 1e9 when the net has no protected branch."""
    branches = protected_branches(pcb_data, net_id)
    if not branches:
        return 1e9
    n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.02) + 1)
    t = np.linspace(0.0, 1.0, n + 1)
    sx, sy = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
    best = 1e9
    for b in branches:
        join = b.join
        if _pad_on(join, layer):
            keep = _off_join_mask(join, sx, sy, _join_shrink(join, half_width))
            px, py = sx[keep], sy[keep]
        else:
            px, py = sx, sy
        if not len(px):
            continue
        for s in b.segments:
            if s.layer != layer:
                continue
            for ax, ay, bx, by in _outside_join(s, join):
                ex, ey = bx - ax, by - ay
                l2 = ex * ex + ey * ey
                tt = (np.clip(((px - ax) * ex + (py - ay) * ey) / l2, 0.0, 1.0)
                      if l2 > 0 else np.zeros_like(px))
                d = np.hypot(px - (ax + tt * ex), py - (ay + tt * ey)) - s.width / 2.0
                best = min(best, float(d.min()))
        for v in b.vias:
            if _in_pad(join, v.x, v.y, 0.0):
                continue
            best = min(best, float((np.hypot(px - v.x, py - v.y) - v.size / 2.0).min()))
        from routing_utils import point_to_pad_rect_dist
        for p in b.sense_pads:
            if _pad_on(p, layer):
                best = min(best, min(point_to_pad_rect_dist(x, y, p)
                                     for x, y in zip(px.tolist(), py.tolist())))
    return best
