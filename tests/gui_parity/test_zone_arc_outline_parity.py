#!/usr/bin/env python3
"""Zone and rule-area outlines drawn with `(arc ...)` vertices parse to pcbnew's own polyline, to the nanometre, on both fronts.

KiCad writes a rounded zone outline as `(arc (start ..) (mid ..) (end ..))`
vertices among its `(xy ..)` points and hands every consumer the polyline
`SHAPE_LINE_CHAIN::Append(SHAPE_ARC)` builds. `build_pcb_data_from_board`
reads that polyline from live pcbnew; `parse_kicad_pcb` read only `(xy ..)`,
so an all-arc outline gave its zone no points (dropped -- key_ripper's
`F&B.Cu` GND pour) and a mixed one lost its arcs. `zone_outline_points` now
ports KiCad's conversion. Arcs:

1. **Random outlines** (seeded): radii 1 um .. 500 mm, sweeps 0.05 .. 359.9
   degrees both ways, on the nm, 10 nm and um grids, a lone arc, a chain of
   arcs, arcs mixed with `xy` points (some on the arc's own end), and KiCad's
   rounded rectangle. Each zone's vertices from BOTH parse paths must equal,
   in integer nm, the vertices pcbnew holds -- for the file as written, and
   again after pcbnew SAVED it (KiCad's own arc serialisation).
2. **Degenerate arcs**: a full circle, an outline closed through an arc or
   by an `xy` repeating the first point (pcbnew drops it), a collinear mid, a
   mid within the error band of the chord, a huge radius, a sub-um arc,
   three equal points, whitespace and point order KiCad's reader accepts.
3. **Witnesses**: a two-layer (`F&B.Cu`) all-arc zone gives one Zone per
   layer with pcbnew's vertex count on both paths, and a rule area drawn with
   arcs reaches `board_info.keepouts` with pcbnew's vertices. A parser that
   reads only `xy` fails both.

Needs pcbnew; re-execs into KiCad's python automatically. Exits 2 when no
pcbnew python is found, rather than 0.

    python3 -X utf8 tests/gui_parity/test_zone_arc_outline_parity.py
"""
import glob
import math
import os
import random
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
N_RANDOM = 1500
PRIORITY0 = 9001
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


def fmt(v_nm):
    return ('%.6f' % (v_nm / 1e6)).rstrip('0').rstrip('.')


def arc_tok(cx, cy, r, a0, sweep, grid):
    pts = []
    for t in (a0, a0 + sweep / 2, a0 + sweep):
        pts.append((round((cx + r * math.cos(math.radians(t))) / grid) * grid,
                    round((cy + r * math.sin(math.radians(t))) / grid) * grid))
    return pts, '(arc (start %s %s) (mid %s %s) (end %s %s))' % tuple(
        fmt(v) for p in pts for v in p)


def random_bodies(n, seed=1164):
    rng = random.Random(seed)
    bodies = []
    for _ in range(n):
        grid = rng.choice([1, 1, 10, 1000])
        cx, cy = rng.uniform(20e6, 200e6), rng.uniform(20e6, 200e6)
        r = math.exp(rng.uniform(math.log(1e3), math.log(5e8)))
        kind = rng.choice(['single', 'chain', 'mixed', 'mixed', 'rounded_rect'])
        toks = []
        if kind == 'single':
            sweep = rng.choice([-1, 1]) * math.exp(rng.uniform(math.log(0.05), math.log(359.9)))
            toks.append(arc_tok(cx, cy, r, rng.uniform(-180, 180), sweep, grid)[1])
        elif kind == 'chain':
            a, d = rng.uniform(-180, 180), rng.choice([-1, 1])
            for _k in range(rng.randint(2, 5)):
                sweep = d * rng.uniform(1, 100)
                toks.append(arc_tok(cx, cy, r * rng.uniform(0.5, 1.5), a, sweep, grid)[1])
                a += sweep + d * rng.uniform(0, 20)
        elif kind == 'mixed':
            a, d = rng.uniform(-180, 180), rng.choice([-1, 1])
            for _k in range(rng.randint(1, 4)):
                sweep = d * rng.uniform(0.2, 120)
                p, t = arc_tok(cx, cy, r, a, sweep, grid)
                toks.append(t)
                a += sweep
                if rng.random() < 0.7:
                    q = p[2] if rng.random() < 0.3 else (
                        p[2][0] + rng.uniform(-1, 1) * r * 0.3,
                        p[2][1] + rng.uniform(-1, 1) * r * 0.3)
                    toks.append('(xy %s %s)' % (fmt(round(q[0] / grid) * grid),
                                                fmt(round(q[1] / grid) * grid)))
        else:
            w, h, rr = r * rng.uniform(1, 2.5), r * rng.uniform(1, 2.5), r * rng.uniform(0.05, 0.5)
            for ccx, ccy, a0 in ((cx + w - rr, cy + rr, -90), (cx + w - rr, cy + h - rr, 0),
                                 (cx + rr, cy + h - rr, 90), (cx + rr, cy + rr, 180)):
                toks.append(arc_tok(ccx, ccy, rr, a0, 90, grid)[1])
        bodies.append(' '.join(toks))
    return bodies


DEGENERATE = [
    # a full circle: start == end, so the outline closes on itself
    '(arc (start 50 50) (mid 60 50) (end 50 50)) (xy 40 40)',
    '(xy 40 40) (arc (start 50 50) (mid 60 50) (end 50 50))',
    # an outline that closes through its last arc, and one closed by an xy
    '(xy 10 10) (xy 20 10) (xy 20 20) (arc (start 15 20) (mid 11.464466 18.535534) (end 10 15))',
    '(arc (start 10 10) (mid 15 5) (end 20 10)) (xy 20 20) (arc (start 20 20) (mid 15 25) (end 10 20)) (xy 10 10)',
    '(xy 10 10) (arc (start 20 10) (mid 25 10) (end 30 10)) (xy 30 20)',
    '(xy 10 10) (arc (start 20 10) (mid 25 10.0000001) (end 30 10)) (xy 30 20)',
    '(xy 10 10) (arc (start 20 10) (mid 25 10.0004) (end 30 10)) (xy 30 20)',
    '(xy 10 10) (arc (start 20 10) (mid 25 10.0006) (end 30 10)) (xy 30 20)',
    '(xy 10 10) (arc (start 20 10) (mid 20.0003 10.0003) (end 20.0006 10)) (xy 30 20)',
    '(xy 10 10) (arc (start 20 10) (mid 20.000001 10.000001) (end 20.000002 10)) (xy 30 20)',
    '(xy 10 10) (arc (start 20 10) (mid 20 10) (end 20 10)) (xy 30 20)',
    '(arc (start 10 0) (mid 7.071068 7.071068) (end 0 10)) (xy 0 0)',
    '(arc  (start 10 0)\n (mid 7.071068 7.071068)  (end 0 10) ) (xy 0 0 )',
    '(arc (end 0 10) (start 10 0) (mid 7.071068 7.071068)) (xy 0 0)',
    '(arc (start 110 100) (mid 100 110) (end 90 100)) (arc (start 90 100) (mid 100 90) (end 110 101))',
]

# key_ripper's GND pour: four 90-degree corner arcs on F&B.Cu, nothing else.
TWO_LAYER = '''\t(zone
\t\t(net "DPA_P")
\t\t(layers "F&B.Cu")
\t\t(uuid "0000aaaa-0000-4000-8000-000000000001")
\t\t(hatch edge 0.508)
\t\t(connect_pads (clearance 0.508))
\t\t(min_thickness 0.254)
\t\t(fill yes (thermal_gap 0.508) (thermal_bridge_width 0.508))
\t\t(polygon
\t\t\t(pts
\t\t\t\t(arc (start 304.8 21.43125) (mid 304.102548 19.747452) (end 302.41875 19.05))
\t\t\t\t(arc (start 21.43125 19.05) (mid 19.747452 19.747452) (end 19.05 21.43125))
\t\t\t\t(arc (start 19.05 130.96875) (mid 19.747452 132.652548) (end 21.43125 133.35))
\t\t\t\t(arc (start 302.41875 133.35) (mid 304.102548 132.652548) (end 304.8 130.96875))
\t\t\t)
\t\t)
\t)
'''

RULE_AREA = '''\t(zone
\t\t(layer "F.Cu")
\t\t(uuid "0000aaaa-0000-4000-8000-000000000002")
\t\t(hatch edge 0.5)
\t\t(connect_pads (clearance 0))
\t\t(min_thickness 0.25)
\t\t(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))
\t\t(fill (thermal_gap 0.5) (thermal_bridge_width 0.5))
\t\t(polygon
\t\t\t(pts
\t\t\t\t(arc (start 60 50) (mid 50 60) (end 40 50)) (arc (start 40 50) (mid 50 40) (end 60 49.9))
\t\t\t)
\t\t)
\t)
'''


def zone_block(i, body):
    return ('\t(zone\n\t\t(layer "F.Cu")\n\t\t(uuid "00000000-0000-4000-8000-%012d")\n'
            '\t\t(hatch edge 0.5)\n\t\t(priority %d)\n\t\t(connect_pads\n\t\t\t(clearance 0.5)\n\t\t)\n'
            '\t\t(min_thickness 0.25)\n\t\t(fill\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n\t\t)\n'
            '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t%s\n\t\t\t)\n\t\t)\n\t)\n' % (i, PRIORITY0 + i, body))


def write_board(path, bodies, extra=''):
    with open(BASE, encoding='utf-8') as fh:
        text = fh.read().rstrip()
    assert text.endswith(')')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text[:-1] + ''.join(zone_block(i, b) for i, b in enumerate(bodies))
                 + extra + ')\n')


def nm(poly):
    from kicad_parser import _kiround
    return [(_kiround(x * 1e6), _kiround(y * 1e6)) for x, y in poly]


def native(board):
    """{priority: [(x_nm, y_nm)]} of Outline(0) for every zone pcbnew holds."""
    out = {}
    for z in board.Zones():
        o = z.Outline()
        c = o.Outline(0) if o.OutlineCount() else None
        out.setdefault(int(z.GetAssignedPriority()), []).append(
            [(c.CPoint(j).x, c.CPoint(j).y) for j in range(c.PointCount())] if c else [])
    return out


def compare_board(label, path, n):
    import pcbnew
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board
    board = pcbnew.LoadBoard(path)
    want = native(board)
    paths = (('text', parse_kicad_pcb(path)), ('pcbnew', build_pcb_data_from_board(board)))
    for name, pcb in paths:
        got = {}
        for z in pcb.zones:
            if z.priority >= PRIORITY0:
                got.setdefault(z.priority, []).append(nm(z.polygon))
        missing = [i for i in range(n) if PRIORITY0 + i not in got]
        bad = [i for i in range(n) if PRIORITY0 + i in got
               and got[PRIORITY0 + i] != want.get(PRIORITY0 + i)]
        nv = sum(len(p) for i in range(n) for p in want.get(PRIORITY0 + i, []))
        detail = ''
        if bad:
            i = bad[0]
            w, g = want[PRIORITY0 + i][0], got[PRIORITY0 + i][0]
            detail = 'outline %d: pcbnew %d vertices, %s %d; first difference %s' % (
                i, len(w), name, len(g),
                next(((a, b) for a, b in zip(w, g) if a != b), 'in length'))
        check(not missing and not bad,
              '%s, %s path: %d outlines (%d vertices) equal pcbnew to the nm'
              % (label, name, n, nv), detail or (missing[:5] and 'dropped: %s' % missing[:5]))
    return board


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    import pcbnew
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board

    tmp = tempfile.mkdtemp(prefix='zarc_')

    print('Random arc outlines')
    bodies = random_bodies(N_RANDOM)
    a = os.path.join(tmp, 'random.kicad_pcb')
    write_board(a, bodies)
    board = compare_board('as written', a, len(bodies))
    b = os.path.join(tmp, 'random_saved.kicad_pcb')
    pcbnew.SaveBoard(b, board)
    with open(b, encoding='utf-8') as fh:
        n_arcs = fh.read().count('(arc')
    check(n_arcs > N_RANDOM, 'pcbnew saved the outlines with their arcs (%d)' % n_arcs)
    compare_board('as pcbnew saved them', b, len(bodies))

    print('\nDegenerate arcs')
    d = os.path.join(tmp, 'degenerate.kicad_pcb')
    write_board(d, DEGENERATE)
    compare_board('degenerate', d, len(DEGENERATE))

    print('\nWitnesses')
    w = os.path.join(tmp, 'witness.kicad_pcb')
    write_board(w, [], TWO_LAYER + RULE_AREA)
    board = pcbnew.LoadBoard(w)
    pours = [z for z in board.Zones() if not z.GetIsRuleArea()]
    rules = [z for z in board.Zones() if z.GetIsRuleArea()]
    pc = pours[0].Outline().Outline(0)
    want_pour = [[(pc.CPoint(j).x, pc.CPoint(j).y) for j in range(pc.PointCount())]]
    check(len(pours) == 1 and len(want_pour[0]) == 116,
          'pcbnew holds the F&B.Cu all-arc pour with 116 vertices',
          [len(p) for p in want_pour])
    for name, pcb in (('text', parse_kicad_pcb(w)), ('pcbnew', build_pcb_data_from_board(board))):
        zs = [z for z in pcb.zones if z.net_name == 'DPA_P']
        check(sorted(z.layer for z in zs) == ['B.Cu', 'F.Cu']
              and all(nm(z.polygon) == want_pour[0] for z in zs),
              '%s path: one Zone per layer, each with pcbnew\'s %d vertices'
              % (name, len(want_pour[0])), [(z.layer, len(z.polygon)) for z in zs])
        ko = pcb.board_info.keepouts
        rc = rules[0].Outline().Outline(0)
        rule_pts = [(rc.CPoint(j).x, rc.CPoint(j).y) for j in range(rc.PointCount())]
        check(len(ko) == 1 and nm(ko[0]['polygon']) == rule_pts,
              '%s path: the arc rule area reaches keepouts with pcbnew\'s %d vertices'
              % (name, len(rule_pts)), [len(k['polygon']) for k in ko])

    print('\n%d failure(s)' % len(FAILURES))
    for fl in FAILURES:
        print('  FAILED: %s' % fl)
    if FAILURES:
        return 1
    print('PASS  zone / rule-area arc outlines (text vs pcbnew, to the nm)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
