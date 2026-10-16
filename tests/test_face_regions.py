#!/usr/bin/env python3
"""Grouped placement on both faces, and the seed's licences on a pile.

* `regions._face_parts`: a group that declares its side is one region on it;
  otherwise the main face is the one its parts cover more area of (two large
  chips on F outweigh forty decaps on B), and the parts on the other face
  are a second region.
* on glasgow_revC piled (connectors and SW1 locked, schematic sheets as the
  groups), `plan_regions` writes each sheet's back-face parts as a
  `<sheet>@B` block, every block's parts on its block's face, each second
  region within a few millimetres of its main one; so
  `place_intent_sides` finds no part to mirror.
* on ulx3s with U9 (an ESP32 module drawn with no courtyard) locked, the
  seed state spaces it by its fab body, which check_assembly grades it on,
  and refuses a resistor over the body's edge; unlocked, U9 keeps its pad
  box and admits it.
* on esp_prog's pile, a pair whose staging poses stack licenses nothing
  (its seed baseline is clean), while on the human layout a pair short of
  clearance keeps that shortfall as its seed baseline.
* `place_groups.later_zone_keepouts` reserves each later group's zone, on
  its face, for every part but this step's (a fixed pose excepted).

    python3 tests/test_face_regions.py
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from kicad_parser import parse_kicad_pcb  # noqa: E402

FAILS = []
PY = [sys.executable, '-X', 'utf8']
PILE = os.path.join(ROOT, '.claude', 'skills', 'pcb-free-agent', 'scripts',
                    'make_unplaced.py')
GLASGOW_FIXED = ['J1', 'J10', 'J2', 'J3', 'J4', 'J5', 'J6', 'J7', 'J8', 'J9', 'SW1']


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILS.append(what)


def run(args):
    return subprocess.run(PY + args, capture_output=True, text=True, cwd=ROOT)


def need(path, r, what):
    if not os.path.isfile(path):
        raise SystemExit(f"BROKEN TEST: {what} wrote nothing (exit "
                         f"{r.returncode}): {(r.stderr or r.stdout)[-400:]}")


def face_parts_units():
    from placement.regions import _face_parts
    side = {'U1': 'F', 'U2': 'F', **{f"C{i}": 'B' for i in range(40)}}
    area = {'U1': 324.0, 'U2': 281.0, **{f"C{i}": 3.4 for i in range(40)}}
    main, parts = _face_parts({'name': 'core'}, list(side), area, side.get)
    check(main == 'F' and sorted(parts['F']) == ['U1', 'U2'] and len(parts['B']) == 40,
          f"two chips on F outweigh forty decaps on B: main face {main}, "
          f"{len(parts.get('B', []))} parts on B")
    main, parts = _face_parts({'name': 'core', 'side': 'B'}, list(side), area, side.get)
    check(main == 'B' and list(parts) == ['B'] and len(parts['B']) == 42,
          "a declared side is one region holding every part")
    main, parts = _face_parts({'name': 'ui'}, ['D1', 'R1'], {'D1': 2.0, 'R1': 2.0},
                              {'D1': 'F', 'R1': 'F'}.get)
    check(main == 'F' and list(parts) == ['F'], "a one-face group is one region")


def glasgow(tmp):
    src = os.path.join(ROOT, 'kicad_files', 'glasgow_revC.kicad_pcb')
    human = parse_kicad_pcb(src)
    pile = os.path.join(tmp, 'gla_pile.kicad_pcb')
    need(pile, run([PILE, src, pile]), 'make_unplaced')
    fixed = os.path.join(tmp, 'gla_fixed.kicad_pcb')
    args = []
    for ref in GLASGOW_FIXED:
        fp = human.footprints[ref]
        args += ['set', ref, f"{fp.x:.4f}", f"{fp.y:.4f}", '--rot', f"{fp.rotation or 0:g}"]
    need(fixed, run([os.path.join(ROOT, 'py_placer', 'place_pose.py'), pile, fixed]
                    + args + ['lock'] + GLASGOW_FIXED), 'place_pose')
    base = os.path.join(tmp, 'gla_base.json')
    need(base, run([os.path.join(ROOT, 'py_tools', 'check_floorplan.py'), fixed,
                    '--emit-intent', base]), 'check_floorplan --emit-intent')
    from placement.grouping import BoardView
    from placement.legality import footprint_side
    pcb = parse_kicad_pcb(fixed)
    sheets = {}
    for ref in BoardView(pcb).parts:
        sheets.setdefault(human.footprints[ref].sheet_name or 'main', []).append(ref)
    groups = os.path.join(tmp, 'gla_groups.json')
    json.dump({'schema': 1, 'groups': [{'name': n.lower(), 'refs': sorted(r)}
                                       for n, r in sorted(sheets.items())]},
              open(groups, 'w'))
    regions = os.path.join(tmp, 'gla_regions.json')
    need(regions, run([os.path.join(ROOT, 'py_placer', 'plan_regions.py'), fixed,
                       '--groups', groups, '--intent', base, '--out', regions]),
         'plan_regions')
    doc = json.load(open(regions))
    blocks = {b['name']: b for b in doc['blocks']
              if (b.get('context') or {}).get('grouped_placement')}
    seconds = {n: b for n, b in blocks.items() if '@' in n}
    check(sorted(seconds) == ['io_banks@B', 'io_buffer_a@B', 'io_buffer_b@B', 'main@B']
          and all(b['side'] == 'B' and b['context']['grouped_placement'].get(
                  'second_region_of') == n.split('@')[0] for n, b in seconds.items()),
          f"each sheet's back-face parts are a <sheet>@B block ({sorted(seconds)})")
    wrong = [(r, n) for n, b in blocks.items() for r in b['refs']
             if footprint_side(pcb.footprints[r]) != b['side']]
    check(not wrong, f"every block's parts are on its face ({wrong[:5]})")

    def gap(a, b):
        return max(0.0, max(a[0], b[0]) - min(a[2], b[2]),
                   max(a[1], b[1]) - min(a[3], b[3]))
    far = {n: round(gap(b['zone'], blocks[n.split('@')[0]]['zone']), 2)
           for n, b in seconds.items()}
    check(all(v <= 5.0 for v in far.values()),
          f"each second region lies within 5 mm of its main one ({far})")
    from place_intent_sides import intent_side_targets, side_moves
    from placement import floorplan
    from placement.groups import parse_sources
    targets, _src, conflicts, _p = intent_side_targets(
        floorplan.load_intent(regions), pcb, parse_sources('auto'))
    moves = side_moves(pcb, targets)
    check(not conflicts and not moves,
          f"place_intent_sides finds no part to mirror ({len(moves)} move(s))")


def locked_body(tmp):
    import pose_score
    from list_nets import board_floor_knobs
    from placement import seeder
    src = os.path.join(ROOT, 'kicad_files', 'ulx3s.kicad_pcb')
    locked = os.path.join(tmp, 'ulx_u9.kicad_pcb')
    need(locked, run([os.path.join(ROOT, 'py_placer', 'place_pose.py'), src, locked,
                      'lock', 'U9']), 'place_pose lock U9')
    states = {}
    for name, path in (('locked', locked), ('unlocked', src)):
        pcb = parse_kicad_pcb(path)
        cl, edge, _ = board_floor_knobs(path)
        states[name] = (pcb, pose_score.make_state(
            pcb, path, clearance=cl, board_edge_clearance=edge, grid_step=0.1))
    # a resistor straddling the drawn body's top edge, a third of it inside
    # (the band between that edge and the pads, on the board, is where the
    # six parts check_assembly blocked had been seated). Wholly inside, the
    # body-containment conjunct refuses it whether U9 is locked or not.
    pcb, st = states['locked']
    x0, y0, x1, _y1 = st.parts['U9'].rect()
    pose = ((x0 + x1) / 2.0, y0 - 0.25, 0.0)
    out = {}
    for name, (pcb, st) in states.items():
        r = st.parts['U9'].rect()
        others = set(st.parts) - {'U9', 'R30'}
        out[name] = (r[3] - r[1], seeder.pose_ok(st, 'R30', *pose, others))
    check(out['locked'][0] > out['unlocked'][0] + 4.0,
          f"locked, U9 is spaced on its fab body, {out['locked'][0]:.1f} mm long, "
          f"not its {out['unlocked'][0]:.1f} mm pad box")
    check(out['locked'][1] is False and out['unlocked'][1] is True,
          f"and a resistor over its body's edge is refused, where unlocked "
          f"U9 admits it ({out['unlocked'][1]})")


def pile_licence(tmp):
    import pose_score
    from list_nets import board_floor_knobs
    from placement.legality import ZERO_SHORTFALL
    src = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
    pile = os.path.join(tmp, 'esp_pile.kicad_pcb')
    need(pile, run([PILE, src, pile]), 'make_unplaced')
    found = {}
    for name, path in (('pile', pile), ('human', src)):
        pcb = parse_kicad_pcb(path)
        cl, edge, _ = board_floor_knobs(path)
        st = pose_score.make_state(pcb, path, clearance=cl,
                                   board_edge_clearance=edge, grid_step=0.1)
        ctx = st.legality_ctx
        refs = sorted(st.parts)
        hit = [(a, b) for i, a in enumerate(refs) for b in refs[i + 1:]
               if (lambda s: s.stack if name == 'pile' else s.pad > 1e-6)(
                   ctx.pair_shortfall(a, b))]
        found[name] = [(ctx.seed_baseline(a, b), ctx.pair_shortfall(a, b)) for a, b in hit]
    rows = found['pile']
    check(rows and all(base == ZERO_SHORTFALL for base, _ in rows),
          f"on the pile, {len(rows)} pair(s) staged stacked license nothing")
    rows = found['human']
    check(rows and all(base == own for base, own in rows),
          f"on the human layout a pair short of clearance keeps its own "
          f"baseline ({len(rows)} pair(s))")


def reservations():
    import place_groups as pg
    intent = {'blocks': [{'name': 'a', 'refs': ['R1', 'R2'], 'zone': [0, 0, 10, 10], 'side': 'F'},
                         {'name': 'b', 'refs': ['R3'], 'zone': [10, 0, 20, 10], 'side': 'F'},
                         {'name': 'b@B', 'refs': ['C3'], 'zone': [10, 0, 20, 10], 'side': 'B'},
                         {'name': 'c', 'refs': ['R1', 'R9'], 'zone': [0, 10, 9, 20], 'side': 'F'}]}
    rows = pg.later_zone_keepouts(intent, future={'R3', 'C3', 'R9'}, step={'R1', 'R2', 'MK1'},
                                  present={'R1', 'R2', 'J1', 'MK1'}, fixed={'MK1'})
    check([(r['name'], r['sides'], r['rect']) for r in rows]
          == [('reserved:b', ['F'], [10, 0, 20, 10]), ('reserved:b@B', ['B'], [10, 0, 20, 10])],
          f"each later group's zone is reserved on its face ({[r['name'] for r in rows]})")
    check(rows and rows[0]['allow'] == ['J1', 'MK1'],
          "for every part but this step's, a fixed pose excepted")
    from placement import floorplan
    loaded = floorplan.intent_from_dict({'schema': floorplan.SCHEMA_VERSION,
                                         'kind': floorplan.KIND, 'keepouts': rows})
    check(len(loaded.keepouts) == 2, "the rows load as intent keep-outs")


def main():
    face_parts_units()
    reservations()
    with tempfile.TemporaryDirectory() as tmp:
        glasgow(tmp)
        locked_body(tmp)
        pile_licence(tmp)
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
