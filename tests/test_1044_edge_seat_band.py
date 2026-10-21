#!/usr/bin/env python3
"""#1044: the edge-seat path honours the board's rule-area keep-out bands.

#1031 made pad copper in a `(keepout (tracks not_allowed))` band a legality
term, through `pads_ok`. Every ordinary seat reaches it (`pose_ok` ->
`candidate_valid` -> `pads_ok`), but the EDGE seat deliberately bypasses
`pose_ok` -- an edge connector overhangs by design -- and `edge_seat_ok`,
its own predicate, never asked about the band. So stage 1 and `_seat_edge`
could seat an SMD connector's pad copper where no track can reach it, and the
polish quench then took that pose as the seed's licence.

Neither committed board with such a band exercises the path (glasgow's edge
connectors are file-locked or through-hole; rp2350's band is an interior
sliver), so the fixture is SEMI-SYNTHETIC, the Phase-0 verifier's: rp2350
with a board-level tracks-forbidden edge ring 3mm wide injected into its
file. Its J3 (north edge) then carries its SMD mounting pad 0.954mm into the
band on every seed without the conjunct.

What each case pins:

* `edge_seat_ok` refuses J3's band pose and names the band; the SAME pose
  passes with the conjunct off (control: nothing else refuses it).
* Seeding the piled board: no pad in the band with the conjunct, J3 at
  0.954mm without it (the hole, reproduced).
* place_seed's final gate charges a band pad the seed PLACED (exit 4, named in
  `keepout_copper_seeded` and on stderr) -- driven in-process with the
  conjunct off, since with it on the seed makes none -- and does NOT charge
  one the board came in with (rp2350's human C6).

    python3 tests/test_1044_edge_seat_band.py
"""
import contextlib
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS_DIR)

from kicad_parser import parse_kicad_pcb           # noqa: E402
from placement import floorplan as fp              # noqa: E402
from placement import legality                     # noqa: E402
from placement import seeder                       # noqa: E402
from placement.parser import extract_locked_refs   # noqa: E402
from placement.writer import write_placed_output   # noqa: E402
import pose_score                                  # noqa: E402

RUN_ALL_TIMEOUT = 900

RP = os.path.join(ROOT, 'kicad_files', 'rp2350_fpga_eensy_prePlane.kicad_pcb')
SOURCES = ('kicad', 'sheet')
CLEARANCE = 0.2
RING_MM = 3.0

ZONE = '''	(zone
		(layers "F.Cu" "B.Cu")
		(uuid "11111111-2222-3333-4444-555555555555")
		(hatch edge 0.508)
		(connect_pads (clearance 0))
		(min_thickness 0.0256)
		(keepout (tracks not_allowed) (vias not_allowed) (pads allowed)
			(copperpour allowed) (footprints allowed))
		(fill (thermal_gap 0.508) (thermal_bridge_width 0.508))
		(polygon (pts (xy {x0} {y1}) (xy {x1} {y1}) (xy {x1} {y0})
			(xy {x0} {y0})))
		(polygon (pts (xy {a0} {b1}) (xy {a1} {b1}) (xy {a1} {b0})
			(xy {a0} {b0})))
	)
'''


def _strip_copper(text):
    """The board text without its top-level tracks, arcs and vias --
    place_seed refuses a routed board (a seed would strand the copper)."""
    out, i, n = [], 0, len(text)
    while i < n:
        j = text.find('(', i)
        if j < 0:
            out.append(text[i:])
            break
        head = text[j + 1:j + 9]
        # Only a block at the board's top level: preceded by a newline and
        # indentation alone.
        k = text.rfind('\n', 0, j)
        top = text[k + 1:j].strip() == '' and text[k + 1:j].count('\t') <= 1
        word = (head.split() or [''])[0]
        if top and word in ('segment', 'via', 'arc'):
            depth, m = 0, j
            while m < n:
                if text[m] == '(':
                    depth += 1
                elif text[m] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                m += 1
            out.append(text[i:k + 1])
            i = m + 1
            while i < n and text[i] in ' \t\r':
                i += 1
            if i < n and text[i] == '\n':
                i += 1
            continue
        out.append(text[i:j + 1])
        i = j + 1
    return ''.join(out)


def _siblings(src, dst):
    for ext in ('.kicad_pro', '.kicad_dru'):
        a = os.path.splitext(src)[0] + ext
        if os.path.exists(a):
            shutil.copy2(a, os.path.splitext(dst)[0] + ext)


def band_board(td):
    """rp2350 with a `RING_MM` tracks-forbidden ring along its outline, and
    the intent emitted from it (J1 / J3 north, U8 west)."""
    p0 = parse_kicad_pcb(RP)
    x0, y0, x1, y1 = p0.board_info.board_bounds
    x0, y0, x1, y1 = x0 - 0.05, y0 - 0.05, x1 + 0.05, y1 + 0.05
    zone = ZONE.format(x0=x0, y0=y0, x1=x1, y1=y1, a0=x0 + RING_MM,
                       b0=y0 + RING_MM, a1=x1 - RING_MM, b1=y1 - RING_MM)
    text = _strip_copper(open(RP, encoding='utf-8', newline='').read()
                         ).rstrip()
    assert text.endswith(')')
    board = os.path.join(td, 'band.kicad_pcb')
    with open(board, 'w', encoding='utf-8', newline='') as fh:
        fh.write(text[:-1] + zone + ')\n')
    _siblings(RP, board)
    pcb = parse_kicad_pcb(board)
    doc = fp.emit_intent(pcb, board)
    ipath = os.path.join(td, 'band.json')
    with open(ipath, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    return board, fp.load_intent(ipath), ipath


def pile(board, td):
    pcb = parse_kicad_pcb(board)
    locked = extract_locked_refs(board)
    bb = pcb.board_info.board_bounds
    cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    out = os.path.join(td, 'pile.kicad_pcb')
    write_placed_output(board, out, [
        {'reference': r, 'new_x': cx, 'new_y': cy,
         'new_rotation': f.rotation or 0.0}
        for r, f in pcb.footprints.items() if r not in locked])
    _siblings(board, out)
    return out


@contextlib.contextmanager
def band_gate(on):
    was = seeder._edge_band_gate
    seeder._edge_band_gate = on
    try:
        yield
    finally:
        seeder._edge_band_gate = was


def test_edge_seat_ok_refuses_a_band_pose_and_names_it():
    with tempfile.TemporaryDirectory() as td:
        board, intent, _ip = band_board(td)
        pcb = parse_kicad_pcb(board)
        claim = next(c for c in intent.edge_claims() if c['ref'] == 'J3')
        band = claim.get('overhang_mm') or {}
        lo, hi = float(band.get('min', 0.0)), float(band.get('max'))
        st = pose_score.make_state(pcb, board, clearance=CLEARANCE,
                                   board_edge_clearance=0.55)
        assert st.legality_ctx.keepouts is not None
        part = st.parts['J3']
        why = []
        with band_gate(True):
            ok_on = seeder.edge_seat_ok(st, part, part.x, part.y,
                                        claim['edge'], lo, hi, reasons=why)
        with band_gate(False):
            ok_off = seeder.edge_seat_ok(st, part, part.x, part.y,
                                         claim['edge'], lo, hi)
        assert ok_off, "control: the human J3 pose fails another conjunct"
        assert not ok_on and any('rule-area keep-out band' in w
                                 for w in why), why
    print(f"  PASS: J3's human pose refused -- {why[0]}; passes with the "
          f"conjunct off")


def _seed_band(board, intent, td, seed='0'):
    src = pile(board, td)
    res = seeder.seed_from_intent(
        parse_kicad_pcb(src), src, intent, random.Random(seed),
        group_sources=SOURCES, clearance=CLEARANCE,
        board_edge_clearance=0.55, grid_step=0.1)
    out = os.path.join(td, f'seed{seed}.kicad_pcb')
    write_placed_output(src, out, res['placements'])
    _siblings(board, out)
    g = legality.board_keepout_findings(parse_kicad_pcb(out), CLEARANCE, out)
    return res, dict(g['oob_keepout_copper_refs'])


def test_a_seed_puts_no_edge_connector_pad_in_the_band():
    with tempfile.TemporaryDirectory() as td:
        board, intent, _ip = band_board(td)
        with band_gate(False):
            _r0, off = _seed_band(board, intent, td)
        with band_gate(True):
            res, on = _seed_band(board, intent, td)
        assert off.get('J3', 0) > 0.9, off
        assert not on, on
        note = [n for n in res['notes'] if n.startswith('edge connector J3')
                and 'keep-out band' in n]
        assert note, [n for n in res['notes'] if 'J3' in n]
    print(f"  PASS: without the conjunct J3 is {off['J3']}mm into the band; "
          f"with it, none -- {note[0]}")


def _place_seed_in_process(argv):
    import place_seed
    out, err = io.StringIO(), io.StringIO()
    old = sys.argv
    sys.argv = ['place_seed.py'] + [str(a) for a in argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = place_seed.main()
    finally:
        sys.argv = old
    m = re.search(r'^JSON_SUMMARY: (.*)$', out.getvalue(), re.M)
    assert m, out.getvalue()[-1500:] + err.getvalue()[-1500:]
    return rc, json.loads(m.group(1)), err.getvalue()


def test_place_seed_charges_a_band_pad_it_placed_and_not_one_it_inherited():
    with tempfile.TemporaryDirectory() as td:
        board, _intent, ipath = band_board(td)
        src = pile(board, td)
        out = os.path.join(td, 'cli.kicad_pcb')
        with band_gate(False):
            # 0.55: the edge floor the in-process seeds above use (#1301
            # moved the unset default to the 0.2 fab floor).
            rc, s, err = _place_seed_in_process(
                [src, out, '--intent', ipath, '--clearance', CLEARANCE,
                 '--board-edge-clearance', 0.55])
        seeded = dict(s['keepout_copper_seeded'])
        assert rc == 4 and seeded.get('J3', 0) > 0.9, (rc, seeded)
        assert s['oob_keepout_copper_count'] >= 1, s
        # The reason names the band only when nothing earlier fired: an
        # intent error outranks it, so read the summary, not the line.
        # INHERITED: rp2350's human C6 sits 0.1187mm in its own band, and a
        # seed of the human board only moves it shallower.
        doc = fp.emit_intent(parse_kicad_pcb(RP), RP)
        ip2 = os.path.join(td, 'rp.json')
        with open(ip2, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        src2 = os.path.join(td, 'rp_in.kicad_pcb')
        with open(src2, 'w', encoding='utf-8', newline='') as fh:
            fh.write(_strip_copper(open(RP, encoding='utf-8',
                                        newline='').read()))
        _siblings(RP, src2)
        # File-locked, so the seed does not touch it and it stays in the band
        # on the written board -- an inherited band pad in the output, which
        # the gate must report and not charge.
        seeder.stamp_locked(src2, ['C6'])
        out2 = os.path.join(td, 'rp_out.kicad_pcb')
        _rc2, s2, _e2 = _place_seed_in_process(
            [src2, out2, '--intent', ip2, '--clearance', CLEARANCE,
             '--force'])
        assert s2['keepout_copper_seeded'] == [], s2['keepout_copper_seeded']
        out_band = dict(legality.board_keepout_findings(
            parse_kicad_pcb(out2), CLEARANCE, out2)['oob_keepout_copper_refs'])
        assert out_band.get('C6', 0) > 0.1, out_band
        assert s2['oob_keepout_copper_count'] >= 1, s2
        inherited = legality.board_keepout_findings(
            parse_kicad_pcb(src2), CLEARANCE, src2)['oob_keepout_copper_refs']
        assert dict(inherited).get('C6', 0) > 0.1, inherited
    print(f"  PASS: exit 4 with J3 {seeded['J3']}mm charged; the human C6 "
          f"({dict(inherited)['C6']}mm, inherited) is not")


def test_repair_names_the_band_and_its_real_next_move():
    """`--repair` of the gate-on seed tries to put J3 back on its north edge
    through `_seat_edge`, finds every position in the band, and says so
    ONCE, at its deepest -- with the move a board rule area has (move it, or
    the connector's declaration), never the declared keep-out's `allow`."""
    with tempfile.TemporaryDirectory() as td:
        board, intent, _ip = band_board(td)
        with band_gate(True):
            res, _band = _seed_band(board, intent, td)
        seeded = os.path.join(td, 'seed0.kicad_pcb')
        rep = seeder.repair_placement(
            parse_kicad_pcb(seeded), seeded, intent, group_sources=SOURCES,
            clearance=CLEARANCE)
        note = [n for n in rep['notes'] if n.startswith('J3: every position')]
        assert note, [n for n in rep['notes'] if n.startswith('J3')]
        assert 'rule-area keep-out band' in note[0], note
        assert note[0].count('rule-area keep-out band') == 1, note
        assert '`allow`' not in note[0] and 'rule area' in note[0], note
        assert 'J3' not in {m['reference'] for m in rep['moves']} or \
            'J3' not in dict(legality.board_keepout_findings(
                parse_kicad_pcb(seeded), CLEARANCE, seeded)[
                    'oob_keepout_copper_refs']), rep['moves']
    print(f"  PASS: {note[0]}")


def test_gate_reason_names_the_band():
    import place_seed
    why = place_seed.gate_reason([], [], [], 0, band=['J3'])
    assert why and 'rule-area keep-out band on J3' in why, why
    assert place_seed.gate_reason([], [], [], 0) is None
    print(f"  PASS: {why}")


TESTS = [
    test_edge_seat_ok_refuses_a_band_pose_and_names_it,
    test_a_seed_puts_no_edge_connector_pad_in_the_band,
    test_place_seed_charges_a_band_pad_it_placed_and_not_one_it_inherited,
    test_repair_names_the_band_and_its_real_next_move,
    test_gate_reason_names_the_band,
]


if __name__ == '__main__':
    only = sys.argv[1:]
    for t in TESTS:
        if only and not any(o in t.__name__ for o in only):
            continue
        print(f"--- {t.__name__}")
        t()
    print('ALL PASS')
