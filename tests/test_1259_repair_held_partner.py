#!/usr/bin/env python3
"""#1259 (1): `--repair` seats the decap-only PARTNER of a pair the census
charged to the other member, when that clears the pair.

#1150 (b) (2cc5cc73) made the repair's ordinary seat skip a part charged by
decap rules alone. The census charges ONE member of a pair, though, so a cap
that is the uncharged partner of a pad conflict, or of a courtyard pair that
gates against `--baseline`, is charged by its decap rules alone and was left
in place. When the charged member's own seat failed, the pair shipped --
before 2cc5cc73 the cap's seat cleared it as a side effect.

Now such a member (`held_pad` / `held_cy` in `repair_placement`) waits until
every charged member has had its seat, and if its pair is still on the board
it takes its own ordinary seat (`no_worse` first, the proportion budget),
kept only when the move clears the pair: its pad / hole / stack shortfall
against the charged member falls with none of it growing, or a gating
courtyard pair it was in is gone -- with no new gating pair in its place.
Otherwise it is put back and reads as a decap-only zero move. esp_prog C2
(#1150's own case) is in no pair and still stays put
(tests/test_1188_1150_decap_rung.py, test_1150b).

The fixtures are the issue's: 12 parts perturbed on each board, decap limits
2.5 mm, clearance 0.2. Measured at 9af8d492 and after:

  watchy (default repair): pad conflicts C15/TP7, C6/C9, R4/SW2
      -> C6/C9, R4/SW2. TP7's only seat is 4.12 mm away, out of proportion;
      C15 takes a `no_worse` seat 4.00 mm away, pair shortfall 0.066 -> 0,
      its decap distance not grown.
  splitflap_driver (baseline + body model): gating courtyard C3/J6, C8/J7,
      C9/J7 -> C3/J6, C9/J7. J7 seated on its band leaves C8/J7 gating; C8
      has no `no_worse` pose and its plain seat, 0.91 mm, clears the pair.

What each case pins:

  W  watchy: C15/TP7 is gone, C15 moved and says why, its decap distance did
     not grow. U4 (an IC held by TP2, whose own seat clears TP2/U4) is NOT
     moved and NOT searched -- the held member waits for the charged one.
  S  splitflap: C8/J7 no longer gates, C8 moved and says why.
  P  a held member's seat that does not clear the pair (watchy, C15's seat
     forced 0.1 mm TOWARD TP7) is put back: C15 unmoved, the pair still
     there, C15 a decap-only zero move, UNRESOLVED.
  N  a held member's seat that clears its pair but lands in a NEW gating
     courtyard pair (splitflap, C8's seat forced onto J16) is put back.

    python3 -X utf8 tests/test_1259_repair_held_partner.py [case ...]
"""
import contextlib
import io
import json
import math
import os
import sys
import tempfile
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_utils import evidence                     # noqa: E402

RUN_ALL_TIMEOUT = 900

CLEARANCE = 0.2
SOURCES = ('kicad', 'sheet')

WATCHY = ('watchy.kicad_pcb', [
    ('R10', 73.2166, 83.9307, -90.0), ('G***', 82.8052, 103.3638, 0.0),
    ('SW2', 102.7957, 107.6038, -90.0), ('C15', 85.389, 93.1589, 0.0),
    ('SW1', 102.8192, 79.3999, -90.0), ('C9', 91.7798, 81.9068, 0.0),
    ('R5', 94.3516, 99.833, -90.0), ('TP7', 84.8552, 92.215, 0.0),
    ('C20', 77.2861, 110.4635, 0.0), ('TP5', 91.3469, 107.7874, 0.0),
    ('TP2', 90.2215, 85.3711, 0.0), ('C10', 91.7173, 94.9896, 90.0)], False)
SPLITFLAP = ('splitflap_driver.kicad_pcb', [
    ('H3', 27.2508, 53.5683, 0.0), ('J6', 160.578, 31.4861, 180.0),
    ('C5', 160.2055, 35.537, 90.0), ('J7', 99.9762, 28.0081, 180.0),
    ('R9', 90.2726, 40.9234, 0.0), ('U2', 193.1053, 35.1797, 90.0),
    ('J5', 181.1738, 31.5975, 180.0), ('J8', 195.0543, 48.5859, 90.0),
    ('H7', 219.6681, 55.8727, 0.0), ('J20', 33.8887, 37.6227, 0.0),
    ('C9', 90.767, 33.8928, 90.0), ('H6', 36.7125, 23.5628, 0.0)], True)

_TMP = tempfile.TemporaryDirectory(prefix='t1259r_')
_RUNS = {}


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **k)


def _parse(path):
    from kicad_parser import parse_kicad_pcb
    return _quiet(parse_kicad_pcb, path)


def _forced_seat(ref, pose):
    """`seeder._try_place` with `ref`'s seat forced to `pose` (every other
    part searched as usual): the keep test then decides on a seat whose
    effect is known."""
    from placement import seeder
    real = seeder._try_place

    def fake(state, r, tx, ty, exclude, **kw):
        if r != ref:
            return real(state, r, tx, ty, exclude, **kw)
        state.apply_move(r, *pose)
        return state.clearance
    return patch.object(seeder, '_try_place', fake)


def _run(case, forced=None):
    """The issue's repro: perturb, emit an intent (decap limits 2.5 mm),
    repair, write, grade. Memoised per (board, forced seat)."""
    key = (case[0], forced)
    if key in _RUNS:
        return _RUNS[key]
    from placement import floorplan as fp, legality as L, seeder
    from placement.portfolio import copy_siblings
    from placement.writer import write_placed_output
    name, moves, baseline = case
    board = evidence(os.path.join(ROOT, 'kicad_files', name), name)
    td = tempfile.mkdtemp(dir=_TMP.name)
    src = os.path.join(td, 'in.kicad_pcb')
    _quiet(write_placed_output, board, src, [
        {'reference': r, 'new_x': x, 'new_y': y, 'new_rotation': a}
        for r, x, y, a in moves])
    copy_siblings(board, src)
    evidence(src, 'perturbed input')
    doc = _quiet(fp.emit_intent, _parse(board), board)
    doc['decaps'] = dict(doc.get('decaps') or {}, max_distance_mm=2.5,
                         max_pin_distance_mm=2.5)
    ipath = os.path.join(td, 'intent.json')
    with open(ipath, 'w', encoding='utf-8') as f:
        json.dump(doc, f)
    intent = fp.load_intent(evidence(ipath, 'intent'))
    kw = {'baseline_file': board, 'body_model': True} if baseline else {}
    ctx = (_forced_seat(*forced) if forced is not None
           else contextlib.nullcontext())
    with ctx:
        res = _quiet(seeder.repair_placement, _parse(src), src, intent,
                     group_sources=SOURCES, clearance=CLEARANCE, **kw)
    out = os.path.join(td, 'out.kicad_pcb')
    _quiet(write_placed_output, src, out, res['moves'])
    copy_siblings(src, out)
    po, base = _parse(evidence(out, 'repaired board')), _parse(board)
    gating = L.CourtyardCensus(po, out).grade(moved=L.moved_refs(po, base))
    pads = _quiet(L.grade_pad_legality, po, CLEARANCE, worst_n=0,
                  pcb_file=out)
    got = {'res': res, 'src': src, 'out': out, 'intent': intent,
           'moved': {m['reference'] for m in res['moves']},
           'pad_pairs': {tuple(sorted(p[:2]))
                         for p in pads['pad_conflict_pairs']},
           'gating': {tuple(sorted((q.a, q.b)))
                      for q in gating.gating or ()}}
    _RUNS[key] = got
    return got


def _notes(run, ref):
    return [n for n in run['res']['notes'] if n.startswith(ref + ':')]


def _decap_mm(run, path, ref):
    from placement import floorplan as fp
    g = _quiet(fp.grade, run['intent'], _parse(path), path,
               group_sources=SOURCES, clearance=CLEARANCE)
    return [v.measured.get('distance_mm') for v in g.violations
            if v.ref == ref and v.rule == 'decap_distance']


def test_w_watchy_c15_takes_the_seat_and_clears_c15_tp7():
    run = _run(WATCHY)
    assert ('C15', 'TP7') not in run['pad_pairs'], (
        'C15/TP7 ships (before #1259: TP7 out of proportion, C15 skipped)',
        sorted(run['pad_pairs']))
    assert 'C15' in run['moved'], run['moved']
    said = [n for n in _notes(run, 'C15') if 're-seated' in n]
    assert said and 'moved for its pair(s) with TP7' in said[0] \
        and 'pad/hole shortfall' in said[0], _notes(run, 'C15')
    from placement.seeder import FINDING_WORSE_EPS_MM
    before = _decap_mm(run, run['src'], 'C15')
    after = _decap_mm(run, run['out'], 'C15')
    # No worse by the repair's own tolerance (the grade rounds to 0.1 um:
    # 3.3114 -> 3.3115 mm here).
    assert before and after and         after[0] <= before[0] + FINDING_WORSE_EPS_MM, (
            'the no_worse seat must not grow its decap distance', before,
            after)
    # The held member waits for the charged one: TP2's own seat clears
    # TP2/U4, so the IC is neither moved nor searched.
    assert 'TP2' in run['moved'] and 'U4' not in run['moved'], run['moved']
    assert not [n for n in _notes(run, 'U4') if 'in a pair charged' in n], \
        _notes(run, 'U4')
    print(f"  PASS: pad pairs {sorted(run['pad_pairs'])}; {said[0]}; "
          f"decap {before[0]} -> {after[0]} mm; U4 not moved")


def test_s_splitflap_c8_clears_the_gating_pair():
    run = _run(SPLITFLAP)
    assert ('C8', 'J7') not in run['gating'], (
        'C8/J7 still gates (before #1259: C8 decap-only, skipped)',
        sorted(run['gating']))
    assert 'C8' in run['moved'], run['moved']
    said = [n for n in _notes(run, 'C8') if 're-seated' in n]
    assert said and 'gating courtyard pair(s) with J7 gone' in said[0], \
        _notes(run, 'C8')
    print(f"  PASS: gating {sorted(run['gating'])}; {said[0]}")


def _towards(src, ref, other, mm):
    fps = _parse(src).footprints
    a, b = fps[ref], fps[other]
    d = math.hypot(b.x - a.x, b.y - a.y)
    return (round(a.x + (b.x - a.x) * mm / d, 4),
            round(a.y + (b.y - a.y) * mm / d, 4), (a.rotation or 0.0) % 360)


def test_p_a_seat_that_does_not_clear_the_pair_is_put_back():
    pose = _towards(_run(WATCHY)['src'], 'C15', 'TP7', 0.1)
    run = _run(WATCHY, forced=('C15', pose))
    assert 'C15' not in run['moved'], ('a seat that does not clear the pair '
                                       'was kept', pose, run['moved'])
    assert ('C15', 'TP7') in run['pad_pairs'], sorted(run['pad_pairs'])
    put = [n for n in _notes(run, 'C15') if 'does not clear them' in n]
    assert put, _notes(run, 'C15')
    assert [n for n in _notes(run, 'C15')
            if 'charged by decap rules only' in n], _notes(run, 'C15')
    assert 'C15' in run['res']['unresolved'], run['res']['unresolved']
    print(f"  PASS: {put[0]}")


def test_n_a_seat_into_a_new_gating_pair_is_put_back():
    fps = _parse(_run(SPLITFLAP)['src']).footprints
    j16, c8 = fps['J16'], fps['C8']
    pose = (j16.x, j16.y, (c8.rotation or 0.0) % 360)
    run = _run(SPLITFLAP, forced=('C8', pose))
    assert 'C8' not in run['moved'], ('a seat into a new gating pair was '
                                      'kept', pose, run['moved'])
    put = [n for n in _notes(run, 'C8')
           if 'puts it in a gating courtyard pair with J16' in n]
    assert put, _notes(run, 'C8')
    assert ('C8', 'J7') in run['gating'], sorted(run['gating'])
    print(f"  PASS: {put[0]}")


TESTS = [
    test_w_watchy_c15_takes_the_seat_and_clears_c15_tp7,
    test_s_splitflap_c8_clears_the_gating_pair,
    test_p_a_seat_that_does_not_clear_the_pair_is_put_back,
    test_n_a_seat_into_a_new_gating_pair_is_put_back,
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
