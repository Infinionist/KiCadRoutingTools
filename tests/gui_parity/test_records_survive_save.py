#!/usr/bin/env python3
"""What a GUI step records survives the user's KiCad save, and the CLI reads it.

    python3 tests/gui_parity/test_records_survive_save.py

(re-execs into KiCad's bundled python automatically, like its siblings, and
does NOT self-skip without it: exit 2)

THE GAP. The GUI's writers put their records -- ``kicad_routing_tools.
fab_floor_origin``, ``protected_nets``, ``net_impedance``,
``pour_served_pads`` -- into the board's ``.kicad_pro`` ON DISK. KiCad's own
project save rewrites that file from the copy it loaded when the board was
opened, so the user's next save dropped every one of them, and the next CLI
step read a project that had lost them: the origin re-seeded from the lowered
floors (no FAB FLOOR RELAXED), protected nets rippable, impedance widths gone,
pour promises unaudited. The plan executor's end-of-plan writeback has the
same shape for RULES: it wrote the project with fix_project_for_output, and a
rule it did not also set in memory reverted on save.

WHAT THIS DRIVES, on real boards through real pcbnew saves:

  1. flat_hierarchy (a project): a manual GUI step's writeback
     (apply_targets_to_board, then gui_utils.update_live_drc_floors, with the
     engine's protection / impedance / pour notes pending), then
     ``pcbnew.SaveBoard`` with its project -- what KiCad's File > Save does.
     Every record is read back by the CLI's own readers (declared_fab_floor,
     read_protected_nets, read_impedance_specs, read_pour_served_pads, the
     obstacle map's hole-floor origin), and a CLI step on the saved board
     still says FAB FLOOR RELAXED against the board's original floors.
  2. splitflap_driver with NO project: the same, where the records have no
     project to live in at all until KiCad's save creates one.
  3. the plan executor's REAL ``_write_drc_floors`` on flat_hierarchy, then a
     save: every rule its fix_project_for_output wrote is still in the saved
     project, and the class clearances too.
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

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

sys.path.insert(0, os.path.join(REPO, 'py_router'))
from kicad_locate import path_version_key  # noqa: E402
del sys.path[0]
KICAD_PYTHONS = [
    '/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/'
    'Versions/Current/bin/python3',
    '/usr/bin/python3',
    *sorted(glob.glob(r"C:\Program Files\KiCad\*\bin\python.exe"),
            key=path_version_key, reverse=True),
]
DECLARING = os.path.join(REPO, 'kicad_files', 'flat_hierarchy.kicad_pcb')
PROJECTLESS = os.path.join(REPO, 'kicad_files', 'splitflap_driver.kicad_pcb')
STEP = {'clearance': 0.15, 'track_width': 0.15, 'via_size': 0.4, 'via_drill': 0.2}


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand == sys.executable or not os.path.exists(cand):
            continue
        if subprocess.run([cand, '-c', 'import pcbnew, wx'],
                          capture_output=True).returncode == 0:
            sys.exit(subprocess.run([cand, os.path.abspath(__file__)]
                                    + sys.argv[1:]).returncode)
    print("ERROR: no python with pcbnew + wx found; this gate does not "
          "self-skip, because a gate that exits 0 without running reports "
          "everything it guards as checked.")
    sys.exit(2)


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def main():
    try:
        import wx  # noqa: F401
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    os.environ.setdefault('WXSUPPRESS_SIZER_FLAGS_CHECK', '1')
    import wx
    sys.path.insert(0, REPO)
    for sub in ('py_router', 'py_placer', 'py_tools'):
        sys.path.insert(0, os.path.join(REPO, sub))
    sys.path.insert(0, os.path.dirname(REPO))
    app = wx.App(False)  # noqa: F841
    import pcbnew as _pcbnew
    import protected_nets as P
    from copy_board import copy_board
    from fix_kicad_drc_settings import (apply_targets_to_board, compute_targets,
                                        declared_fab_floor, fix_project_for_output)
    from kicad_parser import parse_kicad_pcb
    from kicad_routing_plugin.gui_utils import (board_minima_from_live,
                                                update_live_drc_floors)
    from obstacle_map import declared_hole_clearance

    failures, tmpdirs = [], []

    def check(name, cond, info=""):
        if not cond:
            failures.append(name)
        print(("  PASS " if cond else "  FAIL ") + name + (f"  {info}" if info else ""))

    def stage(src, with_project):
        d = tempfile.mkdtemp(prefix='records_save_')
        tmpdirs.append(d)
        dst = os.path.join(d, 'b.kicad_pcb')
        if with_project:
            _quiet(copy_board, src, dst)
        else:
            shutil.copyfile(src, dst)
        return dst

    PROT = {'/RECORD_A': 'length-matched'}
    IMP = {'/RECORD_A': {'ohms': 50.0}}
    POUR = {'U1.1': {'net': 'GND', 'layer': 'F.Cu', 'how': 'pour'}}

    def gui_step(board):
        """One manual GUI routing step's writeback, as the signal tab runs it,
        with the engine's notes pending."""
        P.note_protection_candidates(PROT)
        P.note_impedance_specs(IMP)
        P.note_pour_served_pads(POUR)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            apply_targets_to_board(board, compute_targets(
                clearance=STEP['clearance'], track_width=STEP['track_width'],
                via_diameter=STEP['via_size'], via_drill=STEP['via_drill'],
                minima=board_minima_from_live(board)), {})
            update_live_drc_floors(board, clearance=STEP['clearance'],
                                   track_width=STEP['track_width'],
                                   via_size=STEP['via_size'],
                                   via_drill=STEP['via_drill'])
        return out.getvalue()

    def read_back(path, label, origin_expect):
        pro = os.path.splitext(path)[0] + '.kicad_pro'
        o = declared_fab_floor(path, 'min_via_diameter')
        check(f"{label}: the CLI reads the fab-floor origin", o == origin_expect,
              f"min_via_diameter origin {o}, expected {origin_expect}")
        check(f"{label}: ... the protected nets",
              P.read_protected_nets(pro) == PROT, f"{P.read_protected_nets(pro)}")
        check(f"{label}: ... the impedance specs",
              P.read_impedance_specs(pro) == IMP, f"{P.read_impedance_specs(pro)}")
        check(f"{label}: ... the pour-served balls",
              P.read_pour_served_pads(pro) == POUR, f"{P.read_pour_served_pads(pro)}")

    print("--- 1. a project board: GUI step, then KiCad's save")
    path = stage(DECLARING, True)
    with open(os.path.splitext(path)[0] + '.kicad_pro', encoding='utf-8') as f:
        declared_via = json.load(f)['board']['design_settings']['rules'][
            'min_via_diameter']
    board = _pcbnew.LoadBoard(path)
    _pcbnew.GetBoard = lambda: board
    gui_step(board)
    check("before the save, the project on disk carries the records",
          P.read_protected_nets(os.path.splitext(path)[0] + '.kicad_pro') == PROT)
    check("pcbnew saves the board and its project", bool(_pcbnew.SaveBoard(path, board)))
    with open(os.path.splitext(path)[0] + '.kicad_pro', encoding='utf-8') as f:
        saved = json.load(f)
    check("(the save really rewrote the project from KiCad's copy: no "
          "protected_nets left in it)",
          'protected_nets' not in (saved.get('kicad_routing_tools') or {}),
          f"{saved.get('kicad_routing_tools')}")
    read_back(path, "after the save", declared_via)
    hc = declared_hole_clearance(parse_kicad_pcb(path))
    check("after the save: the obstacle map's hole floor still takes the "
          "board's original 0.25", abs(hc[0] - 0.25) < 1e-9, f"{hc}")
    cli_out = os.path.join(os.path.dirname(path), 'next.kicad_pcb')
    shutil.copyfile(path, cli_out)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fix_project_for_output(cli_out, input_pcb=path, clearance=0.12,
                               track_width=0.12, via_diameter=0.35, via_drill=0.18)
    txt = buf.getvalue()
    check("a CLI step on the saved board says FAB FLOOR RELAXED against the "
          f"ORIGINAL via floor {declared_via:g}",
          'FAB FLOOR RELAXED' in txt and f'via diameter: {declared_via:g} -> 0.35'
          in txt, txt[-500:])
    check("and carries the records on in its own project",
          P.read_protected_nets(os.path.splitext(cli_out)[0] + '.kicad_pro') == PROT)
    from kicad_parser import build_pcb_data_from_board, compare_pcb_data
    reloaded = _pcbnew.LoadBoard(path)
    diffs = compare_pcb_data(build_pcb_data_from_board(reloaded),
                             parse_kicad_pcb(path))
    check("the saved board, carrying its records, parses the same on both "
          "fronts (records and copper-to-hole floor included)", not diffs,
          f"{diffs[:4]}")
    from kicad_writer import add_tracks_and_vias_to_pcb
    written = os.path.join(os.path.dirname(path), 'written.kicad_pcb')
    _quiet(add_tracks_and_vias_to_pcb, path, written, [], [])
    with open(written, encoding='utf-8') as f:
        carried = '(property "kicad_routing_tools"' in f.read()
    check("and the CLI's board writer carries the board's copy into its output",
          carried)

    print("--- 2. a project-less board: GUI step, then KiCad's save")
    bare = stage(PROJECTLESS, False)
    board2 = _pcbnew.LoadBoard(bare)
    _pcbnew.GetBoard = lambda: board2
    gui_step(board2)                              # step 1: no origin, as the CLI
    STEP_2 = {'clearance': 0.12, 'track_width': 0.12, 'via_size': 0.35,
              'via_drill': 0.18}
    _quiet(apply_targets_to_board, board2, compute_targets(
        clearance=STEP_2['clearance'], track_width=STEP_2['track_width'],
        via_diameter=STEP_2['via_size'], via_drill=STEP_2['via_drill'],
        minima=board_minima_from_live(board2)), {})
    _quiet(update_live_drc_floors, board2, **STEP_2)
    check("pcbnew saves it (creating its project)", bool(_pcbnew.SaveBoard(bare, board2)))
    read_back(bare, "after the save", 0.4)

    print("--- 3. the plan executor's end-of-plan writeback, then KiCad's save")
    from kicad_routing_plugin.swig_gui import RoutingDialog
    from kicad_routing_plugin.ai_plan import PlanExecutor
    plan = stage(DECLARING, True)
    board3 = _pcbnew.LoadBoard(plan)
    _pcbnew.GetBoard = lambda: board3
    dlg = RoutingDialog(None, parse_kicad_pcb(plan), plan)
    dlg._suppress_completion_popups = True
    steps = [{'action': 'route', 'params': dict(STEP)}]
    ex = PlanExecutor(dlg, steps, [0], lambda *a: None, lambda *a: None)
    _quiet(ex._write_drc_floors)
    with open(os.path.splitext(plan)[0] + '.kicad_pro', encoding='utf-8') as f:
        written = json.load(f)
    check("pcbnew saves the board and its project", bool(_pcbnew.SaveBoard(plan, board3)))
    dlg.Destroy()
    with open(os.path.splitext(plan)[0] + '.kicad_pro', encoding='utf-8') as f:
        after = json.load(f)
    w_rules = written['board']['design_settings']['rules']
    a_rules = after['board']['design_settings']['rules']
    # `min_via_drill` is the repo's own floor, not a KiCad rule: KiCad's save
    # keeps only its own rules, whichever front wrote the project (a CLI
    # output saved in KiCad loses it the same way). Its durable copy is the
    # fab_floor_origin record, which the disclosure compares against.
    lost = {k: (w_rules[k], a_rules.get(k)) for k in w_rules
            if k != 'min_via_drill' and isinstance(w_rules[k], (int, float))
            and (not isinstance(a_rules.get(k), (int, float))
                 or abs(w_rules[k] - a_rules[k]) > 1e-6)}
    check("every rule the plan's writeback wrote survives the save", not lost,
          f"(written, after save): {lost}")
    w_cls = {c['name']: c.get('clearance') for c in written['net_settings']['classes']}
    a_cls = {c['name']: c.get('clearance') for c in after['net_settings']['classes']}
    lost_c = {k: (w_cls[k], a_cls.get(k)) for k in w_cls
              if w_cls[k] is not None and (a_cls.get(k) is None
                                           or abs(w_cls[k] - a_cls[k]) > 1e-6)}
    check("and every class clearance", not lost_c, f"(written, after save): {lost_c}")
    o = declared_fab_floor(plan, 'min_via_diameter')
    check("and the origin it recorded", o == declared_via, f"{o}")

    for d in tmpdirs:
        shutil.rmtree(d, ignore_errors=True)
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("PASS: a GUI step's records and rules survive the user's KiCad save, "
          "and the CLI reads them")
    return 0


if __name__ == '__main__':
    sys.exit(main())
