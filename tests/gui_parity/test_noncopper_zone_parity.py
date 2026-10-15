#!/usr/bin/env python3
"""A zone on no copper layer is not copper, and neither parse path models it.

KiCad lets a zone sit on solder mask, paste or silkscreen -- a mask opening
drawn as a zone (`(layers "*.Mask")`, allwinner_a13_som), a paste window
(`(layers "F.Mask" "F.Paste")`, simplebldc_inv: 8 of them), a silkscreen fill.
pcbnew strips such a zone's net on load (`parseZONE`: "non copper zones do
not have a net"). `build_pcb_data_from_board` used to model a MULTI-layer one
as a net-0 pour on the UNDEFINED layer (its layer set has no copper layer, so
it fell back to `GetLayer()`), which the text parser skipped; a SINGLE-layer
one reached both models on its mask/silk layer, the text one keeping a net
pcbnew drops. `pcb.zones` is copper. Arms:

1. **pcbnew is the referee.** `IsOnCopperLayer()` per zone, on a board
   carrying zones on `*.Mask`, `F.Mask F.Paste`, `F.SilkS` (with a net),
   `B.Mask`, a copper zone that also lists `F.Mask`, and a plain copper
   pour.
2. **Both paths** model exactly pcbnew's copper zones, on their copper layers
   only, and `compare_pcb_data` reports no zone line.
3. **Witness.** The copper zones counted per layer equal the declared ones
   ({F.Cu: 2, B.Cu: 1}), so a path keeping a mask zone -- or dropping the
   mixed one -- fails.

Needs pcbnew; re-execs into KiCad's python automatically. Exits 2 when no
pcbnew python is found, rather than 0.

    python3 -X utf8 tests/gui_parity/test_noncopper_zone_parity.py
"""
import collections
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

BASE = os.path.join(REPO, 'kicad_files', 'cap_chain.kicad_pcb')
# (layer clause, net clause, copper layers it must be modelled on)
ZONES = [
    ('(layers "*.Mask")', '', []),
    ('(layers "F.Mask" "F.Paste")', '', []),
    ('(layer "F.SilkS")', '(net "DPA_P")', []),
    ('(layer "B.Mask")', '', []),
    ('(layers "F.Cu" "F.Mask")', '(net "DPA_P")', ['F.Cu']),
    ('(layers "F&B.Cu")', '(net "DPB_P")', ['F.Cu', 'B.Cu']),
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


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    import pcbnew
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board, compare_pcb_data

    with open(BASE, encoding='utf-8') as fh:
        text = fh.read().rstrip()
    blocks = ''.join(
        '\t(zone\n\t\t%s\n\t\t%s\n\t\t(uuid "00000000-0000-4000-8000-%012d")\n'
        '\t\t(hatch edge 0.5)\n\t\t(priority %d)\n\t\t(connect_pads\n\t\t\t(clearance 0.2)\n\t\t)\n'
        '\t\t(min_thickness 0.25)\n\t\t(fill\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n\t\t)\n'
        '\t\t(polygon\n\t\t\t(pts\n\t\t\t\t(xy %d 10) (xy %d 10) (xy %d 15) (xy %d 15)\n\t\t\t)\n\t\t)\n\t)\n'
        % (net, layers, i, 9001 + i, 10 + 10 * i, 15 + 10 * i, 15 + 10 * i, 10 + 10 * i)
        for i, (layers, net, _cu) in enumerate(ZONES))
    tmp = tempfile.mkdtemp(prefix='zncu_')
    path = os.path.join(tmp, 'noncopper.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text[:-1] + blocks + ')\n')

    print('pcbnew')
    board = pcbnew.LoadBoard(path)
    native = {int(z.GetAssignedPriority()) - 9001: z for z in board.Zones()}
    check(len(native) == len(ZONES), 'pcbnew loads all %d zones' % len(ZONES), len(native))
    for i, (layers, net, cu) in enumerate(ZONES):
        z = native.get(i)
        check(z is not None and bool(z.IsOnCopperLayer()) == bool(cu),
              'pcbnew: %s is %s' % (layers, 'copper' if cu else 'not copper'))
        if z is not None and not cu:
            check(z.GetNetCode() == 0, 'pcbnew: %s carries no net' % layers, z.GetNetCode())

    declared = collections.Counter(l for _l, _n, cu in ZONES for l in cu)
    print('\nBoth parse paths')
    f = parse_kicad_pcb(path)
    g = build_pcb_data_from_board(board)
    for name, pcb in (('text', f), ('pcbnew', g)):
        ours = [z for z in pcb.zones if z.priority >= 9001]
        got = collections.Counter(z.layer for z in ours)
        check(got == declared, '%s path: the copper zones per layer are the declared ones %s'
              % (name, dict(declared)), dict(got))
        for i, (layers, _net, cu) in enumerate(ZONES):
            check(sorted(z.layer for z in ours if z.priority == 9001 + i) == sorted(cu),
                  '%s path: %s modelled on %s' % (name, layers, cu or 'no layer'),
                  [z.layer for z in ours if z.priority == 9001 + i])
    zone_diffs = [d for d in compare_pcb_data(g, f) if d.startswith('Zone')]
    check(not zone_diffs, 'compare_pcb_data reports no zone difference', zone_diffs[:4])

    print('\n%d failure(s)' % len(FAILURES))
    for fl in FAILURES:
        print('  FAILED: %s' % fl)
    if FAILURES:
        return 1
    print('PASS  non-copper zones (text vs pcbnew: neither models one)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
