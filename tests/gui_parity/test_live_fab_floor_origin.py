#!/usr/bin/env python3
"""A GUI run that relaxes a fab floor says so, and records what it relaxed.

The file writers (fix_project_for_output, apply_routed_floors) record the
board's ORIGINAL fab floors in `kicad_routing_tools.fab_floor_origin` before
lowering anything, and print FAB FLOOR RELAXED against that origin (ad7f24de,
run 14). The GUI lowers the same floors on the LIVE board -- each step runs
fix_kicad_drc_settings.apply_targets_to_board and then
gui_utils.update_live_drc_floors -- and did neither. A manual GUI run that took
min_via_diameter 0.8 -> 0.4 printed nothing, and a later CLI step baselined on
0.4.

Real pcbnew, a real routed board (lvds_converter_dualclk_gnd: 97 tracks, 9
vias) with a .kicad_pro declaring floors ABOVE what the step routes at, the
two live writers called as a routing tab calls them, stdout captured (every
tab's apply phase routes print() into its log):

  * the origin lands in the project at the DECLARED value, not the lowered one;
  * FAB FLOOR RELAXED names ORIGINAL -> now and counts real copper under it;
  * a second step, which lowers nothing more, still says the board is under
    its original (the run-14 rule);
  * a board whose floors the step does not lower gets no banner;
  * the fanout tab's step, where update_live_drc_floors is the only writer,
    records the same origin and prints the same banner.

Needs KiCad python (pcbnew); re-execs into it like its siblings.

    python3 tests/gui_parity/test_live_fab_floor_origin.py
"""
import contextlib
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Every versioned install, newest first by NUMERIC version (a string sort
# puts KiCad\9.0 above KiCad\10.0).
sys.path.insert(0, os.path.join(REPO, 'py_router'))
from kicad_locate import path_version_key  # noqa: E402
del sys.path[0]    # main() orders its own sys.path below
KICAD_PYTHONS = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
    "/usr/bin/python3",
    *sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\python.exe"),
            key=path_version_key, reverse=True),
]

SRC = os.path.join(REPO, 'kicad_files', 'lvds_converter_dualclk_gnd.kicad_pcb')
DECLARED = {'min_track_width': 0.3, 'min_via_diameter': 0.8}
STEP = {'track_width': 0.15, 'via_size': 0.4, 'via_drill': 0.2}
FAILS = []


def check(name, ok, detail=''):
    print(f"  {'PASS' if ok else 'FAIL'}: {name}" + (f" -- {detail}" if detail else ''))
    if not ok:
        FAILS.append(name)


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand == sys.executable or not os.path.exists(cand):
            continue
        if subprocess.run([cand, '-c', 'import pcbnew'],
                          capture_output=True).returncode == 0:
            # os.execv re-splits argv on spaces on Windows ("Program Files").
            sys.exit(subprocess.run([cand, os.path.abspath(__file__)]
                                    + sys.argv[1:]).returncode)
    print("ERROR: no python with pcbnew found; this gate does not self-skip, "
          "because a gate that exits 0 without running reports everything it "
          "guards as checked.")
    sys.exit(2)


def _stage(d, rules):
    dst = os.path.join(d, 'b.kicad_pcb')
    shutil.copyfile(SRC, dst)
    with open(os.path.join(d, 'b.kicad_pro'), 'w', encoding='utf-8') as f:
        json.dump({"board": {"design_settings": {"rules": dict(rules)}},
                   "meta": {"version": 1}}, f, indent=2)
    return dst


def _step(board):
    """One GUI routing step's writeback, as swig_gui calls it; stdout captured."""
    from fix_kicad_drc_settings import apply_targets_to_board
    from kicad_routing_plugin.gui_utils import update_live_drc_floors
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        apply_targets_to_board(board, {'min_track_width': STEP['track_width'],
                                       'min_via_diameter': STEP['via_size']}, {})
        update_live_drc_floors(board, **STEP)
    return buf.getvalue()


def _origin(board_path):
    with open(os.path.splitext(board_path)[0] + '.kicad_pro', encoding='utf-8') as f:
        return (json.load(f).get('kicad_routing_tools') or {}).get('fab_floor_origin')


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    import pcbnew
    sys.path.insert(0, os.path.dirname(REPO))
    for sub in ('', 'py_router', 'py_placer', 'py_tools'):
        sys.path.insert(0, os.path.join(REPO, sub))
    import fix_kicad_drc_settings as F

    td = tempfile.mkdtemp(prefix='livefab_')
    try:
        print("--- a step that relaxes the declared floors")
        getattr(F, '_LIVE_FAB_ORIGIN', {}).clear()
        path = _stage(td, DECLARED)
        board = pcbnew.LoadBoard(path)
        pcbnew.GetBoard = lambda: board
        bds = board.GetDesignSettings()
        check("the fixture loads the declared floors",
              abs(bds.m_ViasMinSize / 1e6 - 0.8) < 1e-9
              and abs(bds.m_TrackMinWidth / 1e6 - 0.3) < 1e-9,
              f"{bds.m_ViasMinSize / 1e6}, {bds.m_TrackMinWidth / 1e6}")
        out = _step(board)
        check("the step really lowered the live via floor",
              bds.m_ViasMinSize / 1e6 < 0.8, f"{bds.m_ViasMinSize / 1e6}")
        origin = _origin(path) or {}
        check("the origin is recorded at the DECLARED floors, not the lowered ones",
              origin.get('min_via_diameter') == 0.8
              and origin.get('min_track_width') == 0.3, f"{origin}")
        check("FAB FLOOR RELAXED is printed", 'FAB FLOOR RELAXED' in out, out[-600:])
        check("it names the original via floor and the new one",
              'via diameter: 0.8 -> 0.4 mm' in out, out[-600:])
        check("it counts the real copper under the original floors",
              'object(s) on this board are below the ORIGINAL 0.3mm' in out
              and 'object(s) on this board are below the ORIGINAL 0.8mm' in out,
              out[-900:])

        print("--- a second step that lowers nothing more")
        out2 = _step(board)
        # In the file writer's own words for a floor this step inherited: the
        # step's input is passed beside the origin, as the CLI passes its
        # rules_before, so a step that lowered nothing does not claim it did.
        check("still disclosed: the board is under its original, as inherited",
              'FAB FLOOR RELAXED' in out2
              and "via diameter: 0.4 mm, unchanged by this step but still below "
                  "the board's ORIGINAL 0.8mm" in out2,
              out2[-400:])
        check("the origin is not re-seeded from the lowered value",
              (_origin(path) or {}).get('min_via_diameter') == 0.8, f"{_origin(path)}")

        print("--- a step that relaxes nothing")
        getattr(F, '_LIVE_FAB_ORIGIN', {}).clear()
        td2 = tempfile.mkdtemp(prefix='livefab_quiet_')
        try:
            # Below both the step's values and this board's own copper, which
            # update_live_drc_floors also lowers to: nothing can go under them.
            path2 = _stage(td2, {'min_track_width': 0.05, 'min_via_diameter': 0.2,
                                 'min_via_annular_width': 0.03,
                                 'min_through_hole_diameter': 0.1})
            board2 = pcbnew.LoadBoard(path2)
            pcbnew.GetBoard = lambda: board2
            out3 = _step(board2)
            check("no banner when no fab floor goes under its origin",
                  'FAB FLOOR RELAXED' not in out3, out3[-400:])
        finally:
            shutil.rmtree(td2, ignore_errors=True)

        print("--- the fanout tab's step: update_live_drc_floors alone")
        # fanout_gui never calls apply_targets_to_board, so there
        # update_live_drc_floors is the step's FIRST writer and must seed the
        # origin before it lowers a floor itself. Every step above calls
        # apply_targets_to_board first, which seeds, so none of them can see an
        # update_live_drc_floors that seeds after lowering -- the ad7f24de
        # shape, which records the lowered value and says nothing.
        getattr(F, '_LIVE_FAB_ORIGIN', {}).clear()
        td3 = tempfile.mkdtemp(prefix='livefab_fanout_')
        try:
            path3 = _stage(td3, DECLARED)
            board3 = pcbnew.LoadBoard(path3)
            pcbnew.GetBoard = lambda: board3
            from kicad_routing_plugin.gui_utils import update_live_drc_floors
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                update_live_drc_floors(board3, **STEP)
            out4 = buf.getvalue()
            bds3 = board3.GetDesignSettings()
            check("the fanout step really lowered the live via floor",
                  bds3.m_ViasMinSize / 1e6 < 0.8, f"{bds3.m_ViasMinSize / 1e6}")
            origin3 = _origin(path3) or {}
            check("its origin is recorded at the DECLARED floors, not the "
                  "lowered ones",
                  origin3.get('min_via_diameter') == 0.8
                  and origin3.get('min_track_width') == 0.3, f"{origin3}")
            check("and FAB FLOOR RELAXED names the original via floor and the "
                  "new one", 'via diameter: 0.8 -> 0.4 mm' in out4, out4[-600:])
        finally:
            shutil.rmtree(td3, ignore_errors=True)

        print("--- a PROJECT-LESS board, two steps, both fronts")
        # pcbnew holds a project-less board at its factory floors (track 0.2,
        # via 0.5, hole 0.3, ...), which no file declares. The CLI's first
        # writeback on it records no origin and prints nothing; its second
        # seeds the origin from what the first wrote. The GUI must record and
        # print the same: the factory values are not floors anyone set.
        getattr(F, '_LIVE_FAB_ORIGIN', {}).clear()
        getattr(F, '_LIVE_DECLARED', {}).clear()
        td4 = tempfile.mkdtemp(prefix='livefab_bare_')
        try:
            from fix_kicad_drc_settings import fix_project_for_output
            gui = os.path.join(td4, 'gui', 'b.kicad_pcb')
            cli0 = os.path.join(td4, 'cli', 'b.kicad_pcb')
            cli1 = os.path.join(td4, 'cli', 'b1.kicad_pcb')
            for d in (os.path.dirname(gui), os.path.dirname(cli0)):
                os.makedirs(d)
            shutil.copyfile(SRC, gui)
            shutil.copyfile(SRC, cli0)
            board4 = pcbnew.LoadBoard(gui)
            pcbnew.GetBoard = lambda: board4

            def _relaxed(text):
                """The 'label: was -> now mm' lines of the FAB FLOOR block."""
                out, inside = [], False
                for ln in text.splitlines():
                    if 'FAB FLOOR RELAXED' in ln:
                        inside = True
                        continue
                    if inside and ln.strip().startswith('Every checker'):
                        inside = False
                    elif inside and ' -> ' in ln:
                        out.append(ln.strip().split(';')[0])
                return sorted(out)

            g1 = _step(board4)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                fix_project_for_output(cli0, input_pcb=SRC,
                                       track_width=STEP['track_width'],
                                       via_diameter=STEP['via_size'],
                                       via_drill=STEP['via_drill'])
            c1 = buf.getvalue()
            check("step 1: the CLI says nothing about a fab floor",
                  'FAB FLOOR RELAXED' not in c1, c1[-400:])
            check("step 1: neither does the GUI (pcbnew's factory floors are "
                  "not the board's)", 'FAB FLOOR RELAXED' not in g1, g1[-600:])

            STEP2 = {'track_width': 0.12, 'via_size': 0.35, 'via_drill': 0.18}
            from fix_kicad_drc_settings import apply_targets_to_board
            from kicad_routing_plugin.gui_utils import update_live_drc_floors
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                apply_targets_to_board(board4, {
                    'min_track_width': STEP2['track_width'],
                    'min_via_diameter': STEP2['via_size']}, {})
                update_live_drc_floors(board4, **STEP2)
            g2 = buf.getvalue()
            shutil.copyfile(cli0, cli1)
            shutil.copyfile(os.path.splitext(cli0)[0] + '.kicad_pro',
                            os.path.splitext(cli1)[0] + '.kicad_pro')
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                fix_project_for_output(cli1, input_pcb=cli0,
                                       track_width=STEP2['track_width'],
                                       via_diameter=STEP2['via_size'],
                                       via_drill=STEP2['via_drill'])
            c2 = buf.getvalue()
            check("step 2: both fronts print the banner",
                  'FAB FLOOR RELAXED' in g2 and 'FAB FLOOR RELAXED' in c2,
                  f"GUI: {g2[-300:]} CLI: {c2[-300:]}")
            check("step 2: the same relaxations, original -> now, on both fronts",
                  _relaxed(g2) == _relaxed(c2) and _relaxed(g2),
                  f"GUI {_relaxed(g2)} CLI {_relaxed(c2)}")
        finally:
            shutil.rmtree(td4, ignore_errors=True)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print('=' * 60)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + '; '.join(FAILS))
        return 1
    print("PASS: a GUI step that relaxes a fab floor records the original and "
          "says so, like the CLI writeback")
    return 0


if __name__ == '__main__':
    sys.exit(main())
