#!/usr/bin/env python3
"""The GUI prices NPTH holes at its LIVE board's min hole clearance (#1038,
#1238), and a KiCad session re-reads a project it saved.

`obstacle_map.resolve_hole_clearance` is the copper-to-hole floor the router
holds tracks off NPTH walls at, and since #1038 any value above 0 also turns on
the via-copper keep-out around them. The CLI reads it from the board's sibling
.kicad_pro, in a fresh process every step. The GUI plugin is one process for a
whole KiCad session, and it used to read the same file through a cache keyed
on the board path: a Board Setup edit was invisible until saved, a save was
invisible for the rest of the session, and an unsaved board (#1238) had no file
to read at all.

Real pcbnew, real boards:

  * flat_hierarchy (its project declares 0.25): both fronts read 0.25 off a
    fresh load; an UNSAVED Board Setup edit to 0.30 is priced by the GUI -- by
    the PCBData built before the edit -- while the file, and so the CLI, still
    says 0.25, and compare_pcb_data reports the difference; after pcbnew saves
    the board, a file parse in the SAME process reads 0.30;
  * splitflap_driver with no project: pcbnew loads it at its factory 0.25, the
    file declares nothing, and both fronts read 0.0 (no divergence on a fresh
    load); after one GUI step's writeback (apply_targets_to_board at 0.2) the
    GUI reads 0.2 -- the floor the CLI's next step reads from the project its
    own writeback (fix_project_for_output at 0.2) leaves;
  * a board that was never saved prices a live edit.

Needs KiCad python (pcbnew); re-execs into it like its siblings, and does NOT
self-skip without it (exit 2).

    python3 tests/gui_parity/test_live_hole_clearance.py
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

DECLARING = os.path.join(REPO, 'kicad_files', 'flat_hierarchy.kicad_pcb')
PROJECTLESS = os.path.join(REPO, 'kicad_files', 'splitflap_driver.kicad_pcb')
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


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    import pcbnew
    sys.path.insert(0, os.path.dirname(REPO))
    for sub in ('', 'py_router', 'py_placer', 'py_tools'):
        sys.path.insert(0, os.path.join(REPO, sub))
    from copy_board import copy_board
    from fix_kicad_drc_settings import (apply_targets_to_board, compute_targets,
                                        fix_project_for_output)
    from kicad_parser import (build_pcb_data_from_board, compare_pcb_data,
                              parse_kicad_pcb)
    from obstacle_map import resolve_hole_clearance

    def floor(pcb):
        return _quiet(resolve_hole_clearance, pcb, None)

    def hole_diffs(a, b):
        return [d for d in compare_pcb_data(a, b) if 'Copper-to-hole' in d]

    # What pcbnew really initialises the rule to -- the independent check on
    # the constant the parser reads. The board is held while it is read: a
    # temporary's design settings would be freed under the attribute read.
    fresh = pcbnew.BOARD()
    factory = fresh.GetDesignSettings().m_HoleClearance / 1e6
    td = tempfile.mkdtemp(prefix='live_hc_')
    try:
        print("--- a board whose project declares 0.25 (flat_hierarchy)")
        path = os.path.join(td, 'decl', 'b.kicad_pcb')
        os.makedirs(os.path.dirname(path))
        _quiet(copy_board, DECLARING, path)
        board = pcbnew.LoadBoard(path)
        pcbnew.GetBoard = lambda: board
        bds = board.GetDesignSettings()
        live = build_pcb_data_from_board(board)
        check("the pcbnew path records pcbnew's factory hole clearance",
              abs((getattr(live, 'live_rule_defaults', None) or {})
                  .get('min_hole_clearance', -1) - factory) < 1e-9,
              f"{getattr(live, 'live_rule_defaults', None)} vs {factory}")
        f0, l0 = floor(parse_kicad_pcb(path)), floor(live)
        check("fresh load: both fronts read the declared 0.25",
              abs(f0 - 0.25) < 1e-9 and abs(l0 - 0.25) < 1e-9, f"file {f0}, live {l0}")
        check("and compare_pcb_data agrees",
              not hole_diffs(live, parse_kicad_pcb(path)),
              f"{hole_diffs(live, parse_kicad_pcb(path))}")

        bds.m_HoleClearance = 300000                  # Board Setup, NOT saved
        l1, f1 = floor(live), floor(parse_kicad_pcb(path))
        check("an unsaved Board Setup edit to 0.30 is priced by the GUI -- "
              "by the PCBData built BEFORE the edit", abs(l1 - 0.30) < 1e-9, f"{l1}")
        check("while the file, which the CLI reads, still says 0.25",
              abs(f1 - 0.25) < 1e-9, f"{f1}")
        d = hole_diffs(live, parse_kicad_pcb(path))
        check("and compare_pcb_data names the difference", bool(d), f"{d}")

        check("pcbnew saves the board and its project",
              bool(pcbnew.SaveBoard(path, board)))
        with open(os.path.splitext(path)[0] + '.kicad_pro', encoding='utf-8') as f:
            saved = json.load(f)['board']['design_settings']['rules'].get(
                'min_hole_clearance')
        check("the saved project declares 0.30", saved == 0.3, f"{saved}")
        f2 = floor(parse_kicad_pcb(path))
        check("after the save, a file parse in the SAME process reads 0.30 "
              "(a path-keyed cache kept 0.25 for the whole session)",
              abs(f2 - 0.30) < 1e-9, f"{f2}")

        print("--- a project-less board (splitflap_driver)")
        bare = os.path.join(td, 'bare', 'b.kicad_pcb')
        os.makedirs(os.path.dirname(bare))
        shutil.copyfile(PROJECTLESS, bare)               # deliberately no .kicad_pro
        board2 = pcbnew.LoadBoard(bare)
        pcbnew.GetBoard = lambda: board2
        bds2 = board2.GetDesignSettings()
        check("pcbnew loads it at the factory value",
              abs(bds2.m_HoleClearance / 1e6 - factory) < 1e-9,
              f"{bds2.m_HoleClearance / 1e6}")
        live2 = build_pcb_data_from_board(board2)
        f3, l3 = floor(parse_kicad_pcb(bare)), floor(live2)
        check("both fronts read it as declaring nothing (0.0): no divergence "
              "on a fresh load", f3 == 0.0 and l3 == 0.0, f"file {f3}, live {l3}")
        check("and compare_pcb_data agrees",
              not hole_diffs(live2, parse_kicad_pcb(bare)),
              f"{hole_diffs(live2, parse_kicad_pcb(bare))}")

        _quiet(apply_targets_to_board, board2, compute_targets(clearance=0.2), {})
        l4 = floor(live2)
        check("one GUI step's writeback lowers the live rule to 0.2, and the "
              "GUI's next step reads 0.2", abs(l4 - 0.2) < 1e-9,
              f"live rule {bds2.m_HoleClearance / 1e6}, floor {l4}")
        cli = os.path.join(td, 'cli', 'b.kicad_pcb')
        os.makedirs(os.path.dirname(cli))
        shutil.copyfile(PROJECTLESS, cli)
        _quiet(fix_project_for_output, cli, input_pcb=PROJECTLESS, clearance=0.2)
        f4 = floor(parse_kicad_pcb(cli))
        check("the floor the CLI's next step reads from its own writeback's "
              "project", abs(f4 - l4) < 1e-9, f"CLI {f4}, GUI {l4}")

        print("--- a board that was never saved (#1238)")
        board3 = pcbnew.BOARD()
        live3 = build_pcb_data_from_board(board3)
        check("no file", not getattr(live3, 'source_path', ''),
              f"{getattr(live3, 'source_path', '')!r}")
        l5 = floor(live3)
        check("at the factory value it declares nothing", l5 == 0.0, f"{l5}")
        board3.GetDesignSettings().m_HoleClearance = 300000
        l6 = floor(live3)
        check("a live edit to 0.30 is priced with no file to read",
              abs(l6 - 0.30) < 1e-9, f"{l6}")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print('=' * 60)
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + '; '.join(FAILS))
        return 1
    print("PASS: the GUI prices its live board's hole clearance, and a session "
          "re-reads a project it saved")
    return 0


if __name__ == '__main__':
    sys.exit(main())
