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
`context.grouped_placement` record of what the zone was chosen on. It also
adds to `decaps.exempt` every cap the groups file places away from the IC
the decap rule would claim it for (a crystal's load cap, an RC filter; see
`check_groups.py`), so the seed places it with its group. Then grade the plan
before any part moves:

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
                                load_groups)
from placement import regions  # noqa: E402

#: How far a member may stand past its zone (blocks[].tolerance_mm).
DEFAULT_TOLERANCE_MM = 0.5


def build_intent(base, doc, plan, conflicts, tolerance):
    """`base` with the planned zones as blocks and the conflicting caps exempt."""
    if base:
        out = copy.deepcopy(base)
    else:
        from placement.floorplan import KIND, SCHEMA_VERSION
        out = {'schema': SCHEMA_VERSION, 'kind': KIND, 'units': 'mm'}
    planned = set(plan['zones'])
    blocks = [b for b in (out.get('blocks') or []) if b.get('name') not in planned]
    roles = {g['name']: g for g in doc['groups']}
    for name in [g['name'] for g in doc['groups'] if g['name'] in planned]:
        z = plan['zones'][name]
        g = roles[name]
        blocks.append({
            'name': name,
            'refs': list(z['refs']),
            'zone': z['zone'],
            'side': z['side'],
            'tolerance_mm': tolerance,
            'context': {'grouped_placement': {
                'role': g.get('role'), 'why': g.get('why'),
                'fixed_members': z['fixed'], 'free_mm2': z['free_mm2'],
                'need_mm2': z['need_mm2'], 'planned_by': 'plan_regions.py'}},
        })
    out['blocks'] = blocks
    exempt_caps = sorted({c['cap'] for c in conflicts})
    if exempt_caps and out.get('decaps'):
        ex = list(out['decaps'].get('exempt') or [])
        out['decaps']['exempt'] = ex + [c for c in exempt_caps if c not in ex]
    return out


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
    plan = regions.plan(pcb, a.board, doc, grid=a.grid, util=a.util,
                        gutter=a.gutter, edge_clearance=edge, seed=a.seed)
    conflicts = decap_conflicts(doc, BoardView(pcb))
    intent = build_intent(base, doc, plan, conflicts, a.tolerance)
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
    for w in plan['warnings']:
        print(f"  WARN  {w}")
    infeasible = [s for s, f_ in plan['faces'].items()
                  if f_['terms'].get('infeasible', 0) >= regions.INFEASIBLE]
    summary = {'zones': {n: z['zone'] for n, z in plan['zones'].items()},
               'infeasible_faces': infeasible,
               'decap_exempt': [c['cap'] for c in conflicts], 'out': a.out}
    print('JSON_SUMMARY: ' + json.dumps(summary, sort_keys=True))
    return 4 if infeasible else 0


if __name__ == '__main__':
    import cli_banner
    cli_banner.install()   # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
