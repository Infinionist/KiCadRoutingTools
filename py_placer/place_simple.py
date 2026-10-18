#!/usr/bin/env python3
"""Place a board by function, simply: groups, one objective, annealed.

    python3 -X utf8 py_placer/place_simple.py board.kicad_pcb placed.kicad_pcb \\
        --groups groups.json --intent intent.json [--seed 0] [--json report.json]

1. GROUPS: the groups file (`check_groups.py`), a crystal and its load caps
   folded into their IC's group. A group is a set of parts kept together:
   the objective's group term keeps it in one piece, whatever shape the
   free space gives it. A group's `near`,
   `apart` and `edge` hints are soft terms of the objective; its `side`, and
   every face the intent declares, is applied first (the writer's flip).
2. The parts the file locks stay put (connectors on their edges, mounting
   holes: whatever the enclosure or a mating part decides), and so does
   every part the intent seats at a declared pose (`fixed_poses[]`, from
   the design brief or mechanical.json), written there first; every other
   part starts at a random place on the board. The intent's keep-outs and
   zones bind every part through the anneal's overlap term.
3. ANNEAL: one cost (`placement/simple.py`) -- signal and rail trees,
   crossings, claims, decaps spread over their own group's supply pins,
   each group's gap tree, the halo in front of fine-pitch parts' pads,
   airwires over foreign pads, routing congestion -- lowered by simulated
   annealing (`placement/simple_anneal.py`), MOVES_PER_PART moves per
   movable part from `--seed`, so the same board, inputs and seed give the
   same placement. A run that leaves a part with no legal pose (a big part
   wedged between fixed ones on a full board) is run again from a fresh
   start, up to RESTARTS times, the first legal placement kept. On
   ch32v003_usb (22 movable parts) the whole run takes about 4.3 s.

Exit codes: 0 placed, 2 unreadable input or a face the board cannot take
(two declarations disagree, or a KiCad-locked part is declared on the other
face or at another pose), 4 placed with parts left illegal.
"""
#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement', 'combined'], 'kind': 'actor'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)

#: The anneal's default length per movable part, set by the time budget:
#: ch32v003_usb (22 movable parts) anneals in about 4 s. More moves still pay
#: (`--moves`); fewer leave the run cooling too fast.
MOVES_PER_PART = 500
#: Further anneals, each from a fresh start, when one leaves parts illegal.
RESTARTS = 2

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

from kicad_parser import parse_kicad_pcb  # noqa: E402


def _is_ground(name):
    """A ground or return net, by name: GND or VSS anywhere in it. Wider than
    `net_queries.is_ground_net_name` on purpose, which leaves out VSS and
    names that only end in GND for the routing tools' sake: here `LV_GND`,
    `VSSA` and `VSS_PA` are grounds too (a decap's other pad, a pour, not a
    channel), and a single-pad `unconnected-(J1-GND-Pad6)` is never priced."""
    u = (name or '').upper()
    return 'GND' in u or 'VSS' in u


def face_targets(intent, blocks, doc, pcb):
    """`(sides, conflicts)`: the face each part must be on -- every face the
    intent declares (`floorplan.declared_side_claims`) and each group's
    `side` -- and the parts two declarations put on different faces."""
    from placement import floorplan
    claims = {r: list(rows) for r, rows in
              floorplan.declared_side_claims(intent, blocks).items()
              if r in pcb.footprints}
    for g in doc['groups']:
        if g.get('side') in ('F', 'B'):
            for r in g['refs']:
                if r in pcb.footprints:
                    claims.setdefault(r, []).append((g['side'], f"group {g['name']}"))
    return floorplan.declared_sides(claims)


def hints(doc, state, movable, args):
    """The groups file's `near`, `apart` and `edge` hints as cost-model
    terms, each over every member the board has (fixed members included);
    a hint whose members are all fixed is a constant and is left out."""
    members = {g['name']: [r for r in g['refs'] if r in state.parts] for g in doc['groups']}
    mov = set(movable)
    out, seen = [], set()
    for g in doc['groups']:
        a = members[g['name']]
        if not a:
            continue
        if g.get('edge') and any(r in mov for r in a):
            out.append({'key': f"edge:{g['name']}:{g['edge']}", 'kind': 'edge', 'a': a,
                        'b': None, 'edge': g['edge'], 'weight': args.edge_weight})
        for kind, w in (('near', args.near_weight), ('apart', args.apart_weight)):
            for other in g.get(kind) or ():
                b = members.get(other) or []
                pair = (kind,) + tuple(sorted((g['name'], other)))
                if not b or pair in seen or not any(r in mov for r in a + b):
                    continue
                seen.add(pair)
                out.append({'key': f"{kind}:{pair[1]}:{pair[2]}", 'kind': kind, 'a': a,
                            'b': b, 'edge': None, 'weight': w})
    return out


def build(pcb, board, doc, base, args, intent=None, blocks=None, fixed=()):
    """`(state, model, movable, notes)` for the board, its groups and its
    base intent (the JSON for its claims; the loaded `intent`, with its
    resolved `blocks`, for its keep-outs and zones)."""
    import pose_score
    from list_nets import board_floor_knobs
    from placement import floorplan as fp
    from placement.grouping import (BoardView, claim_rows, decap_conflicts,
                                    fold_followers, group_of)
    from placement.simple import Model
    notes = []
    clearance, edge, _ = board_floor_knobs(board)
    # every declared zone binds its members (containment) and, when exclusive,
    # keeps strangers out; the board's bounds open a zone side on the outline
    # to an edge-claim member (#1174)
    zones = (fp.zone_entries(intent, blocks or {}, bounds=pcb.board_info.board_bounds)
             if intent is not None else ())
    state = pose_score.make_state(pcb, board, clearance=clearance,
                                  board_edge_clearance=edge,
                                  extra_locked_refs=set(fixed) or None,
                                  keepouts=intent.keepouts if intent is not None else (),
                                  intent_zones=list(zones),
                                  exclusive_zones=[z for z in zones if z['exclusive']])
    movable = sorted(r for r, p in state.parts.items() if not p.locked)
    # claims: the intent's own, and each crystal at its IC's oscillator pins
    rows, xnotes = claim_rows(pcb, base)
    notes += xnotes
    doc, folded = fold_followers(doc, rows)
    if folded:
        notes.append("in their IC's group: " + ', '.join(
            f"{r} ({a} -> {b})" for r, (a, b) in sorted(folded.items())))
    groups = []
    for g in doc['groups']:
        members = [r for r in g['refs'] if r in state.parts]
        if len(members) >= 2 and any(r in movable for r in members):
            groups.append((g['name'], members))
    claims = []
    for c in rows:
        s, p = str(c['ref']), str(c['near'])
        if s not in pcb.footprints or p not in pcb.footprints:
            continue
        pads = c.get('pads') or {}
        sp = [q for q in pcb.footprints[s].pads
              if q.net_id and (not pads.get(s) or str(q.pad_number) in map(str, pads[s]))]
        legs = []
        for q in sp:
            pp = [str(o.pad_number) for o in pcb.footprints[p].pads if o.net_id == q.net_id
                  and (not pads.get(p) or str(o.pad_number) in map(str, pads[p]))]
            if pp:
                legs.append((str(q.pad_number), pp))
        if legs:
            claims.append((s, p, legs))
    # decaps: each cap from a supply rail to ground, matched to that rail's
    # supply pins (`simple.Model._sites`) -- not a claimed part, and not a cap
    # whose IC is in another group (an RC cap beside its button, not a decap)
    view = BoardView(pcb)
    exempt = {str(c['ref']) for c in rows} | {
        c['cap'] for c in decap_conflicts(doc, view)}
    supply = {}
    for ic, rec in fp.supply_pins(pcb).items():
        for pad, net in rec.get('pins') or ():
            nid = getattr(pad, 'net_id', None) or (net if isinstance(net, int) else None)
            if nid and not _is_ground(pcb.nets[nid].name):
                supply.setdefault(nid, []).append((ic, str(pad.pad_number)))
    # a decap serves its own group's IC when the group has one on its rail:
    # an LDO's output cap belongs at the LDO, not at the MCU's VDD pin. Its
    # other pad is on ground: a cap from a rail to anything else (a
    # converter's feed-forward cap, rail to FB) is not decoupling anything
    gof = group_of(doc)
    decaps, decap_pins = [], {}
    for nid, caps in fp._decap_caps_by_net(pcb).items():
        if nid in supply:
            for cap in caps:
                r = cap.reference
                other = {p.net_id for p in cap.pads if p.net_id > 0} - {nid}
                if not any(_is_ground(pcb.nets[n].name) for n in other):
                    continue
                if r in state.parts and r not in exempt:
                    decaps.append((r, nid))
                    own = [q for q in supply[nid] if gof.get(q[0]) == gof.get(r)]
                    if own:
                        decap_pins[r] = own
    for r, nid in sorted(set(decaps)):
        pins = decap_pins.get(r) or supply[nid]
        notes.append(f"decap {r} on {pcb.nets[nid].name} serves "
                     + ', '.join(sorted({ic for ic, _n in pins}))
                     + ("" if r in decap_pins else " (no IC of its group is on its rail)"))
    # congestion counts every net that is routed as tracks: all but ground,
    # which is poured as a plane
    cnets = sorted(n for n in state.net_refs if n and not _is_ground(pcb.nets[n].name))
    model = Model(state, pcb, rails=view.rails, claims=claims,
                  decaps=sorted(set(decaps)), supply=supply, decap_pins=decap_pins,
                  groups=groups, congestion_weight=args.congestion_weight,
                  track_pitch=args.track_pitch, congestion_nets=cnets,
                  rail_weight=args.rail_weight,
                  ground_nets={n for n in view.rails if _is_ground(pcb.nets[n].name)},
                  ground_weight=args.ground_weight, pad_weight=args.pad_weight,
                  crossing_weight=args.crossing_weight, claim_weight=args.claim_weight,
                  decap_weight=args.decap_weight, group_weight=args.group_weight,
                  hints=hints(doc, state, movable, args),
                  halo_base=args.halo_base, halo_coef=args.halo_coef,
                  halo_weight=args.halo_weight)
    return state, model, movable, notes


def _line(t):
    groups = ', '.join(f"{n} {v:.1f}" for n, v in t['per_group'].items() if v > 0.05)
    hints = ', '.join(f"{n} {v:.1f}" for n, v in t['per_hint'].items() if v > 0.05)
    return (f"cost {t['cost']:.1f} (wire {t['wire']:.1f} mm, {t['crossings']} crossings, "
            f"claims {t['claims']:.1f}, decaps {t['decaps']:.1f}, halo {t['halo']:.2f}, "
            f"pad hits {t['pad_hits']:.0f}, congestion {t['congestion']:.2f}, "
            f"groups {t['groups']:.1f}" + (f": {groups}" if groups else "")
            + f", hints {t['hints']:.1f}" + (f": {hints}" if hints else "") + ")")


def _seed_list(spec):
    """'0-3' or '0,1,2,3' (or a mix: '0-2,7') -> [0, 1, 2, 3]."""
    out = []
    for part in str(spec).split(','):
        part = part.strip()
        if '-' in part:
            lo, hi = part.split('-', 1)
            out += list(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


def _many_seeds(ap, a):
    """`--seeds`: each seed a `--seed` run of this script in its own process,
    `--jobs` at a time, every other option passed through as parsed. Exit 2
    if any seed failed outright, else 4 if any left parts illegal, else 0."""
    import subprocess
    try:
        seeds = _seed_list(a.seeds)
    except ValueError:
        print(f"place_simple: --seeds {a.seeds!r} is not a list like 0-3 or 0,1,2,3",
              file=sys.stderr)
        return 2
    if not seeds or len(set(seeds)) != len(seeds):
        print(f"place_simple: --seeds {a.seeds!r} names no seed or one twice", file=sys.stderr)
        return 2
    many = len(seeds) > 1
    if many and ('{seed}' not in a.out or (a.json and '{seed}' not in a.json)):
        print("place_simple: with several --seeds, OUT (and --json) must carry {seed}",
              file=sys.stderr)
        return 2
    skip = {'help', 'board', 'out', 'seed', 'seeds', 'jobs', 'json'}
    passed = []
    for act in ap._actions:
        if act.dest in skip or not act.option_strings:
            continue
        val = getattr(a, act.dest)
        if val is not None:
            passed += [act.option_strings[0], str(val)]
    jobs = max(1, a.jobs or min(len(seeds), 4))
    runs = []
    for s in seeds:
        out = a.out.format(seed=s)
        js = a.json.format(seed=s) if a.json else None
        for p in (out, js):
            if p and os.path.dirname(p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
        cmd = ([sys.executable, '-X', 'utf8', os.path.abspath(__file__), a.board, out]
               + passed + ['--seed', str(s)] + (['--json', js] if js else []))
        runs.append((s, out, cmd))
    print(f"place_simple: seeds {', '.join(map(str, seeds))} ({jobs} at a time)")
    live, done = [], {}
    pending = list(runs)
    while pending or live:
        while pending and len(live) < jobs:
            s, out, cmd = pending.pop(0)
            log = open(os.path.splitext(out)[0] + '.log', 'w', encoding='utf-8')
            live.append((s, out, log, subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)))
        fin = [x for x in live if x[3].poll() is not None]
        if not fin:
            time.sleep(0.2)
            continue
        s, out, log, proc = fin[0]
        live.remove(fin[0])
        rc = proc.returncode
        log.close()
        done[s] = rc
        summary = None
        with open(os.path.splitext(out)[0] + '.log', encoding='utf-8') as f:
            for line in f:
                if line.startswith('JSON_SUMMARY: '):
                    summary = json.loads(line[len('JSON_SUMMARY: '):])
        if summary:
            fin = summary['final']
            ill = summary.get('illegal') or []
            print(f"  seed {s}: cost {fin['cost']:.1f}, {summary['moves']} moves in "
                  f"{summary['seconds']['anneal']} s"
                  + (f", no legal pose for {', '.join(ill)}" if ill else '') + f" -> {out}")
        else:
            print(f"  seed {s}: exit {rc}, no summary (see {os.path.splitext(out)[0]}.log)")
    rcs = set(done.values())
    return 2 if rcs - {0, 4} else (4 if 4 in rcs else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('board', help='the board; the parts it locks stay put')
    ap.add_argument('out', help='the placed board to write')
    ap.add_argument('--groups', required=True, help='the groups file (check_groups.py)')
    ap.add_argument('--intent', required=True,
                    help='the emitted intent (check_floorplan --emit-intent): its '
                         'proximity claims, keep-outs, zones and declared faces')
    ap.add_argument('--crossing-weight', type=float, default=2.0,
                    help='mm of wire one airwire crossing costs (default 2)')
    ap.add_argument('--claim-weight', type=float, default=1.0,
                    help='per mm of a claimed pad to its partner pad (default 1)')
    ap.add_argument('--decap-weight', type=float, default=2.0,
                    help='per mm of a decap to the supply-pin site it is matched to '
                         '(default 2)')
    ap.add_argument('--group-weight', type=float, default=5.0,
                    help='per mm of gap in a group\'s tree over its members\' '
                         'courtyards (default 5)')
    ap.add_argument('--near-weight', type=float, default=2.0,
                    help='per mm of gap between two groups the groups file puts '
                         '`near` each other (default 2)')
    ap.add_argument('--apart-weight', type=float, default=5.0,
                    help='per mm two groups kept `apart` come within 3 mm of each '
                         'other (default 5)')
    ap.add_argument('--edge-weight', type=float, default=2.0,
                    help="per mm a group with an `edge` is from that board edge "
                         "(default 2)")
    ap.add_argument('--halo-base', type=float, default=0.0,
                    help='mm of halo in front of every side a fine-pitch part\'s pads '
                         'face (default 0)')
    ap.add_argument('--halo-coef', type=float, default=0.25,
                    help='mm of halo per net leaving by a side (default 0.25)')
    ap.add_argument('--halo-weight', type=float, default=10.0,
                    help='cost per mm^2 of a halo shortfall (default 10)')
    ap.add_argument('--rail-weight', type=float, default=0.3,
                    help='per mm of a supply rail\'s tree: less than a signal\'s 1, '
                         'being wide or poured, but not nothing (default 0.3)')
    ap.add_argument('--ground-weight', type=float, default=0.02,
                    help='per mm of ground\'s tree: a plane, each pad only drops a '
                         'via, so almost nothing (default 0.02)')
    ap.add_argument('--pad-weight', type=float, default=2.0,
                    help='per crossing of a tree edge over a foreign pad, a through-hole '
                         'pad counting twice (default 2)')
    ap.add_argument('--congestion-weight', type=float, default=5.0,
                    help='per mm^2 of routing overflow squared, summed over a 0.5 mm '
                         'grid (default 5)')
    ap.add_argument('--track-pitch', type=float, default=0.33,
                    help='mm of board one track takes, width plus clearance, for the '
                         'congestion supply (default 0.33)')
    ap.add_argument('--moves', type=int, default=0,
                    help='annealing moves (default 0: MOVES_PER_PART per movable '
                         'part)')
    ap.add_argument('--seed', type=int, default=0,
                    help='the random stream: the start and the moves (default 0)')
    ap.add_argument('--seeds', metavar='LIST',
                    help='place several seeds in one call, e.g. 0-3 or 0,1,2,3: OUT and '
                         '--json then carry {seed}, each seed is its own process (the '
                         'same placement a --seed run gives) and its log goes beside its '
                         'board as .log')
    ap.add_argument('--jobs', type=int, default=0,
                    help='with --seeds, how many seeds run at once (default: one per '
                         'seed, at most 4)')
    ap.add_argument('--json', help='write the report here')
    a = ap.parse_args(argv)
    if a.seeds:
        return _many_seeds(ap, a)
    t0 = time.time()
    from placement import floorplan
    from placement.groups import parse_sources
    from placement.grouping import GroupsError, load_groups
    from placement.legality import footprint_side
    from placement.parser import extract_locked_refs
    from placement.portfolio import copy_siblings
    from placement.writer import SideFlipUnsupported, write_placed_output
    try:
        doc = load_groups(a.groups)
        base = json.load(open(a.intent, encoding='utf-8'))
        intent = floorplan.load_intent(a.intent)
        pcb = parse_kicad_pcb(a.board)
    except (GroupsError, OSError, ValueError) as ex:
        print(f"place_simple: {ex}", file=sys.stderr)
        return 2
    sources = parse_sources('auto')
    blocks, _problems = floorplan.resolve_blocks(intent, pcb, sources)
    # FACES first: the parts a declaration puts on the other face are
    # flipped at their pose (the writer's #714 flip) before anything is placed
    sides, conflicts = face_targets(intent, blocks, doc, pcb)
    if conflicts:
        print("place_simple: contradictory face declarations: " + '; '.join(
            f"{r}: " + ', '.join(f"{s} from {src}" for s, src in rows)
            for r, rows in sorted(conflicts.items())) + ". Nothing was written.",
            file=sys.stderr)
        return 2
    # ... and every part the intent seats at a declared pose goes there and
    # stays (`fixed_poses[]`: the mechanical facts the brief compiles)
    fixed = {str(f['ref']): f for f in intent.fixed_poses if str(f['ref']) in pcb.footprints}
    pre = {}
    for r, f in sorted(fixed.items()):
        foot = pcb.footprints[r]
        rot = float(f['rot']) if f.get('rot') is not None else (foot.rotation or 0.0)
        if (abs(float(f['x']) - foot.x) > 1e-6 or abs(float(f['y']) - foot.y) > 1e-6
                or abs((rot - (foot.rotation or 0.0) + 180.0) % 360.0 - 180.0) > 1e-6):
            pre[r] = {'reference': r, 'new_x': float(f['x']), 'new_y': float(f['y']),
                      'new_rotation': rot}
    for r, side in sorted(sides.items()):
        foot = pcb.footprints[r]
        if footprint_side(foot) != side:
            pre.setdefault(r, {'reference': r, 'new_x': foot.x, 'new_y': foot.y,
                               'new_rotation': foot.rotation or 0.0})['new_side'] = side
    flips = [pre[r] for r in sorted(pre)]
    locked = sorted(set(pre) & extract_locked_refs(a.board))
    if locked:
        print(f"place_simple: {', '.join(locked)} locked in the board on a face or at a "
              f"pose its declaration does not name; a lock is a decision. Nothing was "
              f"written.", file=sys.stderr)
        return 2
    src, tmp = a.board, None
    if flips:
        tmp = tempfile.mkdtemp(prefix='place_simple_')
        src = os.path.join(tmp, os.path.basename(a.board))
        try:
            write_placed_output(a.board, src, flips)
        except (ValueError, SideFlipUnsupported) as ex:
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"place_simple: cannot flip: {ex}", file=sys.stderr)
            return 2
        copy_siblings(a.board, src)
        pcb = parse_kicad_pcb(src)
        blocks, _problems = floorplan.resolve_blocks(intent, pcb, sources)
        print("  NOTE: at their declared pose or face: " + ', '.join(
            f"{f['reference']}" + (f" -> {f['new_side']}" if 'new_side' in f else
                                   f" at ({f['new_x']:g}, {f['new_y']:g}) {f['new_rotation']:g}")
            for f in flips))
    from placement import simple_anneal
    from placement.simple import legal
    state, model, movable, notes = build(pcb, src, doc, base, a, intent, blocks, set(fixed))
    for n in notes:
        print(f"  NOTE: {n}")
    print(f"place_simple: {len(movable)} movable part(s), fixed: "
          f"{', '.join(sorted(r for r in state.parts if r not in movable)) or 'none'}")
    for name, members in model.groups:
        print(f"  group {name}: {', '.join(members)}")
    t1 = time.time()
    moves = a.moves or MOVES_PER_PART * len(movable)

    def ok(r):
        p = state.parts[r]
        return (legal(state, r, p.x, p.y, p.rot)
                and state.intent_clear(r, p.grade_rects(p.x, p.y, p.rot)))
    kept = None             # (illegal count, poses, res) of the best run yet
    for k in range(RESTARTS + 1):
        # a restart's own stream, distinct from every other seed's first run
        seed = a.seed if k == 0 else (a.seed + 1) * 1000003 + k
        simple_anneal.scatter(model, movable, seed=seed)
        res = simple_anneal.anneal(model, movable, moves=moves, seed=seed,
                                   log=lambda s: print(f"  {s}"))
        illegal = [r for r in movable if not ok(r)]
        if kept is None or len(illegal) < kept[0]:
            kept = (len(illegal), {r: (state.parts[r].x, state.parts[r].y, state.parts[r].rot)
                                   for r in movable}, res)
        if not illegal:
            break
        print(f"  NOTE: run {k + 1} left no legal pose for {', '.join(illegal)}"
              + ("; annealing again from a fresh start" if k < RESTARTS else ""))
    _n, poses, res = kept
    for r, pose in poses.items():
        state.parts[r].ensure_rotation(pose[2])
        state.apply_move(r, *pose)
    model.reprice_all()
    final = model.total()
    t2 = time.time()
    illegal = [r for r in movable if not ok(r)]
    print(f"place_simple: annealed {res['moves']} moves in {t2 - t1:.1f} s; {_line(final)}")
    if res['reseated']:
        print(f"  NOTE: moved to the nearest legal pose after the anneal: {', '.join(res['reseated'])}")
    if illegal:
        print(f"  WARNING: no legal pose found for: {', '.join(illegal)}")
    placements = [{'reference': r, 'new_x': round(state.parts[r].x, 4),
                   'new_y': round(state.parts[r].y, 4),
                   'new_rotation': state.parts[r].rot % 360.0}
                  for r in movable]
    write_placed_output(src, a.out, placements)
    copy_siblings(a.board, a.out)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    final.pop('per_group', None)
    final.pop('per_hint', None)
    report = {'final': final, 'moves': res['moves'], 'seed': a.seed, 'runs': k + 1,
              'declared': sorted(f['reference'] for f in flips),
              'reseated': res['reseated'], 'illegal': illegal, 'notes': notes,
              'groups': model.groups, 'stats': res['stats'],
              'seconds': {'setup': round(t1 - t0, 1), 'anneal': round(t2 - t1, 1)}}
    if a.json:
        json.dump(report, open(a.json, 'w', encoding='utf-8'), indent=1)
    print('JSON_SUMMARY: ' + json.dumps({k: report[k] for k in
                                          ('final', 'moves', 'seed', 'reseated', 'illegal', 'seconds')}))
    return 4 if illegal else 0


if __name__ == '__main__':
    sys.exit(main())
