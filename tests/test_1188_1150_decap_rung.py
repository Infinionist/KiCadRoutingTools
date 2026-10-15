#!/usr/bin/env python3
"""#1188 and #1150: the decap repair works with the caps a finding can use,
and never takes a decap-charged cap farther from its IC.

#1188 -- the rung moved only `measured.cap`, the ONE nearest cap, so when one
cap is the nearest for several failing pins, moving it to one pin worsened the
others and the move was reverted (glasgow: 18 of 27 pin tries). Now every
usable cap on the pin's rail is ranked, caps no other pin relies on first, and
the closing note of an IC names the caps tried for it.

  * the issue's minimal input, built here: two ICs on one rail, each with a
    supply pin 3.275 mm from the SAME cap C1 (limit 2.5), and a second cap C2
    on the rail that serves neither. The rung serves U1 with C2 and U2 with
    C1; both findings clear. With the ranking off (the nearest cap only, the
    old rung), C1 can serve neither pin without worsening the other, both
    stay unresolved -- and each IC's note names C1 and what became of it.

#1150 --
  (a) a cap a --decaps-from reference holds, stranded past the search radius
      (`decap_ungraded`, an ERROR), is pulled back toward its IC by the rung:
      esp_prog C2 at (118, 95), 5.64 mm from U1 -> cleared. Without the rung
      it is not charged and does not move, as #1142 left it.
  (b) a cap charged by decap rules ALONE is not moved by the ordinary seat,
      which has no decap target: esp_prog C2 at (119, 95.2), (119.3, 95.0),
      (119, 96) used to ship 4.6253 -> 4.7248, 4.3647 -> 4.422 and
      4.575 -> 4.659 mm from U1, each reported unresolved. It now stays at its
      distance, unresolved, and says why.
  (c) the quench's tether gate measures a STRANDED held cap past the radius
      (it read 0 there): a move taking it farther from its IC is refused, one
      bringing it closer is not; without the held list the gate is inert on
      it, as before.

The aside pass (#1150 follow-up): U1's pin 1 boxed in by locked resistors and
its own pads, R1 in the one spot within 1.5 mm. With R1 movable the rung
seats C1 there and moves R1 aside (in `moves`, not `repaired`); with R1
locked it does not; with no room for R1 of its own, both are put back.

The ordinary seat (#1150 follow-up): its first rung refuses a pose that
CREATES a decap finding too, not only one that grows a finding the part is
already in -- on the #1188 board at a 3.5 mm limit, U1 moved away from C1
strands its pin 1, C1 moved away strands both pins, C2 is free.

The rung's speed (#1188 follow-up): its searches ask `pose_ok` against the
parts within reach of the search disc only, and `accept` decides a check by a
bound where the bound settles it. On esp_prog every pose two rung searches
ask gets the same `pose_ok` scoped as unscoped, and the same `accept` as the
cap posed and measured.

    python3 -X utf8 tests/test_1188_1150_decap_rung.py [case ...]
"""
import contextlib
import io
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, TESTS_DIR)

from kicad_parser import parse_kicad_pcb           # noqa: E402
from placement import floorplan as fp              # noqa: E402
from placement import seeder                       # noqa: E402
from placement.writer import write_placed_output   # noqa: E402
from run_utils import check, evidence              # noqa: E402

RUN_ALL_TIMEOUT = 900

ESP = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
PLACE_SEED = os.path.join(ROOT, 'py_placer', 'place_seed.py')
SOURCES = ('kicad', 'sheet')
CLEARANCE = 0.2
_TMP = tempfile.TemporaryDirectory(prefix='t1188_')


def _quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


def _parse(path):
    return _quiet(parse_kicad_pcb, path)


# ---------------------------------------------------------------------------
# #1188's minimal input
# ---------------------------------------------------------------------------

NETS = {1: '+3V3', 2: 'GND', 3: '/S1', 4: '/S2', 5: '/S3', 6: '/S4',
        7: '/S5', 8: '/S6'}
_ROWS = (-1.905, -0.635, 0.635, 1.905)


def _ic(ref, x, y, pin1_side, uid, sigs):
    """An 8-pad IC whose pad 1 is a +3V3 `power_in` on the `pin1_side`
    column (+1 right, -1 left) and pad 4 its GND."""
    pads = []
    s1 = iter(sigs)
    for n in range(1, 9):
        col = pin1_side if n <= 4 else -pin1_side
        row = _ROWS[(n - 1) % 4]
        if n == 1:
            net, kind = 1, 'power_in'
        elif n == 4:
            net, kind = 2, 'power_in'
        else:
            net, kind = next(s1), 'bidirectional'
        pads.append(
            f'    (pad "{n}" smd rect (at {2.5 * col} {row}) (size 1.5 0.6) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") (net {net} "{NETS[net]}") '
            f'(pintype "{kind}"))\n')
    return (f'  (footprint "P:SOIC8" (layer "F.Cu") (uuid "{uid}") '
            f'(at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -3 0) '
            f'(layer "F.SilkS"))\n' + ''.join(pads) + '  )\n')


def _cap(ref, x, y, uid):
    """A vertical 0603: pad 1 (+3V3) above, pad 2 (GND) below."""
    return (f'  (footprint "C:C_0603" (layer "F.Cu") (uuid "{uid}") '
            f'(at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -1.5 0) '
            f'(layer "F.SilkS"))\n'
            f'    (pad "1" smd rect (at 0 -0.8) (size 0.95 0.9) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") (net 1 "+3V3"))\n'
            f'    (pad "2" smd rect (at 0 0.8) (size 0.95 0.9) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") (net 2 "GND"))\n'
            f'  )\n')


def _shared_cap_board():
    """U1's pin 1 and U2's pin 1 each 3.275 mm from C1's +3V3 pad; C2 on the
    rail, 5.86 mm from either, serving neither."""
    nets = ''.join(f' (net {k} "{v}")\n' for k, v in NETS.items())
    text = ('(kicad_pcb (version 20240108) (generator "t1188")\n'
            ' (general (thickness 1.6))\n'
            ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal)\n'
            '  (44 "Edge.Cuts" user) (37 "F.SilkS" user) (39 "F.Mask" user)\n'
            '  (35 "F.Paste" user) (49 "F.Fab" user) (47 "F.CrtYd" user))\n'
            ' (setup)\n (net 0 "")\n' + nets +
            ' (gr_rect (start 0 0) (end 40 30) (stroke (width 0.1) '
            '(type solid)) (fill no) (layer "Edge.Cuts"))\n'
            + _ic('U1', 13.0, 15.0, +1, 'u1', (3, 4, 5, 6, 7, 8))
            + _ic('U2', 27.0, 15.0, -1, 'u2', (3, 4, 5, 6, 7, 8))
            + _cap('C1', 20.0, 13.9, 'c1')
            + _cap('C2', 20.0, 19.5, 'c2')
            + ')\n')
    path = os.path.join(tempfile.mkdtemp(dir=_TMP.name), 'shared.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return evidence(path, 'the #1188 fixture')


def _pin_intent(limit=2.5):
    return fp.intent_from_dict({'schema': 1, 'kind': fp.KIND, 'units': 'mm',
                                'decaps': {'max_pin_distance_mm': limit}})


def _pin_findings(intent, board, moves=()):
    out = board
    if moves:
        out = os.path.join(tempfile.mkdtemp(dir=_TMP.name), 'after.kicad_pcb')
        _quiet(write_placed_output, board, out, list(moves))
    g = _quiet(fp.grade, intent, _parse(out), out, group_sources=SOURCES,
               clearance=CLEARANCE)
    return sorted((v.ref, str(v.measured['pad']), v.measured['cap'],
                   v.measured['gap_mm'])
                  for v in g.errors if v.rule == 'decap_pin_distance')


def _rung(board, intent, **kw):
    return _quiet(seeder.repair_placement, _parse(board), board, intent,
                  group_sources=SOURCES, clearance=CLEARANCE,
                  repair_decaps=True, **kw)


def test_1188_a_cap_no_pin_relies_on_serves_first():
    board = _shared_cap_board()
    intent = _pin_intent()
    before = _pin_findings(intent, board)
    assert before == [('U1', '1', 'C1', 3.275), ('U2', '1', 'C1', 3.275)], \
        before
    res = _rung(board, intent)
    after = _pin_findings(intent, board, res['moves'])
    assert after == [], (after, res['notes'])
    rows = {(c, x['ic']): (x['result'], x.get('shared'))
            for c, r in res['decap_rung'].items() for x in r['tried']}
    # U1 first (findings in IC order): C2 is relied on by no pin, so it is
    # tried before C1, which U2's failing pin has as its nearest cap.
    assert rows.get(('C2', 'U1')) == ('seated', False), rows
    assert ('C1', 'U1') not in rows, rows
    # ...after which U1 no longer needs C1, so C1 is free for U2.
    assert rows.get(('C1', 'U2')) == ('seated', False), rows
    assert not res['unresolved'], (res['unresolved'], res['notes'])
    assert {'U1', 'U2'} <= set(res['repaired']), res['repaired']
    print(f"  PASS: {before} -> no pin finding; rows {rows}")


def test_1188_the_nearest_cap_alone_serves_neither_and_the_note_names_it():
    """The old rung's candidate set -- the finding's nearest cap only --
    reproduced by narrowing the ranking to it: C1 can move toward neither
    pin without worsening the other, so both stay. Each IC's closing note
    names C1 and its result instead of only saying the IC did not move."""
    board = _shared_cap_board()
    intent = _pin_intent()
    real = seeder._DecapView.rank_caps

    def nearest_only(self, targets):
        got = real(self, targets)
        best = min(got, key=lambda t: (t[2], t[0]))
        return [best]
    seeder._DecapView.rank_caps = nearest_only
    try:
        res = _rung(board, intent)
    finally:
        seeder._DecapView.rank_caps = real
    after = _pin_findings(intent, board, res['moves'])
    assert [r for r, *_ in after] == ['U1', 'U2'], after
    assert sorted(res['unresolved']) == ['U1', 'U2'], res['unresolved']
    for ic in ('U1', 'U2'):
        note = [n for n in res['notes'] if n.startswith(f'{ic}: UNRESOLVED')]
        assert note and 'never the IC' in note[0] \
            and 'pin 1: C1 (' in note[0], res['notes']
    print(f"  PASS: nearest-only leaves both: {note[0]}")


def test_1188_cli_reports_the_cleared_board():
    board = _shared_cap_board()
    ipath = os.path.join(os.path.dirname(board), 'intent.json')
    import json
    with open(ipath, 'w', encoding='utf-8') as fh:
        json.dump({'schema': 1, 'kind': fp.KIND, 'units': 'mm',
                   'decaps': {'max_pin_distance_mm': 2.5}}, fh)
    out = os.path.join(os.path.dirname(board), 'out.kicad_pcb')
    r = check([sys.executable, '-X', 'utf8', PLACE_SEED, board, out,
               '--intent', ipath, '--repair', '--repair-decaps',
               '--clearance', str(CLEARANCE)], accept=True,
              timeout=RUN_ALL_TIMEOUT)
    line = [ln for ln in r.stdout.splitlines() if ln.startswith('Repair:')]
    assert line and '0 unresolved' in line[0], r.stdout[-1500:]
    assert _pin_findings(_pin_intent(), evidence(out, 'the output')) == []
    print(f"  PASS: {line[0]}")


# ---------------------------------------------------------------------------
# #1150 on esp_prog C2
# ---------------------------------------------------------------------------

def _esp_c2_at(x, y):
    """esp_prog with C2 at (x, y) and its own --decaps-from intent: C2 is a
    cap the reference holds within the radius of U1."""
    from placement.portfolio import copy_siblings
    pcb = _parse(ESP)
    out = os.path.join(tempfile.mkdtemp(dir=_TMP.name), 'c2.kicad_pcb')
    _quiet(write_placed_output, ESP, out,
           [{'reference': 'C2', 'new_x': x, 'new_y': y,
             'new_rotation': pcb.footprints['C2'].rotation or 0.0}])
    copy_siblings(ESP, out)
    doc = _quiet(fp.emit_intent, _parse(out), out, decaps_from=ESP)
    assert 'C2' in (doc['decaps'].get('within_radius_refs') or ()), doc
    return out, fp.intent_from_dict(doc)


def _c2(intent, board, moves=()):
    out = board
    if moves:
        out = os.path.join(tempfile.mkdtemp(dir=_TMP.name), 'after.kicad_pcb')
        _quiet(write_placed_output, board, out, list(moves))
    g = _quiet(fp.grade, intent, _parse(out), out, group_sources=SOURCES,
               clearance=CLEARANCE)
    return [(v.rule, v.severity, v.measured.get('distance_mm'))
            for v in g.violations if v.ref == 'C2']


def test_1150a_a_stranded_held_cap_is_pulled_back_by_the_rung():
    board, intent = _esp_c2_at(118.0, 95.0)
    assert _c2(intent, board) == [('decap_ungraded', 'error', 5.6441)], \
        _c2(intent, board)
    off = _quiet(seeder.repair_placement, _parse(board), board, intent,
                 group_sources=SOURCES, clearance=CLEARANCE)
    assert 'C2' not in off['violators'], off['violators']
    assert 'C2' not in {m['reference'] for m in off['moves']}, off['moves']
    on = _quiet(seeder.repair_placement, _parse(board), board, intent,
                group_sources=SOURCES, clearance=CLEARANCE,
                repair_decaps=True)
    row = (on['decap_rung'].get('C2') or {}).get('tried') or []
    assert [(x['rule'], x['result']) for x in row] == [
        ('decap_ungraded', 'seated')], row
    assert 'C2' in on['violators'] and 'C2' in on['repaired'], on
    assert _c2(intent, board, on['moves']) == [], _c2(intent, board,
                                                      on['moves'])
    print(f"  PASS: C2 5.6441 mm out (held) -> seated "
          f"{row[0]['moved_mm']} mm, no finding left")


def test_1150a_a_held_cap_the_rung_cannot_seat_is_unresolved():
    """The rung found nothing: the cap is reported UNRESOLVED on its
    `decap_ungraded`, by the same honesty re-grade, never silent."""
    board, intent = _esp_c2_at(118.0, 95.0)
    real = seeder._DecapView.accept
    seeder._DecapView.accept = (
        lambda self, cap, **kw: (lambda x, y, rot: False))
    try:
        on = _quiet(seeder.repair_placement, _parse(board), board, intent,
                    group_sources=SOURCES, clearance=CLEARANCE,
                    repair_decaps=True)
    finally:
        seeder._DecapView.accept = real
    assert 'C2' in on['unresolved'], (on['unresolved'], on['notes'])
    assert on['unresolved_claims']['C2'] == ['decap_ungraded'], on
    note = [n for n in on['notes'] if n.startswith('C2: UNRESOLVED')]
    assert note and 'no_legal_pose_within_limit' in note[0], on['notes']
    print(f"  PASS: {note[0]}")


def test_1150b_a_decap_only_cap_is_not_seated_farther_from_its_ic():
    for (x, y), was in (((119.0, 95.2), 4.6253), ((119.3, 95.0), 4.3647),
                        ((119.0, 96.0), 4.575)):
        board, intent = _esp_c2_at(x, y)
        assert _c2(intent, board) == [('decap_distance', 'error', was)], \
            _c2(intent, board)
        res = _quiet(seeder.repair_placement, _parse(board), board, intent,
                     group_sources=SOURCES, clearance=CLEARANCE)
        assert 'C2' in res['violators'], res['violators']
        assert 'C2' not in {m['reference'] for m in res['moves']}, (
            (x, y), res['moves'])
        assert _c2(intent, board, res['moves']) == [
            ('decap_distance', 'error', was)]
        assert 'C2' in res['unresolved'], res['unresolved']
        why = [n for n in res['notes'] if n.startswith(
            'C2: charged by decap rules only')]
        assert why, res['notes']
    print(f"  PASS: C2 stays at {was} mm (was nudged to 4.659); {why[0]}")


def test_1150c_the_gate_holds_a_stranded_held_cap():
    from placement import quench as q
    board, intent = _esp_c2_at(118.0, 95.0)
    pcb = _parse(board)

    def gate_for(it):
        gate, _p = fp.resolve_intent_gate(it, pcb, SOURCES)
        st = _quiet(q.QuenchState, pcb, board, CLEARANCE, 0.55, 30.0, 0.5,
                    0.15, 2.0, 2.0, 2.0, 0.1, 0.3, tethers=gate['tethers'])
        return st
    st = gate_for(intent)
    terms = [t for t in st._tether_terms
             if t.rule == 'decap_distance' and t.data['cap'] == 'C2']
    assert terms and not terms[0].data['graded'] \
        and terms[0].data.get('stranded_error'), [t.data for t in terms]
    p = st.parts['C2']
    away = {'C2': (p.x - 2.0, p.y, p.rot)}         # U1 is to the +x side
    home = {'C2': (p.x + 0.5, p.y, p.rot)}
    fails = st.tether_failures(away)
    assert any(f[0] == 'decap_distance' and f[1].startswith('C2')
               for f in fails), fails
    assert not any(f[1].startswith('C2') for f in st.tether_failures(home))
    # The control: the same board with no held list -- `decap_ungraded` a
    # warn for C2 -- leaves the gate inert on it, as it always was.
    doc = dict(intent.decaps)
    doc.pop('within_radius_refs', None)
    doc.pop('within_radius_mm', None)
    loose = fp.intent_from_dict({'schema': 1, 'kind': fp.KIND,
                                 'units': 'mm', 'decaps': doc})
    st2 = gate_for(loose)
    assert not any(f[1].startswith('C2')
                   for f in st2.tether_failures(away)), 'not inert'
    print(f"  PASS: a move 2 mm farther from U1 is refused {fails}; one "
          f"0.5 mm closer is not; with no held list the gate is inert")


# ---------------------------------------------------------------------------
# A movable passive in the only poses within the limit is moved aside
# ---------------------------------------------------------------------------

def _res(ref, x, y, uid, n1, n2, locked=False):
    """A horizontal 0603 resistor on nets `n1` / `n2`."""
    return (f'  (footprint "R:R_0603" (layer "F.Cu") '
            + ('(locked yes) ' if locked else '')
            + f'(uuid "{uid}") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -1.5 0) '
            f'(layer "F.SilkS"))\n'
            f'    (pad "1" smd rect (at -0.8 0) (size 0.9 0.95) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") (net {n1} "{NETS[n1]}"))\n'
            f'    (pad "2" smd rect (at 0.8 0) (size 0.9 0.95) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") (net {n2} "{NETS[n2]}"))\n'
            f'  )\n')


def _boxed_pin_board(lock_r1=False):
    """U1's supply pin 1 boxed in: locked resistors R2-R6 above it and below
    to its right, U1's own pads to its left and below, and R1 -- movable --
    in the one spot within 1.5 mm of it. C1, U1's only cap on +3V3, sits
    16.95 mm away."""
    nets = ''.join(f' (net {k} "{v}")\n' for k, v in NETS.items())
    text = ('(kicad_pcb (version 20240108) (generator "t1188")\n'
            ' (general (thickness 1.6))\n'
            ' (layers (0 "F.Cu" signal) (31 "B.Cu" signal)\n'
            '  (44 "Edge.Cuts" user) (37 "F.SilkS" user) (39 "F.Mask" user)\n'
            '  (35 "F.Paste" user) (49 "F.Fab" user) (47 "F.CrtYd" user))\n'
            ' (setup)\n (net 0 "")\n' + nets +
            ' (gr_rect (start 0 0) (end 40 30) (stroke (width 0.1) '
            '(type solid)) (fill no) (layer "Edge.Cuts"))\n'
            + _ic('U1', 13.0, 15.0, +1, 'u1', (3, 4, 5, 6, 7, 8))
            + _cap('C1', 32.0, 22.0, 'c1')
            + _res('R1', 17.8, 13.1, 'r1', 3, 4, locked=lock_r1)
            + _res('R2', 18.4, 11.6, 'r2', 5, 6, locked=True)
            + _res('R3', 17.8, 14.9, 'r3', 7, 8, locked=True)
            + _res('R4', 15.3, 11.6, 'r4', 5, 7, locked=True)
            + _res('R5', 12.6, 11.6, 'r5', 6, 8, locked=True)
            + _res('R6', 9.9, 11.6, 'r6', 6, 7, locked=True)
            + ')\n')
    path = os.path.join(tempfile.mkdtemp(dir=_TMP.name), 'boxed.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return evidence(path, 'the boxed-pin fixture')


def _rows(res, ref):
    return [(r['result'], r.get('lifted'), r.get('stuck'))
            for r in (res['decap_rung'].get(ref) or {}).get('tried') or ()]


def test_1150_followup_a_passive_in_the_way_is_moved_aside():
    """Every pose within 1.5 mm of U1's pin 1 that C1 could take is refused:
    U1's own pads, locked R2-R6, and R1. R1 is a movable passive, so the
    rung asks again with it set aside: C1 is seated beside the pin, R1 goes
    to the nearest pose of its own within the repair's cap ladder (asked
    whether it makes a decap finding new or worse -- it is no cap, so
    nothing), and the grade keeps the two moves together. R1's move is in
    `moves`, but R1 was no violator and is not counted repaired."""
    board = _boxed_pin_board()
    intent = _pin_intent(1.5)
    assert _pin_findings(intent, board) == [('U1', '1', 'C1', 16.9535)]
    asked = []
    real = seeder._DecapView.no_worse

    def spy(self, ref, findings):
        asked.append(ref)
        return real(self, ref, findings)
    seeder._DecapView.no_worse = spy
    try:
        res = _rung(board, intent)
    finally:
        seeder._DecapView.no_worse = real
    assert _pin_findings(intent, board, res['moves']) == [], res['notes']
    moved = {m['reference']: m for m in res['moves']}
    assert set(moved) == {'C1', 'R1'}, moved
    assert _rows(res, 'C1') == [('no_legal_pose_within_limit', None, None),
                                ('seated', ['R1'], None)], _rows(res, 'C1')
    assert _rows(res, 'R1') == [('moved_aside', None, None)]
    assert 'R1' in asked, asked
    d = ((moved['R1']['new_x'] - 17.8) ** 2
         + (moved['R1']['new_y'] - 13.1) ** 2) ** 0.5
    assert 0 < d <= seeder.REPAIR_CAPS_MM[-1] + 1e-9, d
    assert 'R1' not in res['repaired'] and 'R1' not in res['violators']
    assert not res['unresolved'], (res['unresolved'], res['notes'])
    note = [n for n in res['notes'] if n.startswith('C1: decap rung seated')]
    assert note and 'moving R1' in note[0] and 'aside' in note[0], note
    print(f"  PASS: {note[0]}")


def test_1150_followup_a_locked_blocker_is_not_moved_aside():
    """The same board with R1 locked: nothing may be set aside, so the
    rung's one search fails and U1 stays unresolved, naming what it tried."""
    board = _boxed_pin_board(lock_r1=True)
    intent = _pin_intent(1.5)
    res = _rung(board, intent)
    assert not res['moves'], res['moves']
    assert _rows(res, 'C1') == [('no_legal_pose_within_limit', None, None)]
    assert res['unresolved'] == ['U1'], res['unresolved']
    print("  PASS: R1 locked -- no aside, U1 unresolved")


def test_1150_followup_a_blocker_with_no_pose_puts_everything_back():
    """R1 set aside must find a pose of its own: with the blocker's ladder
    shrunk to 0.05 mm it has none beside C1's seat, so C1 and R1 are both
    put back (`blocker_has_no_pose`, R1 named), the search is asked again
    past the poses that needed R1, and finds none."""
    board = _boxed_pin_board()
    intent = _pin_intent(1.5)
    real = seeder.REPAIR_CAPS_MM
    seeder.REPAIR_CAPS_MM = (0.05,)
    try:
        res = _rung(board, intent)
    finally:
        seeder.REPAIR_CAPS_MM = real
    assert not res['moves'], res['moves']
    assert _rows(res, 'C1') == [
        ('no_legal_pose_within_limit', None, None),
        ('blocker_has_no_pose', ['R1'], 'R1'),
        ('no_pose_with_a_passive_aside', None, None)], _rows(res, 'C1')
    note = [n for n in res['notes'] if 'put back' in n]
    assert note and 'R1 has no pose' in note[0], res['notes']
    assert res['unresolved'] == ['U1'], res['unresolved']
    print(f"  PASS: {note[0]}")


# ---------------------------------------------------------------------------
# The ordinary seat creates no decap finding where it can avoid one
# ---------------------------------------------------------------------------

def test_the_ordinary_seat_predicate_refuses_a_created_finding():
    """#1150 follow-up: `_DecapView.no_worse` -- the ordinary seat's first
    rung -- also holds the findings a move would CREATE, for a part in none
    yet. The #1188 board with the pin limit at 3.5 mm, so both pins pass
    through C1 (3.275 mm): U1 2 mm farther from C1 strands its pin 1 and is
    refused, 0.5 mm nearer is not; C1 2 mm away strands both pins and is
    refused; C2, which no pin relies on, may go where it likes."""
    import pose_score
    board = _shared_cap_board()
    intent = _pin_intent(3.5)
    assert _pin_findings(intent, board) == [], _pin_findings(intent, board)
    pcb = _parse(board)
    state = _quiet(pose_score.make_state, pcb, board, clearance=CLEARANCE)
    view = seeder._DecapView(state, pcb, intent)
    u1 = view.no_worse('U1', ())
    assert u1 is not None and u1.what == 'creates no new decap finding'
    p = state.parts['U1']
    assert not u1(p.x - 2.0, p.y, p.rot)
    assert u1(p.x + 0.5, p.y, p.rot)
    c1, c2 = view.no_worse('C1', ()), view.no_worse('C2', ())
    q = state.parts['C1']
    assert not c1(q.x, q.y - 2.0, q.rot) and c1(q.x, q.y, q.rot)
    r = state.parts['C2']
    assert c2(r.x, r.y + 2.0, r.rot) and c2(r.x + 3.0, r.y, r.rot)
    print("  PASS: U1 left 2 mm and C1 up 2 mm refused (a pin stranded), "
          "U1 right 0.5 mm and C2 anywhere accepted")


# ---------------------------------------------------------------------------
# The rung's speed shortcuts keep every verdict
# ---------------------------------------------------------------------------

def _accept_by_measurement(view, cap, x, y, rot, targets=(), own=False):
    """What `_DecapView.accept` promises, measured with the cap POSED there:
    every target pin within the limit, with `own` the cap's own distance
    within the limit, and no pin the cap serves -- nor, without `own`, its
    own finding -- new or worse."""
    L = seeder._legality()
    eps, worse = L.EPS, seeder.FINDING_WORSE_EPS_MM
    sub = {cap: L.footprint_at_pose(view.pcb.footprints[cap], (x, y, rot))}
    lim = view.pin_limit
    for i in targets:
        v = view.pin_value(i, sub)
        if v is not None and v > lim + eps:
            return False
    if own:
        d = view.cap_distance(cap, sub)
        if d is None or d > view.dd_limit + eps:
            return False
    for i in view.pins_of_cap.get(cap, ()):
        if i in targets:
            continue
        was, new = view.pin_value(i), view.pin_value(i, sub)
        if new is None:
            continue
        if was is None or was <= lim + eps:
            if new > lim + eps:
                return False
        elif new > was + worse:
            return False
    if not own and view.dd_limit is not None:
        o0 = view.cap_finding(cap, view.cap_distance(cap))
        st = view.cap_finding(cap, view.cap_distance(cap, sub))
        if st is not None and (o0 is None or st > o0 + worse):
            return False
    return True


def test_the_rung_shortcuts_give_the_measured_verdicts():
    """#1188 follow-up (speed): the rung's searches ask `pose_ok` against the
    parts within reach of the search disc (`_search_scope`), and
    `_DecapView.accept` settles a check by a bound instead of posing the cap
    where the bound decides it. Neither may change a verdict. On esp_prog
    with C2 stranded (held) and a 2 mm pin limit, every pose two rung
    searches can ask -- C2 home toward U1 (`own`), and a cap toward a failing
    pin (`targets`) -- gets the same `pose_ok` scoped and unscoped at every
    rung of the clearance ladder, and the same `accept` as the cap posed and
    measured through the view's own numbers."""
    board, intent = _esp_c2_at(118.0, 95.0)
    doc = dict(intent.decaps)
    doc['max_pin_distance_mm'] = 2.0
    intent = fp.intent_from_dict({'schema': 1, 'kind': fp.KIND, 'units': 'mm',
                                  'decaps': doc})
    got = {}
    real = seeder._repair_decap_rung

    def grab(state, pcb_data, graded, grader, limits, rot_ladder, notes,
             view=None, **_kw):
        got.update(state=state, pcb=pcb_data, graded=graded, limits=limits,
                   view=view, ladder=rot_ladder)
        return {}
    seeder._repair_decap_rung = grab
    try:
        _rung(board, intent)
    finally:
        seeder._repair_decap_rung = real
    state, view = got['state'], got['view']
    assert view is not None and view.pins, 'no pin elected: vacuous'
    jobs = []
    tgt = seeder._decap_target(state, got['pcb'], 'C2', 'U1', None)
    jobs.append(('C2', tgt, float(got['limits']['decap_ungraded']),
                 dict(own=True)))
    pins = [v for v in got['graded'].errors
            if v.rule == 'decap_pin_distance']
    assert pins, 'no failing pin: vacuous'
    m = pins[0].measured
    targets = view.pins_named(pins[0].ref, m['pad'], m['net'])
    cap = view.rank_caps(targets)[0][0]
    pp = view.posed(pins[0].ref).pads[view.pins[targets[0]].pad_index]
    jobs.append((cap, (pp.global_x, pp.global_y), 2.0,
                 dict(targets=targets)))
    n_pose = n_acc = n_true = 0
    L = seeder._legality()
    posed = {'n': 0}
    real_pose = L.footprint_at_pose

    def counted(*a, **kw):
        posed['n'] += 1
        return real_pose(*a, **kw)
    full = state.clearance
    for ref, (tx, ty), limit, kw in jobs:
        acc = view.accept(ref, **kw)
        rots = got['ladder'](ref) or [state.parts[ref].rot + d
                                      for d in (0.0, 90.0, 180.0, 270.0)]
        poses = {(x, y, r % 360.0) for x, y in seeder.seat_candidates(
            state, tx, ty, max_disp=2.0 * limit) for r in rots}
        for x, y, rot in sorted(poses):
            want = _accept_by_measurement(view, ref, x, y, rot, **kw)
            L.footprint_at_pose = counted
            try:
                verdict = acc(x, y, rot)
            finally:
                L.footprint_at_pose = real_pose
            assert verdict == want, (ref, kw, (x, y, rot), want)
            n_acc += 1
            n_true += want
        for clr in seeder.seat_clearances(full):
            seeder._set_seat_clearance(state, clr)
            try:
                plain = [seeder.pose_ok(state, ref, x, y, r, set())
                         for x, y, r in sorted(poses)]
                with seeder._search_scope(state, ref, tx, ty,
                                          2.0 * limit) as sc:
                    assert sc.active and len(sc.near) < len(state.parts) - 1
                    scoped = [seeder.pose_ok(state, ref, x, y, r, set())
                              for x, y, r in sorted(poses)]
            finally:
                seeder._set_seat_clearance(state, full)
            assert scoped == plain, (ref, clr)
            n_pose += len(poses)
    assert 0 < n_true < n_acc, (n_true, n_acc)
    # Not vacuous: the bounds decided a good share of the verdicts without
    # posing the cap (about half, measured).
    assert n_acc - posed['n'] > n_acc // 4, (posed['n'], n_acc)
    print(f"  PASS: {n_acc} accept verdicts ({n_true} accepted, "
          f"{posed['n']} posed) and {n_pose} pose_ok verdicts agree")


TESTS = [v for k, v in sorted(globals().items())
         if k.startswith('test_') and callable(v)]


if __name__ == '__main__':
    want = sys.argv[1:]
    n = 0
    for t in TESTS:
        if want and not any(w in t.__name__ for w in want):
            continue
        print(t.__name__)
        t()
        n += 1
    print(f"ALL PASS ({n} case(s))")
