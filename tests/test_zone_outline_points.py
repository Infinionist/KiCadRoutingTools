#!/usr/bin/env python3
"""A zone parses to what pcbnew loads: its outline's vertices, and whether it is copper at all.

The counts and coordinates below were read off pcbnew 10.0.0 loading the
same text (`tests/gui_parity/test_zone_closing_vertex_parity.py` and
`test_zone_arc_outline_parity.py` re-measure the parser against pcbnew);
this file runs without KiCad.

  1. A last vertex repeating the first is dropped, once -- in a KiCad 6-era
     single-line zone as in a KiCad 10 one -- and a consecutive duplicate is
     dropped; a vertex 1 nm away is kept.
  2. An `(arc ...)` vertex becomes pcbnew's polyline, vertex for vertex
     (count, first points, last point and a CRC of the whole list as pcbnew
     10.0.0 held them): key_ripper's all-arc pour, a full circle, an outline
     that closes through its last arc (which pcbnew then ROTATES to start at
     an arc's start), and a mixed outline whose xy repeats the arc's end.
  3. Witnesses through the extractors: an all-arc `F&B.Cu` zone is one Zone
     per layer, not dropped, and an arc rule area reaches extract_keepouts.
  4. KiROUND rounds halves away from zero (Python's round does not).
  5. A zone on no copper layer (mask, paste, silkscreen) is not modelled;
     one listing a copper layer among others is, on that layer only.
  6. An Edge.Cuts polygon (gr_poly) keeps a repeated closing xy, as pcbnew's
     pre-closed chain does, and its zero-length closing edge is skipped: a
     rectangle written that way takes the plain-rectangle path, a pentagon is
     a 5-vertex ring; an arc ending on the first point merges it.

    python3 tests/test_zone_outline_points.py
"""
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(TESTS_DIR), 'py_router'))

import zlib  # noqa: E402

from kicad_parser import (extract_zones, extract_keepouts, zone_outline_points,  # noqa: E402
                          kicad_arc_polyline, _kiround, kicad_poly_shape_points,
                          extract_board_contours)

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

KEY_RIPPER = ('(arc (start 304.8 21.43125) (mid 304.102548 19.747452) (end 302.41875 19.05)) '
              '(arc (start 21.43125 19.05) (mid 19.747452 19.747452) (end 19.05 21.43125)) '
              '(arc (start 19.05 130.96875) (mid 19.747452 132.652548) (end 21.43125 133.35)) '
              '(arc (start 302.41875 133.35) (mid 304.102548 132.652548) (end 304.8 130.96875))')
# (outline, vertex count, crc32 of repr(list of nm tuples), first three, last) -- pcbnew 10.0.0
PCBNEW = [
    ("key_ripper's all-arc pour", KEY_RIPPER, 116, 0xee1bf109,
     [(304800000, 21431250), (304799495, 21361978), (304791437, 21223668)], (304800000, 130968750)),
    ('a full circle and a point', '(arc (start 50 50) (mid 60 50) (end 50 50)) (xy 40 40)', 160,
     0xa4045e20, [(50000000, 50000000), (50000501, 49899946), (50008507, 49699998)],
     (40000000, 40000000)),
    ('an outline closed through its last arc, rotated',
     '(arc (start 174.962 117.833) (mid 174.962 117.833) (end 174.963 117.834)) '
     '(arc (start 174.963 117.834) (mid 174.962 117.835) (end 174.962 117.835)) '
     '(arc (start 174.962 117.835) (mid 174.961 117.835) (end 174.961 117.834)) '
     '(arc (start 174.961 117.834) (mid 174.961 117.833) (end 174.962 117.833))', 12, 0x9ddd5b68,
     [(174961000, 117834000), (174960820, 117833782), (174960820, 117833218)],
     (174960820, 117834218)),
    ("xy, an arc, and an xy on the arc's end",
     '(xy 10 10) (xy 20 10) (arc (start 20 10) (mid 22.5 12.5) (end 20 15)) (xy 20 15) (xy 10 15)',
     60, 0x72f44bf4, [(10000000, 10000000), (20000000, 10000000), (20070129, 10000492)],
     (10000000, 15000000)),
]

print("2. arc vertices, as pcbnew holds them")
for what, body, n, crc, first, last in PCBNEW:
    pts = [(_kiround(x * 1e6), _kiround(y * 1e6)) for x, y in zone_outline_points(body)]
    check(f"{what}: {n} vertices, the same list", len(pts) == n and pts[:3] == first
          and pts[-1] == last and zlib.crc32(repr(pts).encode()) == crc,
          f"{len(pts)} vertices, first {pts[:3]}, last {pts[-1:]}, "
          f"crc {zlib.crc32(repr(pts).encode()):08x}")
check("a 90-degree, 2.38 mm arc is 29 points (1 um max error)",
      len(kicad_arc_polyline((304800000, 21431250), (304102548, 19747452),
                             (302418750, 19050000))) == 29)

print("3. through the extractors")
text = ('(kicad_pcb\n\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal))\n'
        '\t(zone\n\t\t(net 1)\n\t\t(net_name "GND")\n\t\t(layers "F&B.Cu")\n'
        '\t\t(uuid "00000000-0000-4000-8000-000000000001")\n'
        '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t%s\n\t\t\t)\n\t\t)\n\t)\n'
        '\t(zone\n\t\t(layer "F.Cu")\n\t\t(uuid "00000000-0000-4000-8000-000000000002")\n'
        '\t\t(keepout (tracks not_allowed) (vias not_allowed) (copperpour not_allowed))\n'
        '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t%s\n\t\t\t)\n\t\t)\n\t)\n)\n'
        % (KEY_RIPPER, PCBNEW[1][1]))
zones = extract_zones(text)
check("the all-arc F&B.Cu pour is one Zone per layer, 116 vertices each",
      sorted((z.layer, len(z.polygon)) for z in zones) == [('B.Cu', 116), ('F.Cu', 116)],
      f"{[(z.layer, len(z.polygon)) for z in zones]}")
ko = extract_keepouts(text)
check("the arc rule area reaches extract_keepouts with pcbnew's 160 vertices",
      len(ko) == 1 and len(ko[0]['polygon']) == 160, f"{[len(k['polygon']) for k in ko]}")

print("4. KiROUND")
check("halves round away from zero, unlike Python's round",
      (_kiround(2.5), _kiround(-2.5), _kiround(3.5), _kiround(0.49999999999999994))
      == (3, -3, 4, 0))
check("it clamps to the int range like KiROUND<double, int>",
      _kiround(3e9) == 2 ** 31 - 1 and _kiround(-3e9) == -2 ** 31)

print("5. a zone on no copper layer is not copper")
text = '(kicad_pcb\n\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal))\n' + ''.join(
    '\t(zone\n\t\t(net 1)\n\t\t(net_name "GND")\n\t\t%s\n'
    '\t\t(uuid "00000000-0000-4000-8000-%012d")\n\t\t(priority %d)\n'
    '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t(xy 0 0) (xy 1 0) (xy 1 1)\n\t\t\t)\n\t\t)\n\t)\n'
    % (clause, i, i) for i, clause in enumerate(
        ['(layers "*.Mask")', '(layers "F.Mask" "F.Paste")', '(layer "F.SilkS")',
         '(layer "B.Mask")', '(layers "F.Cu" "F.Mask")', '(layer "B.Cu")'])) + ')\n'
got = sorted((z.priority, z.layer) for z in extract_zones(text))
check("only the copper layers are modelled: F.Cu of the mixed zone, and B.Cu",
      got == [(4, 'F.Cu'), (5, 'B.Cu')], f"{got}")

print("6. an Edge.Cuts polygon")
check("a repeated closing xy is kept in a polygon shape (pcbnew holds 5 points)",
      len(kicad_poly_shape_points('(xy 2 2) (xy 34 2) (xy 34 28) (xy 2 28) (xy 2 2)')) == 5)
check("an arc ending on the first point merges it (the chain is already closed)",
      kicad_poly_shape_points('(xy 3 1) (xy 33 1) (arc (start 33 1) (mid 34 2) (end 33 3)) '
                              '(arc (start 33 3) (mid 18 20) (end 3 1))')[-1] != (3.0, 1.0))
edge = '(kicad_pcb\n\t(gr_poly (pts %s) (stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))\n)\n'
outers, cuts = extract_board_contours(edge % '(xy 2 2) (xy 34 2) (xy 34 28) (xy 2 28) (xy 2 2)')
check("a rectangle with its closing point repeated takes the plain-rectangle path",
      outers == [] and cuts == [], f"{[len(o) for o in outers]}")
outers, cuts = extract_board_contours(
    edge % '(xy 2 2) (xy 34 2) (xy 34 20) (xy 18 30) (xy 2 20) (xy 2 2)')
check("a pentagon with its closing point repeated is one 5-vertex ring",
      [len(o) for o in outers] == [5], f"{[len(o) for o in outers]}")

if failures:
    print(f"\nFAILED: {failures}")
    sys.exit(1)
print("\nALL PASSED")
