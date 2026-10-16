#!/usr/bin/env python3
"""Mounting holes nobody locked, on a pile: a default pose at the corners.

`placement/mount_holes.py` gives each unlocked hole of a pile with four
holes the legal pose nearest a free board corner; `plan_regions.py` writes it
as a `fixed_poses[]` row of basis `derived_default`, plans the regions around
it, and zones the hole in no block; `place_groups.py` seats it in the first
step. This holds:

* on glasgow_revC piled, each of the four holes sits by its own corner;
* a hole locked at a corner keeps that corner, and the others take the rest;
* a placed board, and a board without four holes, get nothing;
* on tigard piled, `plan_regions` writes the four rows, which load, plans no
  region over a hole, leaves the holes out of every block, warns, and
  `--no-mount-holes` writes none;
* `place_groups` seats the holes in its first step, at their poses, even
  when their group is placed last.

    python3 tests/test_mount_holes.py
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
from placement.mount_holes import corner_hole_poses, mount_holes  # noqa: E402

FAILS = []
PY = [sys.executable, '-X', 'utf8']
PILE = os.path.join(ROOT, '.claude', 'skills', 'pcb-free-agent', 'scripts',
                    'make_unplaced.py')


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILS.append(what)


def run(args, **kw):
    return subprocess.run(PY + args, capture_output=True, text=True, cwd=ROOT, **kw)


def pile(name, tmp):
    out = os.path.join(tmp, f"{name}_pile.kicad_pcb")
    r = run([PILE, os.path.join(ROOT, 'kicad_files', f"{name}.kicad_pcb"), out])
    if not os.path.isfile(out):
        raise SystemExit(f"BROKEN TEST: make_unplaced wrote no pile for {name} "
                         f"(exit {r.returncode}): {r.stderr[-300:]}")
    return out


def corner_of(pcb, name):
    x0, y0, x1, y1 = pcb.board_info.board_bounds
    return {'top-left': (x0, y0), 'top-right': (x1, y0),
            'bottom-right': (x1, y1), 'bottom-left': (x0, y1)}[name]


def by_corner(tmp):
    path = pile('glasgow_revC', tmp)
    pcb = parse_kicad_pcb(path)
    poses, notes = corner_hole_poses(pcb, path)
    near = [p for p in poses
            if max(abs(p['x'] - corner_of(pcb, p['corner'])[0]),
                   abs(p['y'] - corner_of(pcb, p['corner'])[1])) <= 7.0]
    check(len(poses) == 4 and len({p['corner'] for p in poses}) == 4
          and len(near) == 4 and not notes,
          f"glasgow_revC piled: each hole within 7 mm of its own corner "
          f"({[(p['ref'], p['corner'], p['x'], p['y']) for p in poses]})")

    # Lock MK3 at the top-right corner: it keeps it, the others take the rest.
    locked = os.path.join(tmp, 'glasgow_mk3.kicad_pcb')
    x0, y0, x1, y1 = pcb.board_info.board_bounds
    r = run([os.path.join(ROOT, 'py_placer', 'place_pose.py'), path, locked,
             'set', 'MK3', f"{x1 - 6:.3f}", f"{y0 + 6:.3f}", 'lock', 'MK3'])
    if not os.path.isfile(locked):
        raise SystemExit(f"BROKEN TEST: place_pose wrote nothing (exit "
                         f"{r.returncode}): {r.stderr[-300:]}")
    pl = parse_kicad_pcb(locked)
    poses, _ = corner_hole_poses(pl, locked)
    check(sorted(p['ref'] for p in poses) == ['MK1', 'MK2', 'MK4']
          and 'top-right' not in {p['corner'] for p in poses},
          f"MK3 locked at the top-right corner keeps it; the others take the "
          f"rest ({[(p['ref'], p['corner']) for p in poses]})")
    check(corner_hole_poses(pl, locked, claimed={'MK1', 'MK2', 'MK4'}) == ([], []),
          "a hole the intent already places is not this default's")


def nothing_when_placed():
    for name in ('glasgow_revC', 'tigard'):
        path = os.path.join(ROOT, 'kicad_files', f"{name}.kicad_pcb")
        pcb = parse_kicad_pcb(path)
        check(len(mount_holes(pcb)) == 4 and corner_hole_poses(pcb, path) == ([], []),
              f"{name} as placed (not a pile): four holes, no default")
    path = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
    check(corner_hole_poses(parse_kicad_pcb(path), path) == ([], []),
          "esp_prog (no holes): nothing")


def plan_and_place(tmp):
    path = pile('tigard', tmp)
    pcb = parse_kicad_pcb(path)
    holes = mount_holes(pcb)
    refs = sorted(pcb.footprints)
    rest = [r for r in refs if r not in holes]
    groups = os.path.join(tmp, 'groups.json')
    # The holes' group is placed LAST: half the parts first, then the rest.
    json.dump({'schema': 1, 'groups': [
        {'name': 'a', 'refs': rest[:len(rest) // 2]},
        {'name': 'b', 'refs': rest[len(rest) // 2:] + holes}],
        'order': ['a', 'b']}, open(groups, 'w'))
    intent = os.path.join(tmp, 'intent.json')
    run([os.path.join(ROOT, 'py_tools', 'check_floorplan.py'), path,
         '--emit-intent', intent, '--allow-unplaced'])
    if not os.path.isfile(intent):
        raise SystemExit("BROKEN TEST: check_floorplan --emit-intent wrote nothing")
    out = {}
    for flag in ([], ['--no-mount-holes']):
        reg = os.path.join(tmp, f"regions{len(flag)}.json")
        r = run([os.path.join(ROOT, 'py_placer', 'plan_regions.py'), path,
                 '--groups', groups, '--intent', intent, '--out', reg] + flag)
        if not os.path.isfile(reg):
            raise SystemExit(f"BROKEN TEST: plan_regions {flag} wrote nothing "
                             f"(exit {r.returncode}): {r.stderr[-300:]}")
        out[len(flag)] = (json.load(open(reg)), r.stdout, reg)
    doc, stdout, reg = out[0]
    rows = [f for f in doc.get('fixed_poses') or [] if f.get('basis') == 'derived_default']
    check(sorted(f['ref'] for f in rows) == holes,
          f"plan_regions writes a derived_default fixed pose per hole ({len(rows)})")
    in_blocks = {r for b in doc.get('blocks') or [] for r in b.get('refs') or []}
    check(not in_blocks & set(holes), "the holes are zoned in no block")
    # A region may span a fixed part (it is then simply larger); what the
    # plan must do is size the regions on the area the holes leave free.
    free = [sum(b['context']['grouped_placement']['free_mm2']
                for b in d.get('blocks') or [] if (b.get('context') or {}).get(
                    'grouped_placement')) for d in (doc, out[1][0])]
    check(free[0] < free[1] - 100,
          f"the regions are planned around the holes: {free[0]:.0f} mm2 free "
          f"against {free[1]:.0f} without them")
    check('WARN  mounting hole(s)' in stdout and 'mount_holes_at_corners' in stdout,
          "plan_regions says the holes were not locked")
    from placement import floorplan
    try:
        floorplan.load_intent(reg)
        loads = True
    except Exception as ex:                                # noqa: BLE001
        loads = str(ex)
    check(loads is True and int(doc.get('min_reader') or 0) >= 7,
          f"the intent loads (min_reader {doc.get('min_reader')}): {loads}")
    check(not out[1][0].get('fixed_poses') and 'WARN  mounting hole' not in out[1][1],
          "--no-mount-holes writes no pose")

    placed = os.path.join(tmp, 'placed.kicad_pcb')
    r = run([os.path.join(ROOT, 'py_placer', 'place_groups.py'), path, placed,
             '--groups', groups, '--intent', reg], timeout=900)
    if not os.path.isfile(placed):
        raise SystemExit(f"BROKEN TEST: place_groups wrote nothing (exit "
                         f"{r.returncode}): {r.stderr[-400:]}")
    got = parse_kicad_pcb(placed)
    at = [f['ref'] for f in rows
          if abs(got.footprints[f['ref']].x - f['x']) < 1e-3
          and abs(got.footprints[f['ref']].y - f['y']) < 1e-3]
    check("a fixed pose, seated in the first step ('a')" in r.stdout
          and sorted(at) == holes,
          f"place_groups seats the holes in its first step, at their poses "
          f"({len(at)}/4)")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        by_corner(tmp)
        nothing_when_placed()
        plan_and_place(tmp)
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
