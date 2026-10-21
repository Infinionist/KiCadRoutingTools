#!/usr/bin/env python3
"""#1300 census: the courtyard EXCESS libraries draw, and how well
`body.courtyard_excess` predicts it.

For every footprint that draws BOTH a courtyard and a .Fab body (a real one,
not a #1201 marking), the four offsets of its courtyard bbox from bbox(fab U
pads) -- the body and copper the courtyard was drawn round -- one row per
distinct footprint NAME (a board's hundred 0402s are one library decision).
Footprint-LOCAL geometry only: no pose is read, so a board's placement
cannot enter the number.

It prints the offsets by `part_class` and by body size (the population
behind each `COURTYARD_EXCESS_*` constant), then the fit of
`body.courtyard_excess` against the drawn courtyards: the median per-side
error, the share of footprints whose estimate lies within 0.05 mm of the
drawn courtyard on every side, and the same for a flat 0.25 mm.

    python3 -X utf8 tests/measure_1300_courtyard_excess.py [board ...]

With no board it reads the tracked kicad_files/ (`corpus_boards`); the
constants' docstring cites kicad_files/ plus stress sets 1-10
(`~/Documents/kicad_stress_test/boards_unrouted_set{1..10}/*.kicad_pcb`).
A measurement tool; `run_all` does not collect `measure_*`.
"""
import os
import statistics
import sys
from collections import defaultdict

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
for _p in (ROOT_DIR, os.path.join(ROOT_DIR, 'py_router'),
           os.path.join(ROOT_DIR, 'py_placer'), TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kicad_parser import parse_kicad_pcb  # noqa: E402
from run_utils import corpus_boards  # noqa: E402
import placement.body as body  # noqa: E402
from placement.part_class import classify_part  # noqa: E402


def rows_of(paths):
    seen, rows = set(), []
    for path in paths:
        try:
            pcb = parse_kicad_pcb(path)
            bodies = body.board_bodies(pcb, path)
        except Exception as exc:                             # noqa: BLE001
            print(f"  skipped {path}: {exc}", file=sys.stderr)
            continue
        for ref, g in sorted(bodies.items()):
            if (g.source != body.SOURCE_COURTYARD
                    or g.drawn_source != body.SOURCE_FAB
                    or g.pads_local is None):
                continue
            fp = pcb.footprints[ref]
            key = str(fp.footprint_name or '').rsplit(':', 1)[-1]
            if key in seen:
                continue
            seen.add(key)
            c, d, p = g.body_local, g.drawn_local, g.pads_local
            u = (min(d[0], p[0]), min(d[1], p[1]), max(d[2], p[2]),
                 max(d[3], p[3]))
            rows.append({
                'name': key, 'fp': fp, 'ref': ref, 'union': u,
                'cls': classify_part(fp, ref).name,
                'offs': [u[0] - c[0], u[1] - c[1], c[2] - u[2], c[3] - u[3]],
            })
    return rows


def q(vals, p):
    vals = sorted(vals)
    if not vals:
        return float('nan')
    k = (len(vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def show(label, rows):
    med = [statistics.median(r['offs']) for r in rows]
    print(f"  {label:26s} n={len(rows):4d}  per-footprint median offset: "
          f"p10 {q(med, .1):.3f}  p25 {q(med, .25):.3f}  p50 {q(med, .5):.3f}"
          f"  p75 {q(med, .75):.3f}  p90 {q(med, .9):.3f}")


def fit(label, rows, excess_of):
    err, within = [], 0
    for r in rows:
        e = excess_of(r)
        d = [e - o for o in r['offs']]     # + : the estimate is larger
        err.append(statistics.median(d))
        within += max(abs(x) for x in d) <= 0.05 + 1e-9
    n = max(1, len(rows))
    print(f"  {label:26s} median per-side error {q(err, .5):+.3f} mm "
          f"(p10 {q(err, .1):+.3f}, p90 {q(err, .9):+.3f}); every side "
          f"within 0.05 mm on {within}/{len(rows)} ({100 * within / n:.0f}%)")


def main(argv):
    paths = argv or corpus_boards()
    rows = rows_of(paths)
    print(f"{len(rows)} distinct footprints draw a courtyard and a .Fab body "
          f"({len(paths)} board(s))")
    print("courtyard offset from bbox(fab U pads), mm:")
    show('all', rows)
    by = defaultdict(list)
    for r in rows:
        by[r['cls'] or 'no class'].append(r)
    for k in sorted(by, key=lambda k: -len(by[k])):
        show(f'class {k}', by[k])
    bga = [r for r in rows if 'bga' in r['name'].lower()]
    if bga:
        show('name has "bga"', bga)

    def size(r):
        u = r['union']
        return max(u[2] - u[0], u[3] - u[1])
    for lo, hi in ((0, body.COURTYARD_SMALL_BODY_MM),
                   (body.COURTYARD_SMALL_BODY_MM, 10), (10, 1e9)):
        show(f'body {lo:g}-{hi:g} mm', [r for r in rows
                                        if lo <= size(r) < hi])
    print("fit against the drawn courtyards:")
    fit('body.courtyard_excess',
        rows, lambda r: body.courtyard_excess(r['fp'], r['ref'],
                                              r['union'])[0])
    fit('flat 0.25 mm', rows, lambda r: 0.25)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
