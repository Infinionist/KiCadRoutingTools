"""Repeat one placed hierarchical-sheet channel onto its sibling instances
(#1211) -- the engine half of `place_pose replicate`.

A board drawn from one hierarchical sheet instantiated N times (an audio
mixer's channel strips, one I/O buffer per bank) already carries the
correspondence between the copies: every footprint's `(path "/<sheet>/
<symbol>")` ends in the SAME symbol uuid in every instance, and KiCad's own
"Repeat Layout" maps by it. Nothing in the placement stack used it --
`place_seed` seeds each sheet on its own, `arrays` (#1051) rows identical
parts on ONE sheet, `reconstruct` repairs a damaged placement -- so an agent
that wanted four channels laid out alike wrote its own helper.

This module

1. resolves the sheets a caller named, by sheet NAME (what KiCad shows) or by
   path prefix (`/<uuid>`), refusing an unknown or ambiguous one and listing
   the candidates;
2. maps source -> target footprints by the path BELOW the instance prefix:
   the symbol uuid, plus the uuids of any nested sub-sheets, which every
   instance shares because the sub-sheet symbol lives in the shared file;
3. refuses unless the copy is a pure relabelling -- the same parts, the same
   footprints and pads, and a consistent net correspondence (THE NET RULE);
4. returns the target poses: the source's poses translated by one offset,
   same rotation, same side.

It moves nothing and grades nothing. `pose_ops.resolve_ops` turns its rows
into the writer's placements, and `apply_poses` grades them with the verdict
every other `place_pose` verb gets -- the AI declares the structure, the
tools do the geometry (#1051).

THE NET RULE. Instances of one sheet differ in their LOCAL nets (`/CH1/IN`
vs `/CH2/IN`) and share their GLOBAL ones (GND, the rails), so equal names
cannot be asked for. What is asked for, over every mapped pad pair, is

* a function: a source net reaches ONE target net;
* injective: a target net is reached from ONE source net;
* a net carried by pads of BOTH instances (a rail, a bus both channels sit
  on) maps to itself;
* no net maps to no net.

A pad pair that breaks one is named -- refs, pads and nets -- and the copy is
refused: the two instances are not interchangeable there, so a geometric
copy would seat a target pad where the source's DIFFERENT connection wanted
it.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from placement.arrays import natural_key
from placement.groups import _sheet_of
from placement.pose_ops import PoseRefusal

#: How many offending items a refusal sentence names before it summarises.
#: The summary carries every one (up to `LISTED`).
NAMED = 6
LISTED = 200

#: The shortest leading part of a sheet path accepted in place of the whole.
MIN_PATH_START = 4

#: Equal poses, for "did this part move". The writer's resolution is far
#: finer; this is the tolerance `resolve_ops` compares notes at (4 places).
POSE_EPS = 1e-4


def norm_name(text: str) -> str:
    """A sheet name or path with its slashes trimmed: KiCad 9 spells the
    sheet `/CH1/`, KiCad 7 `CH1`, and a person types either."""
    return (text or '').strip().strip('/').strip()


def _leaf(name: str) -> str:
    return name.rsplit('/', 1)[-1]


def sheet_instances(pcb, sheet_names: Dict[str, str]) -> Dict[str, Dict]:
    """`{prefix: {'names': [...], 'refs': [...]}}` for every NON-ROOT sheet
    prefix on the board, ancestors of nested sheets included.

    `refs` are every footprint at or below the prefix; `names` are the sheet
    names the prefix's OWN parts declare (a sheet holding only sub-sheets
    has none -- it is still reachable by path)."""
    out: Dict[str, Dict] = {}
    for ref, fp in pcb.footprints.items():
        prefix = _sheet_of(fp)
        if not prefix:
            continue
        parts = prefix.split('/')
        for i in range(1, len(parts) + 1):
            row = out.setdefault('/'.join(parts[:i]),
                                 {'names': set(), 'refs': []})
            row['refs'].append(ref)
        name = norm_name(sheet_names.get(ref, ''))
        if name:
            out[prefix]['names'].add(name)
    for row in out.values():
        row['names'] = sorted(row['names'])
        row['refs'] = sorted(row['refs'], key=natural_key)
    return out


def describe(prefix: str, row: Dict) -> Dict:
    """One sheet instance as a refusal or a summary names it."""
    return {'name': row['names'][0] if row['names'] else None,
            'path': '/' + prefix,
            'parts': len(row['refs']),
            'example': row['refs'][:3]}


def _phrase(d: Dict) -> str:
    return '%s (%s, %d parts: %s)' % (
        d['name'] or '<unnamed>', d['path'], d['parts'],
        ', '.join(d['example']) + (', ...' if d['parts'] > 3 else ''))


def resolve_sheet(pcb, sheet_names: Dict[str, str], token: str, role: str,
                  instances: Optional[Dict[str, Dict]] = None) -> str:
    """The sheet prefix `token` names, or a refusal (exit 2) listing the
    candidates.

    Tried in order, the first rung with exactly one answer wins and a rung
    with several is AMBIGUOUS (never resolved by a later rung): the sheet
    name, whole or by its last component (`CH1`, `/CH1/`, or `sub` for
    `/CH1/sub/`); then the same case-insensitively; then the path prefix,
    whole or by a unique leading part of it (`/28f12a69`) at least
    `MIN_PATH_START` characters long, so a short mistyped NAME cannot land
    on whichever uuid happens to start with it."""
    inst = instances if instances is not None else sheet_instances(
        pcb, sheet_names)
    listing = sorted((describe(p, r) for p, r in inst.items()),
                     key=lambda d: (d['name'] is None, d['name'] or '',
                                    d['path']))

    def _refuse(why, matches=None):
        shown = [describe(p, inst[p]) for p in matches] if matches else listing
        raise PoseRefusal(
            '%s %r %s. %s: %s' % (
                role, token, why,
                'It matches' if matches else
                ('Sheet instances on this board' if listing else
                 'This board has no hierarchical sheet instances'),
                '; '.join(_phrase(d) for d in shown[:12])
                + ('; +%d more' % (len(shown) - 12) if len(shown) > 12
                   else '')),
            code=2, sheets=shown[:LISTED])

    q = norm_name(token)
    if not q:
        _refuse('names the ROOT sheet, which is not an instance of a '
                'hierarchical sheet; name one of its sheets')
    for fold in (False, True):
        key = q.casefold() if fold else q

        def _eq(name):
            name = name.casefold() if fold else name
            return key == name or key == _leaf(name)
        hits = sorted(p for p, r in inst.items() if any(_eq(n) for n in r['names']))
        if len(hits) == 1:
            return hits[0]
        if hits:
            _refuse('is ambiguous: %d sheet instances carry that name. Name '
                    'one by its path' % len(hits), hits)
    path = q.lower()
    if path in inst:
        return path
    hits = (sorted(p for p in inst if p.lower().startswith(path))
            if len(path) >= MIN_PATH_START else [])
    if len(hits) == 1:
        return hits[0]
    if hits:
        _refuse('is ambiguous: it is the start of %d sheet paths' % len(hits),
                hits)
    _refuse('is neither a sheet name nor a sheet path on this board')
    raise AssertionError('unreachable')                    # pragma: no cover


def members(pcb, prefix: str) -> Dict[str, str]:
    """`{path below the instance: ref}` for every footprint at or below
    `prefix`. Two footprints claiming one path (a duplicated block) is
    refused: the correspondence would have to pick one."""
    depth = len(prefix.split('/'))
    out: Dict[str, str] = {}
    dup: Dict[str, List[str]] = {}
    for ref in sorted(pcb.footprints, key=natural_key):
        fp = pcb.footprints[ref]
        sheet = _sheet_of(fp)
        if sheet != prefix and not sheet.startswith(prefix + '/'):
            continue
        parts = [q for q in (fp.sheet_path or '').split('/') if q]
        key = '/'.join(parts[depth:])
        if key in out:
            dup.setdefault(key, [out[key]]).append(ref)
        else:
            out[key] = ref
    if dup:
        raise PoseRefusal(
            'sheet /%s has footprints that claim the same schematic symbol '
            '(%s), so a copy cannot tell which one a counterpart maps to'
            % (prefix, '; '.join('/'.join(v) for v in list(dup.values())[:NAMED])),
            duplicates=sorted(dup.values()))
    return out


def correspond(pcb, src: str, dst: str) -> Tuple[List[Tuple[str, str]], Dict]:
    """`([(source ref, target ref), ...], nets)`, or a refusal (exit 4).

    Refused when the instances do not hold the same parts, when a mapped
    pair differs in footprint or pad list, or when a mapped pad pair breaks
    THE NET RULE (module docstring). Pads are paired by INDEX, after the pad
    numbers have been checked equal index for index: a pad number is not
    unique within a footprint (#892), and one library footprint writes its
    pads in one order."""
    fps = pcb.footprints
    sm, tm = members(pcb, src), members(pcb, dst)
    only_s = sorted((sm[k] for k in sm.keys() - tm.keys()), key=natural_key)
    only_t = sorted((tm[k] for k in tm.keys() - sm.keys()), key=natural_key)
    if only_s or only_t:
        raise PoseRefusal(
            'the two sheet instances do not hold the same parts (matched by '
            'schematic symbol): only in /%s: %s; only in /%s: %s. A copy '
            'needs a counterpart for every part' % (
                src, ', '.join(only_s[:NAMED * 2]) or 'none',
                dst, ', '.join(only_t[:NAMED * 2]) or 'none'),
            only_in_source=only_s, only_in_target=only_t)
    pairs = sorted(((sm[k], tm[k]) for k in sm), key=lambda p: natural_key(p[0]))

    differ = []
    for s, t in pairs:
        fs, ft = fps[s], fps[t]
        if fs.footprint_name != ft.footprint_name:
            differ.append('%s is %s but %s is %s' % (
                s, fs.footprint_name, t, ft.footprint_name))
        elif ([p.pad_number for p in fs.pads or ()]
              != [p.pad_number for p in ft.pads or ()]):
            differ.append('%s and %s are both %s but list different pads '
                          '(%d vs %d)' % (s, t, fs.footprint_name,
                                          len(fs.pads or ()), len(ft.pads or ())))
    if differ:
        raise PoseRefusal(
            'mapped parts differ, so the source arrangement does not fit the '
            'target: %s%s' % ('; '.join(differ[:NAMED]),
                              '; +%d more' % (len(differ) - NAMED)
                              if len(differ) > NAMED else ''),
            footprints_differ=differ[:LISTED])

    def _name(nid):
        net = (pcb.nets or {}).get(nid)
        return (getattr(net, 'name', '') or '') if nid else '<no net>'

    rows = [(s, sp, t, tp) for s, t in pairs
            for sp, tp in zip(fps[s].pads or (), fps[t].pads or ())]
    s_nets = {sp.net_id for _s, sp, _t, _tp in rows}
    t_nets = {tp.net_id for _s, _sp, _t, tp in rows}
    fwd: Dict[int, Tuple[int, str]] = {}
    bwd: Dict[int, Tuple[int, str]] = {}
    breaks = []
    for s, sp, t, tp in rows:
        a, b = sp.net_id, tp.net_id
        at = '%s.%s %s -> %s.%s %s' % (s, sp.pad_number, _name(a),
                                       t, tp.pad_number, _name(b))
        why = None
        if (a == 0) != (b == 0):
            why = 'one pad has no net and the other has one'
        elif a in fwd and fwd[a][0] != b:
            why = '%s already maps to %s (%s)' % (_name(a), _name(fwd[a][0]),
                                                  fwd[a][1])
        elif b in bwd and bwd[b][0] != a:
            why = '%s is already reached from %s (%s)' % (
                _name(b), _name(bwd[b][0]), bwd[b][1])
        elif a != b and a in t_nets:
            why = ('%s is on pads of BOTH instances (a shared net), so it '
                   'must map to itself' % _name(a))
        elif a != b and b in s_nets:
            why = ('%s is on pads of BOTH instances (a shared net), so only '
                   'itself may map to it' % _name(b))
        if why:
            breaks.append({'pads': at, 'why': why})
            continue
        fwd.setdefault(a, (b, '%s.%s' % (s, sp.pad_number)))
        bwd.setdefault(b, (a, '%s.%s' % (t, tp.pad_number)))
    if breaks:
        raise PoseRefusal(
            'the two sheet instances are not wired alike: %d mapped pad '
            'pair(s) break a consistent net correspondence (each source net '
            'must reach one target net, each target net come from one source '
            'net, and a net on both instances map to itself) -- %s%s' % (
                len(breaks),
                '; '.join('%s: %s' % (b['pads'], b['why'])
                          for b in breaks[:NAMED]),
                '; +%d more' % (len(breaks) - NAMED)
                if len(breaks) > NAMED else ''),
            net_breaks=breaks[:LISTED])
    renamed = {_name(a): _name(b) for a, (b, _w) in sorted(fwd.items())
               if a != b}
    shared = sorted(_name(a) for a, (b, _w) in fwd.items() if a == b and a)
    return pairs, {'renamed': renamed, 'shared': shared}


def _anchor_rank(fp) -> Tuple:
    """Bigger first: most pads, then the widest pad-copper extent."""
    from kicad_parser import non_aperture_pads
    pads = non_aperture_pads(fp)
    if not pads:
        return (0, 0.0)
    xs = [p.global_x for p in pads]
    ys = [p.global_y for p in pads]
    return (len(pads), (max(xs) - min(xs)) * (max(ys) - min(ys)))


def plan(pcb, sheet_names: Dict[str, str], src_token: str,
         dst_tokens: Sequence[str], *, anchor: Optional[str] = None,
         offset: Optional[Sequence[float]] = None
         ) -> Tuple[List[Dict], Dict]:
    """`(rows, report)` for one `replicate` op.

    `rows`: one per target part -- `{'source', 'ref', 'x', 'y', 'rot',
    'side', 'flip'}`, the pose it should take. `report`: what was mapped and
    how the offset was chosen, for the summary.

    The OFFSET: `offset` (dx, dy) when given -- one target only, the CLI
    refuses it with several; otherwise each target keeps its ANCHOR where
    it is, so a channel is re-arranged in place rather than moved. The
    anchor is `anchor` (a ref in the source sheet or in any target sheet,
    standing for its counterparts), else the source part with the most pads
    (then the widest pad extent, then the first ref): on a channel strip,
    the op-amp. The anchor keeps its (at x y); its rotation and side become
    the source's like every other part's.
    """
    inst = sheet_instances(pcb, sheet_names)
    src = resolve_sheet(pcb, sheet_names, src_token, '--from', inst)
    dsts = [resolve_sheet(pcb, sheet_names, t, '--to', inst)
            for t in dst_tokens]
    for t, d in zip(dst_tokens, dsts):
        if d == src:
            raise PoseRefusal('--to %r is the --from sheet itself (/%s); a '
                              'sheet is not copied onto itself' % (t, src),
                              code=2)
        if d.startswith(src + '/') or src.startswith(d + '/'):
            raise PoseRefusal(
                '--to %r (/%s) and --from %r (/%s) are nested, one inside '
                'the other, so they share parts; replicate copies between '
                'SIBLING instances' % (t, d, src_token, src), code=2)
    if len(set(dsts)) != len(dsts):
        raise PoseRefusal('--to names one sheet instance twice (%s)' % ', '.join(
            '/' + d for d in dsts), code=2)
    for i, a in enumerate(dsts):
        for b in dsts[i + 1:]:
            if a.startswith(b + '/') or b.startswith(a + '/'):
                raise PoseRefusal('--to sheets /%s and /%s are nested, one '
                                  'inside the other' % (a, b), code=2)

    from placement.legality import footprint_side
    fps = pcb.footprints
    anchor_key = None
    if anchor is not None and offset is None:
        for prefix in [src] + dsts:
            hit = [k for k, r in members(pcb, prefix).items() if r == anchor]
            if hit:
                anchor_key = hit[0]
                break
        if anchor_key is None:
            raise PoseRefusal(
                '--anchor %s is in neither the --from sheet nor a --to sheet '
                '(%s)' % (anchor, ', '.join('/' + p for p in [src] + dsts)),
                code=2)

    rows: List[Dict] = []
    targets = []
    src_members = members(pcb, src)
    if offset is None and anchor_key is None:
        anchor_key = min(src_members, key=lambda k: (
            tuple(-v for v in _anchor_rank(fps[src_members[k]])),
            natural_key(src_members[k])))
    for token, dst in zip(dst_tokens, dsts):
        pairs, nets = correspond(pcb, src, dst)
        if offset is not None:
            dx, dy = float(offset[0]), float(offset[1])
            basis = {'kind': 'explicit'}
        else:
            s_ref = src_members[anchor_key]
            t_ref = members(pcb, dst)[anchor_key]
            dx = float(fps[t_ref].x) - float(fps[s_ref].x)
            dy = float(fps[t_ref].y) - float(fps[s_ref].y)
            basis = {'kind': 'anchor', 'source': s_ref, 'target': t_ref,
                     'chosen': 'given' if anchor else 'most pads'}
        flips = []
        moved = 0
        for s, t in pairs:
            fs, ft = fps[s], fps[t]
            side_s, side_t = footprint_side(fs), footprint_side(ft)
            row = {'source': s, 'ref': t,
                   'x': round(float(fs.x) + dx, 6),
                   'y': round(float(fs.y) + dy, 6),
                   'rot': float(fs.rotation or 0.0) % 360.0,
                   'side': side_s, 'flip': side_s != side_t}
            if basis.get('source') == s:
                # The anchor keeps its own coordinates EXACTLY -- `a + (b - a)`
                # is not always `b` in floating point.
                row['x'], row['y'] = float(ft.x), float(ft.y)
            rot_t = float(ft.rotation or 0.0) % 360.0
            row['moved'] = (abs(row['x'] - float(ft.x)) > POSE_EPS
                            or abs(row['y'] - float(ft.y)) > POSE_EPS
                            or _rot_differs(row['rot'], rot_t)
                            or row['flip'])
            moved += row['moved']
            if row['flip']:
                flips.append('%s %s->%s' % (t, side_t, side_s))
            rows.append(row)
        d = describe(dst, inst[dst])
        d['as_given'] = token
        targets.append({'sheet': d, 'pairs': len(pairs),
                        'moved': moved, 'offset': [round(dx, 6), round(dy, 6)],
                        'offset_basis': basis, 'side_flips': flips,
                        'nets_renamed': nets['renamed'],
                        'nets_shared': nets['shared']})
    s_desc = describe(src, inst[src])
    s_desc['as_given'] = src_token
    report = {'from': s_desc, 'to': targets,
              'source_refs': sorted(src_members.values(), key=natural_key)}
    return rows, report


def _rot_differs(a: float, b: float) -> bool:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d) > 1e-6
