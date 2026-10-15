#!/usr/bin/env python3
"""A board outline drawn as an Edge.Cuts polygon gives both parse paths KiCad's own board outline.

KiCad draws a free-form outline as a `gr_poly` (or a footprint's `fp_poly`)
on Edge.Cuts. Its chain is closed before the points arrive, so a polygon
written with its first point repeated at the end KEEPS that point in pcbnew
(Castor_and_Pollux's faceplate), and an `(arc ...)` vertex becomes KiCad's
polyline. The pcbnew builder turned that ring into segments and skipped the
zero-length closing one; the text parser kept it, so the faceplate's
rectangle chained into a 5-vertex ring on the CLI while the GUI took the
plain-rectangle path (no ring, the bounds). An xy-only pattern also meant a
polygon with an arc vertex reached the text outline not at all. Arms:

1. **KiCad is the referee.** `BOARD.GetBoardPolygonOutlines` -- KiCad's own
   board outline -- for each fixture.
   - a rectangle written with its closing point repeated: KiCad's outline is
     4 vertices, so both paths take the rectangle path (no ring) and their
     bounds are its box;
   - a pentagon written the same way: one 5-vertex ring on both paths, its
     vertices KiCad's (to 1 um);
   - a rounded rectangle with `(arc ...)` corners: one ring on both paths,
     with the vertices pcbnew's polygon holds;
   - the same rounded rectangle as a rotated footprint's `fp_poly`, closing
     point repeated: one ring on both paths, the same vertex count.
2. **Parity.** `compare_pcb_data` reports no board-outline line on any of
   them.

Needs pcbnew; re-execs into KiCad's python automatically. Exits 2 when no
pcbnew python is found, rather than 0.

    python3 -X utf8 tests/gui_parity/test_edge_poly_outline_parity.py
"""
import glob
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.join(REPO, 'py_router'))
from kicad_locate import path_version_key  # noqa: E402
del sys.path[0]    # this file orders its own sys.path further down
KICAD_PYTHONS = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    "/usr/bin/python3",
    os.path.expandvars(r"C:\Program Files\KiCad\bin\python.exe"),
    *sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\python.exe"),
            key=path_version_key, reverse=True),
]

BASE = os.path.join(REPO, 'kicad_files', 'cap_chain.kicad_pcb')
STROKE = '(stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts")'
ROUNDED = ('(xy 3 1) (xy 33 1) (arc (start 33 1) (mid 34.414214 1.585786) (end 35 3)) '
           '(xy 35 27) (arc (start 35 27) (mid 34.414214 28.414214) (end 33 29)) '
           '(xy 3 29) (arc (start 3 29) (mid 1.585786 28.414214) (end 1 27)) '
           '(xy 1 3) (arc (start 1 3) (mid 1.585786 1.585786) (end 3 1))')
FIXTURES = [
    ('a rectangle with its closing point repeated',
     '\t(gr_poly (pts (xy 2 2) (xy 34 2) (xy 34 28) (xy 2 28) (xy 2 2)) %s)\n' % STROKE),
    ('a pentagon with its closing point repeated',
     '\t(gr_poly (pts (xy 2 2) (xy 34 2) (xy 34 20) (xy 18 30) (xy 2 20) (xy 2 2)) %s)\n'
     % STROKE),
    ('a rounded rectangle drawn with arcs', '\t(gr_poly (pts %s) %s)\n' % (ROUNDED, STROKE)),
    ("a rotated footprint's fp_poly, closing point repeated",
     '\t(footprint "outline" (layer "F.Cu") (uuid "00000000-0000-4000-8000-0000000000ab")\n'
     '\t\t(at 18 15 30)\n'
     '\t\t(property "Reference" "OL1" (at 0 0 0) (layer "F.SilkS") (hide yes)\n'
     '\t\t\t(uuid "00000000-0000-4000-8000-0000000000ac") (effects (font (size 1 1))))\n'
     '\t\t(fp_poly (pts (xy -16 -13) (xy 16 -13) (xy 16 13) (xy 0 16) (xy -16 13)'
     ' (xy -16 -13)) (stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts")\n'
     '\t\t\t(uuid "00000000-0000-4000-8000-0000000000ad"))\n'
     '\t)\n'),
]
FAILURES = []


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand == sys.executable or not os.path.exists(cand):
            continue
        if subprocess.run([cand, '-c', 'import pcbnew'],
                          capture_output=True).returncode == 0:
            argv = [cand, '-X', 'utf8', os.path.abspath(__file__)] + sys.argv[1:]
            if os.name == 'nt':
                sys.exit(subprocess.run(argv).returncode)
            os.execv(cand, argv)
    print("NOT RUN: no python with pcbnew found (exit 2, not a pass)")
    sys.exit(2)


def check(cond, what, detail=''):
    if cond:
        print('  ok    %s' % what)
    else:
        print('  FAIL  %s%s' % (what, ('  -- ' + str(detail)) if detail else ''))
        FAILURES.append(what)


def board_without_outline():
    """cap_chain with its four Edge.Cuts gr_lines removed."""
    with open(BASE, encoding='utf-8') as fh:
        text = fh.read()
    from kicad_parser import find_matching_paren
    out, pos = [], 0
    for m in re.finditer(r'\(gr_line(?=[\s(])', text):
        if m.start() < pos:
            continue
        end = find_matching_paren(text, m.start())
        if '"Edge.Cuts"' in text[m.start():end]:
            out.append(text[pos:m.start()])
            pos = end
    out.append(text[pos:])
    text = ''.join(out).rstrip()
    assert text.endswith(')')
    return text[:-1]


def kicad_outline(board):
    """KiCad's own board outline: [[(x_nm, y_nm), ...] per outer ring]."""
    import pcbnew
    ps = pcbnew.SHAPE_POLY_SET()
    if not board.GetBoardPolygonOutlines(ps, False):
        return None
    return [[(ps.Outline(i).CPoint(j).x, ps.Outline(i).CPoint(j).y)
             for j in range(ps.Outline(i).PointCount())] for i in range(ps.OutlineCount())]


def edge_poly_points(board):
    """pcbnew's polygon points of the (single) Edge.Cuts poly shape, nm."""
    import pcbnew
    items = list(board.GetDrawings()) + [g for fp in board.GetFootprints()
                                         for g in fp.GraphicalItems()]
    for d in items:
        if d.GetLayer() == pcbnew.Edge_Cuts:
            o = d.GetPolyShape().Outline(0)
            return [(o.CPoint(j).x, o.CPoint(j).y) for j in range(o.PointCount())]
    return []


def um(points):
    return sorted((round(x * 1000), round(y * 1000)) for x, y in points)


def um_nm(points):
    return sorted((round(x / 1000), round(y / 1000)) for x, y in points)


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    import pcbnew
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board, compare_pcb_data

    head = board_without_outline()
    tmp = tempfile.mkdtemp(prefix='edgepoly_')
    for i, (what, block) in enumerate(FIXTURES):
        print('\n%s' % what)
        path = os.path.join(tmp, 'edge_%d.kicad_pcb' % i)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(head + block + ')\n')
        board = pcbnew.LoadBoard(path)
        ko = kicad_outline(board)
        check(ko is not None and len(ko) == 1, 'KiCad builds one board outline', ko and len(ko))
        if not ko:
            continue
        native = edge_poly_points(board)
        fronts = (('text', parse_kicad_pcb(path)), ('pcbnew', build_pcb_data_from_board(board)))
        for name, pcb in fronts:
            bi = pcb.board_info
            rings = bi.board_outlines or []
            if i == 0:
                xs = [p[0] for p in ko[0]]
                ys = [p[1] for p in ko[0]]
                box = (min(xs) / 1e6, min(ys) / 1e6, max(xs) / 1e6, max(ys) / 1e6)
                check(len(ko[0]) == 4 and rings == [] and bi.board_bounds is not None
                      and all(abs(a - b) < 1e-6 for a, b in zip(bi.board_bounds, box)),
                      '%s path: the rectangle path (no ring), bounds = KiCad\'s box %s'
                      % (name, box), (len(ko[0]), [len(r) for r in rings], bi.board_bounds))
            elif i == 1:
                check(len(rings) == 1 and um(rings[0]) == um_nm(ko[0]),
                      '%s path: one ring with KiCad\'s %d vertices' % (name, len(ko[0])),
                      [len(r) for r in rings])
            else:
                want = len([p for k, p in enumerate(native)
                            if p != native[(k + 1) % len(native)]])
                check(len(rings) == 1 and len(rings[0]) == want,
                      '%s path: one ring with the %d vertices pcbnew\'s polygon holds'
                      % (name, want), [len(r) for r in rings])
                if i == 2 and len(rings) == 1:
                    check(um(rings[0]) == um_nm([p for k, p in enumerate(native)
                                                 if p != native[(k + 1) % len(native)]]),
                          '%s path: those vertices, to 1 um' % name)
        diffs = [d for d in compare_pcb_data(fronts[1][1], fronts[0][1]) if d.startswith('Board')]
        check(not diffs, 'compare_pcb_data reports no board-outline difference', diffs)

    print('\n%d failure(s)' % len(FAILURES))
    for fl in FAILURES:
        print('  FAILED: %s' % fl)
    if FAILURES:
        return 1
    print('PASS  Edge.Cuts polygon outlines (text vs pcbnew vs KiCad\'s board outline)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
