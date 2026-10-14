#!/usr/bin/env python3
"""#1051 phase 7: the cases `tests/mutate_1051.py`'s first run found
unguarded -- each test here exists because a named mutant survived every
test the PR had written, and each names the row it kills.

24 tests.

Kept apart from the phase files so each case is a FAST, single-purpose
killer (synthetic boards and direct calls wherever the mechanism allows),
and so the battery can select it by name:

* pin order: a net several members share orders nothing, and a member's
  key is its LOWEST host pad (`pin-order-shared-net-orders`,
  `pin-order-highest-pad`);
* a ball-grid host suggests no order (`suggest-grid-host-gets-pin-order`);
* an array naming a ref the board lacks is skipped as ABSENT, not abstained
  as geometry-less (`array-formation-grades-a-row-missing-a-part`);
* place_seed judges a row and a fixed pose at the WRITTEN poses
  (`place-seed-row-verdict-at-the-seed`,
  `place-seed-fixed-pose-judged-at-the-seed`);
* a partly-locked rigid group is anchored, never translated
  (`merge-partly-locked-row-translates`);
* the row seat: courtyard-centre offsets, the pin order over a shuffled
  declaration, the self-check's verdict, the mod-180 rotation dedupe, the
  axis parallel to the host's side, a refused row reverted in place, and a
  row the intent check refuses or with a placed member not seated
  (`row-offsets-by-origin-not-courtyard`, `row-order-ignores-the-pins`,
  `row-self-check-always-formed`, `row-rotation-no-mod-180-dedupe`,
  `row-axis-perpendicular-to-the-pins`, `seat-block-failed-row-not-reverted`,
  `row-refused-by-the-intent-check-still-seated`,
  `row-with-a-placed-member-still-seated`);
* stage 0: pad clearance between two DECLARED poses whose courtyards clear,
  a seated fixed pose frozen to the eviction rung, and a copper-less part
  judged by its courtyard (`fixed-pad-clearance-unchecked`,
  `fixed-seated-evictable`, `fixed-padless-courtyard-unchecked`);
* a row's caps are not the decap pin stage's
  (`row-member-claimed-by-the-decap-stage`);
* a zoned row that seats nowhere is not zone-packed a second time
  (`zoned-row-zone-packed-first`; added after the phase-7 verifier reached
  the double failure the battery had called unreachable);
* release and rejoin: a block move that clears keeps the member, a member
  that moved away stays released, a tight formation is its own business on
  both sides, and a release in an empty pass still gets its pass
  (`release-although-a-block-move-clears-it`, `rejoin-out-of-its-slot`,
  `release-judges-the-formation-pairs`,
  `rejoin-with-a-different-exclusion-set`,
  `release-stops-before-the-member-moves`);
* tethers: a swap checked on both halves in either order, a term past its
  limit allowed to improve, a tethers-only quench still gating swaps, and
  no cluster for a locked IC (`tether-swap-checks-one-half`,
  `tether-not-monotone`, `swap-gate-tethers-only-not-asked`,
  `tether-cluster-without-its-ic`).

    python3 tests/test_1051_hardening.py [name-filter ...]
"""
import os
import random
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS_DIR)

from kicad_parser import parse_kicad_pcb           # noqa: E402
from placement import arrays as arr                # noqa: E402
from placement import floorplan as fp              # noqa: E402
from placement import quench as q                  # noqa: E402
from placement import seeder                       # noqa: E402

RUN_ALL_TIMEOUT = 600

BOARDS = os.path.join(ROOT, 'kicad_files')
SPLITFLAP = os.path.join(BOARDS, 'splitflap_driver.kicad_pcb')
ULX3S = os.path.join(BOARDS, 'ulx3s.kicad_pcb')
ESP = os.path.join(BOARDS, 'esp_prog.kicad_pcb')

#: esp_prog's R3/R4, the series resistors on U1 pins 3 and 4 (as in
#: test_1051_seed_arrays).
ESP_ROW = {'name': 'u1_uart', 'members': ['R4', 'R3'], 'serves': 'U1',
           'order': 'pin', 'rotation': 'shared', 'pitch_mm': 'auto',
           'axis': 'auto', 'why': 'test row'}


def _pad(num, net):
    return SimpleNamespace(pad_number=num, net_id=net)


def _esp_intent(**row):
    doc = fp.emit_intent(parse_kicad_pcb(ESP), ESP)
    doc['arrays'] = [dict(ESP_ROW, **row)]
    return fp.intent_from_dict(doc, ESP)


def _seed(board, intent, **kw):
    kw.setdefault('group_sources', ('kicad', 'sheet'))
    kw.setdefault('clearance', 0.2)
    return seeder.seed_from_intent(parse_kicad_pcb(board), board, intent,
                                   random.Random('0'), **kw)


def _write_board(td, text, name='b.kicad_pcb'):
    path = os.path.join(td, name)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return path


# --------------------------------------------------------------------------
# arrays
# --------------------------------------------------------------------------

def test_pin_order_ignores_shared_nets_and_keys_on_the_lowest_pad():
    """RB's only own net lands on host pads 7 and 3 (no single-pad net, so
    its key is the LOWER, 3); RA's lands on pad 5 alone. Net 10 reaches
    both members and host pad 1: a shared net orders nothing. So RB before
    RA. Counting net 10 as own keys both at pad 1 (ties by name: RA
    first); keying on the highest pad puts RB at 7 (after RA)."""
    pcb = SimpleNamespace(footprints={
        'U1': SimpleNamespace(pads=[_pad('1', 10), _pad('7', 12),
                                    _pad('3', 12), _pad('5', 11)]),
        'RA': SimpleNamespace(pads=[_pad('1', 10), _pad('2', 11)]),
        'RB': SimpleNamespace(pads=[_pad('1', 10), _pad('2', 12)]),
    })
    order, unres = arr.pin_order(pcb, 'U1', ['RA', 'RB'])
    assert order == ['RB', 'RA'] and unres == {}, (order, unres)
    print("  PASS: the shared net orders nothing; RB keys on pad 3, not 7")


def test_a_ball_grid_host_suggests_no_order():
    """ulx3s's LED resistors R41-R48 run on U1's balls: their sort order is
    no position along a row, so the suggestion carries `order: "unknown"`
    and says why (`_grid_named`)."""
    pcb = parse_kicad_pcb(ULX3S)
    sug = [s for s in arr.suggest_arrays(pcb)
           if s['criterion'] == 'pin_run' and s['serves'] == 'U1']
    assert sug, "no pin run on U1: the arm is vacuous"
    for s in sug:
        assert s['order'] == 'unknown', (s['name'], s['order'])
        assert s['evidence']['host_pads'] == 'grid', s['evidence']
        assert s['evidence']['order_basis'] == arr.GRID_ORDER_NOTE
    print(f"  PASS: {len(sug)} pin run(s) on U1's balls suggest no order")


def test_a_row_naming_a_missing_ref_is_skipped_as_absent():
    """A member the board does not have is `array_problems`' ERROR; the
    formation rule SKIPS that row (`not on this board`) rather than abstain
    on it as geometry it could not measure."""
    pcb = parse_kicad_pcb(SPLITFLAP)
    doc = fp.emit_intent(pcb, SPLITFLAP)
    doc['arrays'] = [{'name': 'ghost', 'members': ['R6', 'R999'],
                      'order': 'unknown'}]
    g = fp.grade(fp.intent_from_dict(doc), pcb, SPLITFLAP,
                 group_sources=('kicad', 'sheet'), clearance=0.2)
    row = next(a for a in g.array_measured if a['name'] == 'ghost')
    assert row['formed'] is None, row
    assert row['skipped'] == 'not on this board: R999', row
    assert not any(k.startswith('arrays[ghost]')
                   for k in (getattr(g, 'abstained', None) or {})), \
        g.abstained
    print(f"  PASS: {row['skipped']}")


# --------------------------------------------------------------------------
# place_seed's disclosure, at the written poses
# --------------------------------------------------------------------------

def test_place_seed_judges_rows_and_fixed_poses_at_the_written_board():
    import place_seed
    result = {
        'arrays_formed': {'r': {'verdict': 'formed', 'members': ['A', 'B']}},
        'fixed_seated': {'J1': {'x': 1.0, 'y': 2.0, 'rot': 90.0},
                         'J2': {'x': 5.0, 'y': 5.0, 'rot': 0.0}},
        'fixed_refused': {}, 'array_unseated': {}, 'decap_stage': None}
    graded = SimpleNamespace(array_measured=[
        {'name': 'r', 'formed': False, 'failed': ['pitch']}])
    written = {'J1': SimpleNamespace(x=1.5, y=2.0, rotation=90.0),
               'J2': SimpleNamespace(x=5.0, y=5.0, rotation=360.0)}
    import io
    from contextlib import redirect_stdout
    with redirect_stdout(io.StringIO()):
        s = place_seed.seed_structure_summary(result, graded, written)
    r = s['arrays_formed']['r']
    assert r['verdict'] == 'broken' and r['verdict_at_seed'] == 'formed', r
    assert r['failed'] == ['pitch'], r
    assert s['fixed_seated']['J1']['at_written_pose'] is False, s
    assert s['fixed_seated']['J2']['at_written_pose'] is True, s
    why = place_seed.fixed_pose_reason(s)
    assert why and '(J1)' in why, why
    print("  PASS: a row the polish broke reads broken; a fixed pose moved "
          "0.5mm is not honoured and fails the gate")


# --------------------------------------------------------------------------
# quench: the rigid-group merge
# --------------------------------------------------------------------------

def test_a_partly_locked_rigid_group_is_anchored_not_translated():
    parts = {r: None for r in 'ABCD'}
    blocks, info = q.merge_groups({}, {'array:r': ['A', 'B', 'C'],
                                       'block:b': ['D', 'A']}, {},
                                  {'A', 'B', 'D'}, parts)
    assert 'array:r' not in blocks, blocks
    assert info['anchored'] == {'array:r': ['C']}, info['anchored']
    assert info['held'] == {'A': 'array:r', 'B': 'array:r'}, info['held']
    print("  PASS: a row with one immovable member is anchored: its movable "
          "members are held, and it takes no translate")


# --------------------------------------------------------------------------
# the row seat
# --------------------------------------------------------------------------

def test_row_offsets_line_up_courtyard_centres_not_origins():
    """A member whose courtyard centre is (1.0, 0.5) off its origin: the
    offsets put every COURTYARD centre on the row line, `pitch` apart."""
    class _P:
        def __init__(self, cx, cy):
            self.c = (cx, cy)

        def rect(self, x, y, rot):
            cx, cy = self.c
            return (x + cx - 0.5, y + cy - 0.3, x + cx + 0.5, y + cy + 0.3)
    st = SimpleNamespace(parts={'A': _P(1.0, 0.5), 'B': _P(-0.4, 0.0),
                                'C': _P(0.0, -0.2)})
    for axis in ('x', 'y'):
        offs = seeder._row_offsets(st, ['A', 'B', 'C'], 0.0, axis, 2.0)
        centres = []
        for m, (ox, oy) in zip('ABC', offs):
            r = st.parts[m].rect(ox, oy, 0.0)
            centres.append(((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0))
        k = 0 if axis == 'x' else 1
        along = [round(c[k], 9) for c in centres]
        across = {round(c[1 - k], 9) for c in centres}
        assert along == [-2.0, 0.0, 2.0] and across == {0.0}, (axis, centres)
    print("  PASS: courtyard centres on the line at the pitch, both axes")


def test_a_shuffled_declaration_is_seated_in_pin_order():
    """`order: "pin"` over members declared in some other order: the row is
    seated in the served part's PIN order (either direction), not in the
    order the list happens to be written."""
    pcb = parse_kicad_pcb(SPLITFLAP)
    doc = fp.emit_intent(pcb, SPLITFLAP, derive_arrays='auto')
    row = next(a for a in doc['arrays'] if a['name'] == 'U4:47k')
    pin = list(row['members'])
    shuffled = pin[3:] + pin[:3]
    assert shuffled not in (pin, pin[::-1])
    doc['arrays'] = [dict(row, members=shuffled)]
    res = _seed(SPLITFLAP, fp.intent_from_dict(doc, SPLITFLAP))
    rec = res['arrays_formed'].get('U4:47k')
    assert rec is not None, res['array_unseated']
    assert rec['members'] in (pin, pin[::-1]), (rec['members'], pin)
    assert rec['verdict'] == 'formed', rec
    print(f"  PASS: declared {shuffled}, seated {rec['members']}")


def test_the_seeded_verdict_is_the_self_checks():
    """`arrays_formed[].verdict` is `_formation_at`'s reading, not a
    constant: a self-check that fails reports `broken`, and says so."""
    real = seeder._formation_at
    seeder._formation_at = lambda *a, **k: {
        'formed': False, 'failed': ['pitch'], 'unchecked': []}
    try:
        res = _seed(ESP, _esp_intent())
    finally:
        seeder._formation_at = real
    rec = res['arrays_formed']['u1_uart']
    assert rec['verdict'] == 'broken' and rec['failed'] == ['pitch'], rec
    assert any('self-check FAILED: pitch' in n for n in res['notes'])
    print("  PASS: a failing self-check is reported broken")


def _spy_seat_block():
    calls = []
    real = seeder._seat_block

    def spy(state, members, rot_options, pitch_spec, axes, target, *a, **k):
        calls.append({'members': list(members), 'rots': list(rot_options),
                      'axes': list(axes), 'target': target})
        return real(state, members, rot_options, pitch_spec, axes, target,
                    *a, **k)
    return calls, real, spy


def test_a_two_pad_row_tries_each_angle_once_modulo_180():
    """A row of <= 2-pad parts looks the same turned 180: the seat tries
    each angle once modulo 180, never both halves of a pair."""
    calls, real, spy = _spy_seat_block()
    seeder._seat_block = spy
    try:
        _seed(ESP, _esp_intent())
    finally:
        seeder._seat_block = real
    assert calls, "the row seat was never called"
    rots = calls[0]['rots']
    assert rots, calls[0]
    for i, a in enumerate(rots):
        for b in rots[i + 1:]:
            assert arr._ang_diff(a, b, 180.0) > 1e-6, rots
    print(f"  PASS: rotations tried {rots}")


#: A locked host U1 whose two row pins are on its EAST side, one above the
#: other, and nothing else on the board: the row runs PARALLEL to that side
#: (along y).
AXIS_BOARD = """(kicad_pcb
 (version 20241229)
 (net 0 "") (net 1 "/A") (net 2 "/B") (net 3 "/C") (net 4 "/D")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
 (gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (uuid "e1"))
 (footprint "t:U" (layer "F.Cu") (uuid "fp-U1") (at 20 20) (locked yes)
  (property "Reference" "U1" (at 0 0 0))
  (fp_rect (start -3 -3) (end 3 3) (layer "F.CrtYd") (uuid "cu"))
  (pad "1" smd rect (at 2.5 -1) (size 0.6 0.4) (layers "F.Cu") (net 1 "/A") (uuid "u1"))
  (pad "2" smd rect (at 2.5 1) (size 0.6 0.4) (layers "F.Cu") (net 2 "/B") (uuid "u2")))
 (footprint "t:R" (layer "F.Cu") (uuid "fp-R1") (at 5 35)
  (property "Reference" "R1" (at 0 0 0))
  (fp_rect (start -0.8 -0.5) (end 0.8 0.5) (layer "F.CrtYd") (uuid "c1"))
  (pad "1" smd rect (at -0.4 0) (size 0.4 0.5) (layers "F.Cu") (net 1 "/A") (uuid "r11"))
  (pad "2" smd rect (at 0.4 0) (size 0.4 0.5) (layers "F.Cu") (net 3 "/C") (uuid "r12")))
 (footprint "t:R" (layer "F.Cu") (uuid "fp-R2") (at 10 35)
  (property "Reference" "R2" (at 0 0 0))
  (fp_rect (start -0.8 -0.5) (end 0.8 0.5) (layer "F.CrtYd") (uuid "c2"))
  (pad "1" smd rect (at -0.4 0) (size 0.4 0.5) (layers "F.Cu") (net 2 "/B") (uuid "r21"))
  (pad "2" smd rect (at 0.4 0) (size 0.4 0.5) (layers "F.Cu") (net 4 "/D") (uuid "r22")))
)
"""


def test_the_row_runs_parallel_to_the_side_its_pins_are_on():
    with tempfile.TemporaryDirectory() as td:
        path = _write_board(td, AXIS_BOARD)
        doc = {'schema': 1, 'kind': fp.KIND, 'units': 'mm',
               'arrays': [{'name': 'r', 'members': ['R1', 'R2'],
                           'serves': 'U1', 'order': 'pin', 'rotation': 0,
                           'pitch_mm': 'auto', 'axis': 'auto'}]}
        res = _seed(path, fp.intent_from_dict(doc, path), group_sources=(),
                    board_edge_clearance=0.1)
    rec = res['arrays_formed'].get('r')
    assert rec is not None, res['array_unseated']
    assert rec['axis'] == 'y', rec
    assert rec['anchor'][0] > 20.0, rec     # on U1's east side
    print(f"  PASS: pins on U1's east side, row along {rec['axis']} at "
          f"{rec['anchor']}")


#: SIBLING_BOARD: two parts whose pads reach past their courtyards
#: (test_1051_seed_arrays' sibling fixture): no anchor seats them as a row.
SIBLING_BOARD = '''(kicad_pcb
 (version 20241229)
 (net 0 "") (net 1 "/A") (net 2 "/B") (net 3 "/C") (net 4 "/D")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
 (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts") (uuid "e1"))
%s)
'''
SIBLING_FP = ('''  (footprint "t:R" (layer "F.Cu") (uuid "fp-%(r)s") (at %(x)s 10)
   (property "Reference" "%(r)s" (at 0 0 0))
   (fp_rect (start -0.2 -0.2) (end 0.2 0.2) (layer "F.CrtYd") (uuid "c-%(r)s"))
   (pad "1" smd rect (at -0.9 0) (size 0.8 0.8) (layers "F.Cu") (net %(a)s "/%(na)s") (uuid "%(r)s1"))
   (pad "2" smd rect (at 0.9 0) (size 0.8 0.8) (layers "F.Cu") (net %(b)s "/%(nb)s") (uuid "%(r)s2"))
  )
''')


def _sibling_board(td, x1=25, x2=5):
    parts = (SIBLING_FP % dict(r='R1', x=x1, a=1, na='A', b=2, nb='B')
             + SIBLING_FP % dict(r='R2', x=x2, a=3, na='C', b=4, nb='D'))
    return _write_board(td, SIBLING_BOARD % parts)


def test_a_refused_row_is_put_back_where_it_was():
    """`_seat_block` applies a hit's moves, re-checks with the siblings
    seated, and REVERTS a hit that fails: a row that seats nowhere leaves
    every member exactly where it started."""
    import pose_score
    with tempfile.TemporaryDirectory() as td:
        path = _sibling_board(td)
        st = pose_score.make_state(parse_kicad_pcb(path), path,
                                   clearance=0.2, board_edge_clearance=0.1)
        before = {m: (st.parts[m].x, st.parts[m].y, st.parts[m].rot)
                  for m in ('R1', 'R2')}
        res = seeder._seat_block(st, ['R1', 'R2'], [0.0], 'auto', ['x'],
                                 (15.0, 10.0), {'R1', 'R2'}, cap=200)
        after = {m: (st.parts[m].x, st.parts[m].y, st.parts[m].rot)
                 for m in ('R1', 'R2')}
    assert res['ok'] is False, res
    assert after == before, (before, after)
    print(f"  PASS: refused after {res['poses_tried']} poses, members back "
          f"at their input poses")


def test_a_row_the_intent_check_refuses_or_with_a_placed_member_is_not_seated():
    """Two refusals before the seat: a row `array_problems` rejects (mixed
    footprints), and a row one of whose members is already placed (outside
    the seed scope) -- that member is NOT moved to form the row."""
    pcb = parse_kicad_pcb(ESP)
    fps = pcb.footprints
    other = next(r for r in sorted(fps) if r.startswith('C')
                 and fps[r].footprint_name != fps['R3'].footprint_name)
    res = _seed(ESP, _esp_intent(members=['R3', other], serves='U1'))
    un = res['array_unseated'].get('u1_uart')
    assert un and un['reason'].startswith('refused by the intent check'), un
    scope = set(fps) - {'R4'}
    res = _seed(ESP, _esp_intent(), seed_refs=scope)
    un = res['array_unseated'].get('u1_uart')
    assert un and 'R4 already placed' in un['reason'], un
    r4 = fps['R4']
    got = {p['reference']: p for p in res['placements']}
    if 'R4' in got:
        assert (abs(got['R4']['new_x'] - r4.x) < 1e-6
                and abs(got['R4']['new_y'] - r4.y) < 1e-6), got['R4']
    print("  PASS: mixed footprints refused by the intent check; a placed "
          "R4 is left where it is")


def test_a_declared_row_of_caps_is_not_the_decap_stages_to_claim():
    """A cap that is an array member belongs to its row, not to the decap
    pin stage -- even when the row does not seat (a pitch below the
    courtyard) and its members fall through. Control: the same caps, with
    no array declared, ARE claimed at a supply pin."""
    from placement import groups as groups_mod
    pcb = parse_kicad_pcb(ESP)
    doc = fp.emit_intent(pcb, ESP)
    doc['decaps'] = dict(doc.get('decaps') or {}, max_distance_mm=3.0)
    # The owner ICs seated first (stage 0), so the pin stage has pins: the
    # stage's own owner set (`seeder.decap_owner_refs`). Read off the
    # elected tether ICs instead, U1 dropped out once C3 elected U2, its
    # regulator (#1249), and only one near cap was claimed.
    from placement import seeder as seeder_mod
    near, _beyond, _o = groups_mod.decap_populations(pcb)
    fps = pcb.footprints
    doc['fixed_poses'] = [
        {'ref': r, 'x': fps[r].x, 'y': fps[r].y, 'rot': fps[r].rotation or 0,
         'basis': 'declared', 'why': 'the owner IC, seated first'}
        for r in sorted(seeder_mod.decap_owner_refs(pcb))]
    ctl = _seed(ESP, fp.intent_from_dict(doc, ESP))
    claimed = sorted(n.split(':')[0] for n in ctl['notes']
                     if ': decap for ' in n)
    caps = sorted(c for ic in near for c, _d in near[ic] if c in claimed)
    same = {}
    for c in caps:
        same.setdefault(pcb.footprints[c].footprint_name, []).append(c)
    members = next((v[:2] for _k, v in sorted(same.items()) if len(v) >= 2),
                   None)
    assert members, ("no two claimed caps of one footprint: the arm is "
                     "vacuous", claimed)
    doc['arrays'] = [{'name': 'caps', 'members': members, 'order': 'unknown',
                      'rotation': 'shared', 'pitch_mm': 0.3, 'axis': 'x'}]
    res = _seed(ESP, fp.intent_from_dict(doc, ESP))
    assert 'caps' in res['array_unseated'], res['arrays_formed']
    assert res['decap_stage']['array_members_skipped'] == members, \
        res['decap_stage']
    got = [n for n in res['notes']
           if any(n.startswith(f"{m}: decap for") for m in members)]
    assert not got, got
    print(f"  PASS: {members} claimed by the pin stage without the array, "
          f"and not with it")


def test_a_zoned_row_that_seats_nowhere_is_not_zone_packed_again():
    """A ZONED row owns its members through stage 2: `_seat_array` runs in
    the zone's turn and, when the row does not seat, puts each member into
    the zone one by one. The zone pack must not then take them a second
    time. esp_prog (the phase-7 verifier's probe): U1 pinned at its own
    pose, R3/R4 a row in a block zoned to U1's pad bounding box -- the row
    hits its pose cap AND the one-by-one fallback finds no pose in the zone
    (U1 fills it). The pack, re-run over them, would log "no legal pose
    inside zone 'z'" for each and hand them a zone-jittered context."""
    pcb = parse_kicad_pcb(ESP)
    u1 = pcb.footprints['U1']
    xs = [p.global_x for p in u1.pads]
    ys = [p.global_y for p in u1.pads]
    zone = [min(xs), min(ys), max(xs), max(ys)]
    doc = fp.emit_intent(pcb, ESP)
    doc['fixed_poses'] = [{'ref': 'U1', 'x': u1.x, 'y': u1.y,
                           'rot': u1.rotation or 0, 'basis': 'declared'}]
    doc['blocks'] = list(doc.get('blocks') or []) + [
        {'name': 'z', 'refs': ['R3', 'R4'], 'zone': zone}]
    doc['arrays'] = [dict(ESP_ROW)]
    res = _seed(ESP, fp.intent_from_dict(doc, ESP), array_pose_cap=200)
    assert 'U1' in res['fixed_seated'], res['fixed_refused']
    un = res['array_unseated'].get('u1_uart')
    assert un and un['capped'], (un, res['arrays_formed'])
    got = {p['reference']: p for p in res['placements']}
    for m in ('R3', 'R4'):
        x, y = got[m]['new_x'], got[m]['new_y']
        # The fallback did NOT put it in the zone: the double failure is
        # reached, so the assertion below is not vacuous.
        assert not (zone[0] <= x <= zone[2] and zone[1] <= y <= zone[3]), \
            (m, x, y, zone)
    packed = [n for n in res['notes'] if 'no legal pose inside zone' in n
              and n.split(':')[0] in ('R3', 'R4')]
    assert not packed, packed
    # The fallback's own miss is disclosed, in the notes and the record.
    assert sorted(un.get('zone_unseated') or ()) == ['R3', 'R4'], un
    missed = [n for n in res['notes'] if "zone fallback found no pose" in n
              and n.split(':')[0] in ('R3', 'R4')]
    assert len(missed) == 2, missed
    print("  PASS: the row capped, the fallback found no zone pose and said "
          "so for R3/R4, and the zone pack left them alone")


# --------------------------------------------------------------------------
# stage 0
# --------------------------------------------------------------------------

def test_declared_poses_whose_courtyards_clear_but_pads_collide_are_refused():
    """The courtyards of R1 at x=10 and R2 at x=10.5 are 0.1mm apart (legal
    to KiCad), but their pads -- which reach past the courtyards -- overlap:
    both declarations are refused on PAD clearance, each naming the other."""
    with tempfile.TemporaryDirectory() as td:
        path = _sibling_board(td)
        doc = {'schema': 1, 'kind': fp.KIND, 'units': 'mm',
               'fixed_poses': [
                   {'ref': 'R1', 'x': 10.0, 'y': 10.0, 'rot': 0,
                    'basis': 'declared'},
                   {'ref': 'R2', 'x': 10.5, 'y': 10.0, 'rot': 0,
                    'basis': 'declared'}]}
        res = _seed(path, fp.intent_from_dict(doc, path), group_sources=(),
                    board_edge_clearance=0.1)
    ref = res['fixed_refused']
    assert set(ref) == {'R1', 'R2'}, (ref, res['fixed_seated'])
    assert 'pad clearance to R2' in ref['R1']['reason'], ref['R1']
    # The SHORT is named, not only its clearance number (re-review: an
    # `if sf.pad_overlap:` -> `if False:` mutant survived on the number).
    assert "pads short R2's" in ref['R1']['reason'], ref['R1']
    assert 'courtyard' not in ref['R1']['reason'], ref['R1']
    assert ref['R1']['conflicts_with_declared'] == ['R2'], ref['R1']
    print(f"  PASS: {ref['R1']['reason']}")


#: A 9 x 4mm board whose big part X1 fits only where the fixed-pose R1 sits.
EVICT_BOARD = '''(kicad_pcb
 (version 20241229)
 (net 0 "") (net 1 "/A") (net 3 "/C")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
 (gr_rect (start 0 0) (end 9 4) (layer "Edge.Cuts") (uuid "e1"))
 (footprint "t:R" (layer "F.Cu") (uuid "fp-R1") (at 7 2)
  (property "Reference" "R1" (at 0 0 0))
  (fp_rect (start -0.8 -0.5) (end 0.8 0.5) (layer "F.CrtYd") (uuid "c1"))
  (pad "1" smd rect (at -0.4 0) (size 0.5 0.6) (layers "F.Cu") (net 1 "/A") (uuid "a1"))
  (pad "2" smd rect (at 0.4 0) (size 0.5 0.6) (layers "F.Cu") (net 3 "/C") (uuid "a2")))
 (footprint "t:BIG" (layer "F.Cu") (uuid "fp-X1") (at 4 2)
  (property "Reference" "X1" (at 0 0 0))
  (fp_rect (start -3.6 -1.6) (end 3.6 1.6) (layer "F.CrtYd") (uuid "c3"))
  (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 3 "/C") (uuid "x1")))
)
'''


def test_a_seated_fixed_pose_is_frozen_to_the_eviction_rung():
    with tempfile.TemporaryDirectory() as td:
        path = _write_board(td, EVICT_BOARD)
        doc = {'schema': 1, 'kind': fp.KIND, 'units': 'mm',
               'fixed_poses': [{'ref': 'R1', 'x': 4.5, 'y': 2.0, 'rot': 0,
                                'basis': 'declared'}]}
        res = _seed(path, fp.intent_from_dict(doc, path), group_sources=(),
                    board_edge_clearance=0.1, evict_depth=1)
    assert 'R1' in res['fixed_seated'], res['fixed_refused']
    assert 'X1' in res['unseated'], res['unseated']
    frozen = (res['no_pose_census'].get('X1') or {}).get('frozen') or {}
    assert frozen.get('R1') == 'fixed_pose', res['no_pose_census'].get('X1')
    assert not [e for e in res['evictions'] if e.get('accepted')], \
        res['evictions']
    print(f"  PASS: X1's census names R1 frozen ({frozen})")


def test_a_copperless_fixed_pose_past_the_outline_is_refused():
    """A part with no copper pad and no hole (a logo: one paste-only pad)
    has nothing to anchor an overhang, so it is judged by its COURTYARD at
    zero margin: 1.5mm past the edge is refused; inside, it seats."""
    board = '''(kicad_pcb
 (version 20241229)
 (net 0 "") (net 1 "/A")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
 (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts") (uuid "e1"))
 (footprint "t:LOGO" (layer "F.Cu") (uuid "fp-G1") (at 10 10)
  (property "Reference" "G1" (at 0 0 0))
  (fp_rect (start -2 -2) (end 2 2) (layer "F.CrtYd") (uuid "c1"))
  (pad "" smd rect (at 0 0) (size 1 1) (layers "F.Paste") (uuid "p1")))
 (footprint "t:R" (layer "F.Cu") (uuid "fp-R1") (at 5 5)
  (property "Reference" "R1" (at 0 0 0))
  (fp_rect (start -0.8 -0.5) (end 0.8 0.5) (layer "F.CrtYd") (uuid "c2"))
  (pad "1" smd rect (at -0.4 0) (size 0.4 0.5) (layers "F.Cu") (net 1 "/A") (uuid "r1")))
)
'''
    got = {}
    with tempfile.TemporaryDirectory() as td:
        path = _write_board(td, board)
        for x in (19.5, 10.0):
            doc = {'schema': 1, 'kind': fp.KIND, 'units': 'mm',
                   'fixed_poses': [{'ref': 'G1', 'x': x, 'y': 10.0, 'rot': 0,
                                    'basis': 'declared'}]}
            got[x] = _seed(path, fp.intent_from_dict(doc, path),
                           group_sources=(), board_edge_clearance=0.1)
    why = (got[19.5]['fixed_refused'].get('G1') or {}).get('reason', '')
    assert 'no copper pad or hole to anchor an overhang' in why, got[19.5]
    assert got[10.0]['fixed_seated']['G1']['how'] == 'contained', got[10.0]
    print(f"  PASS: {why}")


#: A free 30 x 20mm board: a two-part row R1+R2, and X1 (the intruder,
#: locked by the caller) somewhere near it. Nets join the row to each other.
ROW_BOARD = '''(kicad_pcb
 (version 20241229)
 (net 0 "") (net 1 "/A") (net 2 "/B") (net 3 "/C")
 (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
 (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts") (uuid "e1"))
%s)
'''
ROW_FP = ('''  (footprint "t:R" (layer "F.Cu") (uuid "fp-%(r)s") (at %(x)s %(y)s)
   (property "Reference" "%(r)s" (at 0 0 0))
   (fp_rect (start -0.8 -0.5) (end 0.8 0.5) (layer "F.CrtYd") (uuid "c-%(r)s"))
   (pad "1" smd rect (at -0.4 0) (size 0.4 0.5) (layers "F.Cu") (net %(a)s "/%(na)s") (uuid "%(r)s1"))
   (pad "2" smd rect (at 0.4 0) (size 0.4 0.5) (layers "F.Cu") (net 3 "/C") (uuid "%(r)s2"))
  )
''')


def _row_board(td, poses):
    """`poses`: [(ref, x, y)]; nets A/B alternate, all share net C."""
    body = ''.join(ROW_FP % dict(r=r, x=x, y=y, a=1 + i % 2,
                                 na='AB'[i % 2])
                   for i, (r, x, y) in enumerate(poses))
    return _write_board(td, ROW_BOARD % body)


def _qstate(path):
    import io
    from contextlib import redirect_stdout
    with redirect_stdout(io.StringIO()):
        st = q.QuenchState(parse_kicad_pcb(path), path, 0.2, 0.1, 30.0, 0.5,
                           0.15, 2.0, 2.0, 2.0, 0.1, 0.3)
    st.build_neighbor_lists(3.1)
    return st


def test_a_member_a_block_move_can_clear_is_not_released():
    """Release needs BOTH halves: the incumbent pose fails a clause, AND no
    admissible block offset clears it. X1 overlaps R2 by 0.6mm on a free
    board: a 1mm translate of the row clears it, so R2 stays in its row at
    a 3mm cap; at a 0.3mm cap no offset reaches, and it is released."""
    with tempfile.TemporaryDirectory() as td:
        path = _row_board(td, [('R1', 10, 10), ('R2', 12, 10),
                               ('X1', 13, 10)])
        st = _qstate(path)
        row = ['R1', 'R2']
        assert q._clause_failing(st, 'R2', exclude=set(row)) == 'legality'
        at3 = q._release_clause(st, 'R2', set(row), row, 3.0, 1.0, 0.1)
        at03 = q._release_clause(st, 'R2', set(row), row, 0.3, 0.3, 0.1)
    assert at3 is None, at3
    assert at03 == 'legality', at03
    print("  PASS: a block move clears R2 at a 3mm cap (kept); at 0.3mm "
          "nothing does (released)")


def test_a_released_member_that_moved_away_clean_stays_released():
    """Rejoin needs the member IN ITS SLOT: R3 released after pass 1, then
    moved 4mm alone to a clean pose, is clean and still does not rejoin;
    the control (R3 left in its slot) does."""
    got = {}
    with tempfile.TemporaryDirectory() as td:
        path = _row_board(td, [('R1', 10, 10), ('R2', 12, 10),
                               ('R3', 14, 10)])
        for moved in (False, True):
            st = _qstate(path)
            name = 'array:r'
            row = ['R1', 'R2', 'R3']
            info = {'groups': {name: list(row)}, 'anchored': {}}
            held = {'R1': name, 'R2': name}
            blocks = {name: ['R1', 'R2']}
            released = [{'ref': 'R3', 'group': name, 'clause': 'legality',
                         'pass': 1, '_anchor': 'R1',
                         '_slot': q._slot_of(st, 'R3', 'R1')}]
            rejoined = []
            if moved:
                p = st.parts['R3']
                st.apply_move('R3', p.x, p.y + 4.0, p.rot)
                assert q._clause_failing(st, 'R3',
                                         exclude=set(row)) is None
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                q._update_releases(st, held, blocks, info, released,
                                   rejoined, 3, 3.0, 1.0, 0.1)
            got[moved] = [r['ref'] for r in rejoined]
    assert got[False] == ['R3'], got
    assert got[True] == [], got
    print("  PASS: in its slot R3 rejoins; moved 4mm away (clean) it stays "
          "released")


def test_a_formation_tighter_than_the_clearance_is_its_own_business():
    """A row seated at a reduced courtyard clearance (the seat's 0.02mm
    floor) has siblings closer than the board clearance: that spacing is
    the formation's, so neither the release nor the rejoin test counts it.
    R1-R2-R3 at 0.05mm courtyard gaps: R2 is not released, and a released
    R2 still in its slot rejoins."""
    with tempfile.TemporaryDirectory() as td:
        path = _row_board(td, [('R1', 10, 10), ('R2', 11.65, 10),
                               ('R3', 13.3, 10)])
        st = _qstate(path)
        row = ['R1', 'R2', 'R3']
        assert q._clause_failing(st, 'R2') == 'legality', \
            "the siblings do not violate the clearance: the arm is vacuous"
        assert q._release_clause(st, 'R2', set(row), None, 3.0, 1.0,
                                 0.1) is None
        name = 'array:r'
        info = {'groups': {name: list(row)}, 'anchored': {}}
        held = {'R1': name, 'R3': name}
        blocks = {name: ['R1', 'R3']}
        released = [{'ref': 'R2', 'group': name, 'clause': 'legality',
                     'pass': 1, '_anchor': 'R1',
                     '_slot': q._slot_of(st, 'R2', 'R1')}]
        rejoined = []
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            q._update_releases(st, held, blocks, info, released, rejoined,
                               3, 3.0, 1.0, 0.1)
    assert [r['ref'] for r in rejoined] == ['R2'], rejoined
    print("  PASS: a 0.05mm formation neither releases R2 nor keeps it from "
          "rejoining")


def test_a_release_in_a_pass_that_moved_nothing_still_gets_its_pass():
    """The pass loop stops at `moves == 0` only when nothing was released
    or rejoined either: R1 (locked) anchors the row, so R2 is held and the
    first pass moves NOTHING -- and releases R2, which X1 (locked) sits on.
    The next pass must run, or the released R2 never moves off X1."""
    with tempfile.TemporaryDirectory() as td:
        path = _row_board(td, [('R1', 10, 10), ('R2', 12, 10),
                               ('X1', 12.3, 10)])
        m = {}
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            pl = q.quench(parse_kicad_pcb(path), path, clearance=0.2,
                          board_edge_clearance=0.1, metrics_out=m,
                          intent_gate={'rigid_blocks': {
                              'array:r': ['R1', 'R2']}},
                          lock_refs=['R1', 'X1'], max_displacement=3.0)
        st = _qstate(path)
    rel = [r['ref'] for r in m['rigid_released']]
    assert rel == ['R2'], m['rigid_released']
    assert m['rigid_released'][0]['pass'] == 1, m['rigid_released']
    got = {p['reference']: p for p in pl}
    assert 'R2' in got, "R2 never moved"
    st.apply_move('R2', got['R2']['new_x'], got['R2']['new_y'],
                  got['R2']['new_rotation'])
    assert q._clause_failing(st, 'R2', exclude={'R1', 'R2'}) is None, got
    print(f"  PASS: released after an empty pass 1, R2 then moves off X1 to "
          f"({got['R2']['new_x']:.2f}, {got['R2']['new_y']:.2f})")


# --------------------------------------------------------------------------
# quench: tethers (the fixtures of test_1051_quench_blocks)
# --------------------------------------------------------------------------

def _qb():
    import test_1051_quench_blocks as qb
    return qb


def test_a_swap_is_checked_on_both_halves_in_either_order():
    """A swap's tether check reads the terms of BOTH refs, whichever is
    named first: a pair whose exchange strands only ONE of them is refused
    with that one named first and with it named second."""
    from collections import defaultdict
    qb = _qb()
    board = qb._glasgow_unlocked()
    pcb = parse_kicad_pcb(board)
    _p, _i, _g, st = qb._glasgow_state(fp.load_intent(qb.RUN32_INTENT), pcb,
                                       board)
    by_fp = defaultdict(set)
    for t in st._tether_terms:
        if t.rule == 'decap_distance' and t.data['graded']:
            by_fp[st.parts[t.data['cap']].footprint_name].add(t.data['cap'])
    found = None
    for _fpn, caps in sorted(by_fp.items()):
        caps = sorted(caps)
        for i, ra in enumerate(caps):
            for rb in caps[i + 1:]:
                pa, pb = st.parts[ra], st.parts[rb]
                fails = st.tether_failures({ra: (pb.x, pb.y, pb.rot),
                                            rb: (pa.x, pa.y, pa.rot)})
                if not fails:
                    continue
                own = [set(st._tethers_of.get(r, ())) for r in (ra, rb)]
                idx = {i for i, t in enumerate(st._tether_terms)
                       if t.name in {f[1] for f in fails}}
                if idx <= own[0] and not idx & own[1]:
                    found = (ra, rb)
                elif idx <= own[1] and not idx & own[0]:
                    found = (rb, ra)
                if found:
                    break
            if found:
                break
        if found:
            break
    assert found, "no swap that strands exactly one of its two caps"
    stranded, other = found
    assert st.swap_intent_ok(stranded, other) is False
    assert st.swap_intent_ok(other, stranded) is False
    print(f"  PASS: {stranded} <-> {other} strands {stranded} only; refused "
          f"in both orders")


def test_a_term_already_past_its_limit_may_improve():
    """MONOTONE per claim: a cap already past its limit (glasgow, run-32
    limits) may move CLOSER to its IC while still past it -- the gate
    refuses a worse reading, never a better one."""
    from placement import groups as _groups
    qb = _qb()
    _p, _i, _g, st = qb._glasgow_state()
    case = None
    for i, t in enumerate(st._tether_terms):
        if t.rule != 'decap_distance' or not t.data['graded']:
            continue
        cap, ic = t.data['cap'], t.data['ic']
        if cap not in st.parts or st.parts[cap].locked:
            continue
        u = st._incumbent_tether(i)
        if u <= t.threshold + 0.5:
            continue
        cx, cy = _groups._centroid(st._posed_fp(cap))
        b = st._chip_bounds(ic)
        mx, my = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        d = ((mx - cx) ** 2 + (my - cy) ** 2) ** 0.5
        p = st.parts[cap]
        pose = (p.x + 0.2 * (mx - cx) / d, p.y + 0.2 * (my - cy) / d, p.rot)
        v = st._tether_value(i, {cap: pose})
        if t.threshold < v < u:
            case = (i, t, cap, pose, u, v)
            break
    assert case, "no graded decap term past its limit with a movable cap"
    i, t, cap, pose, u, v = case
    fails = st.tether_failures({cap: pose})
    assert t.name not in {f[1] for f in fails}, fails
    print(f"  PASS: {t.name} past its limit ({u:.2f} > {t.threshold}) may "
          f"move to {v:.2f}")


def test_a_tethers_only_quench_gates_swaps_and_drops_icless_clusters():
    """An intent declaring ONLY a tether (no zone, no keep-out): the swap
    phase still asks `swap_intent_ok` -- the tether conjunct is the only
    tether check a swap gets -- and a cluster whose IC cannot move (locked)
    is not formed at all: caps translating without their IC would move OFF
    it. watchy, decaps armed, one owning IC locked, one pass."""
    from placement.groups import derive_groups
    qb = _qb()
    board = os.path.join(BOARDS, 'watchy.kicad_pcb')
    pcb = parse_kicad_pcb(board)
    d = fp.emit_intent(pcb, board)
    d['decaps'] = dict(d.get('decaps') or {}, max_distance_mm=2.5)
    gate = qb._gate(fp.intent_from_dict(d), pcb)
    tg = {'tethers': gate['tethers']}
    groups = derive_groups(pcb, ('decap',))
    ic = next(n.split(':', 1)[1] for n, refs in sorted(groups.items())
              if n.split(':', 1)[1] in refs and len(refs) >= 3
              and not pcb.footprints[n.split(':', 1)[1]].locked)
    capsonly = derive_groups(pcb, ('decap',),
                             movable=set(pcb.footprints) - {ic})
    assert any(n.split(':', 1)[1] == ic and ic not in refs
               for n, refs in capsonly.items()), \
        "no caps-only cluster for a locked IC: the arm is vacuous"
    calls = []
    real = q.QuenchState.swap_intent_ok

    def spy(self, ra, rb):
        calls.append((ra, rb))
        return real(self, ra, rb)
    q.QuenchState.swap_intent_ok = spy
    m = {}
    try:
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            q.quench(parse_kicad_pcb(board), board, clearance=qb.CLEARANCE,
                     board_edge_clearance=qb.EDGE, metrics_out=m,
                     intent_gate=tg, lock_refs=[ic], max_passes=1)
    finally:
        q.QuenchState.swap_intent_ok = real
    assert calls, "the swap phase never asked the tether gate"
    cl = m['tethers']['clusters']
    assert cl, m['tethers']
    assert f"tether:{ic}" not in cl, cl
    # Not formed, rather than formed and then dropped: `clusters_dropped` is
    # the disclosure of a cluster a RIGID group took the IC of, and there is
    # no rigid group here.
    assert m['tethers']['clusters_dropped'] == [], m['tethers']
    assert all(n.split(':', 1)[1] in r for n, r in cl.items()), cl
    print(f"  PASS: {len(calls)} swap(s) asked the tether gate; {ic} locked, "
          f"so no tether:{ic} cluster ({len(cl)} kept)")


TESTS = [
    test_pin_order_ignores_shared_nets_and_keys_on_the_lowest_pad,
    test_a_ball_grid_host_suggests_no_order,
    test_a_row_naming_a_missing_ref_is_skipped_as_absent,
    test_place_seed_judges_rows_and_fixed_poses_at_the_written_board,
    test_a_partly_locked_rigid_group_is_anchored_not_translated,
    test_row_offsets_line_up_courtyard_centres_not_origins,
    test_a_shuffled_declaration_is_seated_in_pin_order,
    test_the_seeded_verdict_is_the_self_checks,
    test_a_two_pad_row_tries_each_angle_once_modulo_180,
    test_the_row_runs_parallel_to_the_side_its_pins_are_on,
    test_a_refused_row_is_put_back_where_it_was,
    test_a_row_the_intent_check_refuses_or_with_a_placed_member_is_not_seated,
    test_declared_poses_whose_courtyards_clear_but_pads_collide_are_refused,
    test_a_seated_fixed_pose_is_frozen_to_the_eviction_rung,
    test_a_copperless_fixed_pose_past_the_outline_is_refused,
    test_a_member_a_block_move_can_clear_is_not_released,
    test_a_released_member_that_moved_away_clean_stays_released,
    test_a_formation_tighter_than_the_clearance_is_its_own_business,
    test_a_release_in_a_pass_that_moved_nothing_still_gets_its_pass,
    test_a_swap_is_checked_on_both_halves_in_either_order,
    test_a_term_already_past_its_limit_may_improve,
    test_a_tethers_only_quench_gates_swaps_and_drops_icless_clusters,
    test_a_declared_row_of_caps_is_not_the_decap_stages_to_claim,
    test_a_zoned_row_that_seats_nowhere_is_not_zone_packed_again,
]


if __name__ == '__main__':
    only = sys.argv[1:]
    for t in TESTS:
        if only and not any(o in t.__name__ for o in only):
            continue
        print(f"--- {t.__name__}")
        t()
    print('ALL PASS')
