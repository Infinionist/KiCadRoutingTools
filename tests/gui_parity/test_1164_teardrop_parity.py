#!/usr/bin/env python3
"""#1164 parity gate: both parse paths flag the same zones as teardrops, and the two-path diff sees a disagreement.

`Zone.teardrop` decides `pour_zones()` membership: rip-eligibility, the
--force-reroute skip, the plane-finalize hand-off and the GUI's plane
suggestions. The two fronts derive it independently -- `parse_kicad_pcb` from
the `(attr (teardrop ...))` text of the zone header, `build_pcb_data_from_board`
from `ZONE.IsTeardropArea()` -- and `compare_pcb_data` used to pair zones by
sort order on (net, layer) and compare neither the flag nor anything that
could tell a pour from a teardrop sharing its net and layer. Arms:

1. **A board pcbnew WROTE.** The tracked lvds_converter_dualclk_gnd board
   (a /GND pour on B.Cu) is loaded with pcbnew, given teardrop zones through
   `ZONE.SetTeardropAreaType` -- three of them on the pour's own net and
   layer, the case sort-order pairing could not tell apart, and two
   track-end teardrops on F.Cu -- and saved with `SaveBoard` to a temp dir,
   which writes its `.kicad_pro`. (The setter takes a `TEARDROP_TYPE`, which
   KiCad's SWIG does not wrap as an enum; the two values are read off a
   template board pcbnew loaded.)
2. **A native oracle.** pcbnew's own `IsTeardropArea()` count on the saved
   board equals the declared count.
3. **Witnesses.** On BOTH parse paths, the teardrop zones per (net, layer)
   equal the declared ones, and `pour_zones()` drops exactly them and keeps
   the pour. A parser reading False everywhere passes every parity arm; this
   makes it fail.
4. **Parity.** `compare_pcb_data` reports no zone line for the saved board.
5. **The comparator sees a disagreement.** The pcbnew model of the saved
   board against the text parse of the same file with ONE teardrop's
   `(attr ...)` deleted -- once in the shared /GND B.Cu group, once on
   F.Cu -- must be reported as a teardrop-count difference on that net and
   layer.

Needs pcbnew; re-execs into KiCad's python automatically. Exits 2 when no
pcbnew python is found, rather than 0, so a mutation battery using it as a
killer cannot score SURVIVED on an environment accident.

    python3 -X utf8 tests/gui_parity/test_1164_teardrop_parity.py
"""
import collections
import glob
import os
import re
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

BOARD = os.path.join(REPO, 'kicad_files', 'lvds_converter_dualclk_gnd.kicad_pcb')
POUR_NET, POUR_LAYER = '/GND', 'B.Cu'

FAILURES = []


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


TEMPLATE = '''(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
\t(zone (layer "F.Cu") (uuid "11111111-1111-1111-1111-111111111111") (hatch full 0.1)
\t\t(attr (teardrop (type padvia)))
\t\t(fill yes) (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))
\t(zone (layer "F.Cu") (uuid "22222222-2222-2222-2222-222222222222") (hatch full 0.1)
\t\t(attr (teardrop (type track_end)))
\t\t(fill yes) (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))
)
'''


def teardrop_types(tmp):
    """{'padvia': TEARDROP_TYPE, 'track_end': TEARDROP_TYPE}, read off a board
    pcbnew loaded: SWIG exposes the type only as an opaque value."""
    import pcbnew
    path = os.path.join(tmp, 'template.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(TEMPLATE)
    board = pcbnew.LoadBoard(path)
    zones = list(board.Zones())
    check(len(zones) == 2 and all(z.IsTeardropArea() for z in zones),
          'pcbnew reads both template zones as teardrops',
          [z.IsTeardropArea() for z in zones])
    return board, {'padvia': zones[0].GetTeardropAreaType(),
                   'track_end': zones[1].GetTeardropAreaType()}


def build_fixture(tmp):
    """Save BOARD through pcbnew with five teardrop zones added; returns the
    saved path and the declared {(net name, layer): count}."""
    import pcbnew
    keep, types = teardrop_types(tmp)
    board = pcbnew.LoadBoard(BOARD)
    gnd = board.FindNet(POUR_NET)
    pours = [z for z in board.Zones()
             if z.GetNetname() == POUR_NET and z.GetLayer() == pcbnew.B_Cu]
    check(gnd is not None and len(pours) == 1,
          'the fixture carries one %s pour on %s' % (POUR_NET, POUR_LAYER), len(pours))
    other = next(p.GetNet() for fp in board.GetFootprints() for p in fp.Pads()
                 if p.GetNetCode() > 0 and p.GetNetname() != POUR_NET)
    # (net, layer, type, triangle) -- three on the pour's own net + layer,
    # the triangles the same size, so only the flag tells them from each other.
    plan = [(gnd, pcbnew.B_Cu, 'padvia', (120.0, 80.0)),
            (gnd, pcbnew.B_Cu, 'padvia', (122.0, 80.0)),
            (gnd, pcbnew.B_Cu, 'track_end', (124.0, 80.0)),
            (other, pcbnew.F_Cu, 'track_end', (120.0, 84.0)),
            (other, pcbnew.F_Cu, 'padvia', (122.0, 84.0))]
    declared = collections.Counter()
    for net, layer, kind, (x, y) in plan:
        z = pcbnew.ZONE(board)
        z.SetLayer(layer)
        z.SetNetCode(net.GetNetCode())
        z.SetTeardropAreaType(types[kind])
        z.Outline().NewOutline()
        for px, py in ((x, y), (x + 1.0, y), (x + 0.5, y + 1.0)):
            z.AppendCorner(pcbnew.VECTOR2I(pcbnew.FromMM(px), pcbnew.FromMM(py)), -1)
        board.Add(z)
        declared[(net.GetNetname(), board.GetLayerName(layer))] += 1
    check(sum(z.IsTeardropArea() for z in board.Zones()) == len(plan),
          'pcbnew holds the %d teardrops before saving' % len(plan))
    out = os.path.join(tmp, 'teardrops.kicad_pcb')
    pcbnew.SaveBoard(out, board)
    check(os.path.isfile(os.path.splitext(out)[0] + '.kicad_pro'),
          'SaveBoard wrote the board with its .kicad_pro')
    del keep
    return out, declared


def teardrops_by_group(pcb):
    return collections.Counter((z.net_name, z.layer) for z in pcb.zones if z.teardrop)


def drop_one_attr(text, net_name, layer):
    """The board text with the (attr ...) block of the FIRST teardrop zone on
    (net_name, layer) deleted, or None when there is none."""
    from kicad_parser import find_matching_paren
    for m in re.finditer(r'\(zone(?=[\s(])', text):
        end = find_matching_paren(text, m.start())
        block = text[m.start():end]
        hdr = block[:block.find('(polygon')]
        if ('(net "%s")' % net_name) not in hdr or ('(layer "%s")' % layer) not in hdr:
            continue
        a = re.search(r'\(attr\s*\(teardrop', block)
        if not a:
            continue
        a0 = m.start() + a.start()
        a1 = find_matching_paren(text, a0)
        return text[:a0] + text[a1:]
    return None


def main():
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        _reexec_into_kicad()
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    import pcbnew
    from kicad_parser import (parse_kicad_pcb, build_pcb_data_from_board,
                              compare_pcb_data, pour_zones)

    tmp = tempfile.mkdtemp(prefix='td1164_')
    print('Fixture: teardrops written by pcbnew')
    path, declared = build_fixture(tmp)
    n_declared = sum(declared.values())

    print('\nNative oracle')
    board = pcbnew.LoadBoard(path)
    native = collections.Counter((z.GetNetname(), board.GetLayerName(z.GetLayer()))
                                 for z in board.Zones() if z.IsTeardropArea())
    check(native == declared, 'pcbnew reads back the %d declared teardrops' % n_declared,
          (dict(native), dict(declared)))

    print('\nWitnesses: both parse paths')
    f = parse_kicad_pcb(path)
    g = build_pcb_data_from_board(board)
    for name, pcb in (('text', f), ('pcbnew', g)):
        got = teardrops_by_group(pcb)
        check(got == declared, '%s path: the teardrops per (net, layer) are the declared ones'
              % name, (dict(got), dict(declared)))
        pours = pour_zones(pcb.zones)
        check(len(pcb.zones) - len(pours) == n_declared,
              '%s path: pour_zones drops exactly the %d teardrops' % (name, n_declared),
              (len(pcb.zones), len(pours)))
        gnd_pours = [z for z in pours if z.net_name == POUR_NET and z.layer == POUR_LAYER]
        check(len(gnd_pours) == 1, '%s path: pour_zones keeps the %s %s pour'
              % (name, POUR_NET, POUR_LAYER), len(gnd_pours))

    print('\nParity')
    zone_diffs = [d for d in compare_pcb_data(g, f) if d.startswith('Zone')]
    check(not zone_diffs, 'compare_pcb_data reports no zone difference', zone_diffs[:4])

    print('\nThe comparator sees one teardrop flag disagree')
    with open(path, encoding='utf-8', newline='') as fh:
        text = fh.read()
    for net_name, layer in ((POUR_NET, POUR_LAYER),
                            next(k for k in declared if k[0] != POUR_NET)):
        mutated = drop_one_attr(text, net_name, layer)
        check(mutated is not None and mutated != text,
              'mutation: one %s %s teardrop lost its (attr ...)' % (net_name, layer))
        if mutated is None:
            continue
        mpath = os.path.join(tmp, 'mutated.kicad_pcb')
        with open(mpath, 'w', encoding='utf-8', newline='') as fh:
            fh.write(mutated)
        fm = parse_kicad_pcb(mpath)
        check(sum(teardrops_by_group(fm).values()) == n_declared - 1,
              'the text parser reads one teardrop fewer from the mutated file')
        nid = next(i for i, n in g.nets.items() if n.name == net_name)
        want = 'Zone net=%d layer=%s teardrop count: board=%d file=%d' % (
            nid, layer, declared[(net_name, layer)], declared[(net_name, layer)] - 1)
        got = compare_pcb_data(g, fm)
        check(want in got, 'compare_pcb_data names it: %r' % want,
              [d for d in got if d.startswith('Zone')][:4])

    print('\n%d failure(s)' % len(FAILURES))
    for fl in FAILURES:
        print('  FAILED: %s' % fl)
    if FAILURES:
        return 1
    print('PASS  #1164 teardrop parity (text vs pcbnew, and the comparator sees it)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
