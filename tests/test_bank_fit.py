#!/usr/bin/env python3
"""An indicator bank is written as rows its region can hold.

`plan_regions.fit_banks` asks of each bank (`placement/group_rows.py`)
whether its row -- at the seeder's own pitch (`seeder.row_block`) -- fits the
free space of its zone (`regions.FitChecker`: the plan's grid, the locked
parts and the mounting holes' poses taken out); if not, `split_bank` lays it
as the fewest rows that fit side by side, and a bank no split fits is left
out rather than written as a row nothing can seat. This holds:

* `split_bank`: whole when it fits; split in order, balanced to within one
  channel, every LED beside its resistor and every row two channels or
  more; [] when nothing fits;
* on glasgow_revC piled, its connectors and SW1 locked where the human put
  them and its schematic sheets as the groups (the benchmark of
  docs/grouped-placement.md): each sheet's front-face region is sized for
  its front-face parts, IO_Banks' a 48 mm strip beside J4/J5 that holds its
  16.5 mm row whole, so both banks are written whole; the seed forms both
  rows, the four mounting holes are seated at their corner poses, and the
  board is buildable;
* in IO_Banks' zone as it was planned when the back-face parts shared the
  front-face region -- the strip the J3/J4 courtyards cut -- the bank is laid
  as two rows side by side (3+2 channels).

    python3 tests/test_bank_fit.py
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
from placement.group_rows import split_bank  # noqa: E402

FAILS = []
PY = [sys.executable, '-X', 'utf8']
FIXED = ['J1', 'J10', 'J2', 'J3', 'J4', 'J5', 'J6', 'J7', 'J8', 'J9', 'SW1']


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILS.append(what)


def run(args, **kw):
    return subprocess.run(PY + args, capture_output=True, text=True, cwd=ROOT, **kw)


def need(path, r, what):
    if not os.path.isfile(path):
        raise SystemExit(f"BROKEN TEST: {what} wrote nothing (exit "
                         f"{r.returncode}): {(r.stderr or r.stdout)[-400:]}")


def units():
    row = {'name': 'leds:z:D1', 'members': [m for i in range(1, 8)
                                           for m in (f"D{i}", f"R{i}")],
           'order': 'declared'}
    check(split_bank(row, lambda rows: True) == [row], "a bank that fits stays whole")
    got = split_bank(row, lambda rows: max(len(r) for r in rows) <= 6)
    check([len(r['members']) // 2 for r in got] == [3, 2, 2]
          and [r['name'] for r in got] == ['leds:z:D1', 'leds:z:D4', 'leds:z:D6']
          and [m for r in got for m in r['members']] == row['members'],
          f"7 channels, rows of 3 at most: 3+2+2, in order, named by their "
          f"first LED ({[r['name'] for r in got]})")
    check(all(r['members'][i + 1] == 'R' + r['members'][i][1:]
              for r in got for i in range(0, len(r['members']), 2)),
          "every LED beside its own resistor")
    check(split_bank(row, lambda rows: max(len(r) for r in rows) <= 2) == [],
          "rows of one channel are not a bank: []")
    check(split_bank(row, lambda rows: False) == [], "nothing fits: []")


def glasgow(tmp):
    src = os.path.join(ROOT, 'kicad_files', 'glasgow_revC.kicad_pcb')
    human = parse_kicad_pcb(src)
    pile = os.path.join(tmp, 'pile.kicad_pcb')
    r = run([os.path.join(ROOT, '.claude', 'skills', 'pcb-free-agent', 'scripts',
                          'make_unplaced.py'), src, pile])
    need(pile, r, 'make_unplaced')
    fixed = os.path.join(tmp, 'fixed.kicad_pcb')
    args = []
    for ref in FIXED:
        fp = human.footprints[ref]
        args += ['set', ref, f"{fp.x:.4f}", f"{fp.y:.4f}", '--rot', f"{fp.rotation or 0:g}"]
    r = run([os.path.join(ROOT, 'py_placer', 'place_pose.py'), pile, fixed]
            + args + ['lock'] + FIXED)
    need(fixed, r, 'place_pose')
    intent = os.path.join(tmp, 'intent.json')
    r = run([os.path.join(ROOT, 'py_tools', 'check_floorplan.py'), fixed,
             '--emit-intent', intent])
    need(intent, r, 'check_floorplan --emit-intent')
    from placement.grouping import BoardView
    view = BoardView(parse_kicad_pcb(fixed))
    sheets = {}
    for ref in view.parts:
        sheets.setdefault(human.footprints[ref].sheet_name or 'main', []).append(ref)
    groups = os.path.join(tmp, 'groups.json')
    json.dump({'schema': 1, 'groups': [
        {'name': n.lower(), 'role': 'core' if n == 'main' else 'interface',
         'refs': sorted(refs)} for n, refs in sorted(sheets.items())]},
        open(groups, 'w'))
    regions = os.path.join(tmp, 'regions.json')
    r = run([os.path.join(ROOT, 'py_placer', 'plan_regions.py'), fixed,
             '--groups', groups, '--intent', intent, '--out', regions])
    need(regions, r, 'plan_regions')
    doc = json.load(open(regions))
    rows = {a['name']: a['members'] for a in doc.get('arrays') or []}
    check(sorted(rows) == ['leds:io_banks:D6', 'leds:main:D1']
          and len(rows['leds:main:D1']) == 10
          and rows['leds:io_banks:D6']
          == ['D6', 'R11', 'D7', 'R20', 'D8', 'R21', 'D9', 'R22', 'D10', 'R23'],
          f"IO_Banks and main each one whole row ({sorted(rows)})")
    cut_zone(fixed, doc)
    holes = {f['ref']: f for f in doc.get('fixed_poses') or []}
    check(sorted(holes) == ['MK1', 'MK2', 'MK3', 'MK4'],
          f"the four mounting holes get corner poses ({sorted(holes)})")

    placed = os.path.join(tmp, 'placed.kicad_pcb')
    r = run([os.path.join(ROOT, 'py_placer', 'place_seed.py'), fixed, placed,
             '--intent', regions, '--seed', '0'], timeout=900)
    need(placed, r, 'place_seed')
    summ = next((json.loads(ln[len('JSON_SUMMARY: '):]) for ln in r.stdout.splitlines()
                 if ln.startswith('JSON_SUMMARY: ')), {})
    check(sorted(summ.get('arrays_formed') or {}) == sorted(rows)
          and not summ.get('array_unseated'),
          f"the seed forms every row (formed {sorted(summ.get('arrays_formed') or {})}, "
          f"unseated {sorted(summ.get('array_unseated') or {})})")
    got = parse_kicad_pcb(placed)
    at = [h for h, f in holes.items()
          if abs(got.footprints[h].x - f['x']) < 1e-3 and abs(got.footprints[h].y - f['y']) < 1e-3]
    check(len(at) == 4, f"the holes are seated at their poses ({sorted(at)})")
    a = run([os.path.join(ROOT, 'py_tools', 'check_assembly.py'), placed,
             '--baseline', fixed])
    verdict = next((ln.split('VERDICT:', 1)[1].strip() for ln in a.stdout.splitlines()
                    if 'VERDICT:' in ln), '')
    check(verdict.startswith('buildable'), f"check_assembly: {verdict!r}")


#: IO_Banks' zone when its group's back-face parts were planned into its
#: front-face region: a 6.9 mm wide strip whose length J3 and J4 cut.
CUT_ZONE = [82.802, 94.55, 89.702, 119.5]


def cut_zone(fixed, doc):
    """The planned intent with IO_Banks' zone set to `CUT_ZONE`: its bank
    is laid as two rows side by side."""
    import copy
    from plan_regions import fit_banks, indicator_banks
    intent = copy.deepcopy(doc)
    intent.pop('arrays', None)
    for b in intent['blocks']:
        if b['name'] == 'io_banks':
            b['zone'] = list(CUT_ZONE)
    pcb = parse_kicad_pcb(fixed)
    holes = list(doc.get('fixed_poses') or [])
    banks, _dropped = indicator_banks(pcb, intent)
    rows, notes = fit_banks(pcb, fixed, intent, banks, holes)
    got = {a['name']: a['members'] for a in rows}
    check(sorted(got) == ['leds:io_banks:D6', 'leds:io_banks:D9', 'leds:main:D1']
          and got['leds:io_banks:D6'] + got['leds:io_banks:D9']
          == ['D6', 'R11', 'D7', 'R20', 'D8', 'R21', 'D9', 'R22', 'D10', 'R23']
          and len(got['leds:io_banks:D6']) == 6,
          f"in the zone J3/J4 cut, IO_Banks as two rows (3+2 channels) "
          f"({sorted(got)})")
    check(any('its region holds it only as 2 rows side by side' in n for n in notes),
          f"and fit_banks says the bank was split ({notes})")


def main():
    units()
    with tempfile.TemporaryDirectory() as tmp:
        glasgow(tmp)
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
