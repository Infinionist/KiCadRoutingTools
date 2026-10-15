#!/usr/bin/env python3
"""A zone outline parses to the vertices pcbnew loads.

The counts and coordinates below were read off pcbnew 10.0.0 loading the
same text (`tests/gui_parity/test_zone_closing_vertex_parity.py` re-measures
them against pcbnew); this file runs without KiCad.

  1. A last vertex repeating the first is dropped, once -- in a KiCad 6-era
     single-line zone as in a KiCad 10 one -- and a consecutive duplicate is
     dropped; a vertex 1 nm away is kept.

    python3 tests/test_zone_outline_points.py
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), 'py_router'))

from kicad_parser import extract_zones  # noqa: E402

failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def v10_zone(pts, priority=0):
    return ('\t(zone\n\t\t(layer "F.Cu")\n\t\t(uuid "00000000-0000-4000-8000-%012d")\n'
            '\t\t(hatch edge 0.5)\n\t\t(priority %d)\n\t\t(polygon\n\t\t\t(pts\n\t\t\t\t%s\n'
            '\t\t\t)\n\t\t)\n\t)\n' % (priority, priority, pts))


def v6_zone(pts, priority=0):
    return ('  (zone (net 0) (net_name "") (layer "F.Cu") (tstamp 00000000-0000-4000-8000-%012d)'
            ' (hatch edge 0.508)\n    (priority %d)\n    (polygon\n      (pts\n        %s\n'
            '      )\n    )\n  )\n' % (priority, priority, pts))


def zones_of(text):
    return {z.priority: z.polygon for z in extract_zones(text)}


CLOSING = [
    ('closed by a repeated first vertex',
     '(xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20) (xy 10 10)', 4),
    ('open', '(xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20)', 4),
    ('closed, consecutive duplicates around it',
     '(xy 10 10) (xy 20 10) (xy 20 10) (xy 20 20) (xy 10 20) (xy 10 10) (xy 10 10)', 4),
    ("'closed' 1 nm off", '(xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20) (xy 10.000001 10)', 5),
    ('a leading duplicate', '(xy 10 10) (xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20)', 4),
]

print("1. the closing vertex, as pcbnew drops it (every file format)")
for fmt_name, make in (('KiCad 10 zone', v10_zone), ('KiCad 6 single-line zone', v6_zone)):
    text = '(kicad_pcb\n' + ''.join(make(b, i + 1) for i, (_w, b, _n) in enumerate(CLOSING)) + ')\n'
    got = zones_of(text)
    for i, (what, _b, want) in enumerate(CLOSING):
        poly = got.get(i + 1, [])
        check(f"{fmt_name}: {what} -> {want} vertices", len(poly) == want, f"{len(poly)}")
    check(f"{fmt_name}: the kept vertices are the written ones, in order",
          got.get(1) == [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0), (10.0, 20.0)], f"{got.get(1)}")

if failures:
    print(f"\nFAILED: {failures}")
    sys.exit(1)
print("\nALL PASSED")
