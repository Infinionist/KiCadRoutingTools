#!/usr/bin/env python3
"""Place a board by function, simply: groups, one objective, annealed.

    python3 -X utf8 py_placer/place_simple.py board.kicad_pcb placed.kicad_pcb \\
        --groups groups.json --intent intent.json [--moves 11000] [--seed 0] [--json report.json]

1. GROUPS: the groups file (`check_groups.py`), a crystal and its load caps
   folded into their IC's group. A group is a set of parts kept together;
   it gets no region and no shape -- the objective's group term keeps it in
   one piece, whatever shape the free space gives it.
2. The parts the file locks stay put (connectors on their edges, mounting
   holes: whatever the enclosure or a mating part decides); every other
   part starts at a random place on the board.
3. ANNEAL: one cost (`placement/simple.py`) -- signal and rail trees,
   crossings, claims, decaps at their own group's supply pins, each group's
   gap tree, the halo between unwired neighbours, airwires over foreign
   pads, routing congestion -- lowered by simulated annealing
   (`placement/simple_anneal.py`), a fixed number of moves from `--seed`,
   so the same board and seed give the same placement. On ch32v003_usb
   (22 movable parts) the whole run takes about 4.3 s.

Exit codes: 0 placed, 2 unreadable input, 4 placed with parts left illegal.
"""
#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement', 'combined'], 'kind': 'actor'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)

import argparse
import json
import sys
import time

from kicad_parser import parse_kicad_pcb  # noqa: E402


def _is_ground(name):
    u = (name or '').upper()
    return 'GND' in u or 'VSS' in u


def build(pcb, board, doc, base, args):
    """`(state, model, movable, notes)` for the board, its groups and its
    base intent."""
    import pose_score
    from list_nets import board_floor_knobs
    from placement import floorplan as fp
    from placement.crystals import crystal_claims
    from placement.grouping import BoardView, decap_conflicts, fold_followers, group_of
    from placement.simple import Model
    notes = []
    clearance, edge, _ = board_floor_knobs(board)
    state = pose_score.make_state(pcb, board, clearance=clearance,
                                  board_edge_clearance=edge, grid_step=0.1,
                                  halo_base=args.halo_base, halo_coef=args.halo_coef,
                                  halo_weight=args.halo_weight)
    movable = sorted(r for r, p in state.parts.items() if not p.locked)
    # claims: the intent's own, and each crystal at its IC's oscillator pins
    rows = list(base.get('proximity') or [])
    have = {(str(c.get('ref')), str(c.get('near'))) for c in rows}
    xrows, xnotes = crystal_claims(pcb)
    rows += [r for r in xrows if (r['ref'], r['near']) not in have]
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
    # decaps: each cap on a supply rail, to that rail's nearest supply pin --
    # not a claimed part, and not a cap whose IC is in another group (an RC
    # cap beside its button, not a decap)
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
    # an LDO's output cap belongs at the LDO, not at the MCU's VDD pin
    gof = group_of(doc)
    decaps, decap_pins = [], {}
    for nid, caps in fp._decap_caps_by_net(pcb).items():
        if nid in supply:
            for cap in caps:
                r = cap.reference
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
                  decap_weight=args.decap_weight, group_weight=args.group_weight)
    return state, model, movable, notes


def _line(t):
    groups = ', '.join(f"{n} {v:.1f}" for n, v in t['per_group'].items() if v > 0.05)
    return (f"cost {t['cost']:.1f} (wire {t['wire']:.1f} mm, {t['crossings']} crossings, "
            f"claims {t['claims']:.1f}, decaps {t['decaps']:.1f}, halo {t['halo']:.2f}, "
            f"pad hits {t['pad_hits']:.0f}, congestion {t['congestion']:.2f}, "
            f"groups {t['groups']:.1f}" + (f": {groups}" if groups else "") + ")")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('board', help='the board; the parts it locks stay put')
    ap.add_argument('out', help='the placed board to write')
    ap.add_argument('--groups', required=True, help='the groups file (check_groups.py)')
    ap.add_argument('--intent', required=True,
                    help='the emitted intent (check_floorplan --emit-intent): its '
                         'proximity claims and decap rules')
    ap.add_argument('--crossing-weight', type=float, default=2.0,
                    help='mm of wire one airwire crossing costs (default 2)')
    ap.add_argument('--claim-weight', type=float, default=1.0,
                    help='per mm of a claimed pad to its partner pad (default 1)')
    ap.add_argument('--decap-weight', type=float, default=2.0,
                    help='per mm of a decap to its supply pin (default 2)')
    ap.add_argument('--group-weight', type=float, default=5.0,
                    help='per mm of gap in a group\'s tree over its members\' '
                         'courtyards (default 5)')
    ap.add_argument('--halo-base', type=float, default=0.0,
                    help='mm of halo every part keeps (default 0)')
    ap.add_argument('--halo-coef', type=float, default=0.2,
                    help='mm of halo per sqrt(pin count) (default 0.2)')
    ap.add_argument('--halo-weight', type=float, default=10.0,
                    help='cost per mm^2 of an unwired pair\'s halo shortfall (default 10)')
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
    ap.add_argument('--moves', type=int, default=11000,
                    help='annealing moves (default 11000)')
    ap.add_argument('--seed', type=int, default=0,
                    help='the random stream: the start and the moves (default 0)')
    ap.add_argument('--json', help='write the report here')
    a = ap.parse_args(argv)
    t0 = time.time()
    from placement.grouping import GroupsError, load_groups
    try:
        doc = load_groups(a.groups)
        base = json.load(open(a.intent, encoding='utf-8'))
        pcb = parse_kicad_pcb(a.board)
    except (GroupsError, OSError, ValueError) as ex:
        print(f"place_simple: {ex}", file=sys.stderr)
        return 2
    from placement import simple_anneal
    from placement.simple import legal
    state, model, movable, notes = build(pcb, a.board, doc, base, a)
    for n in notes:
        print(f"  NOTE: {n}")
    print(f"place_simple: {len(movable)} movable part(s), fixed: "
          f"{', '.join(sorted(r for r in state.parts if r not in movable)) or 'none'}")
    for name, members in model.groups:
        print(f"  group {name}: {', '.join(members)}")
    t1 = time.time()
    simple_anneal.scatter(model, movable, seed=a.seed)
    res = simple_anneal.anneal(model, movable, moves=a.moves, seed=a.seed,
                               log=lambda s: print(f"  {s}"))
    final = model.total()
    t2 = time.time()
    illegal = [r for r in movable
               if not legal(state, r, state.parts[r].x, state.parts[r].y, state.parts[r].rot)]
    print(f"place_simple: annealed {res['moves']} moves in {t2 - t1:.1f} s; {_line(final)}")
    if res['reseated']:
        print(f"  NOTE: moved to the nearest legal pose after the anneal: {', '.join(res['reseated'])}")
    if illegal:
        print(f"  WARNING: no legal pose found for: {', '.join(illegal)}")
    from placement.writer import write_placed_output
    from placement.portfolio import copy_siblings
    placements = [{'reference': r, 'new_x': round(state.parts[r].x, 4),
                   'new_y': round(state.parts[r].y, 4),
                   'new_rotation': state.parts[r].rot % 360.0}
                  for r in movable]
    write_placed_output(a.board, a.out, placements)
    copy_siblings(a.board, a.out)
    final.pop('per_group', None)
    report = {'final': final, 'moves': res['moves'], 'seed': a.seed,
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
