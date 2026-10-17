#!/usr/bin/env python3
"""Plan one board region per functional group, and write it as intent zones.

Grouped placement places a board one functional group at a time, each in a
region of its own. The groups -- and the hints about them: which edge a group
belongs on, which groups neighbour, which stay apart -- are the agent's, in a
groups file (`placement/grouping.py` has the format; `py_tools/check_groups.py`
checks it). This tool computes the GEOMETRY: a slicing floorplan of the free
board area that prices the wiring between groups and to the fixed parts, keeps
each group beside its own fixed members (a connector placed first), honours
the hints, and refuses a region that cannot hold its parts
(`placement/regions.py` has the model).

    python3 py_placer/plan_regions.py board.kicad_pcb --groups groups.json \\
        --intent intent.json --out regions_intent.json --png regions.png

It writes an INTENT: the input intent (or an empty one) with one `blocks[]`
entry per group that has movable parts -- `zone`, `side`, `tolerance_mm` and a
`context.grouped_placement` record of what the zone was chosen on -- and,
for a group with parts on both faces that declares no `side`, a second
block `<group>@B` (or `@F`) of its parts on its other face, planned beside
its main region (`regions._face_parts`), so every block's parts are on its
block's face. It also
adds to `decaps.exempt` every cap the groups file places away from the IC
the decap rule would claim it for (a crystal's load cap, an RC filter; see
`check_groups.py`), so the seed places it with its group.

It also writes the CRYSTAL claims (`placement/crystals.py`): each passive
crystal within reach of its IC's oscillator pins and each load cap within
reach of the crystal, as `proximity[]` rows (a pair the input intent already
claims is left as it is; `--no-crystals` writes none). The seeder seats a
claim's subject right after its partner, wherever that is, so a subject is
written into no zone: the regions are planned on the groups as written (the
space a clock group would take stays free), and each block lists only the
parts it seats; a block left empty is dropped. The load caps go into
`decaps.exempt`, since their own claims govern them. Folding the subjects
into their partner's group before planning was measured and REPLANS the
board: on ch32v003_usb the ui group lost its south-edge strip and routed
vias went 48 -> 70.
It writes the INDICATOR BANKS (`placement/group_rows.py`): each LED with
its own series resistor, the LEDs of one rail in one group's region, as one
`arrays[]` row alternating LED and resistor -- split into the fewest rows
side by side when one row does not fit its zone's free space at the
seeder's pitch, and left out when no split fits (`--no-arrays` writes none).

It also places the MOUNTING HOLES nobody locked, on a pile with four holes
(`placement/mount_holes.py`): each at the legal pose nearest a free board
corner, written as a `fixed_poses[]` row of basis `derived_default`, which
the seeder seats and locks before any group. Such a hole is zoned in no
block, and the tool says it was not locked (`--no-mount-holes` writes none).

Then grade the plan before any part moves:

    python3 py_tools/check_floorplan.py board.kicad_pcb --intent regions_intent.json --plan-only

and place it with `py_placer/place_groups.py`. The board is never modified.

Exit codes: 0 written; 2 unreadable input; 4 written, but the plan is
infeasible on some face (named) -- a region cannot hold its parts.
"""
#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement', 'combined'], 'kind': 'driver'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)

import argparse
import copy
import json
import sys

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement.grouping import (BoardView, GroupsError, decap_conflicts,  # noqa: E402
                                follower_roots, load_groups)
from placement import regions  # noqa: E402

#: How far a member may stand past its zone (blocks[].tolerance_mm).
DEFAULT_TOLERANCE_MM = 0.5


def build_intent(base, doc, plan, conflicts, tolerance, claims=(), exempt=(),
                 followers=(), fixed=()):
    """`base` with the planned zones as blocks, the conflicting caps and
    `exempt` exempt from the decap rule, `claims` appended to `proximity[]`
    and `fixed` (`fixed_poses[]` rows) to `fixed_poses[]`. `followers`
    (claim subjects) and the fixed rows' refs are left out of every block,
    and a block they empty is dropped."""
    if base:
        out = copy.deepcopy(base)
    else:
        from placement.floorplan import KIND, SCHEMA_VERSION
        out = {'schema': SCHEMA_VERSION, 'kind': KIND, 'units': 'mm'}
    planned = set(plan['zones'])
    unzoned = set(followers) | {f['ref'] for f in fixed}
    blocks = [b for b in (out.get('blocks') or []) if b.get('name') not in planned]
    # A group's main region, then its second region on the other face
    # (`<group>@<face>`, regions.plan), each a block of the parts on its face.
    for g in doc['groups']:
        names = sorted((n for n, z in plan['zones'].items()
                        if z.get('group', n) == g['name']),
                       key=lambda n: (n != g['name'], n))
        for name in names:
            z = plan['zones'][name]
            refs = [r for r in z['refs'] if r not in unzoned]
            if not refs:
                continue
            ctx = {'role': g.get('role'), 'why': g.get('why'),
                   'fixed_members': z['fixed'], 'free_mm2': z['free_mm2'],
                   'need_mm2': z['need_mm2'], 'planned_by': 'plan_regions.py'}
            if name != g['name']:
                ctx['second_region_of'] = g['name']
            blocks.append({
                'name': name,
                'refs': refs,
                'zone': z['zone'],
                'side': z['side'],
                'tolerance_mm': tolerance,
                'context': {'grouped_placement': ctx},
            })
    out['blocks'] = blocks
    if claims:
        out['proximity'] = list(out.get('proximity') or []) + [dict(c) for c in claims]
    if fixed:
        out['fixed_poses'] = list(out.get('fixed_poses') or []) + [dict(f) for f in fixed]
        # fixed_poses[] is a schema-7 key
        out['min_reader'] = max(int(out.get('min_reader') or 0), 7)
    exempt_caps = sorted({c['cap'] for c in conflicts} | set(exempt))
    if exempt_caps and out.get('decaps'):
        ex = list(out['decaps'].get('exempt') or [])
        out['decaps']['exempt'] = ex + [c for c in exempt_caps if c not in ex]
    return out


def mount_hole_rows(pcb, board, intent_path, base):
    """`(rows, notes)`: a `fixed_poses[]` row of basis `derived_default` for
    each mounting hole `placement/mount_holes.py` seats at a board corner,
    and its notes. A hole the input intent already places -- a fixed pose,
    an edge connector, a `must_lock` match -- is not this default's; the
    input intent binds the corner search as it binds the seat."""
    import fnmatch
    from placement.mount_holes import corner_hole_poses
    base = base or {}
    claimed = {str(f.get('ref')) for f in base.get('fixed_poses') or []}
    claimed |= {str(c.get('ref')) for c in base.get('edge_connectors') or []}
    pats = [str(p) for p in base.get('must_lock') or []]
    claimed |= {r for r in pcb.footprints
                if any(fnmatch.fnmatchcase(r, p) for p in pats)}
    loaded = None
    if intent_path:
        from placement import floorplan
        loaded = floorplan.load_intent(intent_path)
    poses, notes = corner_hole_poses(pcb, board, claimed=claimed, intent=loaded)
    rows = [{'ref': p['ref'], 'x': p['x'], 'y': p['y'], 'rot': p['rot'],
             'basis': 'derived_default',
             'why': ("mounting hole nobody placed: the legal pose nearest a "
                     "free board corner, where humans put a four-hole "
                     "board's holes (placement/mount_holes.py)"),
             'context': {'planned_by': 'plan_regions.py',
                         'corner': p['corner']}} for p in poses]
    return rows, notes


#: The grid `fit_banks` asks a row's fit on (mm).
FIT_GRID_MM = 0.05


def _bank_state(pcb, board, holes):
    """The seeder's state for measuring and fitting rows: the board's floor
    clearance, the mounting holes at their poses."""
    from list_nets import board_floor_knobs
    import pose_score
    clearance, edge, _ = board_floor_knobs(board)
    state = pose_score.make_state(pcb, board, clearance=clearance,
                                  board_edge_clearance=edge, grid_step=0.1)
    for h in holes:
        if h['ref'] in state.parts:
            state.apply_move(h['ref'], h['x'], h['y'], h['rot'])
    return state


def bank_shapes(pcb, base, doc, followers, holes, state):
    """The indicator banks the groups hold, found before any region exists:
    `[{'members', 'shapes'}]`, each shape a `(length, width)` mm the bank can
    be seated as -- whole, or split into rows side by side
    (`group_rows.bank_splits`, `clearance` apart) -- at the seeder's own row
    size (`seeder.row_block`: the auto pitch, at 0 or 90 degrees, along
    either axis). The region planner makes one of them fit the region that
    holds the bank (`regions.plan`'s `banks`)."""
    from placement.group_rows import bank_splits
    from placement.seeder import _materialise_rotation, row_block
    blocks = [{'name': g['name'], 'zone': [0.0, 0.0, 0.0, 0.0], 'refs': list(g['refs'])}
              for g in doc['groups']]
    found = dict(base or {}, blocks=blocks,
                 fixed_poses=list((base or {}).get('fixed_poses') or []) + list(holes))
    rows, _dropped = indicator_banks(pcb, found, followers)
    out = []
    for row in rows:
        for m in row['members']:
            for rot in (0.0, 90.0):
                _materialise_rotation(state.parts[m], rot)
        shapes = set()
        for chunks in bank_splits(list(row['members'])):
            for rot in (0.0, 90.0):
                for axis in ('x', 'y'):
                    dims = [row_block(state, c, rot, axis) for c in chunks]
                    shapes.add((round(max(d[0] for d in dims), 3),
                                round(sum(d[1] for d in dims)
                                      + (len(chunks) - 1) * state.clearance, 3)))
        out.append({'members': list(row['members']), 'shapes': sorted(shapes)})
    return out


def fit_banks(pcb, board, intent, banks, holes, state=None):
    """`(rows, notes)`: each bank laid in its region by
    `group_rows.split_bank` -- whole when a row of it fits the free space of
    its zone (plus tolerance), else as the fewest rows that fit side by side,
    else left out. Free space is the SEEDER's, which is what has to seat the
    row (`regions.FitChecker` over the obstacles `seeder._overlap_rejector`
    reads: a locked part's courtyard on its own face, its drilled-pad boxes
    on the other, the mounting holes at their poses, no container); a row's
    size is the seeder's too (`seeder.row_block`: the auto pitch, at 0 or 90
    degrees, along either axis), the rows `clearance` apart."""
    from list_nets import board_floor_knobs
    from placement.group_rows import split_bank
    from placement.legality import far_boxes
    from placement.regions import FitChecker
    from placement.seeder import _materialise_rotation, row_block
    _clearance, edge, _ = board_floor_knobs(board)
    if state is None:
        state = _bank_state(pcb, board, holes)
    seated = {h['ref'] for h in holes}
    obstacles = {'F': [], 'B': []}
    for ref, p in state.parts.items():
        if not (p.locked or ref in seated) or ref in state.container_refs:
            continue
        for side in ('F', 'B'):
            if p.side == side:
                obstacles[side].append(p.rect())
            elif p.has_tht:
                obstacles[side].extend(far_boxes(p.tht_rect()))
    # A finer grid than the plan's: a row can fill its zone (plus tolerance)
    # to within 0.2 mm (ch32v006_dev's), and the plan's 0.25 mm cells lose
    # up to one cell at each end of the window.
    checker = FitChecker(pcb, board, grid=FIT_GRID_MM, edge_clearance=edge,
                         obstacles=obstacles)
    block_of = {r: b for b in intent.get('blocks') or [] if b.get('zone')
                for r in b.get('refs') or []}
    out, notes = [], []
    for row in banks:
        b = block_of.get(row['members'][0])
        if b is None:
            out.append(row)
            continue
        z, tol = b['zone'], float(b.get('tolerance_mm') or 0.0)
        rect = (z[0] - tol, z[1] - tol, z[2] + tol, z[3] + tol)
        side = b.get('side') or 'F'
        for m in row['members']:
            for rot in (0.0, 90.0):
                _materialise_rotation(state.parts[m], rot)

        def fits(rows, rect=rect, side=side):
            for rot in (0.0, 90.0):
                for axis in ('x', 'y'):
                    dims = [row_block(state, r, rot, axis) for r in rows]
                    width = (sum(d[1] for d in dims)
                             + (len(rows) - 1) * state.clearance)
                    if checker.fits(rect, side, max(d[0] for d in dims), width):
                        return True
            return False
        got = split_bank(row, fits)
        if not got:
            notes.append(f"indicator bank {row['name']}: no row of it fits "
                         f"its region -- left out")
        elif len(got) > 1:
            notes.append(f"indicator bank {row['name']}: its region holds it "
                         f"only as {len(got)} rows side by side")
        out.extend(got)
    return out, notes


def indicator_banks(pcb, intent, followers=()):
    """`(rows, dropped)`: the board's indicator banks -- LEDs, each with its
    own series resistor, on one rail, inside one group's region -- as intent
    `arrays[]` rows that alternate LED and resistor
    (`placement/group_rows.led_rows`). The seeder seats each row whole, in
    its zone, at one pitch and angle, and the polish moves it as one piece
    (docs/floorplan-intent.md, "Arrays"). A channel whose LED or resistor
    another claim already places -- a proximity claim's follower, an edge
    connector, a fixed pose, a `must_lock` match, an array member -- leaves
    the bank, and a bank of fewer than two channels is dropped; each drop is
    reported."""
    import fnmatch
    from placement import group_rows
    zone_of = {}
    for b in intent.get('blocks') or []:
        if b.get('zone') is not None:
            for r in b.get('refs') or ():
                zone_of.setdefault(r, b['name'])
    claimed = {m: 'an array member' for r in intent.get('arrays') or []
               for m in r.get('members') or []}
    claimed.update({str(c.get('ref')): 'an edge connector'
                    for c in intent.get('edge_connectors') or []})
    claimed.update({str(f.get('ref')): 'a fixed pose'
                    for f in intent.get('fixed_poses') or []})
    claimed.update({f: "a proximity claim's follower" for f in followers})
    pats = [str(p) for p in intent.get('must_lock') or []]
    rows, dropped = [], []
    for r in group_rows.led_rows(pcb, zone_of):
        pairs = [r['members'][i:i + 2] for i in range(0, len(r['members']), 2)]
        why = {}
        for pair in pairs:
            for m in pair:
                w = claimed.get(m) or ('must_lock' if any(
                    fnmatch.fnmatchcase(m, p) for p in pats) else None)
                if w:
                    why[m] = w
        keep = [pr for pr in pairs if not any(m in why for m in pr)]
        if why:
            dropped.append({'name': r['name'], 'members': dict(sorted(why.items())),
                            'row_dropped': len(keep) < 2})
        if len(keep) < 2:
            continue
        rows.append(dict(r, members=[m for pr in keep for m in pr]))
    return rows, dropped


def render_png(pcb, pcb_file, plan, path):
    """Outline, fixed parts and the planned regions, labelled. For LOOKING."""
    from PIL import Image, ImageDraw, ImageFont
    poly = regions._outline(pcb, pcb_file)
    x0, y0, x1, y1 = poly.bounds
    scale = 900.0 / max(x1 - x0, y1 - y0)
    W, H = int((x1 - x0) * scale) + 40, int((y1 - y0) * scale) + 40
    im = Image.new('RGB', (W, H), (250, 250, 247))
    d = ImageDraw.Draw(im)

    def xy(x, y):
        return (20 + (x - x0) * scale, 20 + (y - y0) * scale)
    ext = list(poly.exterior.coords)
    d.polygon([xy(*p) for p in ext], outline=(60, 70, 60), width=3)
    cache = {}
    view = BoardView(pcb)
    for r in sorted(view.locked):
        own, tht = regions._occupancy(pcb, pcb_file, r, cache)
        d.rectangle([xy(own[0], own[1]), xy(own[2], own[3])], outline=(120, 120, 120),
                    fill=(215, 215, 210))
        d.text(xy(own[0] + 0.2, own[1] + 0.2), r, fill=(80, 80, 80))
    palette = [(66, 133, 244), (219, 68, 55), (15, 157, 88), (244, 160, 0),
               (171, 71, 188), (0, 172, 193), (255, 112, 67), (124, 179, 66)]
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial Bold.ttf', 18)
    except Exception:
        font = ImageFont.load_default()
    for i, (name, z) in enumerate(sorted(plan['zones'].items())):
        c = palette[i % len(palette)]
        zx0, zy0, zx1, zy1 = z['zone']
        d.rectangle([xy(zx0, zy0), xy(zx1, zy1)], outline=c, width=4)
        d.text(xy(zx0 + 0.3, zy0 + 0.3), f"{name} ({z['side']})", fill=c, font=font)
        d.text(xy(zx0 + 0.3, zy0 + 1.3), f"{len(z['refs'])} parts, "
               f"{z['need_mm2']:.0f}/{z['free_mm2']:.0f} mm2", fill=c)
    im.save(path)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('board', help='the board (its fixed parts placed and locked)')
    ap.add_argument('--groups', required=True, help='the groups JSON file')
    ap.add_argument('--intent', help='base intent to add the zones to '
                    '(check_floorplan --emit-intent writes one); omitted, an '
                    'intent holding only the zones is written')
    ap.add_argument('--out', required=True, help='the intent JSON to write')
    ap.add_argument('--json', dest='json_out', help='write the plan report here')
    ap.add_argument('--png', help='draw the regions to this PNG')
    ap.add_argument('--grid', type=float, default=0.25,
                    help='free-space grid in mm (default: %(default)s)')
    ap.add_argument('--util', type=float, default=0.55,
                    help="target share of a region the group's courtyards fill: "
                         "demand = courtyard area / util (default: %(default)s)")
    ap.add_argument('--gutter', type=float, default=0.6,
                    help='mm between neighbouring regions (default: %(default)s)')
    ap.add_argument('--edge-clearance', type=float, default=None,
                    help="mm the plan keeps off the outline (default: the board's "
                         "min_copper_edge_clearance, else 0.3)")
    ap.add_argument('--tolerance', type=float, default=DEFAULT_TOLERANCE_MM,
                    help='blocks[].tolerance_mm written per zone (default: %(default)s)')
    ap.add_argument('--seed', type=int, default=0,
                    help='annealing seed when there are more groups than are '
                         'searched exhaustively (default: %(default)s)')
    ap.add_argument('--no-arrays', action='store_true',
                    help="write no indicator banks: rows that alternate each "
                         "LED with its own series resistor, for LEDs on one "
                         "rail in one group's region (placement/group_rows.py)")
    ap.add_argument('--no-mount-holes', action='store_true',
                    help="write no default poses for unlocked mounting holes "
                         "(placement/mount_holes.py: on a pile with four holes, "
                         "each unlocked one at the legal pose nearest a free "
                         "board corner)")
    ap.add_argument('--no-crystals', action='store_true',
                    help="write no crystal claims (placement/crystals.py): each "
                         "crystal at its IC's oscillator pins and each load cap "
                         "at the crystal, and its followers in its IC's region")
    a = ap.parse_args(argv)
    try:
        doc = load_groups(a.groups)
        pcb = parse_kicad_pcb(a.board)
        base = json.load(open(a.intent, encoding='utf-8')) if a.intent else None
    except (GroupsError, OSError, ValueError) as ex:
        print(f"plan_regions: {ex}", file=sys.stderr)
        return 2
    edge = a.edge_clearance
    if edge is None:
        from list_nets import board_constraint
        edge = board_constraint(a.board, 'min_copper_edge_clearance') or 0.3
    claims = list((base or {}).get('proximity') or [])
    new_claims, crystal_notes = [], []
    if not a.no_crystals:
        from placement.crystals import crystal_claims
        have = {(str(c.get('ref')), str(c.get('near'))) for c in claims}
        rows, crystal_notes = crystal_claims(pcb)
        new_claims = [r for r in rows if (r['ref'], r['near']) not in have]
    followers = sorted(follower_roots(claims + new_claims))
    load_caps = [r['ref'] for r in new_claims
                 if (r.get('context') or {}).get('role') == 'crystal load cap']
    holes, hole_notes = [], []
    if not a.no_mount_holes:
        holes, hole_notes = mount_hole_rows(pcb, a.board, a.intent, base)
    # the banks are found before the plan, so a region that holds one is
    # sized for its row (`regions.plan`'s `banks`)
    pre_banks, bank_state = [], None
    if not a.no_arrays:
        bank_state = _bank_state(pcb, a.board, holes)
        pre_banks = bank_shapes(pcb, base, doc, followers, holes, bank_state)
    plan = regions.plan(pcb, a.board, doc, grid=a.grid, util=a.util,
                        gutter=a.gutter, edge_clearance=edge, seed=a.seed,
                        placed={h['ref']: (h['x'], h['y'], h['rot']) for h in holes},
                        banks=pre_banks)
    conflicts = [c for c in decap_conflicts(doc, BoardView(pcb))
                 if c['cap'] not in set(followers)]
    intent = build_intent(base, doc, plan, conflicts, a.tolerance,
                          claims=new_claims, exempt=load_caps,
                          followers=followers, fixed=holes)
    banks, banks_dropped, bank_notes = [], [], []
    if not a.no_arrays:
        banks, banks_dropped = indicator_banks(pcb, intent, followers)
        if banks:
            banks, bank_notes = fit_banks(pcb, a.board, intent, banks, holes,
                                          state=bank_state)
        if banks:
            intent['arrays'] = list(intent.get('arrays') or []) + banks
            intent['min_reader'] = max(int(intent.get('min_reader') or 0), 7)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(intent, f, indent=2)
    if a.png:
        render_png(pcb, a.board, plan, a.png)
    if a.json_out:
        with open(a.json_out, 'w', encoding='utf-8') as f:
            json.dump({'plan': {k: v for k, v in plan.items() if k != 'faces'},
                       'faces': {s: {k: (str(v) if k == 'tree' else v) for k, v in f_.items()}
                                 for s, f_ in plan['faces'].items()},
                       'decap_exempt': [c['cap'] for c in conflicts]}, f, indent=1)
    for line in crystal_notes:
        print(f"  {line}")
    if followers:
        print(f"  zoned in no block (each follows its proximity claim): "
              f"{', '.join(followers)}")
    for side, f_ in plan['faces'].items():
        print(f"  face {side}: {f_['evaluated']} floorplan(s) evaluated, cost "
              f"{f_['cost']}, parts {f_['need_mm2']:.0f} of {f_['free_mm2']:.0f} mm2 free "
              f"({f_['utilisation']:.0%}); terms {f_['terms']}")
    for name, z in plan['zones'].items():
        print(f"  {name:<12} {z['side']} zone {z['zone']}  {len(z['refs'])} parts, "
              f"{z['need_mm2']:.1f} of {z['free_mm2']:.1f} mm2"
              + (f", beside {', '.join(z['fixed'])}" if z['fixed'] else ''))
    if conflicts:
        print(f"  decaps.exempt += {', '.join(c['cap'] for c in conflicts)} "
              f"(placed with their group, not at the decap rule's IC)")
    if load_caps and intent.get('decaps'):
        print(f"  decaps.exempt += {', '.join(load_caps)} (crystal load caps: "
              f"their proximity claims govern them)")
    for r in banks:
        print(f"  indicator bank {r['name']}: {', '.join(r['members'])}")
    for line in bank_notes:
        print(f"  {line}")
    for d in banks_dropped:
        print(f"  indicator bank {d['name']}: left out {d['members']}"
              + (' -- bank dropped' if d.get('row_dropped') else ''))
    if holes:
        print(f"  WARN  mounting hole(s) {', '.join(h['ref'] for h in holes)} "
              f"not locked: a hole's pose is a mechanical decision. Seated by "
              f"default at the board corners (fixed_poses, basis "
              f"derived_default): "
              + '; '.join(f"{h['ref']} {h['context']['corner']} "
                          f"({h['x']:g}, {h['y']:g})" for h in holes)
              + ". Where the enclosure puts them elsewhere, lock them there "
                "and plan again:")
        print("        python3 -X utf8 py_placer/place_pose.py BOARD OUT "
              + ' '.join(f"set {h['ref']} X Y" for h in holes)
              + ' lock ' + ' '.join(h['ref'] for h in holes))
    for line in hole_notes:
        print(f"  WARN  {line}")
    for w in plan['warnings']:
        print(f"  WARN  {w}")
    infeasible = [s for s, f_ in plan['faces'].items()
                  if f_['terms'].get('infeasible', 0) >= regions.INFEASIBLE]
    summary = {'zones': {n: z['zone'] for n, z in plan['zones'].items()},
               'infeasible_faces': infeasible,
               'decap_exempt': [c['cap'] for c in conflicts], 'out': a.out,
               'crystal_claims': len(new_claims), 'followers': followers,
               'indicator_banks': [r['name'] for r in banks],
               'mount_holes_at_corners': [h['ref'] for h in holes]}
    print('JSON_SUMMARY: ' + json.dumps(summary, sort_keys=True))
    return 4 if infeasible else 0


if __name__ == '__main__':
    import cli_banner
    cli_banner.install()   # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
