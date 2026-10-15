#!/usr/bin/env python3
"""Parity gate: the text parser reads a pad and a footprint the way pcbnew's
LOADER does, with native pcbnew as the oracle.

`parse_kicad_pcb` reads the file; `build_pcb_data_from_board` reads what
pcbnew made of it, and pcbnew's reader rewrites several things on load. Each
arm below is one such rule, on a synthetic board written at two file versions
(KiCad 7's 20221018 and KiCad 9's 20241229), and each is checked three ways:
the text parse against the NATIVE pcbnew value, the text parse against the
pcbnew builder, and `compare_pcb_data` (the shipped two-path diff) clean.

- **Pad layers** (`canonical_pad_layers`): `F&B.Cu` is F.Cu + B.Cu;
  `*.Adhes` / `*.Fab` / `*.CrtYd` / `*In.Cu` are their members; `*.Cu` plus
  an explicit inner layer is `*.Cu`; a PLATED pad declared on one side is on
  every copper layer of the board; a User layer is listed by both paths.
  Measured on the raw corpus before the fix: 1,201 pads on F&B.Cu, 622 with
  `*.Cu` plus explicit inners, 9 plated pads declared on one side.
- **Pad numbers**: an NPTH hole and a copper-less SMD/connector pad (an
  aperture) load with NO number, whatever the file wrote (ovc5_camera_carrier
  mounting holes are pad "1").
- **Exponent-form numbers**: `(drill 1e-06)` is 1 nm (arty_mpw_tester U7,
  which the text parser read as a 1 mm hole).
- **Sheet properties**: `(property "Sheet name" ...)` (KiCad 6) and
  `"Sheetname"` are the footprint's sheet up to file version 20230619 and an
  ordinary user field after it (ecp5_mini, rfswitch01).

Needs pcbnew; re-execs into KiCad's python automatically. Exits 2 when no
pcbnew python is found, rather than 0, so a mutation battery using it as a
killer cannot score SURVIVED on an environment accident.

    python3 -X utf8 tests/gui_parity/test_pad_loader_parity.py
"""
import glob
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Newest KiCad first by NUMERIC version: a plain string sort puts KiCad\9.0
# above KiCad\10.0, so a box with both ran this under 9.0.
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

#: The board: a 4-copper-layer stack in KiCad 7-9 layer numbering, one
#: footprint carrying one pad per loader rule. `%(sheet)s` is the sheet
#: property pair under test.
BOARD = '''(kicad_pcb (version %(ver)s) (generator "pcbnew") (general (thickness 1.6))
 (paper "A4")
 (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
  (32 "B.Adhes" user "B.Adhesive") (33 "F.Adhes" user "F.Adhesive") (34 "B.Paste" user)
  (35 "F.Paste" user) (36 "B.SilkS" user "B.Silkscreen") (37 "F.SilkS" user "F.Silkscreen")
  (38 "B.Mask" user) (39 "F.Mask" user) (40 "Dwgs.User" user "User.Drawings")
  (44 "Edge.Cuts" user) (46 "B.CrtYd" user "B.Courtyard") (47 "F.CrtYd" user "F.Courtyard")
  (48 "B.Fab" user) (49 "F.Fab" user) (50 "User.1" user))
 (setup)
 (net 0 "") (net 1 "/A")
 (gr_rect (start 0 0) (end 80 40) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts"))
 (footprint "L:P" (layer "F.Cu") (at 20 20)
  (property "Reference" "U1" (at 0 -3 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
  %(sheet)s
  (pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) (layers "B.Cu" "B.Mask") (net 1 "/A"))
  (pad "2" thru_hole circle (at 3 0) (size 1.6 1.6) (drill 0.8) (layers "F&B.Cu" "*.Mask") (net 1 "/A"))
  (pad "3" np_thru_hole circle (at 6 0) (size 1.6 1.6) (drill 1.6) (layers "F&B.Cu" "*.Mask"))
  (pad "4" np_thru_hole circle (at 9 0) (size 1.6 1.6) (drill 1.6) (layers "*.Cu" "*.Mask"))
  (pad "5" smd rect (at 12 0) (size 1 1) (layers "F.Paste"))
  (pad "6" smd rect (at 15 0) (size 1 1) (layers "F.Cu" "*.Adhes" "F.Mask" "B.Mask") (net 1 "/A"))
  (pad "7" thru_hole circle (at 18 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu" "*.Mask" "In1.Cu") (net 1 "/A"))
  (pad "8" smd rect (at 21 0) (size 1 1) (layers "F.Cu" "*.Fab" "*.CrtYd" "User.1" "F.Paste" "B.Paste") (net 1 "/A"))
  (pad "9" thru_hole circle (at 24 0) (size 1.6 1.6) (drill 1e-06) (layers "*.Cu") (net 1 "/A"))
  (pad "10" connect rect (at 27 0) (size 1 1) (layers "F.Mask"))
  (pad "11" thru_hole circle (at 30 0) (size 1.6 1.6) (drill 0.8) (layers "*In.Cu") (net 1 "/A"))
  (pad "12" smd rect (at 33 0) (size 1 1) (layers "F&B.Cu") (net 1 "/A"))
  (pad "13" np_thru_hole circle (at 36 0) (size 1.6 1.6) (drill 1.6) (layers "F.Cu" "F.Mask"))
 )
)
'''
SHEETS = {'KiCad 6 "Sheet name"': ('Sheet name', 'Sheet file'),
          'KiCad 7 "Sheetname"': ('Sheetname', 'Sheetfile')}

#: Via net propagation (antmicro lpddr4_test_board's stitching vias): pcbnew's
#: connectivity pass gives a via the one net of the fills and pads it joins,
#: whatever the file wrote. V1 (net 0) and V2 (SIG, nothing attached) in the
#: GND fill become GND; V3 (SIG, tracked to a SIG pad) is a conflict and
#: keeps SIG; V4 is `(free yes)` and keeps net 0. A net-0 via reached only by
#: a track (pcbnew re-nets it through the track to the track's pad) is not
#: modelled, and is not in this fixture.
FILL = '(xy 0 0) (xy 30 0) (xy 30 20) (xy 0 20)'
VIA_BOARD = '''(kicad_pcb (version 20241229) (generator "pcbnew") (general (thickness 1.6))
 (paper "A4") (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
 (setup) (net 0 "") (net 1 "GND") (net 2 "SIG")
 (gr_rect (start -5 -5) (end 60 30) (stroke (width 0.1) (type solid)) (fill none) (layer "Edge.Cuts"))
 (footprint "L:P" (layer "F.Cu") (at 45 10)
  (property "Reference" "J1" (at 0 -3 0) (layer "F.Cu") (hide yes) (effects (font (size 1 1) (thickness 0.15))))
  (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "SIG")))
 (via (at 5 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 0) (uuid "00000000-0000-0000-0000-000000000001"))
 (via (at 10 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000002"))
 (via (at 15 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000003"))
 (segment (start 15 5) (end 45 10) (width 0.2) (layer "F.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000013"))
 (via (at 20 5) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (free yes) (net 0) (uuid "00000000-0000-0000-0000-000000000004"))
 (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "00000000-0000-0000-0000-0000000000aa") (hatch edge 0.5)
  (connect_pads (clearance 0.2)) (min_thickness 0.2) (filled_areas_thickness no)
  (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
  (polygon (pts %(fill)s))
  (filled_polygon (layer "B.Cu") (pts %(fill)s)))
)
''' % {'fill': FILL}
#: A KiCad 10 file has no net table and writes an unnetted track `(net "")`
#: (apple1_aci_card: 2,363 of them), so one path lists net 0 and the other
#: does not; the comparator must still call them the same net.
NET0_BOARD = '''(kicad_pcb (version 20260206) (generator "pcbnew") (generator_version "10.0")
 (general (thickness 1.6)) (paper "A4")
 (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
 (setup)
 (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type solid)) (fill no) (layer "Edge.Cuts"))
 (segment (start 2 2) (end 8 2) (width 0.2) (layer "F.Cu") (net "") (uuid "00000000-0000-0000-0000-000000000021"))
 (segment (start 2 6) (end 8 6) (width 0.2) (layer "B.Cu") (net "") (uuid "00000000-0000-0000-0000-000000000022"))
)
'''


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


FAILURES = []


def check(cond, what, detail=''):
    if cond:
        print('  ok    %s' % what)
    else:
        print('  FAIL  %s%s' % (what, ('  -- ' + detail) if detail else ''))
        FAILURES.append(what)


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    import pcbnew
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    from kicad_parser import (parse_kicad_pcb, build_pcb_data_from_board,
                              compare_pcb_data)

    def concrete(tokens):
        """A PARSED layer list as the set of real layer names it stands for.
        Only the wildcards a parsed list may still carry expand; a set token
        the parser should have resolved (`F&B.Cu`, `*.Adhes`) stays itself,
        so it can never match a native name. Its own expansion, not the
        parser's: an oracle built from the code under test proves nothing."""
        out = set()
        for t in tokens:
            if t == '*.Cu':
                out.update(['F.Cu', 'B.Cu'] + ['In%d.Cu' % i for i in range(1, 31)])
            elif t in ('*.Mask', '*.Paste', '*.SilkS'):
                out.update(('F' + t[1:], 'B' + t[1:]))
            else:
                out.add(t)
        return out

    print('KiCad build: %s' % pcbnew.GetBuildVersion())
    scratch = tempfile.mkdtemp(prefix='krt_loader_')
    witnessed = set()
    for ver in ('20221018', '20241229'):
        for tag, (sn, sf) in SHEETS.items():
            name = '%s %s' % (ver, tag)
            print('\n%s' % name)
            path = os.path.join(scratch, '%s_%s.kicad_pcb' % (ver, sn[-4:]))
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(BOARD % {'ver': ver, 'sheet': '(property "%s" "Power") '
                                  '(property "%s" "power.kicad_sch")' % (sn, sf)})
            board = pcbnew.LoadBoard(path)
            check(board is not None, '%s: pcbnew loads the fixture' % name)
            if board is None:
                continue
            f = parse_kicad_pcb(path)
            g = build_pcb_data_from_board(board)
            live = list(next(iter(board.GetFootprints())).Pads())
            tf, gf = f.footprints['U1'], g.footprints['U1']
            check(len(tf.pads) == len(gf.pads) == len(live) == 13,
                  '%s: 13 pads on every side' % name)
            bad_layers, bad_builder, bad_num, bad_drill = [], [], [], []
            for tp, gp, lp in zip(tf.pads, gf.pads, live):
                native = {pcbnew.LSET.Name(l) for l in lp.GetLayerSet().Seq()}
                if concrete(tp.layers) != native:
                    bad_layers.append((lp.GetNumber() or '#', tp.layers, sorted(native)))
                if set(tp.layers) != set(gp.layers):
                    bad_builder.append((tp.layers, gp.layers))
                if tp.pad_number != lp.GetNumber():
                    bad_num.append((tp.pad_number, lp.GetNumber()))
                if abs(tp.drill - pcbnew.ToMM(max(lp.GetDrillSize().x,
                                                  lp.GetDrillSize().y))) > 1e-9:
                    bad_drill.append((tp.drill, pcbnew.ToMM(lp.GetDrillSize().x)))
            check(not bad_layers, '%s: every pad is on the layers pcbnew loads' % name,
                  str(bad_layers[:3]))
            check(not bad_builder, '%s: the text parse lists the builder\'s layer set'
                  % name, str(bad_builder[:3]))
            check(not bad_num, '%s: pad numbers are pcbnew\'s (NPTH / aperture blank)'
                  % name, str(bad_num[:4]))
            check(not bad_drill, '%s: drills are pcbnew\'s, exponent form too' % name,
                  str(bad_drill[:3]))
            lfp = next(iter(board.GetFootprints()))
            want = (str(lfp.GetSheetname()), str(lfp.GetSheetfile()))
            check((tf.sheet_name, tf.sheet_file) == want,
                  '%s: sheet name/file are pcbnew\'s %r' % (name, want),
                  repr((tf.sheet_name, tf.sheet_file)))
            witnessed.add('sheet read' if want[0] else 'sheet not read')
            diffs = compare_pcb_data(g, f)
            check(not diffs, '%s: compare_pcb_data is clean' % name, str(diffs[:4]))
            if any(tp.layers == ['B.Cu', 'B.Mask', 'F.Cu', 'In1.Cu', 'In2.Cu']
                   for tp in tf.pads):
                witnessed.add('plated pad takes the board copper')
    # -- via nets: the connectivity pass on load ------------------------------
    print('\nvia net propagation')
    path = os.path.join(scratch, 'vias.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(VIA_BOARD)
    board = pcbnew.LoadBoard(path)
    f = parse_kicad_pcb(path)
    native = {(round(pcbnew.ToMM(t.GetPosition().x), 3), round(pcbnew.ToMM(t.GetPosition().y), 3)):
              t.GetNetname() for t in board.GetTracks() if t.GetClass() == 'PCB_VIA'}
    text = {(round(v.x, 3), round(v.y, 3)): (f.nets[v.net_id].name if v.net_id in f.nets else '')
            for v in f.vias}
    check(native == {(5.0, 5.0): 'GND', (10.0, 5.0): 'GND', (15.0, 5.0): 'SIG', (20.0, 5.0): ''},
          'pcbnew re-nets the two stitching vias in the GND fill, not the tracked or free one',
          str(native))
    check(text == native, 'every via is on pcbnew\'s net', 'text %s native %s' % (text, native))
    diffs = compare_pcb_data(build_pcb_data_from_board(board), f)
    check(not diffs, 'via board: compare_pcb_data is clean', str(diffs[:4]))
    print('\nKiCad 10 unnetted tracks')
    path = os.path.join(scratch, 'net0.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(NET0_BOARD)
    board = pcbnew.LoadBoard(path)
    check(board is not None, 'pcbnew loads the KiCad 10 fixture')
    if board is not None:
        f = parse_kicad_pcb(path)
        g = build_pcb_data_from_board(board)
        check(0 not in f.nets and 0 in g.nets and len(f.segments) == len(g.segments) == 2,
              'the text model lists no net 0, the pcbnew model does (the case under test)',
              '%s / %s' % (sorted(f.nets), sorted(g.nets)))
        diffs = compare_pcb_data(g, f)
        check(not diffs, 'net-0 board: compare_pcb_data calls both tracks the same net',
              str(diffs[:3]))
    # A gate whose fixture stopped exercising a rule must fail, not pass.
    check(witnessed >= {'sheet read', 'sheet not read',
                        'plated pad takes the board copper'},
          'witnesses: a property read as the sheet, one not, a plated pad '
          'completed to the board copper', str(sorted(witnessed)))
    print()
    if FAILURES:
        print('FAILED: %d check(s): %s' % (len(FAILURES), FAILURES[:6]))
        return 1
    print('PASS: pads and footprints read the way pcbnew loads them')
    return 0


if __name__ == '__main__':
    sys.exit(main())
