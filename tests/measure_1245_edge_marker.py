#!/usr/bin/env python3
"""#1245 calibration: does the "PCB Edge" marker reader agree with the body?

`mating.marker_face_local` reads the face a footprint's "PCB Edge" marker
implies: the straight line beside the marker text, parallel to its baseline,
with the pads on the board side of it. `mating_face_local` lets that reading
CONFIRM the body's face (strong) or VETO it (an abstention), never name one
alone. The reference this measures it against: wherever the body's own face
is STRONG (offset >= `FACE_STRONG_OFFSET_MM`), the marker must agree every
time it does not abstain -- a strong face is the one no wrong reading is
known for.

Two populations:

1. **KiCad's stock library**, every footprint whose own text matches
   `parser.EDGE_MARKER_TEXT`, each placed at rotation 0 on one synthetic
   board and read through the production path (`body.board_bodies` ->
   `mating.board_faces`), so the parse, the body ladder and the reader are
   the ones a real board meets. Found under KiCad's install
   (`KICAD_FOOTPRINT_DIR` overrides); absent -> NOT RUN, exit 2.
2. **The tracked boards' marked parts**, at their real poses (rotated,
   B-side): esp_prog USB1, orangecrab_ext_pll J3, sonde_u J1/J2.

The table is the BODY's verdict (strong / weak / abstained) against the
MARKER's reading (agrees / contradicts / abstains). The gate: zero
`contradicts` against a strong body face, in either population.

    python3 -X utf8 tests/measure_1245_edge_marker.py [--detail]

A measurement tool; `run_all` does not collect `measure_*`.
"""
import glob
import os
import re
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
for _p in (ROOT_DIR, os.path.join(ROOT_DIR, 'py_router'),
           os.path.join(ROOT_DIR, 'py_placer'), TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import mating  # noqa: E402
from placement.parser import EDGE_MARKER_TEXT  # noqa: E402

LIB_CANDIDATES = (
    '/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints',
    '/usr/share/kicad/footprints',
    '/usr/local/share/kicad/footprints',
    r'C:\Program Files\KiCad\10.0\share\kicad\footprints',
    r'C:\Program Files\KiCad\9.0\share\kicad\footprints',
)
TRACKED = (('esp_prog.kicad_pcb', ('USB1',)),
           ('orangecrab_ext_pll.kicad_pcb', ('J3',)),
           ('sonde_u.kicad_pcb', ('J1', 'J2')))
_MARK = re.compile(r'\(fp_text\s+user\s+"([^"]*)"')


def library_dir():
    env = os.environ.get('KICAD_FOOTPRINT_DIR')
    for d in ((env,) if env else ()) + LIB_CANDIDATES:
        if d and os.path.isdir(d):
            return d
    return None


def marked_footprints(lib):
    """`[(lib/name, text), ...]` of every stock footprint with a marker."""
    out = []
    for f in sorted(glob.glob(os.path.join(lib, '*.pretty', '*.kicad_mod'))):
        with open(f, encoding='utf-8') as fh:
            t = fh.read()
        if any(EDGE_MARKER_TEXT.search(m.group(1))
               for m in _MARK.finditer(t)):
            lib_name = os.path.basename(os.path.dirname(f))[:-len('.pretty')]
            out.append((lib_name + '/' + os.path.basename(f)[:-len('.kicad_mod')], t))
    return out


def _rotated(t, rot):
    """A footprint block as KiCad saves it turned by `rot`: every inner
    `(at x y [a])` -- texts and pads, whose stored angle is the ABSOLUTE
    board angle -- gains `rot`. Graphics carry no `(at ...)`."""
    if not rot:
        return t

    def turn(m):
        a = float(m.group(3)) if m.group(3) else 0.0
        return '(at %s %s %g)' % (m.group(1), m.group(2), (a + rot) % 360.0)
    return re.sub(r'\(at\s+([-\d.eE+]+)\s+([-\d.eE+]+)(?:\s+([-\d.eE+]+))?\s*\)',
                  turn, t)


def stock_board(path, footprints, rot=0.0):
    """One board holding `footprints`, each turned by `rot`, refs M1..Mn.
    Returns {ref: lib/name}."""
    parts, names = [], {}
    for i, (name, t) in enumerate(footprints):
        ref = 'M%d' % (i + 1)
        lib, item = name.split('/', 1)
        t = t.strip()
        t = re.sub(r'\(version\s+\d+\)\s*', '', t, count=1)
        t = re.sub(r'\(generator\s+"?[^)"]*"?\)\s*', '', t, count=1)
        t = re.sub(r'\(generator_version\s+"[^"]*"\)\s*', '', t, count=1)
        t = re.sub(r'^\(footprint\s+"[^"]*"',
                   '(footprint "%s:%s"' % (lib, item), t)
        t = _rotated(t, rot)
        t = re.sub(r'(\(layer\s+"[FB]\.Cu"\))',
                   r'\1 (at %g %g %g)' % (100.0 * (i % 40), 100.0 * (i // 40),
                                         rot),
                   t, count=1)
        t = re.sub(r'\(property\s+"Reference"\s+"[^"]*"',
                   '(property "Reference" "%s"' % ref, t, count=1)
        parts.append(t)
        names[ref] = name
    rows = len(footprints) // 40 + 2
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('(kicad_pcb (version 20240108) (generator "pcbnew")\n'
                 '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal)'
                 ' (44 "Edge.Cuts" user))\n  (net 0 "")\n'
                 '  (gr_rect (start -60 -60) (end %g %g) (stroke (width 0.1)'
                 ' (type default)) (layer "Edge.Cuts"))\n'
                 % (4100.0, 100.0 * rows)
                 + '\n'.join(parts) + '\n)\n')
    return names


def body_class(lf):
    if lf.face is None and lf.marker is not None and lf.marker.face \
            and lf.reason.startswith('its PCB-edge marker'):
        return 'vetoed'
    if lf.face is None:
        return 'abstained'
    return 'strong' if lf.offset_mm >= mating.FACE_STRONG_OFFSET_MM - 1e-6 \
        else 'weak'


def geometric_face(lf):
    """The body's own face before the marker spoke (a veto keeps the body
    face out of `face`, so it is read back off the votes)."""
    if lf.face is not None:
        return lf.face
    decisive = {v[1] for v in lf.votes if v[1] is not None}
    return decisive.pop() if len(decisive) == 1 else None


def tally(faces, names, detail, label):
    tab, bad = {}, []
    reasons = {}
    for ref, lf in sorted(faces.items()):
        mk = lf.marker
        if mk is None:
            # An early abstention the marker is never asked about: a
            # vertical mount, a board-copper plug, no pads.
            key = ('abstained', 'not read: ' + lf.reason)
            tab[key] = tab.get(key, 0) + 1
            if detail:
                print('  %-6s %-62s %s' % (ref, names.get(ref, ref)[:62],
                                           key[1]))
            continue
        cls = body_class(lf)
        geo = geometric_face(lf)
        if cls == 'vetoed':
            # the body's face, against which the marker voted: strong or weak
            # by the offset the body vote carried
            off = max(max(abs(v[2]), abs(v[3])) for v in lf.votes
                      if v[1] is not None)
            cls = ('strong' if off >= mating.FACE_STRONG_OFFSET_MM - 1e-6
                   else 'weak')
            verdict = 'contradicts'
        elif mk.face is None:
            verdict = 'abstains'
            reasons[mk.reason] = reasons.get(mk.reason, 0) + 1
        elif geo is None:
            verdict = 'reads %s' % mk.face
        else:
            verdict = 'agrees' if mk.face == geo else 'contradicts'
        tab[(cls, verdict)] = tab.get((cls, verdict), 0) + 1
        if cls == 'strong' and verdict == 'contradicts':
            bad.append((ref, names.get(ref, ref)))
        if detail or verdict == 'contradicts':
            print('  %-6s %-62s body %-9s %-3s marker %-3s %s line %s' % (
                ref, names.get(ref, ref)[:62], cls, geo or '-',
                mk.face or '-', mk.reason or verdict, mk.line))
    print('%s: %d marked footprint(s)' % (label, sum(tab.values())))
    for cls in ('strong', 'weak', 'abstained'):
        row = {v: n for (c, v), n in tab.items() if c == cls}
        if row:
            print('  body %-9s %s' % (cls, ', '.join(
                '%s %d' % kv for kv in sorted(row.items()))))
    for r, n in sorted(reasons.items()):
        print('    marker abstains (%d): %s' % (n, r))
    return bad


def main(argv):
    detail = '--detail' in argv
    bad = []
    lib = library_dir()
    if lib is None:
        print('NOT RUN: no KiCad footprint library (set KICAD_FOOTPRINT_DIR)')
        return 2
    fps = marked_footprints(lib)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, 'stock_marked.kicad_pcb')
        names = stock_board(path, fps)
        pcb = parse_kicad_pcb(path)
        faces = mating.board_faces(pcb, path)
        missing = sorted(set(names) - set(faces))
        bad += tally(faces, names, detail, 'stock library (%s)' % lib)
        if missing:
            print('  NOT READ (no body record): %s' % ', '.join(
                names[r] for r in missing))
        # The same footprints saved turned: the reading is in the LOCAL
        # frame, so it must not move -- a text angle read in the wrong
        # convention would swap the line's axis at 90 and 270.
        moved = []
        for rot in (90.0, 180.0, 270.0):
            rp = os.path.join(td, 'stock_%d.kicad_pcb' % rot)
            stock_board(rp, fps, rot)
            turned = mating.board_faces(parse_kicad_pcb(rp), rp)
            for ref, lf in faces.items():
                t = turned.get(ref)
                key0 = (lf.face, lf.strong, lf.marker and lf.marker.face)
                key1 = t and (t.face, t.strong, t.marker and t.marker.face)
                if key0 != key1:
                    moved.append((rot, ref, names[ref], key0, key1))
        print('ROTATION -- readings that move when the same footprints are '
              'saved at 90/180/270: %d' % len(moved))
        for row in moved:
            print('  %g %s %s %s -> %s' % row)
        bad += [(r[1], r[2]) for r in moved]
    for board, refs in TRACKED:
        p = os.path.join(ROOT_DIR, 'kicad_files', board)
        f = mating.board_faces(parse_kicad_pcb(p), p)
        bad += tally({r: f[r] for r in refs if r in f}, {}, True, board)
    print('GATE -- the marker contradicts a STRONG body face, or a reading '
          'moves with the rotation: %d' % len(bad))
    for ref, name in bad:
        print('  %s %s' % (ref, name))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
