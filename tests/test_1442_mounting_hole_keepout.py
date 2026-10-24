#!/usr/bin/env python3
"""#1442: a mounting hole's screw-head ring is checked, and can be declared.

KiCad checks a mounting hole against the ordinary hole clearance only, so
route.py laid +5V and signal tracks 0.10-0.25 mm from holes whose M3 head
covers 1.3 mm past the edge, and no checker saw it.

  1. mounting_holes: a footprint of 9 pads or fewer, all drilled 2.0 mm or
     wider, none on a signal net, is a hole; a via-sized drill, a signal pad
     and a ground-tied hole are told apart.
  2. check_mounting_holes: exit 1, naming the hole and the nets, when another
     net's track or via is inside the ring; exit 0 with the track outside it
     or on the hole's own net.
  3. add_rule_area --mounting-holes writes one area per net-less hole (the
     ground-tied hole is left open, said so), re-running replaces it, and the
     parser reads it back with tracks and vias not allowed.
  4. route.py: a net routed past a hole comes within the ring without the
     areas (non-vacuity), and keeps out of it with them.

    python3 tests/test_1442_mounting_hole_keepout.py
"""
import contextlib
import io
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, TESTS_DIR)

from synth import RoutingBoard                                 # noqa: E402
from run_utils import check as run_check, evidence, tool       # noqa: E402

failures = []


def ok(label, cond, detail=''):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


def hole(rb, ref, x, y, drill=3.2, net=''):
    """A one-pad mounting-hole footprint: NPTH, or plated on `net`."""
    if net:
        n = rb.net(net)
        pad = ('(pad "1" thru_hole circle (at 0 0) (size %s %s) (drill %s) '
               '(layers "*.Cu" "*.Mask") (net %d "%s") (uuid "%s"))'
               % (drill + 1, drill + 1, drill, n, net, rb._uid()))
    else:
        pad = ('(pad "" np_thru_hole circle (at 0 0) (size %s %s) (drill %s) '
               '(layers "*.Cu" "*.Mask") (uuid "%s"))'
               % (drill, drill, drill, rb._uid()))
    rb.items.append(
        '  (footprint "synth:MH" (layer "F.Cu") (uuid "%s") (at %s %s)\n'
        '    (property "Reference" "%s" (at 0 -4 0) (layer "F.SilkS") '
        '(uuid "%s")\n      (effects (font (size 0.5 0.5) (thickness 0.1))))\n'
        '    %s\n  )' % (rb._uid(), x, y, ref, rb._uid(), pad))


def write(rb, path):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(rb.text())
    return evidence(path, 'the synthetic board')


def py(script, *args, **kw):
    return run_check([sys.executable, '-X', 'utf8', script, *args], **kw)


def parse(path):
    from kicad_parser import parse_kicad_pcb
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return parse_kicad_pcb(path)


def main():
    from mounting_holes import mounting_holes, hole_copper
    checker = tool('check_mounting_holes.py')
    adder = tool('add_rule_area.py')
    with tempfile.TemporaryDirectory() as tmp:
        print('1: which footprints are mounting holes')
        rb = RoutingBoard(w=60, h=40)
        hole(rb, 'H1', 10, 10)                       # an M3 NPTH
        hole(rb, 'H2', 30, 10, drill=2.2)            # an M2 NPTH
        hole(rb, 'H3', 50, 10, net='GND')            # plated, ground-tied
        hole(rb, 'H4', 10, 30, drill=0.9)            # a via-sized drill
        hole(rb, 'H5', 30, 30, net='+5V')            # a signal pad
        p = write(rb, os.path.join(tmp, 'holes.kicad_pcb'))
        found = {h.ref: h for h in mounting_holes(parse(p))}
        ok('M3, M2 and the ground-tied hole are mounting holes',
           sorted(found) == ['H1', 'H2', 'H3'], str(sorted(found)))
        ok('the ground-tied hole carries its net', bool(found['H3'].nets))
        ok('the ring is 0.4 x the drill',
           abs(found['H1'].ring() - 1.28) < 1e-9
           and abs(found['H2'].ring() - 0.88) < 1e-9)

        print('2: check_mounting_holes')
        rb = RoutingBoard(w=60, h=40)
        hole(rb, 'H1', 20, 20)
        rb.pad('R1', 5, 20, '/SIG')
        rb.pad('R2', 35, 20, '/SIG')
        rb.seg(5, 20, 35, 20, '/SIG', w=0.3)         # through the hole's ring
        p = write(rb, os.path.join(tmp, 'in_ring.kicad_pcb'))
        r = run_check([sys.executable, '-X', 'utf8', checker, p],
                      refuse='MOUNTING HOLE COPPER', code=1)
        ok('exit 1 names the hole and the net', 'H1' in r.stdout and '/SIG' in r.stdout,
           r.stdout[-300:])
        rb = RoutingBoard(w=60, h=40)
        hole(rb, 'H1', 20, 20)
        rb.pad('R1', 5, 20, '/SIG')
        rb.pad('R2', 35, 20, '/SIG')
        rb.seg(5, 20, 5, 30, '/SIG', w=0.3)
        rb.seg(5, 30, 35, 30, '/SIG', w=0.3)         # 10 mm away
        rb.seg(35, 30, 35, 20, '/SIG', w=0.3)
        p = write(rb, os.path.join(tmp, 'clear.kicad_pcb'))
        r = py(checker, p, accept=True)
        ok('a track outside the ring passes', 'ring(s) clear' in r.stdout, r.stdout[-200:])
        rb = RoutingBoard(w=60, h=40)
        hole(rb, 'H1', 20, 20, net='GND')
        rb.pad('R1', 5, 20, 'GND')
        rb.pad('R2', 35, 20, 'GND')
        rb.seg(5, 20, 35, 20, 'GND', w=0.3)          # the hole's own net
        p = write(rb, os.path.join(tmp, 'own_net.kicad_pcb'))
        py(checker, p, accept=True)
        ok("copper of the hole's own net passes", True)
        rb = RoutingBoard(w=60, h=40)
        hole(rb, 'H1', 20, 20)
        rb.via(21.5, 20, '/SIG')                     # inside the ring, a via
        p = write(rb, os.path.join(tmp, 'via.kicad_pcb'))
        run_check([sys.executable, '-X', 'utf8', checker, p],
                  refuse='under the screw head', code=1)
        ok('a via inside the ring fails', True)

        print('3: add_rule_area --mounting-holes')
        src = os.path.join(tmp, 'holes.kicad_pcb')
        out = os.path.join(tmp, 'with_areas.kicad_pcb')
        r = py(adder, src, out, '--name', 'MOUNT', '--mounting-holes', accept=True)
        ok('the ground-tied hole is left open, and it says so',
           'H3' in r.stdout and 'left open' in r.stdout, r.stdout[-300:])
        kos = parse(out).board_info.keepouts
        ok('one area per net-less hole, tracks and vias not allowed, pour allowed',
           len(kos) == 2 and all(k.get('tracks_allowed') is False
                                 and k.get('vias_allowed') is False
                                 and k.get('copper_pour_allowed') is not False
                                 for k in kos), str(kos)[:300])
        cx = sorted(sum(x for x, _ in k['polygon']) / len(k['polygon']) for k in kos)
        ok('centred on the holes (10 and 30 mm)',
           abs(cx[0] - 10) < 1e-6 and abs(cx[1] - 30) < 1e-6, str(cx))
        r = py(adder, out, out, '--name', 'MOUNT', '--mounting-holes', accept=True)
        ok('a re-run replaces them', len(parse(out).board_info.keepouts) == 2
           and 'replaced 1 earlier area' in r.stdout, r.stdout[-200:])
        py(adder, src, os.path.join(tmp, 'x.kicad_pcb'), '--name', 'M',
           '--mounting-holes', '--ref', 'H1', refuse='finds the holes itself', code=2,
           allow=('usage:',))

        print('4: route.py keeps out of the ring')
        def board(path):
            rb = RoutingBoard(w=60, h=40)
            hole(rb, 'H1', 30, 20)
            rb.pad('R1', 10, 20, '/SIG')
            rb.pad('R2', 50, 20, '/SIG')
            return write(rb, path)
        route = tool('route.py')
        free = board(os.path.join(tmp, 'free.kicad_pcb'))
        rout = os.path.join(tmp, 'free_routed.kicad_pcb')
        py(route, free, rout, '--nets', '/SIG', accept=True, timeout=600)
        near = [h for _h, hs in hole_copper(parse(rout)) for h in hs]
        ok('without the area the route comes inside the ring (non-vacuity)',
           bool(near), str(near[:2]))
        areas = os.path.join(tmp, 'areas.kicad_pcb')
        py(adder, free, areas, '--name', 'MOUNT', '--mounting-holes', accept=True)
        rout2 = os.path.join(tmp, 'areas_routed.kicad_pcb')
        py(route, areas, rout2, '--nets', '/SIG', accept=True, timeout=600)
        segs = [s for s in parse(rout2).segments if s.net_id]
        near2 = [h for _h, hs in hole_copper(parse(rout2)) for h in hs]
        ok('with the area the route keeps out, and is still routed',
           not near2 and len(segs) >= 1, f'{len(segs)} segments, {near2[:2]}')
        py(checker, rout2, accept=True)

    if failures:
        print(f"\nFAIL ({len(failures)}): " + '; '.join(failures))
        return 1
    print('\nAll checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
