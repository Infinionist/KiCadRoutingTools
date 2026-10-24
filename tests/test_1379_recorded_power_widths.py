#!/usr/bin/env python3
"""#1379: the power widths asked of route.py reach the DONE graders.

route.py measured `power_widths` into JSON_SUMMARY and printed "N mm under the
requested W", but recorded no width, so board_score saw `net_widths` as
UNGRADED and check_complete said DONE on a board whose battery path was 72%
under width. What must hold:

  * the widths a step was asked for are recorded in the output's
    `kicad_routing_tools` records (`power_widths`, net name -> mm), and merge
    with the records of earlier steps;
  * board_score reads them with no flag and grades `net_widths` against them
    (exact net names, after any --net-min-widths patterns, which win);
  * a segment under the requirement is excused ONLY as a pad neck (#1318):
    inside the pad-entry zone of a narrower own-net pad, at no less than that
    pad's short side -- narrower than the pad, or beyond the zone, it counts,
    and what was excused is disclosed (`pad_neck_allowed_mm`);
  * a board with neither a file nor a record stays UNGRADED;
  * END TO END: route.py with --power-nets-widths writes the record, board_score
    exits 4 with net_widths failing, and check_complete is not DONE.

    python3 -X utf8 tests/test_1379_recorded_power_widths.py
"""
import json
import os
import sys
import tempfile

RUN_ALL_TIMEOUT = 600

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _d in ('py_router', 'py_tools', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from run_utils import check as run_check, evidence, tool  # noqa: E402
import protected_nets  # noqa: E402

fails = []


def check(name, cond, detail=''):
    print(('PASS: ' if cond else 'FAIL: ') + name + (f'  {detail}' if detail else ''))
    if not cond:
        fails.append(name)


# a 0.3 x 0.6 pad on net 1 at (10, 10); one segment per case
PAD_X, PAD_Y = 10.0, 10.0


def board_text():
    seg = ('\t(segment\n\t\t(start %s %s)\n\t\t(end %s %s)\n\t\t(width %s)\n'
           '\t\t(layer "F.Cu")\n\t\t(net 1)\n\t\t(uuid "s%d")\n\t)\n')
    segs = [
        (PAD_X, PAD_Y, PAD_X + 1.0, PAD_Y, 0.3),          # at the pad's short side, in the zone
        (PAD_X, PAD_Y + 0.5, PAD_X + 1.0, PAD_Y + 0.5, 0.15),  # in the zone, narrower than the pad
        (60.0, 60.0, 70.0, 60.0, 0.3),                    # far from any pad
        (60.0, 61.0, 70.0, 61.0, 0.5),                    # at the requirement
    ]
    fp = ('\t(footprint "t:FP"\n\t\t(layer "F.Cu")\n\t\t(uuid "fp1")\n\t\t(at %s %s)\n'
          '\t\t(property "Reference" "J1"\n\t\t\t(at 0 0)\n\t\t)\n'
          '\t\t(pad "1" smd rect\n\t\t\t(at 0 0)\n\t\t\t(size 0.3 0.6)\n'
          '\t\t\t(layers "F.Cu")\n\t\t\t(net 1 "PWR")\n\t\t\t(uuid "p1")\n\t\t)\n\t)\n'
          % (PAD_X, PAD_Y))
    body = ''.join(seg % (a, b, c, d, w, i) for i, (a, b, c, d, w) in enumerate(segs))
    return ('(kicad_pcb\n\t(version 20241229)\n\t(generator "synth")\n'
            '\t(net 0 "")\n\t(net 1 "PWR")\n'
            '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 200 200)\n\t\t(layer "Edge.Cuts")\n'
            '\t\t(uuid "edge")\n\t)\n' + fp + body + ')\n')


def write_project(path, ns=None):
    doc = {'meta': {'filename': os.path.basename(path), 'version': 3}}
    if ns is not None:
        doc['kicad_routing_tools'] = ns
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)


def t_record_and_grade():
    import board_score
    with tempfile.TemporaryDirectory() as d:
        pcb = os.path.join(d, 'b.kicad_pcb')
        pro = os.path.join(d, 'b.kicad_pro')
        with open(pcb, 'w', encoding='utf-8') as fh:
            fh.write(board_text())

        # nothing asked, nothing recorded: ungraded, said so
        write_project(pro)
        r = board_score.score_net_widths(pcb, None)
        check('no file and no record: net_widths is skipped, not clean',
              r.get('ran') is False and r.get('count') is None, str(r))

        # the record round-trips and merges with an earlier step's
        protected_nets.persist_power_widths(pro, {'PWR': 0.5}, verbose=False)
        protected_nets.persist_power_widths(pro, {'OTHER': 0.3}, verbose=False)
        check('persist_power_widths records and merges',
              protected_nets.read_power_widths(pro) == {'PWR': 0.5, 'OTHER': 0.3},
              str(protected_nets.read_power_widths(pro)))

        r = board_score.score_net_widths(pcb, None)
        det = (r.get('nets') or {}).get('PWR', {})
        check('recorded request is graded with no flag',
              r.get('ran') and r['count'] == 1 and det.get('required_mm') == 0.5, str(r))
        check('the pad neck at the pad width is excused and disclosed',
              abs(r['pad_neck_allowed_mm'].get('PWR', 0) - 1.0) < 1e-6, str(r))
        check('narrower than the pad, or far from it, still counts',
              det.get('segments_under') == 2
              and abs(det.get('length_under_mm', 0) - 11.0) < 1e-6, str(det))
        check('the segment at the requirement is not under',
              det.get('segments_total') == 4, str(det))

        # an explicit file pattern outranks the record
        spec = os.path.join(d, 'w.json')
        with open(spec, 'w', encoding='utf-8') as fh:
            json.dump({'PW*': 0.1}, fh)
        r = board_score.score_net_widths(pcb, spec)
        check('a --net-min-widths pattern outranks the recorded request',
              r.get('ran') and r['count'] == 0, str(r))


def t_end_to_end():
    src = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
    evidence(src)
    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, 'esp.kicad_pcb')
        out = os.path.join(d, 'r.kicad_pcb')
        run_check([sys.executable, '-X', 'utf8', tool('copy_board.py'), src, a],
                accept=True)
        run_check([sys.executable, '-X', 'utf8', tool('route.py'), a, '--output', out,
                   '--power-nets', 'GND', '/+3.3V', 'Net-(C1-Pad1)',
                   '--power-nets-widths', '0.6', '0.6', '0.6'], accept=True)
        rec = protected_nets.read_power_widths(protected_nets.pro_path_for_board(out))
        check('route.py records the widths it was asked for',
              rec == {'GND': 0.6, '/+3.3V': 0.6, 'Net-(C1-Pad1)': 0.6}, str(rec))
        import subprocess
        js = os.path.join(d, 's.json')
        p = subprocess.run([sys.executable, '-X', 'utf8', tool('board_score.py'), out,
                            '--quiet', '--json', js], capture_output=True, text=True)
        nw = json.load(open(evidence(js), encoding='utf-8'))['components']['net_widths']
        check('board_score grades net_widths unprompted and fails the board',
              nw.get('ran') and nw['count'] >= 1 and p.returncode == 4,
              f"exit {p.returncode} {nw.get('count')}")
        c = subprocess.run([sys.executable, '-X', 'utf8', tool('check_complete.py'), out],
                           capture_output=True, text=True, cwd=ROOT)
        check('check_complete no longer says DONE',
              'VERDICT: DONE' not in c.stdout and 'net_widths' not in
              c.stdout.split('UNEXAMINED')[-1], c.stdout[-300:])


for fn in (t_record_and_grade, t_end_to_end):
    fn()

print('\nFAILED: ' + ', '.join(fails) if fails else '\nall passed')
sys.exit(1 if fails else 0)
