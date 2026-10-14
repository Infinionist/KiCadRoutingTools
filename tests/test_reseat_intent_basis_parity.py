#!/usr/bin/env python3
"""#1068: `place_seed --reseat`'s `intent` basis counts the TETHER rules.

`IntentProbe` built its terms from the zones and keep-outs only, so the
re-seat's accept basis read `intent 0->0` on a board printing decap GRADE
ERRORs on the very refs re-seated (run 34: C31 4.46mm, C53 3.08mm, U7.4,
U8.6), and prune reverted a seat made for a decap reason as a pure hpwl loss.
The probe now holds `decap_distance`, `decap_pin_distance` and `proximity`
too, from the same `tether_gate_spec` the quench's gate holds -- measured
through `QuenchState.tether_graded_value`, and never arming that gate.

What each case pins:

* PARITY, pose by pose: over a 5x5 lattice of one mover's poses, the probe's
  breach count equals `floorplan.grade`'s error count over the rules the probe
  declares (`probe.rules`) -- a decap cap on esp_prog, a proximity subject (R1 near U1)
  on esp_prog, and watchy's caps under both decap rules, where one cap sits on a
  rail with several chips. Each lattice must straddle the limit (two distinct
  counts), or it tested nothing.
* ONCE PER TERM: on watchy a breached term binds three or more refs (a cap
  and its rail's chips); the count is still the grade's, which a per-ref count
  would exceed.
* LOCKED TERMS COUNT: with C3 and its rail chips file-locked on esp_prog, the gate
  drops the C3 term (nothing can move it) but the grade still reports it, and
  so does the probe.
* THE ISSUE'S RECIPE, through the CLI: esp_prog with `decaps.max_distance_mm
  2.0` and C3 pushed 2.5mm east (2.33mm from U2, the regulator it decouples
  since #1249 -- at its human pose it is 1.53mm away, inside the limit,
  where the issue read it 2.125mm from U1), `place_seed --reseat C3`: the
  `intent` basis
  before equals the grade's errors over `accept_basis.intent_rules`, the
  printed basis is labelled `intent[decap_distance]`, and the pass is ACCEPTED
  on `intent` 1 -> 0 with C3's error gone from the written board.
* PRUNE sees them: re-seating C4 under 0.8mm limits reduces its breach at
  an hpwl cost, and prune KEEPS the move (it reverted it on the tuple before).
* TWO VIEWS: the count reads a decap pair the grade's way (past the search
  radius it is `decap_ungraded`, not counted); the licence and prune read it
  the gate's way (still measured), so walking out of the radius is a rise,
  not a fix. A term growing inside its limit is no finding, and a cap at
  EXACTLY its limit is not a breach (the grade's `> limit + EPS`).
* THE CONTROL for that: the same re-seat with the probe's tethers withheld is
  REFUSED at `intent 0->0` and C3's error stays -- so the acceptance above is
  the tether terms' doing, not the board's.

    python3 tests/test_reseat_intent_basis_parity.py
"""
import json
import os
import re
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
from placement import quench as q                  # noqa: E402
from placement import seeder                       # noqa: E402
from placement.writer import write_placed_output   # noqa: E402
from run_utils import check, evidence              # noqa: E402
import pose_score                                  # noqa: E402

RUN_ALL_TIMEOUT = 900

BOARDS = os.path.join(ROOT, 'kicad_files')
ESP = os.path.join(BOARDS, 'esp_prog.kicad_pcb')
WATCHY = os.path.join(BOARDS, 'watchy.kicad_pcb')
PLACE_SEED = os.path.join(ROOT, 'py_placer', 'place_seed.py')
SOURCES = ('kicad', 'sheet')
CLEARANCE = 0.2
STEPS = (-3.0, -1.5, 0.0, 1.5, 3.0)


def _intent(td, extra, name='intent.json'):
    doc = {'schema': 1, 'kind': fp.KIND, 'units': 'mm'}
    doc.update(extra)
    path = os.path.join(td, name)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    return fp.load_intent(path), path


def _probe(pcb, path, intent):
    bundle, _pr = fp.resolve_intent_gate(intent, pcb, SOURCES)
    st = pose_score.make_state(pcb, path, clearance=CLEARANCE,
                               keepouts=intent.keepouts)
    return q.IntentProbe(st, zones=bundle['zones'],
                         tethers=bundle.get('tethers')), st


def _grade_count(intent, pcb, path, rules):
    g = fp.grade(intent, pcb, path, group_sources=SOURCES,
                 clearance=CLEARANCE)
    return len([v for v in g.errors if v.rule in rules])


def _lattice(board, mover, extra, want_rules):
    """[(dx, dy, probe count, grade count)] over the mover's lattice."""
    rows = []
    f = parse_kicad_pcb(board).footprints[mover]
    with tempfile.TemporaryDirectory() as td:
        intent, _p = _intent(td, extra)
        out = os.path.join(td, 'pose.kicad_pcb')
        for dx in STEPS:
            for dy in STEPS:
                write_placed_output(board, out, [{
                    'reference': mover, 'new_x': f.x + dx,
                    'new_y': f.y + dy, 'new_rotation': f.rotation or 0.0}])
                pcb = parse_kicad_pcb(out)
                pr, _st = _probe(pcb, out, intent)
                assert set(pr.rules) == set(want_rules), pr.rules
                rows.append((dx, dy, pr.snapshot()['count'],
                             _grade_count(intent, pcb, out, pr.rules)))
    return rows


def _assert_parity(label, rows):
    bad = [r for r in rows if r[2] != r[3]]
    assert not bad, (label, bad[:5])
    counts = {r[3] for r in rows}
    assert len(counts) >= 2, (label, 'the lattice never crossed a limit',
                              counts)
    print(f"  PASS: {label}: probe == grade on all {len(rows)} poses "
          f"(counts seen {sorted(counts)})")


def test_parity_decap_distance_esp_prog():
    _assert_parity('esp_prog C3, decap_distance 1.5',
                   _lattice(ESP, 'C3', {'decaps': {'max_distance_mm': 1.5}},
                            ('decap_distance',)))


def test_parity_proximity_esp_prog():
    # R1's nearest pad reach to U1 is 6.12mm at its human pose, so a 6mm
    # limit is crossed both ways across the +/-3mm lattice.
    _assert_parity('esp_prog R1 near U1, proximity 6.0',
                   _lattice(ESP, 'R1', {'proximity': [
                       {'ref': 'R1', 'near': 'U1', 'max_mm': 6.0}]},
                       ('proximity',)))


def test_parity_both_decap_rules_watchy_once_per_term():
    extra = {'decaps': {'max_distance_mm': 1.5, 'max_pin_distance_mm': 1.5}}
    _assert_parity('watchy C1, decap_distance + decap_pin_distance 1.5',
                   _lattice(WATCHY, 'C1', extra,
                            ('decap_distance', 'decap_pin_distance')))
    with tempfile.TemporaryDirectory() as td:
        intent, _p = _intent(td, extra)
        pcb = parse_kicad_pcb(WATCHY)
        pr, st = _probe(pcb, WATCHY, intent)
        snap = pr.snapshot()
        wide = [t for t, v in zip(pr.tethers, snap['tethers'])
                if len(set(t.refs)) >= 3
                and v > t.threshold + 1e-9]
        assert wide, "no breached term binds 3+ refs: the arm is vacuous"
        per_ref = sum(1 for t, v in zip(pr.tethers, snap['tethers'])
                      if v > t.threshold + 1e-9 for _r in set(t.refs))
        grade = _grade_count(intent, pcb, WATCHY, pr.rules)
        assert snap['count'] == grade < per_ref, (snap['count'], grade,
                                                  per_ref)
    print(f"  PASS: {len(wide)} breached term(s) bind 3+ refs; count "
          f"{snap['count']} == grade {grade} (per-ref would be {per_ref})")


def test_a_term_whose_refs_are_all_locked_still_counts():
    with tempfile.TemporaryDirectory() as td:
        intent, _p = _intent(td, {'decaps': {'max_distance_mm': 1.5}})
        b = os.path.join(td, 'locked.kicad_pcb')
        write_placed_output(ESP, b, [])
        pcb0 = parse_kicad_pcb(b)
        rail = None
        pr0, st0 = _probe(pcb0, b, intent)
        for t in pr0.tethers:
            if t.data.get('cap') == 'C3':
                rail = set(t.refs)
        assert rail and 'C3' in rail, rail
        seeder.stamp_locked(b, sorted(rail))
        pcb = parse_kicad_pcb(b)
        pr, st = _probe(pcb, b, intent)
        # The GATE's own build drops it: every ref is locked.
        gate = st.tether_terms_for(
            fp.tether_gate_spec(intent), keep_locked=False)
        assert not [t for t in gate if t.data.get('cap') == 'C3'], gate
        grade = _grade_count(intent, pcb, b, pr.rules)
        assert grade == 1, grade
        assert pr.snapshot()['count'] == 1, pr.snapshot()
    print(f"  PASS: C3's term, all of {sorted(rail)} locked: gate drops it, "
          f"grade 1 == probe 1")


#: #1249: C3 at its human pose is 1.53mm from U2, the regulator it
#: decouples -- inside the recipe's 2.0mm limit. Pushed this far east it is
#: 2.33mm from U2, a violator the re-seat can fix and the tether-less
#: control cannot see.
C3_EAST_MM = 2.5


def _c3_east(td):
    """esp_prog with C3 pushed `C3_EAST_MM` east (shipped writer)."""
    c3 = parse_kicad_pcb(ESP).footprints['C3']
    out = os.path.join(td, 'c3_east.kicad_pcb')
    write_placed_output(ESP, out, [{'reference': 'C3',
                                    'new_x': c3.x + C3_EAST_MM,
                                    'new_y': c3.y,
                                    'new_rotation': c3.rotation or 0.0}])
    return evidence(out, 'the board with C3 pushed east')


def _esp_probe(td, decaps):
    intent, _p = _intent(td, {'decaps': decaps}, name='esp.json')
    pcb = parse_kicad_pcb(ESP)
    pr, st = _probe(pcb, ESP, intent)
    return intent, pcb, pr, st


def test_leaving_the_radius_is_counted_as_the_grade_does_but_not_licensed():
    """The COUNT reads a decap pair the way the grade does, so a cap walked
    past the search radius stops counting (it is `decap_ungraded`, warn).
    The LICENCE reads it the way the gate does -- still measured -- so the
    same move is a RISE, not a fix (phase-2 verifier: esp_prog C3, radius
    2.2, 1mm further from U1, read `1 -> 0` and was licensed). Since #1249
    C3 is measured against U2 (1.53mm), and 3mm east takes it to 2.73mm,
    past the 2.2mm radius."""
    with tempfile.TemporaryDirectory() as td:
        intent, pcb, pr, st = _esp_probe(
            td, {'max_distance_mm': 1.5, 'search_radius_mm': 2.2})
        c3 = st.parts['C3']
        before = pr.snapshot()
        assert before['count'] == 1, before
        st.apply_move('C3', c3.x + 3.0, c3.y, c3.rot)
        after = pr.snapshot()
        out = os.path.join(td, 'c3_out.kicad_pcb')
        write_placed_output(ESP, out, [{'reference': 'C3', 'new_x': c3.x,
                                        'new_y': c3.y,
                                        'new_rotation': c3.rot}])
        g = fp.grade(intent, parse_kicad_pcb(out), out,
                     group_sources=SOURCES, clearance=CLEARANCE)
        assert [v.rule for v in g.violations if v.ref == 'C3'] == [
            'decap_ungraded'], g.violations
        assert after['count'] == 0, after
        ok, risen = pr.licence(before, after)
        assert not ok and [r[1] for r in risen] == ['decap_distance'], risen
        assert pr.terms('C3')[-1] > 0.9, pr.terms('C3')
    print(f"  PASS: count 1 -> 0 as the grade has it, licence refuses "
          f"{risen[0][2]} {risen[0][3]:.3f} -> {risen[0][4]:.3f}")


def test_growing_inside_the_limit_is_no_finding():
    """A tether term that grows but stays WITHIN its limit is not risen, and
    prune's vector carries its EXCESS over the limit (0 here), not the raw
    distance -- a revert moving a compliant cap is no claim's business."""
    with tempfile.TemporaryDirectory() as td:
        _i, _pcb, pr, st = _esp_probe(td, {'max_distance_mm': 3.0})
        c3 = st.parts['C3']
        before = pr.snapshot()
        assert pr.terms('C3') == (0.0,), pr.terms('C3')
        # 2mm east: C3 sits within U2's x span (#1249: U2 is its chip), so
        # a shorter move along x does not change the distance at all.
        st.apply_move('C3', c3.x + 2.0, c3.y, c3.rot)
        after = pr.snapshot()
        i = next(k for k, t in enumerate(pr.tethers)
                 if t.data.get('cap') == 'C3')
        assert after['tethers'][i] > before['tethers'][i] + 0.1, (before,
                                                                   after)
        assert after['count'] == 0 and pr.licence(before, after) == (True,
                                                                     []), after
        assert pr.terms('C3') == (0.0,), pr.terms('C3')
    print(f"  PASS: {before['tethers'][i]:.3f} -> {after['tethers'][i]:.3f}mm "
          f"under a 3mm limit: no count, no rise, prune vector (0.0,)")


def test_the_limit_is_exclusive_at_its_tolerance():
    """A limit EQUAL to C3's exact measured distance is not a breach: the
    grade compares `> limit + EPS`, and so must the count. `>= limit` (or any
    slackened compare) counts it; only a limit set to the exact value can
    tell the two apart."""
    with tempfile.TemporaryDirectory() as td:
        _i, _pcb, pr0, _st = _esp_probe(td, {'max_distance_mm': 1.5})
        i = next(k for k, t in enumerate(pr0.tethers)
                 if t.data.get('cap') == 'C3')
        exact = pr0.snapshot()['tethers'][i]
        intent, pcb, pr, _st = _esp_probe(td, {'max_distance_mm': exact})
        assert pr.snapshot()['count'] == 0 == _grade_count(
            intent, pcb, ESP, ('decap_distance',)), exact
    print(f"  PASS: C3 at exactly its limit ({exact!r}mm) counts 0, as "
          f"graded")


def _summary(r):
    m = re.search(r'^JSON_SUMMARY: (.*)$', r.stdout, re.M)
    assert m, r.stdout[-1500:]
    return json.loads(m.group(1))


def test_the_issue_recipe_through_the_cli():
    from placement.portfolio import copy_siblings
    with tempfile.TemporaryDirectory() as td:
        intent, ipath = _intent(td, {'decaps': {'max_distance_mm': 2.0}})
        src = os.path.join(td, 'in.kicad_pcb')
        write_placed_output(_c3_east(td), src, [])
        copy_siblings(ESP, src)
        before = fp.grade(intent, parse_kicad_pcb(src), src,
                          group_sources=SOURCES, clearance=CLEARANCE)
        assert [(v.rule, v.ref) for v in before.errors] == [
            ('decap_distance', 'C3')], before.errors
        out = os.path.join(td, 'out.kicad_pcb')
        r = check([sys.executable, '-X', 'utf8', PLACE_SEED, src, out,
                   '--intent', ipath, '--reseat', 'C3', '--clearance',
                   str(CLEARANCE)], accept=True, timeout=RUN_ALL_TIMEOUT)
        s = _summary(r)
        ab = s['accept_basis']
        assert ab['intent_rules'] == ['decap_distance'], ab
        term = next(t for t in ab['terms'] if t['term'] == 'intent')
        want = len([v for v in before.errors
                    if v.rule in ab['intent_rules']])
        assert term['before'] == want == 1, (term, want)
        assert ab['fired'] == 'intent' and term['after'] == 0, ab
        assert 'accepted on intent[decap_distance]: 1 -> 0' in r.stdout, \
            r.stdout[-1500:]
        after = fp.grade(intent, parse_kicad_pcb(evidence(out, 'output')),
                         out, group_sources=SOURCES, clearance=CLEARANCE)
        assert not [v for v in after.errors if v.rule == 'decap_distance'], \
            after.errors
    print("  PASS: accepted on intent[decap_distance]: 1 -> 0; C3's error "
          "is gone from the written board")


def test_control_without_the_tether_terms_the_same_reseat_is_refused():
    """In-process, so the probe can be built without its tethers: the
    pre-#1068 probe on the same board and intent."""
    real = q.IntentProbe.__init__

    def zones_only(self, state, zones=(), refs=None, tethers=None):
        real(self, state, zones=zones, refs=refs, tethers=None)
    with tempfile.TemporaryDirectory() as td:
        intent, _p = _intent(td, {'decaps': {'max_distance_mm': 2.0}})
        board = _c3_east(td)
        runs = {}
        for arm in ('tethers', 'zones_only'):
            if arm == 'zones_only':
                q.IntentProbe.__init__ = zones_only
            try:
                res = seeder.reseat_scope(
                    parse_kicad_pcb(board), board, intent, refs=['C3'],
                    group_sources=(), clearance=CLEARANCE,
                    board_edge_clearance=0.5, grid_step=0.1, seed=0)
            finally:
                q.IntentProbe.__init__ = real
            out = os.path.join(td, f'{arm}.kicad_pcb')
            write_placed_output(board, out, res['moves'])
            runs[arm] = (res, _grade_count(intent, parse_kicad_pcb(out), out,
                                           ('decap_distance',)))
        (res_t, err_t), (res_z, err_z) = runs['tethers'], runs['zones_only']
        term_z = next(t for t in res_z['accept_basis']['terms']
                      if t['term'] == 'intent')
        assert res_t['accepted'] and err_t == 0, (res_t['accept_basis'],
                                                  err_t)
        assert not res_z['accepted'] and err_z == 1, (res_z['accept_basis'],
                                                      err_z)
        assert (term_z['before'], term_z['after']) == (0, 0), term_z
        assert res_z['accept_basis']['intent_rules'] == [], res_z
    print("  PASS: with the tether terms the re-seat is accepted and C3 is "
          "fixed; without them it reads intent 0->0 and is refused")


def test_prune_keeps_a_move_that_reduces_a_decap_breach():
    """`IntentProbe.terms(ref)` is what prune samples either side of a
    revert. esp_prog under 0.8mm limits: re-seating C4 moves it closer to its
    IC -- still past the limit, so the count does not move and the pass is
    refused, but the move REDUCED the breach at an hpwl cost (46.5 ->
    49.2mm). Prune must keep it on the claim, not revert it on the tuple:
    with the tether terms out of `terms()` it reverts."""
    with tempfile.TemporaryDirectory() as td:
        intent, _p = _intent(td, {'decaps': {'max_distance_mm': 0.8,
                                             'max_pin_distance_mm': 0.8}})
        res = seeder.reseat_scope(
            parse_kicad_pcb(ESP), ESP, intent, refs=['C4'],
            group_sources=(), clearance=CLEARANCE,
            board_edge_clearance=0.5, grid_step=0.1, seed=0)
        kept = [n for n in res['notes']
                if n.startswith('prune: KEPT') and 'C4' in n]
        reverted = [n for n in res['notes']
                    if n.startswith('prune: reverted') and 'C4' in n]
        assert kept and not reverted, res['notes']
    print(f"  PASS: {kept[0]}")


TESTS = [
    test_parity_decap_distance_esp_prog,
    test_parity_proximity_esp_prog,
    test_parity_both_decap_rules_watchy_once_per_term,
    test_a_term_whose_refs_are_all_locked_still_counts,
    test_the_issue_recipe_through_the_cli,
    test_control_without_the_tether_terms_the_same_reseat_is_refused,
    test_prune_keeps_a_move_that_reduces_a_decap_breach,
    test_leaving_the_radius_is_counted_as_the_grade_does_but_not_licensed,
    test_growing_inside_the_limit_is_no_finding,
    test_the_limit_is_exclusive_at_its_tolerance,
]


if __name__ == '__main__':
    only = sys.argv[1:]
    for t in TESTS:
        if only and not any(o in t.__name__ for o in only):
            continue
        print(f"--- {t.__name__}")
        t()
    print('ALL PASS')
