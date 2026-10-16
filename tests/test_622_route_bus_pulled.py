#!/usr/bin/env python3
"""A bus net with a PULL beside its two ends (route_bus.pulled_nets, unpull, repull).

  python3 tests/test_622_route_bus_pulled.py

1. pulled_nets: a net with one pad on each of the bus's two parts whose every other pad is a two-pad part's, the
   part's other pad on a plane's net (one that owns a zone), is pulled; one already among the bus's nets, one whose
   extra part's other pad is a signal (a series part), one with a third pad on a part of more pads, one with two pads
   on an end, and a plane's own net, are not;
2. unpull takes the pull pad off its net (its (net ...) token, and nothing else) and returns the token; repull puts it
   back after the pad's (layers ...), the text as it was; a pad not found is named.
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'awx'))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
import route_bus as rb  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print('FAIL', msg)


# 1. pulled_nets, on a board stood in for: U1 and U5 the bus's parts, RA..RD two-pad parts, U9 a part of four pads
def pad(ref, num, nid):
    return types.SimpleNamespace(component_ref=ref, pad_number=num, net_id=nid)


NETS = {1: 'GND', 2: 'PULLED', 3: 'BUS', 4: 'SERIES', 5: 'MID', 6: 'MULTI', 7: 'TWICE', 8: 'OTHER'}
PADS = [pad('U1', 'A1', 2), pad('U5', 'B1', 2), pad('RA', '2', 2), pad('RA', '1', 1),       # PULLED: RA to GND
        pad('U1', 'A2', 3), pad('U5', 'B2', 3), pad('RB', '2', 3), pad('RB', '1', 1),       # BUS: in names already
        pad('U1', 'A3', 4), pad('U5', 'B3', 4), pad('RC', '2', 4), pad('RC', '1', 5),       # SERIES: RC to a signal
        pad('U1', 'A4', 6), pad('U5', 'B4', 6), pad('U9', '1', 6), pad('U9', '2', 1),       # MULTI: U9 not two-pad
        pad('U9', '3', 8), pad('U9', '4', 8),
        pad('U1', 'A5', 7), pad('U1', 'A6', 7), pad('U5', 'B5', 7), pad('RD', '2', 7), pad('RD', '1', 1),  # TWICE
        pad('U1', 'A7', 1), pad('U5', 'B7', 1)]                                              # GND: the plane's own
nets = {i: types.SimpleNamespace(net_id=i, name=n, pads=[p for p in PADS if p.net_id == i]) for i, n in NETS.items()}
fps = {}
for p in PADS:
    fps.setdefault(p.component_ref, types.SimpleNamespace(pads=[])).pads.append(p)
pcb = types.SimpleNamespace(nets=nets, footprints=fps, zones=[types.SimpleNamespace(net_id=1)])
got = rb.pulled_nets(pcb, 'U1', 'U5', ['BUS'])
check(sorted(got) == ['PULLED'], f'1: PULLED alone: {sorted(got)}')
check([(p.component_ref, p.pad_number) for p in got.get('PULLED', ())] == [('RA', '2')], f'1: its pull pad: {got}')
pcb.zones = []
check(rb.pulled_nets(pcb, 'U1', 'U5', ['BUS']) == {}, '1: no plane, no pull')

# 2. unpull and repull on a board's text
TXT = '''(kicad_pcb
\t(footprint "R_0402"
\t\t(layer "B.Cu")
\t\t(at 1 2 90)
\t\t(property "Reference" "RA"
\t\t\t(at 0 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at -0.4 0 90)
\t\t\t(size 0.6 0.65)
\t\t\t(layers "B.Cu" "B.Mask" "B.Paste")
\t\t\t(net "GND")
\t\t\t(uuid "aaaa")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.4 0 90)
\t\t\t(size 0.6 0.65)
\t\t\t(layers "B.Cu" "B.Mask" "B.Paste")
\t\t\t(net "PULLED")
\t\t\t(pinfunction "x(y)")
\t\t\t(uuid "bbbb")
\t\t)
\t)
)
'''
t2, held = rb.unpull(TXT, [pad('RA', '2', 2)])
check(held == [('RA', '2', '\n\t\t\t(net "PULLED")')], f'2: the token taken: {held}')
check(t2 == TXT.replace('\n\t\t\t(net "PULLED")', ''), '2: unpull takes the token and nothing else')
check('(net "GND")' in t2, '2: the other pad keeps its net')
t3, missed = rb.repull(t2, held)
check(t3 == TXT and missed == [], f'2: repull puts it back where it was: missed {missed}')
_t, missed = rb.repull(t2, [('RZ', '2', '(net "X")')])
check(missed == ['RZ.2'], f'2: a pad not found is named: {missed}')
t4, held4 = rb.unpull(TXT, [pad('RQ', '1', 1)])
check(t4 == TXT and held4 == [], '2: a pad not on the board: nothing taken')

print('route_bus pulled: ' + ('FAIL' if fails else 'PASS: a pulled net found; its pull pad off its net and back, '
                                                  'the text as it was'))
sys.exit(1 if fails else 0)
