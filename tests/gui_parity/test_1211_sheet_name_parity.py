#!/usr/bin/env python3
"""#1211 parity gate: both parse paths read a footprint's schematic sheet --
its path, its NAME and its FILE -- the same, and `place_pose replicate`
resolves the same sheet instances from either.

`parse_kicad_pcb` reads `(sheetname ...)` / `(sheetfile ...)` (KiCad 8+) or
the `Sheetname` / `Sheetfile` properties (KiCad 6/7) from the file text.
`build_pcb_data_from_board` reads `FOOTPRINT.GetSheetname()` /
`GetSheetfile()` from live pcbnew. `replicate` names a channel by the first
and maps its parts by `sheet_path`, so a GUI caller handed a board whose
sheets read differently would copy a different channel, or none. Arms:

1. **Parity, footprint for footprint**, on tracked boards of every spelling:
   glasgow_revC (KiCad 8 token, bare names, two instances of one sheet),
   flat_hierarchy and rp2350 (KiCad 8 token, "/name/" paths), awx's
   fb_t2q_human (a KiCad 6 file: the `Sheetname` PROPERTY, which pcbnew's
   loader folds into the same field) and cap_chain (no sheet at all). Any
   extra board path on the command line gets the same arms (the
   multichannel_mixer demo is untracked, so it rides there).
2. **Witnesses.** The distinct (name, file) pairs on each tracked board are
   exactly the declared ones, on both paths. A parser reading '' everywhere
   passes every parity arm; this makes it fail.
3. **`compare_pcb_data`** -- the shipped two-path diff -- reports no sheet
   difference.
4. **A board pcbnew WROTE** after `SetSheetname` / `SetSheetfile` with a
   quote and a backslash in the name: the text parser reads back the
   unescaped value pcbnew returns.
5. **`replicate.sheet_instances`** lists the same instances (names, files,
   members) from both PCBData.

Needs pcbnew; re-execs into KiCad's python automatically. Exits 2 when no
pcbnew python is found (or a tracked board is missing), rather than 0, so a
mutation battery using it as a killer cannot score SURVIVED on an
environment accident.

    python3 -X utf8 tests/gui_parity/test_1211_sheet_name_parity.py [extra.kicad_pcb ...]
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

#: board -> the distinct (sheet_name, sheet_file) pairs it carries. Named,
#: not globbed: a board that stops being a witness must fail this gate.
TRACKED = {
    'kicad_files/glasgow_revC.kicad_pcb': {
        ('', ''), ('', 'glasgow.kicad_sch'),
        ('IO_Banks', 'io_banks.kicad_sch'),
        ('IO_Buffer_A', 'io_buffer.kicad_sch'),
        ('IO_Buffer_B', 'io_buffer.kicad_sch')},
    'kicad_files/flat_hierarchy.kicad_pcb': {
        ('/pic_programmer/', 'pic_programmer.kicad_sch'),
        ('/sockets/', 'pic_sockets.kicad_sch')},
    'kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb': {
        ('/', 'RP2350_FPGA_eensy.kicad_sch'),
        ('/Power Supply and USB/', 'sch/PSU_USB.kicad_sch'),
        ('/RP2354A/', 'sch/RP2354A.kicad_sch'),
        ('/T8F49I2X/', 'sch/T8F49I2X.kicad_sch')},
    'awx/fb_t2q_human.kicad_pcb': {('DDR3 16x1', 'DDR3_16x1.kicad_sch')},
    'kicad_files/cap_chain.kicad_pcb': {('', '')},
}

FAILURES = []
NOT_RUN = []


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


def sheets(pcb):
    return {k: (fp.sheet_path, fp.sheet_name, fp.sheet_file)
            for k, fp in pcb.footprints.items()}


def instances(pcb):
    from placement import replicate
    return {p: (r['names'], r['files'], r['refs'])
            for p, r in replicate.sheet_instances(pcb).items()}


def compare(name, path, want=None):
    """Arms 1, 2, 3 and 5 on one board. `want`: the witness set, or None."""
    import pcbnew
    from kicad_parser import (parse_kicad_pcb, build_pcb_data_from_board,
                              compare_pcb_data)
    f = parse_kicad_pcb(path)
    g = build_pcb_data_from_board(pcbnew.LoadBoard(path))
    a, b = sheets(f), sheets(g)
    check(set(a) == set(b), '%s: both paths see the same %d footprints'
          % (name, len(a)), 'only text=%s only pcbnew=%s'
          % (sorted(set(a) - set(b))[:3], sorted(set(b) - set(a))[:3]))
    bad = [(k, a[k], b[k]) for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    check(not bad, '%s: sheet path, name and file agree footprint for '
                   'footprint' % name, bad[:3])
    if want is not None:
        got_f = {(v[1], v[2]) for v in a.values()}
        got_g = {(v[1], v[2]) for v in b.values()}
        check(got_f == want and got_g == want,
              '%s: witness, the (name, file) pairs are exactly the declared '
              '%d' % (name, len(want)),
              'text=%s pcbnew=%s' % (sorted(got_f), sorted(got_g)))
    diffs = [d for d in compare_pcb_data(g, f) if 'sheet_' in d]
    check(not diffs, '%s: compare_pcb_data reports no sheet difference' % name,
          diffs[:3])
    # ...and it LOOKS: a name changed on one side is reported. Without this
    # a comparator that skipped the sheet fields would pass the arm above.
    k0 = sorted(g.footprints)[0]
    saved = g.footprints[k0].sheet_name
    g.footprints[k0].sheet_name = saved + '#witness'
    seen = [d for d in compare_pcb_data(g, f) if 'sheet_name' in d]
    g.footprints[k0].sheet_name = saved
    check(len(seen) == 1 and k0 in seen[0],
          '%s: compare_pcb_data reports a sheet name changed on one side'
          % name, seen[:3])
    ia, ib = instances(f), instances(g)
    check(ia == ib and (ia or want == {('', '')}),
          '%s: replicate lists the same %d sheet instances from both paths'
          % (name, len(ia)), 'text=%s pcbnew=%s' % (sorted(ia)[:3], sorted(ib)[:3]))


def written_board(tmp):
    """Arm 4: a name with a quote and a backslash, written by pcbnew."""
    import pcbnew
    from kicad_parser import parse_kicad_pcb, build_pcb_data_from_board
    src = os.path.join(REPO, 'kicad_files', 'glasgow_revC.kicad_pcb')
    board = pcbnew.LoadBoard(src)
    want = {}
    for i, fp in enumerate(list(board.GetFootprints())[:3]):
        name = '/odd "name" %d \\ x/' % i
        sfile = 'sub dir/odd_%d.kicad_sch' % i
        fp.SetSheetname(name)
        fp.SetSheetfile(sfile)
        want[fp.GetReference()] = (name, sfile)
    out = os.path.join(tmp, 'written.kicad_pcb')
    pcbnew.SaveBoard(out, board)
    with open(out, encoding='utf-8') as fh:
        text = fh.read()
    check('(sheetname "/odd \\"name\\" 0 \\\\ x/")' in text,
          'pcbnew serialised the name with its escapes')
    f = parse_kicad_pcb(out)
    g = build_pcb_data_from_board(pcbnew.LoadBoard(out))
    got_f = {fp.reference: (fp.sheet_name, fp.sheet_file)
             for fp in f.footprints.values() if fp.reference in want}
    got_g = {fp.reference: (fp.sheet_name, fp.sheet_file)
             for fp in g.footprints.values() if fp.reference in want}
    check(got_f == want and got_g == want,
          'pcbnew-written: both paths read back the unescaped name and file',
          'want=%s text=%s pcbnew=%s' % (want, got_f, got_g))


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    import pcbnew
    for _p in ('', 'py_router', 'py_placer', 'py_tools', 'tests'):
        _d = os.path.join(REPO, _p)
        if _d not in sys.path:
            sys.path.insert(0, _d)
    print('KiCad build: %s' % pcbnew.GetBuildVersion())
    for rel, want in TRACKED.items():
        path = os.path.join(REPO, rel)
        print('\n%s' % rel)
        if not os.path.isfile(path):
            NOT_RUN.append('%s is gone; replace the witness rather than let '
                           'coverage shrink' % rel)
            continue
        compare(os.path.basename(rel), path, want)
    for extra in sys.argv[1:]:
        print('\n%s (extra)' % extra)
        if not os.path.isfile(extra):
            NOT_RUN.append('%s is not a file' % extra)
            continue
        compare(os.path.basename(extra), extra)
    print('\npcbnew-written board')
    with tempfile.TemporaryDirectory(prefix='krt1211g_') as tmp:
        written_board(tmp)

    print('\n%d failure(s)' % len(FAILURES))
    for fl in FAILURES:
        print('  FAILED: %s' % fl)
    for nr in NOT_RUN:
        print('  NOT RUN: %s' % nr)
    if FAILURES:
        return 1
    if NOT_RUN:
        print('exit 2: an arm did not run, so this is not a pass')
        return 2
    print('PASS  #1211 sheet name / file / path parity (text vs pcbnew)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
