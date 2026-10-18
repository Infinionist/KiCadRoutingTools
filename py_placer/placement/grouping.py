"""Functional groups for grouped placement: the `groups.json` an agent writes,
and what the board says about it.

`place_simple` keeps each FUNCTIONAL GROUP together through its group term
(`placement/simple.py`). The groups are a judgement -- power
input and its conditioning, the core and its clock, an interface and its
protection -- made by an agent reading the parts, their pin functions and their
datasheets. This module does not make that judgement. It loads the file
(`analyze` reports a part in no group or in two as an error, and place_simple
refuses a part in two), and measures what the judgement implies so the agent
can check it:

  * cohesion: per part, its SIGNAL links to its own group and to every other
    group. Ground and board-wide rails are left out (`rail_net_ids`): on a
    small board GND reaches most parts and would make every part look like it
    belongs everywhere;
  * decaps whose IC sits in another group (`decap_conflicts`): place_simple
    leaves such a cap out of its decap term and places it with its own group;
  * bridges: 2-pad parts with signal links into another group (series
    resistors, jumpers) -- they belong to one side and border the other;
  * the inter-group link matrix.

The file (schema 1):

    {"schema": 1,
     "groups": [
       {"name": "core", "role": "core", "refs": ["U3", "C3", "R1"],
        "anchor": "U3", "why": "MCU and its decoupling",
        "edge": null, "side": "F", "near": ["clock"], "apart": []}
     ]}

`refs` may name file-LOCKED parts (a connector placed and locked first): they
are fixed members, the rest of the group is placed around them, and nothing
moves them. `edge` is one of N/S/E/W (the board side a group belongs on),
`near` / `apart` name other groups.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Set, Tuple

SCHEMA = 1
EDGES = ('N', 'S', 'E', 'W')
GROUP_KEYS = ('name', 'role', 'refs', 'anchor', 'why', 'edge', 'side', 'near',
              'apart', 'note')
DOC_KEYS = ('schema', 'board', 'groups', 'note')

#: A net is a RAIL -- left out of every connectivity vote here -- when its
#: name says ground or it reaches more
#: than RAIL_SHARE of the board's parts (and more than RAIL_MIN_OWNERS).
RAIL_SHARE = 0.25
RAIL_MIN_OWNERS = 6
#: Parts with this many copper pads are hubs for the cohesion check.
HUB_PADS = 6


class GroupsError(ValueError):
    """A groups file that cannot be read as one (bad JSON, bad keys)."""


def rail_net_ids(owners: Dict[int, Set[str]], name_of, n_parts: int) -> Set[int]:
    """Net ids that are rails (see RAIL_SHARE). `owners` maps a net id to the
    refs with a copper pad on it, `name_of(net_id)` gives its name."""
    from net_queries import is_ground_net_name
    cap = max(RAIL_MIN_OWNERS, RAIL_SHARE * max(1, n_parts))
    out: Set[int] = set()
    for nid, refs in owners.items():
        name = name_of(nid) or ''
        if (name and is_ground_net_name(name)) or len(refs) > cap:
            out.add(nid)
    return out


def load_groups(path: str) -> Dict:
    """The groups file at `path`, its keys checked. Raises GroupsError."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError) as ex:
        raise GroupsError(f"{path}: not a readable JSON file ({ex})")
    return validate_doc(doc, path)


def validate_doc(doc, where: str = 'groups') -> Dict:
    if not isinstance(doc, dict):
        raise GroupsError(f"{where}: the top level must be an object")
    bad = sorted(set(doc) - set(DOC_KEYS))
    if bad:
        raise GroupsError(f"{where}: unknown key(s) {bad} (known: {list(DOC_KEYS)})")
    if doc.get('schema', SCHEMA) != SCHEMA:
        raise GroupsError(f"{where}: schema {doc.get('schema')!r}, this reader knows {SCHEMA}")
    groups = doc.get('groups')
    if not isinstance(groups, list) or not groups:
        raise GroupsError(f"{where}: `groups` must be a non-empty list")
    names = []
    for i, g in enumerate(groups):
        if not isinstance(g, dict):
            raise GroupsError(f"{where}: groups[{i}] is not an object")
        bad = sorted(set(g) - set(GROUP_KEYS))
        if bad:
            raise GroupsError(f"{where}: groups[{i}] unknown key(s) {bad} "
                              f"(known: {list(GROUP_KEYS)})")
        name = g.get('name')
        if not isinstance(name, str) or not name:
            raise GroupsError(f"{where}: groups[{i}] needs a `name`")
        if name in names:
            raise GroupsError(f"{where}: group name {name!r} is used twice")
        names.append(name)
        refs = g.get('refs')
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs):
            raise GroupsError(f"{where}: group {name!r} needs a non-empty `refs` list of strings")
        if g.get('edge') not in (None,) + EDGES:
            raise GroupsError(f"{where}: group {name!r} edge {g.get('edge')!r} is not one of {EDGES}")
        if g.get('side', 'F') not in ('F', 'B'):
            raise GroupsError(f"{where}: group {name!r} side {g.get('side')!r} is not F or B")
        if g.get('anchor') is not None and g['anchor'] not in refs:
            raise GroupsError(f"{where}: group {name!r} anchor {g['anchor']!r} is not one of its refs")
        for key in ('near', 'apart'):
            v = g.get(key) or []
            if not isinstance(v, list):
                raise GroupsError(f"{where}: group {name!r} `{key}` must be a list of group names")
    for i, g in enumerate(groups):
        for key in ('near', 'apart'):
            for other in g.get(key) or []:
                if other not in names:
                    raise GroupsError(f"{where}: group {g['name']!r} `{key}` names "
                                      f"{other!r}, which is not a group")
    return doc


# ---------------------------------------------------------------------------
# the board's side of the question
# ---------------------------------------------------------------------------

def _cu_pads(fp):
    return [p for p in (fp.pads or ())
            if getattr(p, 'pad_type', '') != 'np_thru_hole'
            and any(str(l).endswith('.Cu') for l in (p.layers or ()))]


def is_ground(name) -> bool:
    """A ground or return net, by name: GND or VSS anywhere in it -- the
    decap rule's ground (`BoardView.decaps`), and place_simple's. Wider than
    `net_queries.is_ground_net_name` on purpose, which leaves out VSS and
    names that only end in GND for the routing tools' sake: here `LV_GND`,
    `VSSA` and `VSS_PA` are grounds too (a decap's other pad, a pour, not a
    channel), and a single-pad `unconnected-(J1-GND-Pad6)` is never priced."""
    u = (name or '').upper()
    return 'GND' in u or 'VSS' in u


#: Roles (`roles.infer_role`) whose supply pins own no decap.
CONNECTOR_ROLES = frozenset({'connector', 'pin header', 'usb receptacle', 'edge receptacle'})


class BoardView:
    """What grouping needs from a parsed board: who carries copper, who is
    locked, which refs share which nets, and which nets are rails."""

    def __init__(self, pcb):
        self.pcb = pcb
        self.parts: Dict[str, object] = {r: fp for r, fp in (pcb.footprints or {}).items()
                                         if _cu_pads(fp)}
        self.locked: Set[str] = {r for r, fp in self.parts.items()
                                 if getattr(fp, 'locked', False)}
        self.owners: Dict[int, Set[str]] = {}
        self.nets_of: Dict[str, Set[int]] = {}
        for r, fp in self.parts.items():
            for p in _cu_pads(fp):
                if p.net_id > 0:
                    self.owners.setdefault(p.net_id, set()).add(r)
                    self.nets_of.setdefault(r, set()).add(p.net_id)
        nets = pcb.nets or {}
        self.name_of = lambda nid: getattr(nets.get(nid), 'name', '') or ''
        self.rails = rail_net_ids(self.owners, self.name_of, len(self.parts))
        self._supply = None
        self._decaps = None

    def supply_pins(self) -> Dict[int, List[Tuple[str, str]]]:
        """{rail net id: [(chip, pad number)]} -- the supply pins
        `floorplan.supply_pins` finds by shape and pin type, function or rail
        name, on every net but ground, less connectors': a cap on a
        connector's power pin is a bulk cap where power enters, placed with
        its group, not a decap pulled to that pin."""
        if self._supply is None:
            from placement.floorplan import supply_pins
            from placement.roles import infer_role
            self._supply = {}
            for ic, rec in supply_pins(self.pcb).items():
                if infer_role(self.pcb.footprints.get(ic), ic, None)['role'] in CONNECTOR_ROLES:
                    continue
                for pad, net in rec.get('pins') or ():
                    nid = getattr(pad, 'net_id', None) or (net if isinstance(net, int) else None)
                    if nid and not is_ground(self.name_of(nid)):
                        self._supply.setdefault(nid, []).append((ic, str(pad.pad_number)))
        return self._supply

    def decaps(self) -> Dict[str, int]:
        """{cap: its rail net id} -- every decap by the electrical rule, the
        ONE rule place_simple prices and check_groups reports by: a cap
        (`groups.is_decoupling_cap`) from ground (`is_ground`) to a rail
        another chip has a supply pin on (`supply_pins`). A cap from a rail to
        anything else (a converter's feed-forward cap, rail to FB) decouples
        nothing. Electrical only: it asks nothing of where the parts are, so
        it answers on a pile too."""
        if self._decaps is None:
            from placement.groups import is_decoupling_cap
            supply = self.supply_pins()
            self._decaps = {}
            for ref, fp in sorted(self.parts.items()):
                if not is_decoupling_cap(fp, ref):
                    continue
                nets = {p.net_id for p in fp.pads if p.net_id > 0}
                rail = [n for n in nets if not is_ground(self.name_of(n))]
                if (len(rail) == 1 and len(nets) == 2
                        and any(ic != ref for ic, _p in supply.get(rail[0], ()))):
                    self._decaps[ref] = rail[0]
        return self._decaps

    def pad_count(self, ref: str) -> int:
        return len(_cu_pads(self.parts[ref]))


def group_of(doc: Dict) -> Dict[str, str]:
    """{ref: group name} -- the first group naming a ref (duplicates are an
    error `analyze` reports)."""
    out: Dict[str, str] = {}
    for g in doc['groups']:
        for r in g['refs']:
            out.setdefault(r, g['name'])
    return out


def cap_owners(view: BoardView, cap: str):
    """`(net name, [owner refs])` when `cap` is a decap (`BoardView.decaps`):
    its rail and the chips with a supply pin on it -- else None."""
    nid = view.decaps().get(cap)
    if nid is None:
        return None
    return view.name_of(nid), sorted({ic for ic, _p in view.supply_pins()[nid]} - {cap})


def decap_conflicts(doc: Dict, view: BoardView) -> List[Dict]:
    """Caps the placer would claim at an IC of ANOTHER group: no decap owner
    of the cap's net is in the cap's own group. The groups file is the author
    saying the cap belongs elsewhere (a crystal's load cap, an RC filter), so
    place_simple leaves it out of its decap term."""
    first = group_of(doc)
    out = []
    for cap, g in sorted(first.items()):
        if cap not in view.parts or cap in view.locked:
            continue
        hit = cap_owners(view, cap)
        if hit is None:
            continue
        net, owners = hit
        if any(first.get(o) == g for o in owners):
            continue
        out.append({'cap': cap, 'cap_group': g, 'net': net, 'owners': owners,
                    'owner_groups': sorted({first.get(o, '?') for o in owners})})
    return out


def claim_rows(pcb, intent_doc: Optional[Dict] = None,
               crystals: bool = True) -> Tuple[List[Dict], List[str]]:
    """`(rows, notes)`: the proximity claims place_simple seats -- the
    intent's own `proximity[]` rows (`intent_doc` is the intent JSON) and,
    unless `crystals` is False, each passive crystal's claim at its IC's
    oscillator pins (`crystals.crystal_claims`) that the intent does not
    already make -- and the crystal finder's notes."""
    rows = list((intent_doc or {}).get('proximity') or [])
    if not crystals:
        return rows, []
    from placement.crystals import crystal_claims
    have = {(str(c.get('ref')), str(c.get('near'))) for c in rows}
    xrows, notes = crystal_claims(pcb)
    rows += [r for r in xrows if (r['ref'], r['near']) not in have]
    return rows, notes


def follower_roots(claims: Iterable[Dict]) -> Dict[str, str]:
    """{subject: the part its chain of `proximity[]` claims ends at}: a load
    cap's claim names its crystal and the crystal's names the IC, so both
    end at the IC. A subject with several claims follows its first; a cycle
    stops where it closes."""
    near: Dict[str, str] = {}
    for c in claims:
        near.setdefault(str(c['ref']), str(c['near']))
    roots = {}
    for sub, nxt in near.items():
        seen = {sub}
        while nxt in near and nxt not in seen:
            seen.add(nxt)
            nxt = near[nxt]
        roots[sub] = nxt
    return roots


def fold_followers(doc: Dict, claims: Iterable[Dict]) -> Tuple[Dict, Dict[str, Tuple[str, str]]]:
    """`(doc, moved)`: the groups file with every `proximity[]` subject moved
    into the group of the part its claims end at (`follower_roots`) -- and,
    when a subject is its group's anchor, the rest of that group with it --
    and {ref: (from group, to group)} for each part moved.

    A follower is placed beside its partner, so it must share its
    partner's group: a crystal in a clock group of its own would be held
    apart from the IC whose pins it belongs at. A group the move empties is
    dropped, and the `near` and `apart` entries naming it are pointed at the
    group that took its parts. The input is not modified."""
    roots = follower_roots(claims)
    where = group_of(doc)
    moved = {s: (where[s], where[r]) for s, r in roots.items()
             if s in where and r in where and where[s] != where[r]}
    # A group whose ANCHOR moved follows it whole: a clock group is its
    # crystal, and the jumpers or 0-ohm bridges on the oscillator pins belong
    # at the crystal. A group whose anchor is not a claim subject keeps its
    # other parts.
    for g in doc['groups']:
        a = g.get('anchor')
        if a in moved and moved[a][0] == g['name']:
            for r in g['refs']:
                if r not in moved and where.get(r) == g['name']:
                    moved[r] = (g['name'], moved[a][1])
    if not moved:
        return doc, {}
    out = json.loads(json.dumps(doc))
    for g in out['groups']:
        g['refs'] = [r for r in g['refs'] if r not in moved]
        g['refs'] += sorted(s for s, (_f, t) in moved.items() if t == g['name'])
        if g.get('anchor') is not None and g['anchor'] not in g['refs']:
            del g['anchor']
    gone = {g['name'] for g in out['groups'] if not g['refs']}
    took: Dict[str, str] = {}
    for _s, (f, t) in sorted(moved.items()):
        if f in gone:
            took.setdefault(f, t)
    out['groups'] = [g for g in out['groups'] if g['refs']]
    for g in out['groups']:
        for key in ('near', 'apart'):
            if g.get(key):
                names = [took.get(n, n) for n in g[key]]
                names = [n for i, n in enumerate(names)
                         if n != g['name'] and n not in gone and n not in names[:i]]
                if names:
                    g[key] = names
                else:
                    del g[key]
    return out, moved


def tree_weight(n: int) -> float:
    """The weight of each pair of a net's `n` nodes when the net is priced
    as its spanning tree: the tree has n - 1 edges over n (n - 1) / 2 pairs,
    so each pair carries 2 / n of an edge, and the net sums to n - 1."""
    return 2.0 / n if n > 1 else 0.0


def link_matrix(doc: Dict, view: BoardView) -> Dict[Tuple[str, str], float]:
    """{(group a, group b): signal links between them}, a < b. A signal net
    reaching m groups is a spanning tree over them, m - 1 links, spread
    evenly over its group pairs (`tree_weight`), however many parts of each
    group it touches: a bus on six parts in two groups is one link, as its
    wiring needs one."""
    gof = group_of(doc)
    out: Dict[Tuple[str, str], float] = {}
    for nid in sorted(view.owners):
        if nid in view.rails:
            continue
        gs = sorted({gof[r] for r in view.owners[nid]
                     if r in gof and r in view.parts})
        w = tree_weight(len(gs))
        for i, ga in enumerate(gs):
            for gb in gs[i + 1:]:
                out[(ga, gb)] = out.get((ga, gb), 0.0) + w
    return out


def analyze(doc: Dict, pcb, movable: Optional[Iterable[str]] = None,
            followers: Iterable[str] = (), extra_fixed: Iterable[str] = ()) -> Dict:
    """Everything the checker reports, as data. `movable` defaults to every
    copper-bearing part the file does not lock and `extra_fixed` does not
    name (the parts the intent seats at a declared pose). `followers` are
    the subjects of proximity claims, seated by their claim rather than by
    the decap rule."""
    view = BoardView(pcb)
    gof: Dict[str, List[str]] = {}
    for g in doc['groups']:
        for r in g['refs']:
            gof.setdefault(r, []).append(g['name'])
    movable = (set(movable) if movable is not None
               else set(view.parts) - view.locked - set(extra_fixed))
    # a part that is not movable is FIXED for every question below: locked in
    # the file, or held out by the caller (`movable`)
    fixed = set(view.parts) - movable
    unknown = sorted(r for r in gof if r not in (pcb.footprints or {}))
    padless = sorted(r for r in gof if r in (pcb.footprints or {}) and r not in view.parts)
    duplicates = {r: names for r, names in sorted(gof.items()) if len(names) > 1}
    missing = sorted(r for r in movable if r not in gof)
    fixed_members = sorted(r for r in gof if r in fixed)
    first = group_of(doc)

    # cohesion, per part: signal links to its own group vs the strongest
    # other MOVABLE group. A link to a group of fixed parts (the headers a core
    # talks to) is not a grouping mistake -- it is what the placer puts the
    # group next to -- so it never makes a part "pulled".
    fixed_groups = {g['name'] for g in doc['groups']
                    if all(r in fixed for r in g['refs'] if r in view.parts)}
    anchors = {g.get('anchor') for g in doc['groups'] if g.get('anchor')}
    pulled = []
    bridges = []
    for ref, own in sorted(first.items()):
        if ref not in view.parts or ref in fixed:
            continue
        # a hub -- the group's anchor, or any part of HUB_PADS+ pads -- talks
        # to every group by design; "pulled" is a question about satellites
        hub = ref in anchors or view.pad_count(ref) >= HUB_PADS
        # one link per (signal net, group it reaches): joining a net needs
        # one connection to it, however many of a group's parts it touches
        by_group: Dict[str, int] = {}
        for nid in view.nets_of.get(ref, ()):
            if nid in view.rails:
                continue
            for g in {first[o] for o in view.owners.get(nid, ())
                      if o != ref and o in first}:
                by_group[g] = by_group.get(g, 0) + 1
        own_n = by_group.get(own, 0)
        others = {g: n for g, n in by_group.items()
                  if g != own and g not in fixed_groups}
        if others and not hub:
            best = max(others, key=lambda g: (others[g], g))
            if others[best] > own_n:
                pulled.append({'ref': ref, 'group': own, 'own_links': own_n,
                               'pulled_by': best, 'links': others[best]})
        # a BRIDGE sits in series between groups: a 2-pad part whose two pads
        # are both on signal nets, and those nets reach another group
        sig = [n for n in view.nets_of.get(ref, ()) if n not in view.rails]
        if view.pad_count(ref) == 2 and len(sig) == 2:
            borders = sorted({first[o] for n in sig for o in view.owners.get(n, ())
                              if o != ref and o in first and first[o] != own})
            if borders:
                bridges.append({'ref': ref, 'group': own, 'borders': borders})

    # A cap that follows a proximity claim (a crystal's load cap) is seated
    # by its claim, not by the decap rule, so it is no conflict.
    decap_cross = [d for d in decap_conflicts(doc, view)
                   if d['cap'] not in set(followers)]

    matrix = link_matrix(doc, view)
    errors = []
    if unknown:
        errors.append(f"refs not on this board: {unknown}")
    if duplicates:
        errors.append("refs in more than one group: "
                      + ', '.join(f"{r} ({'/'.join(n)})" for r, n in duplicates.items()))
    if missing:
        errors.append(f"movable parts in no group: {missing}")
    for g in doc['groups']:
        if not any(r in view.parts for r in g['refs']):
            errors.append(f"group {g['name']!r} has no part on this board")
    warnings = []
    if padless:
        warnings.append(f"refs with no copper pads (ignored): {padless}")
    for p in pulled:
        warnings.append(f"{p['ref']} ({p['group']}) has {p['links']} signal link(s) to "
                        f"{p['pulled_by']} and {p['own_links']} to its own group")
    for d in decap_cross:
        warnings.append(f"{d['cap']} ({d['cap_group']}) is a decap of "
                        f"{'/'.join(d['owners'])} ({'/'.join(d['owner_groups'])}) to the "
                        f"decap rule, and no IC in its own group carries its "
                        f"net {d['net']}: place_simple exempts it from the decap rule "
                        f"so it is placed with {d['cap_group']} -- right for a load or "
                        f"RC cap, wrong for a real decap (move it to its IC's group)")
    return {
        'groups': [{'name': g['name'], 'role': g.get('role'),
                    'refs': g['refs'],
                    'movable': [r for r in g['refs'] if r in movable],
                    'fixed': [r for r in g['refs'] if r in fixed]}
                   for g in doc['groups']],
        'links': [{'a': a, 'b': b, 'signal_links': n}
                  for (a, b), n in sorted(matrix.items())],
        'rails': sorted(view.name_of(n) for n in view.rails),
        'pulled': pulled, 'bridges': bridges, 'decap_cross_group': decap_cross,
        'fixed_members': fixed_members, 'errors': errors, 'warnings': warnings,
    }
