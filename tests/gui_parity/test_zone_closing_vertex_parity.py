#!/usr/bin/env python3
"""A zone outline whose last vertex repeats its first loads WITHOUT it, in every file format, on both fronts.

pcbnew closes a zone outline as it parses it (`SHAPE_LINE_CHAIN::SetClosed(true)`
-> `mergeFirstLastPointIfNeeded`, KiCad 9 and 10), which drops a last vertex
equal to the first. KiCad never writes one, but a KiCad 5-converted board does
on every zone (kangyuzhe zynq7020_ad9364, format 20201116: all 76 outlines),
and `parse_kicad_pcb` kept it -- its comment said pcbnew does too. Arms, on
boards in three file formats (20201116 single-line, 20211014, 20260206):

1. **pcbnew is the referee.** Five outlines -- closed by a repeated first
   vertex, open, closed with consecutive duplicates around it, "closed" 1 nm
   off, and a leading duplicate -- load with the vertex counts pcbnew holds,
   and both parse paths hold exactly those vertices, in integer nm.
2. **Witness.** The counts equal the declared ones (4, 4, 4, 5, 4), so a
   parser that keeps the closing vertex, or drops a last vertex 1 nm away
   from the first, fails in every format.

Needs pcbnew; re-execs into KiCad's python automatically. Exits 2 when no
pcbnew python is found, rather than 0.

    python3 -X utf8 tests/gui_parity/test_zone_closing_vertex_parity.py
"""
import glob
import os
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

OUTLINES = [
    ('closed by a repeated first vertex',
     '(xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20) (xy 10 10)', 4),
    ('open', '(xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20)', 4),
    ('closed, consecutive duplicates around it',
     '(xy 10 10) (xy 20 10) (xy 20 10) (xy 20 20) (xy 10 20) (xy 10 10) (xy 10 10)', 4),
    ("'closed' 1 nm off", '(xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20) (xy 10.000001 10)', 5),
    ('a leading duplicate', '(xy 10 10) (xy 10 10) (xy 20 10) (xy 20 20) (xy 10 20)', 4),
]
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


def board_v6(version):
    """A KiCad 6-era board: single-line zones, (tstamp ...), numeric nets."""
    zones = ''.join(
        '  (zone (net 0) (net_name "") (layer "F.Cu") '
        '(tstamp 00000000-0000-4000-8000-%012d) (hatch edge 0.508)\n'
        '    (priority %d)\n    (connect_pads (clearance 0.508))\n'
        '    (min_thickness 0.254)\n'
        '    (fill (thermal_gap 0.508) (thermal_bridge_width 0.508))\n'
        '    (polygon\n      (pts\n        %s\n      )\n    )\n  )\n'
        % (i, PRIORITY0 + i, body) for i, (_w, body, _n) in enumerate(OUTLINES))
    return ('(kicad_pcb (version %d) (generator pcbnew)\n\n'
            '  (general\n    (thickness 1.6)\n  )\n\n  (paper "A4")\n'
            '  (layers\n    (0 "F.Cu" signal)\n    (31 "B.Cu" signal)\n'
            '    (44 "Edge.Cuts" user)\n  )\n\n'
            '  (setup\n    (pad_to_mask_clearance 0)\n  )\n\n'
            '  (net 0 "")\n\n%s)\n' % (version, zones))


def board_v10():
    zones = ''.join(
        '\t(zone\n\t\t(layer "F.Cu")\n\t\t(uuid "00000000-0000-4000-8000-%012d")\n'
        '\t\t(hatch edge 0.5)\n\t\t(priority %d)\n\t\t(connect_pads\n\t\t\t(clearance 0.5)\n\t\t)\n'
        '\t\t(min_thickness 0.25)\n\t\t(fill\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n\t\t)\n'
        '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t%s\n\t\t\t)\n\t\t)\n\t)\n'
        % (i, PRIORITY0 + i, body) for i, (_w, body, _n) in enumerate(OUTLINES))
    return ('(kicad_pcb\n\t(version 20260206)\n\t(generator "pcbnew")\n'
            '\t(generator_version "10.0")\n\t(general\n\t\t(thickness 1.6)\n\t)\n'
            '\t(paper "A4")\n\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)\n'
            '\t\t(25 "Edge.Cuts" user)\n\t)\n\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n'
            '%s)\n' % zones)


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    import pcbnew
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board

    def nm(poly):
        return [(int(round(x * 1e6)), int(round(y * 1e6))) for x, y in poly]

    tmp = tempfile.mkdtemp(prefix='zclose_')
    for label, text in (('20201116', board_v6(20201116)), ('20211014', board_v6(20211014)),
                        ('20260206', board_v10())):
        print('\nFormat %s' % label)
        path = os.path.join(tmp, 'closing_%s.kicad_pcb' % label)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        board = pcbnew.LoadBoard(path)
        native = {}
        for z in board.Zones():
            c = z.Outline().Outline(0)
            native[int(z.GetAssignedPriority())] = [(c.CPoint(j).x, c.CPoint(j).y)
                                                    for j in range(c.PointCount())]
        fronts = (('text', parse_kicad_pcb(path)), ('pcbnew', build_pcb_data_from_board(board)))
        for i, (what, _body, want_n) in enumerate(OUTLINES):
            pr = PRIORITY0 + i
            got_native = native.get(pr)
            check(got_native is not None and len(got_native) == want_n,
                  '%s: pcbnew holds %d vertices (%s)' % (label, want_n, what),
                  None if got_native is None else len(got_native))
            for name, pcb in fronts:
                zs = [z for z in pcb.zones if z.priority == pr]
                check(len(zs) == 1 and nm(zs[0].polygon) == got_native,
                      '%s: %s path holds pcbnew\'s vertices (%s)' % (label, name, what),
                      [len(z.polygon) for z in zs])

    print('\n%d failure(s)' % len(FAILURES))
    for fl in FAILURES:
        print('  FAILED: %s' % fl)
    if FAILURES:
        return 1
    print('PASS  zone closing vertex (text vs pcbnew, three file formats)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
