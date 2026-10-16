#!/usr/bin/env python3
"""The escape fanout never lays two lanes' runs on one layer within the rule (escape_plan.clear_lanes, lanes_of).
  python3 tests/test_622_escape_clear_lanes.py
sdi_mipi_conv's SDI bus: its ends chosen again on the dog-boned field, where three lanes had no clean tooth and were
offered as they stood, laid SDI_D1, D3 and D6's runs on one channel line of U11 on In2.Cu (overlapping copper); lay()
counted the 13 faults and the round handed the board on. On moves stood in for (legs, layer):
1. three lanes' runs along one line on one layer: the first kept, the other two left out;
2. a lane on that line on ANOTHER layer is kept, and a lane clear of the line on the same layer is kept;
3. a pair's legs are one lane (lanes_of): left out together when one leg meets a kept run, and its own legs side by
   side at the rule are no fault between them;
4. lay()'s faults are run_faults: the kept moves have none.
"""
import os
import sys
import types
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'awx'))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
import escape_plan as ep  # noqa: E402
fails = []


def check(cond, msg):
    print(('PASS ' if cond else 'FAIL ') + msg)
    if not cond:
        fails.append(msg)


R = types.SimpleNamespace(fan_track=0.12, fan_clear=0.1)
names = ['D1', 'D3', 'D6', 'D2', 'D4', 'DQSP', 'DQSN', 'E1']
byname = {nm: (i + 1, None) for i, nm in enumerate(names)}


def run(x0, y, x1, layer):
    return types.SimpleNamespace(legs=[((x0, y), (x1, y), layer)], layer=layer)


moves = [('D1', run(60.1, 68.3, 71.7, 'In2.Cu')), ('D3', run(60.5, 68.3, 71.7, 'In2.Cu')),
         ('D6', run(60.9, 68.3, 71.7, 'In2.Cu')),                         # 1. three on one line, one layer
         ('D2', run(60.5, 68.3, 71.7, 'In4.Cu')),                         # 2. the line, another layer
         ('D4', run(60.5, 69.1, 71.7, 'In2.Cu')),                         # 2. the next channel, same layer
         ('DQSP', run(62.0, 68.3, 71.7, 'B.Cu')), ('DQSN', run(62.0, 68.52, 71.7, 'B.Cu')),   # 3. a pair at the rule
         ('E1', run(62.0, 67.9, 71.7, 'F.Cu'))]
lanes = ep.lanes_of(names)
check(['DQSP', 'DQSN'] in lanes and ['D1'] in lanes and len(lanes) == 7, f'3. lanes_of: a pair one lane -- {lanes}')
kept, left = ep.clear_lanes(moves, byname, lanes, R)
kn = [nm for nm, _m in kept]
check(left[:2] == [['D3'], ['D6']] and 'D1' in kn, f'1. the first of three kept, the other two left out -- {left}')
check('D2' in kn and 'D4' in kn, '2. another layer, and the next channel, kept')
check('DQSP' in kn and 'DQSN' in kn, '3. a pair\'s own legs at the rule are no fault between them')
# 3. a pair one of whose legs meets a kept run: left out whole
moves2 = [('D1', run(60.1, 68.3, 71.7, 'B.Cu')), ('DQSP', run(62.0, 68.3, 71.7, 'B.Cu')),
          ('DQSN', run(62.0, 68.52, 71.7, 'B.Cu'))]
kept2, left2 = ep.clear_lanes(moves2, byname, ep.lanes_of(['D1', 'DQSP', 'DQSN']), R)
check(left2 == [['DQSP', 'DQSN']] and [nm for nm, _m in kept2] == ['D1'], f'3. the pair left out whole -- {left2}')
tr = ep.run_tracks(kept, byname, R)
check(not [f for i, t in enumerate(tr) for f in ep.run_faults([t], tr[i + 1:], R)], '4. the kept moves: no fault')
print(f'\n{len(fails)} failure(s)')
sys.exit(1 if fails else 0)
