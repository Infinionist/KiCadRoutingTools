#!/usr/bin/env python3
"""--escalation board with the DRC-settings writeback OFF floors clearance
descents at the clearance KiCad grades the unchanged project at (#1262).

With the writeback, a descent below the Default class lowers the OUTPUT
project's Default class to the clearance used (#1160's ratchet) and the board
grades clean against the project it ships with. With --no-fix-drc-settings
(or the GUI's "Fix DRC settings after routing" unticked) the input project
ships unchanged, so the same descent is a KiCad violation. Measured on the
board below (min_clearance 0.2, Default class 0.25): the per-net rescue laid
FB along U2's pad tips at its 0.2125 rung; against the input project check_drc
found 42 violations and kicad-cli flagged the same copper at 0.215-0.240 mm.

The board, built here from the s-expression (no pcbnew): a SOT-23-6 whose FB
pin is walled in by GND_B copper so its only way out is a 0.64 mm strip along
the tips of pads 5 and 6 -- too narrow for a 0.25 track at the 0.25 class
(0.75), wide enough for the rescue's 0.2 track at 0.2125 (0.625). A via
keep-out over the box takes the other layer away.

Checks:
  A. the floor rule itself (fab_tiers.board_policy_floors and friends): the
     writeback on is unchanged; off raises the clearance floor to the
     graded class, capped at the run's own clearance, never dropped to the
     fab floor; other policies pass through
  B. both fronts set it: set_policy_from_args reads --no-fix-drc-settings,
     set_fab_tier_from_config reads fix_drc_settings + default_class_clearance
  C. END TO END, writeback off: check_drc at the input project's Default
     class finds nothing, and the run records no clearance descent
  D. END TO END, writeback on (control): the 0.2125 rescue still happens, is
     a design_rules row, and the output project's Default class is lowered

    python3 tests/test_1262_board_class_floor.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, TESTS_DIR)

import fab_tiers                                       # noqa: E402
from run_utils import check as run_check, evidence     # noqa: E402

FAILS = []
MIN_CLR, CLASS_CLR = 0.2, 0.25


def check(name, cond, detail=''):
    if not cond:
        FAILS.append(name)
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  {detail}' if detail else ''))


# --------------------------------------------------------------------- board
_N = [0]


def _uid():
    _N[0] += 1
    return '00000000-0000-4000-8000-%012d' % _N[0]


def _pad(num, x, y, w, h, net):
    return ('\t\t(pad "%s" smd roundrect\n\t\t\t(at %s %s)\n\t\t\t(size %s %s)\n'
            '\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")\n\t\t\t(roundrect_rratio 0.25)\n'
            '\t\t\t(net "%s")\n\t\t\t(uuid "%s")\n\t\t)\n'
            % (num, x, y, w, h, net, _uid()))


def _fp(name, ref, x, y, pads):
    return ('\t(footprint "%s"\n\t\t(layer "F.Cu")\n\t\t(uuid "%s")\n\t\t(at %s %s)\n'
            '\t\t(property "Reference" "%s"\n\t\t\t(at 0 -2 0)\n\t\t\t(layer "F.SilkS")\n'
            '\t\t\t(uuid "%s")\n\t\t)\n%s\t)\n'
            % (name, _uid(), x, y, ref, _uid(), ''.join(pads)))


def _seg(x1, y1, x2, y2, net):
    return ('\t(segment\n\t\t(start %s %s)\n\t\t(end %s %s)\n\t\t(width 0.25)\n'
            '\t\t(layer "F.Cu")\n\t\t(net "%s")\n\t\t(uuid "%s")\n\t)\n'
            % (x1, y1, x2, y2, net, _uid()))


def board_text():
    sot = [_pad('1', -1.1375, -0.95, 1.325, 0.6, 'GND_A'),
           _pad('2', -1.1375, 0, 1.325, 0.6, '/SW'),
           _pad('3', -1.1375, 0.95, 1.325, 0.6, '/VIN'),
           _pad('4', 1.1375, 0.95, 1.325, 0.6, '/FB'),
           _pad('5', 1.1375, 0, 1.325, 0.6, '/EN'),
           _pad('6', 1.1375, -0.95, 1.325, 0.6, '/BOOT')]
    r1 = [_pad('1', -0.825, 0, 0.8, 0.95, '/FB'), _pad('2', 0.825, 0, 0.8, 0.95, '/VOUT')]
    r2 = [_pad('1', -0.825, 0, 0.8, 0.95, '/FB'), _pad('2', 0.825, 0, 0.8, 0.95, 'GND_A')]
    wall = [(17.565, 8.6), (17.565, 14.0), (12.5, 14.0), (12.5, 10.2), (16.6, 10.2)]
    return ''.join([
        '(kicad_pcb\n\t(version 20260206)\n\t(generator "test_1262")\n'
        '\t(generator_version "10.0")\n\t(general\n\t\t(thickness 1.6)\n\t)\n'
        '\t(paper "A4")\n\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)\n'
        '\t\t(13 "F.Paste" user)\n\t\t(1 "F.Mask" user)\n\t\t(3 "B.Mask" user)\n'
        '\t\t(5 "F.SilkS" user "F.Silkscreen")\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
        '\t(setup\n\t\t(pad_to_mask_clearance 0)\n\t)\n',
        _fp('SOT-23-6', 'U2', 15, 12, sot),
        _fp('R_0603_1608Metric', 'R1', 17.0, 7.0, r1),
        _fp('R_0603_1608Metric', 'R2', 13.0, 7.0, r2),
        '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 30 24)\n\t\t(stroke\n\t\t\t(width 0.05)\n'
        '\t\t\t(type default)\n\t\t)\n\t\t(fill no)\n\t\t(layer "Edge.Cuts")\n'
        '\t\t(uuid "%s")\n\t)\n' % _uid(),
        ''.join(_seg(a[0], a[1], b[0], b[1], 'GND_B') for a, b in zip(wall, wall[1:])),
        '\t(zone\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(uuid "%s")\n\t\t(hatch edge 0.5)\n'
        '\t\t(connect_pads\n\t\t\t(clearance 0)\n\t\t)\n\t\t(min_thickness 0.25)\n'
        '\t\t(keepout\n\t\t\t(tracks allowed)\n\t\t\t(vias not_allowed)\n'
        '\t\t\t(pads allowed)\n\t\t\t(copperpour allowed)\n\t\t\t(footprints allowed)\n'
        '\t\t)\n\t\t(fill\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n'
        '\t\t)\n\t\t(polygon\n\t\t\t(pts\n\t\t\t\t(xy 11.5 8.5) (xy 20.5 8.5) '
        '(xy 20.5 15) (xy 11.5 15)\n\t\t\t)\n\t\t)\n\t)\n' % _uid(),
        ')\n'])


def project():
    return {'board': {'design_settings': {'rules': {
                'min_clearance': MIN_CLR, 'min_track_width': 0.2,
                'min_via_diameter': 0.5, 'min_through_hole_diameter': 0.3,
                'min_hole_to_hole': 0.25, 'min_copper_edge_clearance': 0.3},
                'rule_severities': {}}},
            'meta': {'filename': 'in.kicad_pro', 'version': 3},
            'net_settings': {'classes': [{'name': 'Default', 'clearance': CLASS_CLR,
                                          'track_width': 0.25, 'via_diameter': 0.6,
                                          'via_drill': 0.3}],
                             'meta': {'version': 4}}}


def stage(d, name):
    pcb = os.path.join(d, name + '.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as f:
        f.write(board_text())
    with open(os.path.splitext(pcb)[0] + '.kicad_pro', 'w', encoding='utf-8') as f:
        json.dump(project(), f, indent=2)
    return pcb


def route(inp, out, extra):
    r = run_check([sys.executable, '-X', 'utf8', os.path.join(ROOT, 'py_router', 'route.py'),
                   inp, out, '--nets', '*', '!GND_A', '!GND_B', '--keep-input-copper',
                   '--escalation', 'board', '--track-width', '0.25'] + extra,
                  accept=True, env=dict(os.environ, KICAD_PLANE_FINALIZE='0'))
    log = r.stdout + r.stderr
    line = next((l for l in log.splitlines() if l.startswith('JSON_SUMMARY:')), '')
    return log, (json.loads(line.split(':', 1)[1]) if line else {})


def drc(board):
    """check_drc's violation count on `board` (graded at its sibling project).
    check_drc exits 1 when it finds something, so the exit code is not the
    verdict; the count is, and a run that printed neither is a broken test."""
    import subprocess
    evidence(board, 'board to grade')
    p = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'py_router', 'check_drc.py'), board],
                       cwd=ROOT, capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    out = p.stdout + p.stderr
    m = re.search(r'FOUND (\d+) DRC VIOLATIONS', out)
    n = int(m.group(1)) if m else (0 if 'NO DRC VIOLATIONS' in out else None)
    if n is None or 'Traceback' in out:
        raise AssertionError('BROKEN TEST: check_drc printed no verdict\n' + out[-1500:])
    grading = next((l for l in out.splitlines() if l.startswith('Grading at clearance')), '')
    return n, grading, out


def clearance_rows(summ):
    return [r for r in (summ.get('design_rules') or {}).get('narrowed') or []
            if r.get('kind') == 'clearance']


def quiet(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*a, **kw), buf.getvalue()


# ----------------------------------------------------------------- A. rules
def t_rule():
    print('A. the floor rule')
    declared = {'clearance': MIN_CLR, 'track_width': 0.2, 'via_diameter': 0.5}
    graded = fab_tiers.graded_default_clearance(MIN_CLR, CLASS_CLR)
    check('graded clearance is the Default class floored at min_clearance',
          graded == CLASS_CLR and fab_tiers.graded_default_clearance(0.3, 0.25) == 0.3
          and fab_tiers.graded_default_clearance(0, 0.25) == 0.25
          and fab_tiers.graded_default_clearance(None, None) is None)
    req = {'clearance': 0.25, 'track_width': 0.25, 'via_size': 0.6}
    f, _ = quiet(fab_tiers.board_policy_floors, 'board', declared, req, graded,
                 writeback=True)
    check('writeback on: unchanged, the declared min_clearance bounds descents',
          f == declared, str(f))
    f, said = quiet(fab_tiers.board_policy_floors, 'board', declared, req, graded,
                    writeback=False)
    check('writeback off: the clearance floor is the graded Default class',
          f.get('clearance') == CLASS_CLR and f.get('track_width') == 0.2, str(f))
    check('...and it says so', '#1262' in said and '0.25' in said, said.strip())
    f, said = quiet(fab_tiers.board_policy_floors, 'board', declared,
                    dict(req, clearance=0.22), graded, writeback=False)
    check('writeback off, a request below the class: the request is the floor '
          '(no clearance descent)', f.get('clearance') == 0.22, str(f))
    f, said = quiet(fab_tiers.board_policy_floors, 'board', declared,
                    dict(req, clearance=0.15), graded, writeback=False)
    check('writeback off, a request below min_clearance: still the request, '
          'never dropped to the fab floor', f.get('clearance') == 0.15, str(f))
    f, said = quiet(fab_tiers.board_policy_floors, 'board', declared,
                    dict(req, clearance=0.15), graded, writeback=True)
    check('control: writeback on, the same request marks min_clearance stale',
          'clearance' not in f and 'stale' in said, str(f))
    f, _ = quiet(fab_tiers.board_policy_floors, 'board', declared,
                 dict(req, clearance=0.3), graded, writeback=False)
    check('writeback off, a request above the class: descents down to the class',
          f.get('clearance') == CLASS_CLR, str(f))
    for pol in ('fab', 'off'):
        f, _ = quiet(fab_tiers.board_policy_floors, pol, declared, req, graded,
                     writeback=False)
        check(f'{pol}: the floors pass through', f == declared, str(f))


# ----------------------------------------------------------- B. both fronts
def t_fronts(tmp):
    print('B. both fronts')
    from plane_pad_tap import fab_floor_clearance_track
    pcb = stage(tmp, 'fronts')
    bi = NS(board_info=NS(copper_layers=['F.Cu', 'B.Cu']))
    prev = fab_tiers.get_escalation_policy()
    try:
        quiet(fab_tiers.set_policy_from_args,
              NS(escalation='board', clearance=0.25, no_fix_drc_settings=True), pcb)
        check('CLI, --no-fix-drc-settings: the descent ladder stops at the class',
              abs(fab_floor_clearance_track(bi)[0] - CLASS_CLR) < 1e-9,
              str(fab_tiers.get_escalation_policy()[1]))
        quiet(fab_tiers.set_policy_from_args,
              NS(escalation='board', clearance=0.25, no_fix_drc_settings=False), pcb)
        check('CLI, writeback on: the ladder still reaches min_clearance',
              abs(fab_floor_clearance_track(bi)[0] - MIN_CLR) < 1e-9)
        quiet(fab_tiers.set_policy_from_args, NS(escalation='board', clearance=0.25), pcb)
        check('CLI, a caller without the flag keeps the writeback-on floors',
              abs(fab_floor_clearance_track(bi)[0] - MIN_CLR) < 1e-9)
        gui = {'escalation': 'board', 'clearance': 0.25, 'track_width': 0.25,
               'board_floors': {'clearance': MIN_CLR, 'track_width': 0.2},
               'default_class_clearance': CLASS_CLR}
        quiet(fab_tiers.set_fab_tier_from_config, dict(gui, fix_drc_settings=False))
        check('GUI, "Fix DRC settings" unticked: the same floor',
              abs(fab_floor_clearance_track(bi)[0] - CLASS_CLR) < 1e-9,
              str(fab_tiers.get_escalation_policy()[1]))
        quiet(fab_tiers.set_fab_tier_from_config, dict(gui, fix_drc_settings=True))
        check('GUI, ticked: unchanged',
              abs(fab_floor_clearance_track(bi)[0] - MIN_CLR) < 1e-9)
    finally:
        fab_tiers.set_escalation_policy(*prev)


# ------------------------------------------------------------- C/D. routing
def t_route(tmp):
    print('C. writeback off, end to end')
    inp = stage(tmp, 'in')
    out = os.path.join(tmp, 'off.kicad_pcb')
    log, summ = route(inp, out, ['--no-fix-drc-settings'])
    evidence(out, 'routed board (writeback off)')
    # Grade the shipped copper against the INPUT project, as KiCad would with
    # the project the user kept.
    gdir = os.path.join(tmp, 'grade_off')
    os.makedirs(gdir)
    g = os.path.join(gdir, 'in.kicad_pcb')
    shutil.copy(out, g)
    shutil.copy(os.path.splitext(inp)[0] + '.kicad_pro', os.path.join(gdir, 'in.kicad_pro'))
    n, grading, dlog = drc(g)
    check('check_drc grades at the input Default class', '0.25' in grading, grading)
    check('no shipped copper closer than the Default class', n == 0,
          f'{n} violation(s)\n' + dlog[dlog.find('FOUND'):][:900] if n else '')
    rows = clearance_rows(summ)
    check('no clearance descent recorded', not rows, str(rows))
    check('the run says why', '#1262' in log)

    print('D. writeback on (control)')
    out2 = os.path.join(tmp, 'on.kicad_pcb')
    log2, summ2 = route(inp, out2, [])
    evidence(out2, 'routed board (writeback on)')
    rows2 = clearance_rows(summ2)
    check('the rescue still descends to 0.2125, recorded',
          any(r['delivered'] == 0.2125 and r['site'] == 'net rescue'
              and r.get('net_name') == '/FB' for r in rows2), str(rows2))
    with open(os.path.splitext(out2)[0] + '.kicad_pro', encoding='utf-8') as f:
        p2 = json.load(f)
    check('the output project Default class is lowered to it',
          fab_tiers.project_default_class_clearance(p2) == 0.2125,
          str(fab_tiers.project_default_class_clearance(p2)))
    n2, grading2, _ = drc(out2)
    check('...and the board grades clean against the project it ships with',
          n2 == 0 and '0.2125' in grading2, f'{n2} {grading2}')


def main():
    tmp = tempfile.mkdtemp(prefix='t1262_')
    try:
        # Each section on its own, so a missing helper (the base) is a FAIL
        # of that section and the end-to-end evidence still runs. A broken
        # TEST (check_drc printing no verdict, route.py crashing) still raises.
        for name, fn in (('A', t_rule), ('B', lambda: t_fronts(tmp))):
            try:
                fn()
            except AttributeError as e:
                check(f'{name}: the rule exists', False, repr(e))
        t_route(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILS:
        print(f'{len(FAILS)} FAILURE(S): {", ".join(FAILS)}')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
