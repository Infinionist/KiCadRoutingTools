#!/usr/bin/env python3
"""A recorded flag reaches the REAL dialog through a converted plan, and the
next step that omits it runs at the CLI default again.

test_manifest_plan_parity.py proves by AST that each flag converts to a param
naming a settable, reset control. What an AST cannot see is the run itself:
whether the executor's _set_control really sets that widget (a RadioBox has
no SetValue, so bga_fanout's --primary-escape was dropped while every table
named it), whether the control HOLDS the value (a SpinCtrlDouble rounds to its
digits), and whether the value is the one the tab hands its engine.

So, per flag: a recorded manifest line carrying it, followed by the same step
without it, is converted by tests/stress/manifest_to_plan.py's own CLI; the
plan step must carry the param; then the plan runs on the REAL PlanExecutor
over the REAL headless RoutingDialog, with each step's action replaced by a
probe (nothing routes, nothing is written). The probe reads the control AND
the tab config its engine call reads. The flagged step must see the recorded
value in both; the following step must see the CLI default in both -- the
reset_params_to_defaults half, without which a plan leaks one step's value
into every later step.

Needs KiCad's python (re-execs into it); exits 77 when there is none. Seconds.

Run: python3 tests/gui_parity/test_plan_flags_reach_dialog.py
"""
import glob
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


def _reexec_into_kicad():
    for cand in KICAD_PYTHONS:
        if cand == sys.executable or not os.path.exists(cand):
            continue
        if subprocess.run([cand, '-c', 'import pcbnew, wx'],
                          capture_output=True).returncode == 0:
            argv = [cand, os.path.abspath(__file__)] + sys.argv[1:]
            if os.name == 'nt':
                sys.exit(subprocess.run(argv).returncode)
            os.execv(cand, argv)
    print("SKIP: no python with pcbnew+wx found")
    sys.exit(77)


# (label, tool + args BEFORE the boards' flags, the flag and its value, the
# plan param and value it must convert to, probe name, value with the flag,
# value without it). Every probe returns (control value, engine config value).
# A step's boards are filled in by the chain below, so the converter's pruning
# keeps every step.
_PLANES = ('python3 py_router/route_planes.py {src} {dst} --nets GND '
           '--plane-layers B.Cu')
CASES = [
    dict(label='route_diff --diff-pair-centerline-setback',
         cmd='python3 py_router/route_diff.py {src} {dst} "*"',
         flag='--diff-pair-centerline-setback 0.35',
         param=('centerline_setback', 0.35), probe='centerline_setback',
         want=(0.35, 0.35), default=(0.0, None)),
    dict(label='bga_fanout --primary-escape',
         cmd='python3 py_router/bga_fanout.py {src} --output {dst} '
             '--component U1',
         flag='--primary-escape vertical',
         param=('escape_direction', 'vertical'), probe='escape_direction',
         want=('Vertical', 'vertical'), default=('Horizontal', 'horizontal')),
    dict(label='route_planes --min-thickness',
         cmd=_PLANES, flag='--min-thickness 0.0762',
         param=('min_thickness', 0.0762), probe='min_thickness',
         want=(0.0762, 0.0762), default=(0.1, 0.1)),
    dict(label='route_planes --plane-max-iterations',
         cmd=_PLANES, flag='--plane-max-iterations 50000',
         param=('plane_max_iterations', 50000), probe='plane_max_iterations',
         want=(50000, 50000), default=(200000, 200000)),
    dict(label='route_planes --voronoi-seed-interval',
         cmd=_PLANES, flag='--voronoi-seed-interval 3.25',
         param=('voronoi_seed_interval', 3.25), probe='voronoi_seed_interval',
         want=(3.25, 3.25), default=(2.0, 2.0)),
    dict(label='route_planes --skip-existing-zones',
         cmd=_PLANES, flag='--skip-existing-zones',
         param=('skip_existing_zones', True), probe='skip_existing_zones',
         want=(True, True), default=(False, False)),
]


def _probes(dlg):
    """{probe name: (control value, engine config value)} right now."""
    d = dlg.differential_tab
    bga = dlg.fanout_tab.bga_options
    po = dlg.planes_tab.create_options
    # What the planes tab hands create_plane: its ONE assembly point.
    plane_cfg = dlg.planes_tab._build_mode_config()
    planes = {name: (round(getattr(po, name).GetValue(), 6),
                     plane_cfg.get(name))
              for name in ('min_thickness', 'plane_max_iterations',
                           'voronoi_seed_interval')}
    planes['skip_existing_zones'] = (po.skip_existing_zones.GetValue(),
                                     plane_cfg.get('skip_existing_zones'))
    return {
        **planes,
        'centerline_setback': (
            round(d.centerline_setback.GetValue(), 6),
            d.get_config().get('diff_pair_centerline_setback')),
        'escape_direction': (
            bga.escape_direction.GetStringSelection(),
            bga.get_config().get('primary_escape')),
    }


def main():
    try:
        import wx  # noqa: F401
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()

    os.environ.setdefault('WXSUPPRESS_SIZER_FLAGS_CHECK', '1')
    import wx
    import pcbnew
    for p in (REPO, os.path.join(REPO, 'py_router'),
              os.path.join(REPO, 'py_tools'),
              os.path.join(REPO, 'tests', 'gui_parity')):
        sys.path.insert(0, p)
    app = wx.App(False)  # noqa: F841  (before any wx object)
    wx.MessageBox = lambda *a, **k: wx.OK

    from kicad_parser import parse_kicad_pcb
    from kicad_routing_plugin.swig_gui import RoutingDialog
    from kicad_routing_plugin import ai_plan, gui_utils
    import fix_kicad_drc_settings
    from copy_board import copy_board
    from wx_pump import run_until

    tmp = tempfile.mkdtemp(prefix='plan_flags_')
    results = []

    def check(label, ok, detail):
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {detail}")

    try:
        # 1. A manifest with each case's flagged step and then its plain step,
        #    chained board to board so pruning keeps them all.
        lines, n = ['#!/bin/bash'], 0
        for case in CASES:
            for with_flag in (True, False):
                cmd = case['cmd'].format(src=f'b{n}.kicad_pcb',
                                         dst=f'b{n + 1}.kicad_pcb')
                lines.append(cmd + (' ' + case['flag'] if with_flag else ''))
                n += 1
        manifest = os.path.join(tmp, 'redo_commands.sh')
        plan_path = os.path.join(tmp, 'plan.json')
        with open(manifest, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
        conv = subprocess.run(
            [sys.executable, os.path.join(REPO, 'tests', 'stress',
                                          'manifest_to_plan.py'),
             manifest, plan_path], capture_output=True, text=True)
        if conv.returncode != 0 or not os.path.isfile(plan_path):
            print(f"BROKEN TEST: manifest_to_plan exited {conv.returncode}:\n"
                  f"{conv.stdout}{conv.stderr}")
            return 2
        with open(plan_path, encoding='utf-8') as f:
            steps = json.load(f)['steps']
        if len(steps) != 2 * len(CASES):
            print(f"BROKEN TEST: {len(steps)} plan step(s) for "
                  f"{2 * len(CASES)} manifest lines:\n{conv.stdout}")
            return 2

        print("[1] manifest_to_plan: the flagged step carries the param, the "
              "plain step does not")
        for k, case in enumerate(CASES):
            key, val = case['param']
            on, off = steps[2 * k], steps[2 * k + 1]
            got = (on.get('params') or {}).get(key, '<absent>')
            check(case['label'], got == val and key not in
                  (off.get('params') or {}),
                  f"flagged step params[{key!r}] = {got!r} (want {val!r}); "
                  f"plain step {'omits it' if key not in (off.get('params') or {}) else 'CARRIES it'}"
                  f" -- {lines[2 * k + 1]}")

        # 2. The plan through the real executor; each action is a probe.
        board_path = os.path.join(tmp, 'board.kicad_pcb')
        copy_board(os.path.join(REPO, 'kicad_files',
                                'splitflap_driver.kicad_pcb'), board_path)
        board = pcbnew.LoadBoard(board_path)
        pcbnew.GetBoard = lambda: board
        # Nothing is written: the end-of-plan floor writers are spies.
        gui_utils.update_live_drc_floors = lambda *a, **k: []
        fix_kicad_drc_settings.fix_project_for_output = lambda *a, **k: None
        dlg = RoutingDialog(None, parse_kicad_pcb(board_path), board_path)
        try:
            seen, done = [], []
            ex = ai_plan.PlanExecutor(dlg, steps, range(len(steps)),
                                      on_status=lambda i, s: None,
                                      on_finished=lambda n, r: done.append(r))
            ex.POLL_MS = 10
            ex._action_parts = lambda action: (
                lambda: seen.append(_probes(dlg)), lambda: False)
            ex.start()
            run_until(lambda: bool(done), 120)
        finally:
            dlg.Destroy()
        if len(seen) != len(steps):
            print(f"BROKEN TEST: the executor ran {len(seen)} of "
                  f"{len(steps)} steps (finished: {done})")
            return 2

        print("\n[2] the real dialog: the flagged step's control and engine "
              "config hold the value; the next step is back at the default")
        for k, case in enumerate(CASES):
            on = seen[2 * k][case['probe']]
            off = seen[2 * k + 1][case['probe']]
            want, default = tuple(case['want']), tuple(case['default'])
            check(case['label'] + ' (flagged step)', on == want,
                  f"(control, engine config) = {on!r}, want {want!r}")
            check(case['label'] + ' (next step, flag omitted)', off == default,
                  f"(control, engine config) = {off!r}, want the CLI default "
                  f"{default!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n_ok = sum(results)
    print(f"\n{n_ok}/{len(results)} checks passed")
    return 0 if n_ok == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
