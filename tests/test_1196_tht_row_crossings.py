#!/usr/bin/env python3
"""#1196: the channel tools see through-hole pin rows, PGA inner pins and
fine-pitch parts at the board's own rules.

fa10 board 05 (interf_u, 2 layers, DIPs + a PGA) routed at its 0.4 mm class
track with 137-479 reroutes while `check_channels` said `Faces in DEFICIT: 0`
and `check_capacity` said nothing was short: every other net crosses the DIP
rows between adjacent pins, and on the router's 0.1 mm grid a 0.4 mm track
crosses none of them, though 0.4 + 2 x 0.254 = 0.908 fits the 0.94 mm gap on
paper. The same tool told the PGA's 70 inner pins "they need a via, not a
lane" -- pins already on every layer. And edgehero's board 04: a SOIC (a
0.67 mm pin gap) at the board's own track 0.3 / clearance 0.3 (0.9 needed)
was never ledgered, because "fine pitch" was two absolute numbers.

What each case pins, on fixtures built here:

* a DIP-16 row: 0 of 14 gaps at track 0.4 / clearance 0.254 while the
  analytic gap test passes, 6 at 0.3, all at 0.2 -- measured in the router's
  own obstacle map; and `check_channels` / `check_capacity` print it;
* a 5x5 through-hole pin grid: its 9 inner pins are face demand
  (`through_row_pads`), not interior, in BOTH ledgers, with the conservation
  law intact -- and the same lattice as SMD pads, or an SMD part's thermal
  vias, stay interior;
* a SOIC-8 on a board whose Default class is 0.2/0.2 and whose Board Setup
  asks for 0.3/0.3 (multichannel_mixer's shape): `check_channels` names the
  minimums, auto-ledgers the SOIC at 0.3/0.3 and finds the face a neighbour
  ate; the escape ledger takes it only when asked (`fine_at_board_rules`).

    python3 tests/test_1196_tht_row_crossings.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools', 'rust_router'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_utils import check, evidence, tool    # noqa: E402

RUN_ALL_TIMEOUT = 600

CHANNELS = tool('check_channels.py')
CAPACITY = tool('check_capacity.py')
_TMP = tempfile.TemporaryDirectory(prefix='t1196_')


def _edge(x1, y1, x2, y2):
    pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return "\n".join(
        f' (gr_line (start {pts[i][0]} {pts[i][1]}) '
        f'(end {pts[(i + 1) % 4][0]} {pts[(i + 1) % 4][1]}) '
        f'(layer "Edge.Cuts") (width 0.1))' for i in range(4))


def _fp(ref, name, x, y, pads):
    return (f' (footprint "t:{name}" (layer "F.Cu") (at {x} {y})\n'
            f'  (property "Reference" "{ref}" (at 0 -14) (layer "F.SilkS"))\n'
            + "\n".join(pads) + "\n )")


def _tht(num, x, y, size, drill, net):
    return (f'  (pad "{num}" thru_hole circle (at {x:.3f} {y:.3f}) '
            f'(size {size} {size}) (drill {drill}) (layers "*.Cu" "*.Mask") '
            f'(net {net} "N{net}"))')


def _smd(num, x, y, w, h, net):
    return (f'  (pad "{num}" smd rect (at {x:.3f} {y:.3f}) (size {w} {h}) '
            f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "N{net}"))')


def _connector(ref, nets, y=35.0):
    """One SMD pad per net, so every net has a second owner (demand). A
    1.0 mm gap, so it is not itself fine at 0.2/0.2 or 0.3/0.3."""
    return _fp(ref, 'CONN', 0, 0, [_smd(i + 1, 1.5 + i * 1.5, y, 0.5, 1.0, n)
                                   for i, n in enumerate(nets)])


def _write(name, parts, nets, classes=None, rules=None):
    netdecl = "\n".join(f' (net {n} "N{n}")' for n in nets)
    path = os.path.join(_TMP.name, name, 'b.kicad_pcb')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(f'(kicad_pcb\n (version 20221018)\n (net 0 "")\n{netdecl}\n'
                f'{_edge(0, 0, 40, 40)}\n' + "\n".join(parts) + '\n)\n')
    if classes or rules:
        pro = {'net_settings': {'classes': [dict(name='Default',
                                                 **(classes or {}))]},
               'board': {'design_settings': {'rules': rules or {}}}}
        with open(path[:-len('.kicad_pcb')] + '.kicad_pro', 'w',
                  encoding='utf-8') as f:
            json.dump(pro, f)
    return evidence(path, f'{name} fixture')


def _dip_board():
    """A DIP-16 (1.6 mm pins, 2.54 mm pitch) placed at a grid phase where a
    0.4 mm track crosses none of its gaps and 0.3 mm crosses some."""
    pads = []
    for i in range(16):
        col = -3.81 if i < 8 else 3.81
        k = i if i < 8 else 15 - i
        pads.append(_tht(i + 1, col, -8.89 + 2.54 * k, 1.6, 0.8, i + 1))
    return _write('dip', [_fp('U1', 'DIP-16', 20.03, 20.03, pads)],
                  range(1, 17),
                  classes={'clearance': 0.254, 'track_width': 0.4,
                           'via_diameter': 0.8, 'via_drill': 0.4})


def _grid_board(kind):
    """A 5x5 pin grid at 2.54 mm (U1) plus a connector owning every net.
    kind: 'tht' (a PGA), 'smd' (the same lattice as SMD pads)."""
    pads, n = [], 0
    for r in range(5):
        for c in range(5):
            n += 1
            x, y = -5.08 + 2.54 * c, -5.08 + 2.54 * r
            pads.append(_tht(n, x, y, 1.4, 0.9, n) if kind == 'tht'
                        else _smd(n, x, y, 1.4, 1.4, n))
    return _write(f'grid_{kind}', [_fp('U1', 'GRID25', 20, 15, pads),
                                   _connector('J1', range(1, 26))],
                  range(1, 26))


def _thermal_via_board():
    """An SMD QFN-like part whose exposed pad carries four plated vias."""
    pads, n = [], 0
    for side in range(4):
        for k in range(4):
            n += 1
            t = -1.5 + 1.0 * k
            x, y, w, h = [(t, -3.0, 0.4, 0.8), (3.0, t, 0.8, 0.4),
                          (-t, 3.0, 0.4, 0.8), (-3.0, -t, 0.8, 0.4)][side]
            pads.append(_smd(n, x, y, w, h, n))
    pads.append(_smd(17, 0, 0, 3.0, 3.0, 17))
    for vx, vy in ((-0.8, -0.8), (0.8, -0.8), (-0.8, 0.8), (0.8, 0.8)):
        pads.append(_tht(17, vx, vy, 0.6, 0.3, 17))
    return _write('thermal', [_fp('U1', 'QFN16_EP', 20, 15, pads),
                              _connector('J1', range(1, 18))], range(1, 18))


def _soic_board():
    """A SOIC-8 with a neighbour hard against its west pins, on a board whose
    Default class is 0.2/0.2 and whose Board Setup asks for 0.3/0.3."""
    pads = []
    for i in range(8):
        x = -2.7 if i < 4 else 2.7
        k = i if i < 4 else 7 - i
        pads.append(_smd(i + 1, x, -1.905 + 1.27 * k, 1.55, 0.6, i + 1))
    # pad edge 0.35 mm off U1's west pad edge, covering the whole face
    wall = _fp('U2', 'BLOCK', 20 - 2.7 - 0.775 - 0.35 - 1.0, 20,
               [_smd(1, 0, 0, 2.0, 6.0, 9)])
    return _write('soic', [_fp('U1', 'SOIC-8', 20, 20, pads), wall,
                           _connector('J1', range(1, 9))], range(1, 10),
                  classes={'clearance': 0.2, 'track_width': 0.2},
                  rules={'min_clearance': 0.3, 'min_track_width': 0.3})


def _parse(path):
    import contextlib
    import io
    from kicad_parser import parse_kicad_pcb
    with contextlib.redirect_stdout(io.StringIO()):
        return parse_kicad_pcb(path)


# ---------------------------------------------------------------------------

def test_a_dip_row_is_closed_on_the_grid_where_the_arithmetic_says_open():
    from placement import escape, pin_rows
    path = _dip_board()
    pcb = _parse(path)
    fp = pcb.footprints['U1']
    gap = escape.min_row_gap(fp)
    assert abs(gap - 0.94) < 1e-6, gap
    assert gap > 0.4 + 2 * 0.254, 'the analytic test passes 0.4 mm'
    rep = pin_rows.tht_row_crossings(pcb, track_width=0.4, clearance=0.254,
                                     grid_step=0.1, min_track_width=0.1)
    lad = {r['track_width']: r['open'] for r in rep['ladder']}
    assert rep['gaps'] == 14 and rep['open'] == 0, rep['ladder']
    assert lad[0.3] == 6 and lad[0.25] == 10 and lad[0.2] == 14, lad
    assert rep['opens_all_at'] == 0.2, rep['opens_all_at']
    phrase = pin_rows.ladder_phrase(rep)
    assert phrase.startswith('0.4 mm crosses 0/14 through-hole pin gaps; '
                             '0.35 mm opens 0/14; 0.3 mm opens 6/14'), phrase
    print('  PASS: %s' % phrase)


def test_check_channels_and_check_capacity_report_the_rows():
    path = _dip_board()
    js = os.path.join(_TMP.name, 'dip', 'cc.json')
    r = check([sys.executable, '-X', 'utf8', CHANNELS, path, '--json', js],
              accept=True)
    assert 'track 0.4 [board netclass] clearance 0.254' in r.stdout, \
        r.stdout[:400]
    assert ('0.4 mm crosses 0/14 adjacent-pin gaps' in r.stdout
            and '0.3 mm opens 6/14' in r.stdout), r.stdout[-1200:]
    assert '  U1: 0.4 mm crosses 0/14 gaps (pitch 2.54)' in r.stdout
    tht = json.load(open(evidence(js, 'check_channels JSON')))['tht_rows']
    assert (tht['gaps'], tht['open'], tht['opens_all_at']) == (14, 0, 0.2), tht

    js = os.path.join(_TMP.name, 'dip', 'cap.json')
    r = check([sys.executable, '-X', 'utf8', CAPACITY, path, '--only',
               'narrow_track', '--json', js], accept=True)
    assert 'narrow_track: pin_gaps_open=0, pin_gaps=14' in r.stdout, r.stdout
    nt = json.load(open(evidence(js, 'check_capacity JSON')))['options'][
        'narrow_track']
    assert nt['ran'] and nt['measured']['opens_all_at'] == 0.2, nt
    assert nt['expected'] == {'pin_gaps_open': 14}, nt['expected']
    print('  PASS: both tools print 0/14 at 0.4 mm and the width that opens '
          'them')


def _ledger_rows(path, ref='U1'):
    from placement import routability
    pcb = _parse(path)
    return pcb, routability.face_lane_ledger(
        pcb, ref, clearance=0.2, track_width=0.2, grid_step=0.1,
        pcb_file=path)


def test_pga_inner_pins_are_demand_not_interior():
    from placement import escape
    path = _grid_board('tht')
    pcb, rows = _ledger_rows(path)
    r0 = rows[0]
    assert (r0['interior_pads'], r0['through_row_pads'],
            r0['through_row_nets'], r0['interior_pads_box']) == (0, 9, 9, 9), r0
    assert r0['interior_demand_nets'] == 0, r0
    assert sum(r['demand_nets'] for r in rows) == 25, [
        r['demand_nets'] for r in rows]
    assert (r0['interior_pads'] + r0['face_corridor_escapes']
            + r0['through_row_pads'] == r0['interior_pads_box']), r0
    pe = escape.part_escape(pcb, 'U1', clearance=0.2, track_width=0.2,
                            pitch_mm=0.4)
    d = pe.to_dict()
    assert (d['interior_pads'], d['through_row_pads']) == (0, 9), d
    assert (d['interior_pads'] + d['face_corridor_escapes']
            + d['through_row_pads'] == d['interior_pads_box']), d

    r = check([sys.executable, '-X', 'utf8', CHANNELS, path, '--refs', 'U1',
               '--track-width', '0.2', '--clearance', '0.2'], accept=True)
    assert 'U1   through-row: 9 through-hole pin(s)' in r.stdout, r.stdout
    assert 'U1   interior:' not in r.stdout, r.stdout
    print('  PASS: 9 inner pins are demand on the faces in both ledgers')


def test_smd_inner_pads_and_thermal_vias_stay_interior():
    from placement import pin_rows
    _pcb, rows = _ledger_rows(_grid_board('smd'))
    assert (rows[0]['interior_pads'], rows[0]['through_row_pads']) == (9, 0), \
        rows[0]
    path = _thermal_via_board()
    pcb, rows = _ledger_rows(path)
    assert not pin_rows.is_through_hole_part(pcb.footprints['U1'])
    assert rows[0]['through_row_pads'] == 0, rows[0]
    assert rows[0]['interior_pads'] >= 4, rows[0]
    print('  PASS: SMD lattice interior 9, thermal vias interior %d, '
          'through-row 0' % rows[0]['interior_pads'])


def test_a_soic_is_fine_at_the_boards_own_rules():
    from placement import escape
    path = _soic_board()
    pcb = _parse(path)
    fp = pcb.footprints['U1']
    assert abs(escape.min_row_gap(fp) - 0.67) < 1e-6, escape.min_row_gap(fp)
    assert escape.fine_by_rules(fp, track_width=0.3, clearance=0.3) == 'gap'
    assert escape.fine_by_rules(fp, track_width=0.2, clearance=0.2) is None
    assert 'U1' not in escape.fine_pitch_parts(pcb)
    assert 'U1' in escape.fine_pitch_parts(pcb, rules=(0.3, 0.3, 0.0))
    plain = [p.ref for p in escape.escape_ledger(pcb, pcb_file=path,
                                                 track_width=0.3,
                                                 clearance=0.3)]
    asked = [p.ref for p in escape.escape_ledger(pcb, pcb_file=path,
                                                 track_width=0.3,
                                                 clearance=0.3,
                                                 fine_at_board_rules=True)]
    assert 'U1' not in plain and 'U1' in asked, (plain, asked)
    print('  PASS: gap 0.67 mm is fine at 0.3/0.3 (needs 0.9), not at '
          '0.2/0.2; the escape ledger takes it when asked')


def test_check_channels_names_the_minimums_and_ledgers_the_soic():
    path = _soic_board()
    r = check([sys.executable, '-X', 'utf8', CHANNELS, path], accept=True)
    assert ("the board's Board Setup asks for min_clearance 0.3 / "
            "min_track_width 0.3" in r.stdout), r.stdout[:800]
    assert '--clearance 0.3 --track-width 0.3' in r.stdout, r.stdout[:800]
    assert 'no fine-pitch parts auto-detected' in r.stdout, r.stdout[:800]

    r = check([sys.executable, '-X', 'utf8', CHANNELS, path,
               '--clearance', '0.3', '--track-width', '0.3'], accept=True)
    assert 'no fine-pitch parts auto-detected' not in r.stdout, r.stdout
    assert 'NOTE:' not in r.stdout, 'an explicit floor is not second-guessed'
    assert 'U1 W: short' in r.stdout and 'eaten_by U2' in r.stdout, \
        r.stdout[-1200:]

    js = os.path.join(_TMP.name, 'soic', 'cap.json')
    check([sys.executable, '-X', 'utf8', CAPACITY, path, '--only',
           'move_blocker', '--clearance', '0.3', '--track-width', '0.3',
           '--json', js], accept=True)
    mb = json.load(open(evidence(js, 'check_capacity JSON')))['options'][
        'move_blocker']
    worst = mb['measured']['worst'][0]
    assert (worst['ref'], worst['face'], worst['blockers']) == (
        'U1', 'west', ['U2']), mb
    print('  PASS: at the board minimums U1 west is short %d lane(s), eaten '
          'by U2' % worst['deficit_lanes'])


TESTS = [
    test_a_dip_row_is_closed_on_the_grid_where_the_arithmetic_says_open,
    test_check_channels_and_check_capacity_report_the_rows,
    test_pga_inner_pins_are_demand_not_interior,
    test_smd_inner_pads_and_thermal_vias_stay_interior,
    test_a_soic_is_fine_at_the_boards_own_rules,
    test_check_channels_names_the_minimums_and_ledgers_the_soic,
]


if __name__ == '__main__':
    only = sys.argv[1:]
    ran = 0
    for t in TESTS:
        if only and not any(o in t.__name__ for o in only):
            continue
        print(f"--- {t.__name__}")
        t()
        ran += 1
    if only and not ran:
        print(f"NO TEST matches {only}")
        sys.exit(2)
    print('ALL PASS')
