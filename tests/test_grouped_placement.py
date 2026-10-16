#!/usr/bin/env python3
"""Grouped placement: the groups file, the region planner, the one-pass
placement into the regions and the group-at-a-time loop
(`placement/grouping.py`, `placement/regions.py`, `py_tools/check_groups.py`,
`py_placer/plan_regions.py`, `place_seed --intent`, `py_placer/place_groups.py`)
and the seeder's `decap_owner_scope`.

  1. A groups file is refused, by name, for each shape it cannot have.
  2. check_groups on a tracked board (kicad_files/esp_prog): a part in no
     group is an ERROR; the crystal's load caps are named as decap conflicts
     (the decap rule would claim them at the CH340); the order starts with the
     fixed group, then the core.
  3. The floorplan enumeration is exhaustive and unique: n! x the large
     Schroeder number R(n-1) trees for n groups (4, 36, 528, 10800).
  4. A cut puts FREE area on each side in proportion to demand, and the
     free-space opening removes slivers no part fits in.
  5. trim_intent drops rows about absent parts; write_step_board deletes the
     absent footprints and locks the placed ones, on the step board only.
  6. END TO END on esp_prog from a pile: plan_regions writes one zone per
     group and exempts the load caps; place_seed --intent (one pass) and
     place_groups (the loop) each place every movable part on the board,
     inside its group's zone (+ tolerance); the loop does it
     deterministically, leaves the input untouched and stamps no lock on the
     output. A placed pad on another part's copper is caught by check_drc:
     the decap owners each step names keep esp_prog's C3 off the LDO's tab.
  7. place_groups' grade sets a fixed part's own error apart (pinned, not
     counted against the run) and resolves an emitted intent's `sheet:`
     blocks on a hierarchical board (kicad_files/flat_hierarchy).
  8. A decap the pin stage declines in a grouped step is put back at its
     group chip's supply pin, not at the zone centre: esp_prog with C2
     rewired to +3.3V and grouped with the LDO (measured: 2.0 mm from U2's
     +3.3V pad, against 5.1 mm with the zone-centre target).
  9. Every placed owner may own a decap claim unless a caller names them.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
for p in ('py_placer', 'py_tools', 'py_router'):
    sys.path.insert(0, os.path.join(ROOT, p))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import grouping, regions  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
PY = [sys.executable, '-X', 'utf8']
FIXED = ['USB1', 'CON1', 'CON2', 'Ref*', 'Ref*~2']
GROUPS = {
    "schema": 1,
    "groups": [
        {"name": "io", "role": "io", "refs": FIXED},
        {"name": "power", "role": "power", "refs": ["U2", "C1", "C3"], "anchor": "U2"},
        {"name": "bridge", "role": "core", "refs": ["U1", "R3", "R4"], "anchor": "U1"},
        {"name": "clock", "role": "clock", "refs": ["Y1", "C2", "C4"], "anchor": "Y1",
         "near": ["bridge"]},
        {"name": "autoreset", "role": "interface", "refs": ["Q1", "Q2", "R1", "R2"]},
    ]}

results = []


def ok(name, cond, detail=''):
    results.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'} {name}"
          f"{(' -- ' + str(detail)[-500:]) if detail and not cond else ''}")


def refused(doc, needle):
    try:
        grouping.validate_doc(doc)
    except grouping.GroupsError as ex:
        return needle in str(ex), str(ex)
    return False, 'accepted'


def test_validate():
    base = {"schema": 1, "groups": [{"name": "a", "refs": ["R1"]}, {"name": "b", "refs": ["R2"]}]}
    cases = [
        ({**base, 'extra': 1}, "unknown key(s) ['extra']"),
        ({"schema": 1, "groups": []}, "non-empty list"),
        ({"schema": 1, "groups": [{"name": "a", "refs": ["R1"]}, {"name": "a", "refs": ["R2"]}]},
         "used twice"),
        ({"schema": 1, "groups": [{"name": "a", "refs": ["R1"], "edge": "top"}]}, "is not one of"),
        ({"schema": 1, "groups": [{"name": "a", "refs": ["R1"], "anchor": "U9"}]},
         "is not one of its refs"),
        ({"schema": 1, "groups": [{"name": "a", "refs": ["R1"], "near": ["zz"]}]},
         "which is not a group"),
        ({**base, "order": ["a"]}, "every group exactly once"),
        ({"schema": 2, "groups": base['groups']}, "this reader knows"),
    ]
    for doc, needle in cases:
        hit, msg = refused(doc, needle)
        ok(f"refused: {needle}", hit, msg)
    ok("a well-formed file loads", grouping.validate_doc(base) is base)


def test_check_groups(pcb):
    rep = grouping.analyze(GROUPS, pcb, movable=set(grouping.BoardView(pcb).parts) - set(FIXED))
    ok("esp_prog groups have no errors", not rep['errors'], rep['errors'])
    caps = sorted(d['cap'] for d in rep['decap_cross_group'])
    ok("the crystal's load caps are decap conflicts (C2, C4)", caps == ['C2', 'C4'], caps)
    ok("the order starts at the fixed group, then the core",
       rep['order'][:2] == ['io', 'bridge'], rep['order'])
    missing = json.loads(json.dumps(GROUPS))
    missing['groups'][-1]['refs'].remove('R2')
    rep2 = grouping.analyze(missing, pcb, movable=set(grouping.BoardView(pcb).parts) - set(FIXED))
    ok("a part in no group is an ERROR", any('R2' in e for e in rep2['errors']), rep2['errors'])


def test_enumeration():
    want = {1: 1, 2: 4, 3: 36, 4: 528, 5: 10800}
    for n, count in want.items():
        names = [chr(97 + i) for i in range(n)]
        trees = list(regions.enumerate_trees(names))
        ok(f"{n} group(s): {count} floorplans", len(trees) == count, len(trees))
        ok(f"{n} group(s): every floorplan distinct",
           len({repr(t) for t in trees}) == len(trees))


def test_cut_and_opening():
    import numpy as np
    from shapely.geometry import box
    fg = regions._free_grid(box(0, 0, 10, 10), 0.0, [], 0.5)
    cells = regions.layout(('V', 'a', 'b'), fg, {'a': 1.0, 'b': 3.0})
    ra, rb = fg.rect_mm(*cells['a']), fg.rect_mm(*cells['b'])
    ok("a 1:3 vertical cut gives widths 2.5 and 7.5",
       abs((ra[2] - ra[0]) - 2.5) < 1e-9 and abs((rb[2] - rb[0]) - 7.5) < 1e-9, (ra, rb))
    # an obstacle in the right half: the cut moves so FREE area stays 1:3
    fg2 = regions._free_grid(box(0, 0, 10, 10), 0.0, [(6, 0, 10, 5)], 0.5)
    c2 = regions.layout(('V', 'a', 'b'), fg2, {'a': 1.0, 'b': 3.0})
    fa = fg2.cells(*c2['a']) / fg2.cells(0, 0, fg2.ny, fg2.nx)
    ok("with an obstacle the cut keeps free area at 1:3", abs(fa - 0.25) < 0.03, fa)
    # two obstacles 0.3 mm apart: the sliver between them is not free space
    fg3 = regions._free_grid(box(0, 0, 10, 10), 0.0, [(0, 0, 4.85, 10), (5.15, 0, 10, 10)], 0.1)
    ok("a 0.3 mm sliver is removed by the opening", fg3.free.sum() == 0, int(fg3.free.sum()))
    fg4 = regions._free_grid(box(0, 0, 10, 10), 0.0, [(0, 0, 4, 10), (6, 0, 10, 10)], 0.1)
    ok("a 2 mm channel survives it", fg4.free.sum() > 0 and
       np.isclose(fg4.free.sum() * 0.01, 20.0, atol=1.0), float(fg4.free.sum() * 0.01))


def test_trim_and_step_board(tmp):
    import place_groups as pg
    intent = {"blocks": [{"name": "a", "refs": ["R1", "R2"]}, {"name": "b", "refs": ["R9"]}],
              "edge_connectors": [{"ref": "J1", "edge": "north"}, {"ref": "J9", "edge": "south"}],
              "proximity": [{"ref": "Y1", "near": "U1"}, {"ref": "Y1", "near": "U9"}],
              "decaps": {"max_distance_mm": 1, "within_radius_refs": ["C1", "C9"]}}
    t = pg.trim_intent(intent, {'R1', 'J1', 'Y1', 'U1', 'C1'})
    ok("a block keeps its present refs, an empty block is dropped",
       t['blocks'] == [{"name": "a", "refs": ["R1"]}], t['blocks'])
    ok("rows about absent parts are dropped",
       [e['ref'] for e in t['edge_connectors']] == ['J1']
       and [p['near'] for p in t['proximity']] == ['U1']
       and t['decaps']['within_radius_refs'] == ['C1'], t)
    ok("the input intent is not modified", len(intent['blocks']) == 2)
    dst = os.path.join(tmp, 'step.kicad_pcb')
    pg.write_step_board(BOARD, dst, {'Y1', 'C2', 'C4'}, {'U1'})
    p = parse_kicad_pcb(dst)
    ok("the step board has no future-group footprints",
       not ({'Y1', 'C2', 'C4'} & set(p.footprints)), sorted(p.footprints))
    ok("the step board keeps the rest", {'U1', 'U2', 'USB1'} <= set(p.footprints))
    ok("placed groups are locked on the step board", p.footprints['U1'].locked)
    ok("and the source board is untouched",
       not parse_kicad_pcb(BOARD).footprints['U1'].locked)


def _stage(tmp, board=BOARD):
    """A pile of esp_prog with its connectors and fiducials placed and locked."""
    src = os.path.join(tmp, 'src.kicad_pcb')
    if os.path.abspath(board) != os.path.abspath(src):
        shutil.copy(board, src)
    pile = os.path.join(tmp, 'pile.kicad_pcb')
    r = subprocess.run(PY + [os.path.join(ROOT, '.claude/skills/pcb-free-agent/scripts/make_unplaced.py'),
                             src, pile], capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stdout + r.stderr
    p = parse_kicad_pcb(BOARD)
    args = []
    for ref in FIXED:
        f = p.footprints[ref]
        args += ['set', ref, f'{f.x:.4f}', f'{f.y:.4f}', '--rot', f'{(f.rotation or 0):g}']
    fixed = os.path.join(tmp, 'fixed.kicad_pcb')
    r = subprocess.run(PY + [os.path.join(ROOT, 'py_placer/place_pose.py'), pile, fixed]
                       + args + ['lock'] + FIXED, capture_output=True, text=True)
    return (fixed if os.path.isfile(fixed) else None), r.stdout + r.stderr


def test_end_to_end(tmp):
    fixed, log = _stage(tmp)
    ok("staged a pile with the fixed parts locked", fixed, log[-600:])
    if not fixed:
        return
    gpath = os.path.join(tmp, 'groups.json')
    json.dump(GROUPS, open(gpath, 'w'))
    intent = os.path.join(tmp, 'regions.json')
    r = subprocess.run(PY + [os.path.join(ROOT, 'py_placer/plan_regions.py'), fixed,
                             '--groups', gpath, '--out', intent], capture_output=True, text=True)
    ok("plan_regions exits 0", r.returncode == 0, r.stdout[-600:] + r.stderr[-600:])
    if r.returncode != 0:
        return
    doc = json.load(open(intent))
    zones = {b['name']: b for b in doc['blocks']}
    ok("one zone per group with movable parts",
       sorted(zones) == ['autoreset', 'bridge', 'clock', 'power'], sorted(zones))
    before = open(fixed, 'rb').read()
    outs = []
    for k in (1, 2):
        out = os.path.join(tmp, f'placed{k}.kicad_pcb')
        r = subprocess.run(PY + [os.path.join(ROOT, 'py_placer/place_groups.py'), fixed, out,
                                 '--groups', gpath, '--intent', intent],
                           capture_output=True, text=True)
        ok(f"place_groups run {k} writes a board (exit 0 or 4)",
           r.returncode in (0, 4) and os.path.isfile(out), r.stdout[-800:] + r.stderr[-800:])
        outs.append(out)
    if not all(os.path.isfile(o) for o in outs):
        return
    ok("the input board is untouched", open(fixed, 'rb').read() == before)
    _check_in_zones("the loop", outs[0], zones)
    p = parse_kicad_pcb(outs[0])
    locked = sorted(r for r, f in p.footprints.items() if f.locked)
    ok("the output carries the input's locks and no others", locked == sorted(FIXED), locked)
    # The case that failed before each step named its decap owners: centre
    # packing at seed 2 put C3 (+3.3V) on the LDO's SOT-89 tab, because the
    # CH340 placed in the step before carries +3.3V too.
    c2 = os.path.join(tmp, 'placed_c2.kicad_pcb')
    subprocess.run(PY + [os.path.join(ROOT, 'py_placer/place_groups.py'), fixed, c2,
                         '--groups', gpath, '--intent', intent, '--seed', '2'],
                   capture_output=True, text=True)
    for label, b in (('seed 0', outs[0]), ('seed 2', c2)):
        r = subprocess.run(PY + [os.path.join(ROOT, 'py_router/check_drc.py'), b,
                                 '--clearance', '0.1'], capture_output=True, text=True)
        ok(f"{label}: no pad sits on another part's copper (check_drc)",
           'NO DRC VIOLATIONS' in r.stdout, r.stdout[-500:])
    q = parse_kicad_pcb(outs[1])
    same = all((round(p.footprints[r].x, 4), round(p.footprints[r].y, 4), p.footprints[r].rotation)
               == (round(q.footprints[r].x, 4), round(q.footprints[r].y, 4), q.footprints[r].rotation)
               for r in p.footprints)
    ok("two runs place every part identically", same)
    # The one-pass flow: the same regions, every group placed by one
    # place_seed run.
    one = os.path.join(tmp, 'one_pass.kicad_pcb')
    r = subprocess.run(PY + [os.path.join(ROOT, 'py_placer/place_seed.py'), fixed, one,
                             '--intent', intent], capture_output=True, text=True)
    ok("place_seed --intent <regions> writes a board (exit 0 or 4)",
       r.returncode in (0, 4) and os.path.isfile(one), r.stdout[-800:] + r.stderr[-800:])
    if os.path.isfile(one):
        _check_in_zones("one pass", one, zones)
    test_pinned_errors(outs[0], intent)


def _check_in_zones(label, board, zones):
    p = parse_kicad_pcb(board)
    bb = p.board_info.board_bounds
    inside, in_zone = [], []
    for name, b in zones.items():
        x0, y0, x1, y1 = b['zone']
        tol = b.get('tolerance_mm', 0.5) + 0.01
        for ref in b['refs']:
            f = p.footprints[ref]
            inside.append(bb[0] <= f.x <= bb[2] and bb[1] <= f.y <= bb[3])
            in_zone.append((ref, x0 - tol <= f.x <= x1 + tol and y0 - tol <= f.y <= y1 + tol))
    ok(f"{label}: every movable part is on the board", all(inside))
    ok(f"{label}: every part's origin is inside its group's zone (+ tolerance)",
       all(z for _, z in in_zone), [r for r, z in in_zone if not z])


def test_pinned_errors(board, intent):
    """A zone a FIXED part breaks is that part's own error: reported, set
    apart, and not counted against the run."""
    import place_groups
    base = place_groups.grade(board, intent, board)
    doc = json.load(open(intent))
    con1 = parse_kicad_pcb(board).footprints['CON1']
    far = [con1.x + 40.0, con1.y + 40.0, con1.x + 41.0, con1.y + 41.0]
    doc['blocks'].append({'name': 'pinned_probe', 'refs': ['CON1'], 'zone': far})
    probe = os.path.join(os.path.dirname(board), 'pinned_probe.json')
    json.dump(doc, open(probe, 'w'))
    g = place_groups.grade(board, probe, board)
    ok("a fixed part's zone error is graded as pinned",
       any('CON1' in e for e in g['floorplan_errors_pinned']), g['floorplan_errors_pinned'])
    ok("... and does not join the run's own errors",
       g['floorplan_errors'] == base['floorplan_errors'],
       (base['floorplan_errors'], g['floorplan_errors']))


def test_sheet_blocks(tmp):
    """check_floorplan --emit-intent names a hierarchical board's blocks by
    sheet; place_groups' grade must find them as place_seed does."""
    import place_groups
    board = os.path.join(ROOT, 'kicad_files', 'flat_hierarchy.kicad_pcb')
    intent = os.path.join(tmp, 'sheets.json')
    r = subprocess.run(PY + [os.path.join(ROOT, 'py_tools/check_floorplan.py'), board,
                             '--emit-intent', intent], capture_output=True, text=True)
    blocks = [b.get('name', '') for b in json.load(open(intent)).get('blocks', [])] \
        if os.path.isfile(intent) else []
    ok("the emitted intent has sheet: blocks", any(n.startswith('sheet:') for n in blocks),
       r.stdout[-400:] + r.stderr[-400:])
    g = place_groups.grade(board, intent, board)
    ok("place_groups' grade resolves sheet: blocks",
       not any('[block_unresolved]' in e for e in g['floorplan_errors']), g['floorplan_errors'])


def test_putback(tmp):
    """A grouped step declines a cap when its chip has fewer supply pins than
    caps; the put-back seats it at that chip's supply pin."""
    from kicad_parser import iter_footprint_blocks
    import math
    txt = open(BOARD).read()
    for start, end, blk, _raw, key in iter_footprint_blocks(txt):
        if key == 'C2':
            txt = txt[:start] + blk.replace('(net "Net-(C2-Pad1)")', '(net "/+3.3V")') + txt[end:]
            break
    d = os.path.join(tmp, 'putback')
    os.makedirs(d)
    src = os.path.join(d, 'src.kicad_pcb')
    open(src, 'w').write(txt)
    ok("C2 rewired to +3.3V",
       [q.net_name for q in parse_kicad_pcb(src).footprints['C2'].pads] == ['/+3.3V', 'GND'])
    fixed, log = _stage(d, src)
    if not fixed:
        ok("staged the rewired board", False, log[-600:])
        return
    groups = dict(GROUPS, groups=[
        {"name": "io", "role": "io", "refs": FIXED},
        {"name": "power", "role": "power", "refs": ["U2", "C1", "C3", "C2"], "anchor": "U2"},
        {"name": "bridge", "role": "core", "refs": ["U1", "R3", "R4"], "anchor": "U1"},
        {"name": "clock", "role": "clock", "refs": ["Y1", "C4"], "anchor": "Y1", "near": ["bridge"]},
        {"name": "autoreset", "role": "interface", "refs": ["Q1", "Q2", "R1", "R2"]}])
    gpath, base, intent = (os.path.join(d, n) for n in ('groups.json', 'base.json', 'regions.json'))
    json.dump(groups, open(gpath, 'w'))
    subprocess.run(PY + [os.path.join(ROOT, 'py_tools/check_floorplan.py'), fixed,
                         '--emit-intent', base, '--decaps-from', src], capture_output=True, text=True)
    subprocess.run(PY + [os.path.join(ROOT, 'py_placer/plan_regions.py'), fixed, '--groups', gpath,
                         '--intent', base, '--out', intent], capture_output=True, text=True)
    out, steps = os.path.join(d, 'placed.kicad_pcb'), os.path.join(d, 'steps')
    subprocess.run(PY + [os.path.join(ROOT, 'py_placer/place_groups.py'), fixed, out,
                         '--groups', gpath, '--intent', intent, '--workdir', steps,
                         '--keep-steps'], capture_output=True, text=True)
    logs = [open(os.path.join(steps, f)).read() for f in sorted(os.listdir(steps))
            if f.endswith('power.log')] if os.path.isdir(steps) else []
    ok("the power step declined C2 at the pin stage",
       any("C2: zone-packed into 'power' after the pin stage declined it" in t for t in logs),
       [ln for t in logs for ln in t.splitlines() if 'NOTE' in ln][:6])
    if not os.path.isfile(out):
        ok("place_groups wrote the rewired board", False)
        return
    p = parse_kicad_pcb(out)
    pads = [q for q in p.footprints['U2'].pads if q.net_name == '/+3.3V']
    px = sum(q.global_x for q in pads) / len(pads)
    py = sum(q.global_y for q in pads) / len(pads)
    dist = math.hypot(p.footprints['C2'].x - px, p.footprints['C2'].y - py)
    ok("the put-back seats C2 within 3 mm of U2's +3.3V pad", dist <= 3.0, f"{dist:.2f} mm")


def test_seeder_defaults():
    import inspect
    from placement import seeder
    sig = inspect.signature(seeder.seed_from_intent)
    ok("every placed owner may own a decap claim by default",
       sig.parameters['decap_owner_scope'].default is None)


def main():
    print("grouped placement")
    pcb = parse_kicad_pcb(BOARD)
    test_validate()
    test_check_groups(pcb)
    test_enumeration()
    test_cut_and_opening()
    tmp = tempfile.mkdtemp(prefix='grp_test_')
    try:
        test_trim_and_step_board(tmp)
        test_end_to_end(tmp)
        test_sheet_blocks(tmp)
        test_putback(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    test_seeder_defaults()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
