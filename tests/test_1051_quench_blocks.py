#!/usr/bin/env python3
"""#1051 / #1052 / #1043 phase 4: the QUENCH holds declared rows and rigid
blocks together, and holds decap / proximity tethers.

What each case pins, and why:

* a row the seeder formed stays formed through `place_seed`'s polish
  (splitflap, the detector's four rows): the GRADER's `array_formation` on
  the written board, read off JSON_SUMMARY. The control quenches the same
  --no-polish seed with the gate's `rigid_blocks` emptied, and breaks a row --
  which is what the polish did before this phase.
* a member leaves its row ONLY through a disclosed release: a locked part
  dropped onto one member, at a displacement cap no block offset can clear,
  releases exactly that member (`rigid_released`, clause `legality`), and
  every other member keeps its offset from its siblings.
* `rigid: true` blocks move as one: the block translates (`moved_as_block`
  > 0) and its SHAPE -- the multiset of member poses relative to its corner
  -- is unchanged (a same-footprint swap inside a rigid block is allowed and
  keeps it); the control without `rigid` pulls the same parts apart.
* a ref in two groups is deduped and disclosed (`groups_deduped`), kept by
  its rigid group.
* an IC move that would strand its caps is REFUSED on the tether (the refusal
  tallied under `decap_distance`), while the IC+caps cluster can take the
  same offset.
* the tether terms equal the GRADER's reading on every tethered pair of a
  real board (glasgow_revC under the run-32 intent, plus three proximity
  claims in both arities and both bases), at the board's own poses and at
  perturbed ones.
* every caller receives it: place_optimize (JSON_SUMMARY `rigid`, `tethers`)
  and place_portfolio are RUN on a rows+decaps intent; place_route_loop's
  quench call is read in the source (it quenches only after a route), and
  must pass the gate `resolve_intent_gate_for_cli` built.
* bit-identity: with no array, no `rigid` block and no tether declared, the
  quench's placements and metrics equal those recorded at 8bcce3fba
  (`fixtures/1051/quench_unarmed_baseline.json`, from
  `fixtures/1051/record_quench_unarmed.py`; the esp_prog case re-recorded
  for #1266, whose pad gate sees U2's drawn tab).

    python3 tests/test_1051_quench_blocks.py [name-filter ...]
"""
import copy
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS_DIR)

from kicad_parser import parse_kicad_pcb           # noqa: E402
from placement import floorplan as fp              # noqa: E402
from placement import quench as q                  # noqa: E402
from placement.groups import derive_groups         # noqa: E402
from placement.portfolio import copy_siblings      # noqa: E402
from placement.writer import write_placed_output   # noqa: E402

RUN_ALL_TIMEOUT = 1200

BOARDS = os.path.join(ROOT, 'kicad_files')
SPLITFLAP = os.path.join(BOARDS, 'splitflap_driver.kicad_pcb')
GLASGOW = os.path.join(BOARDS, 'glasgow_revC.kicad_pcb')
FIX = os.path.join(TESTS_DIR, 'fixtures', '1051')
RUN32_INTENT = os.path.join(FIX, 'glasgow_run32.intent.json')
UNARMED = os.path.join(FIX, 'quench_unarmed_baseline.json')
PLACE_SEED = os.path.join(ROOT, 'py_placer', 'place_seed.py')
CLEARANCE = 0.2
EDGE = 0.5
#: place_seed's polish knobs (place_seed.py, the `quench(` call).
POLISH = dict(step=1.0, grid_step=0.1, clearance=CLEARANCE,
              board_edge_clearance=EDGE, crossing_penalty=30.0,
              length_weight=0.3, halo_base=0.5, halo_coef=0.15,
              halo_weight=2.0, edge_halo=2.0, edge_weight=2.0,
              max_displacement=3.0)
_CACHE = {}


def _quiet(fn, *a, **kw):
    with redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def _summary(stdout):
    m = re.search(r'^JSON_SUMMARY: (.*)$', stdout, re.M)
    assert m, stdout[-1500:]
    return json.loads(m.group(1))


def _run_seed(args):
    r = subprocess.run([sys.executable, '-X', 'utf8', PLACE_SEED] + args,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=ROOT, timeout=900)
    out = r.stdout + r.stderr
    assert 'Traceback' not in out, out[-2000:]
    # 0, or 4 for a grade finding (splitflap's seed carries a courtyard
    # overlap budget error with or without this phase) -- never a crash.
    assert r.returncode in (0, 4), (r.returncode, out[-2000:])
    return r


def _workdir():
    if 'td' not in _CACHE:
        _CACHE['tdo'] = tempfile.TemporaryDirectory()
        _CACHE['td'] = _CACHE['tdo'].name
    return _CACHE['td']


def _splitflap_intent():
    """The detector's rows on splitflap, as `emit_intent` writes them."""
    if 'sf_intent' not in _CACHE:
        td = _workdir()
        doc = fp.emit_intent(parse_kicad_pcb(SPLITFLAP), SPLITFLAP,
                             derive_arrays='auto')
        names = [a['name'] for a in doc['arrays']]
        assert 'U4:47k' in names, names
        path = os.path.join(td, 'sf.intent.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh, indent=1)
        _CACHE['sf_intent'] = (doc, path)
    return _CACHE['sf_intent']


#: The place_seed `--seed` of the shared splitflap fixture. The release
#: and rigid-block arms read the layout it seeds (a dropped intruder must be
#: unable to clear a member from its own side at 1mm and able to at 3mm; a
#: rigid block must have somewhere to go), so the layout is a fixture choice:
#: 0 was the seed until #1051 Phase 6 made stage 2.4 opt-in and moved the
#: rows; seed 2 is the first whose layout still makes those arms live
#: (measured over 0-3).
SF_SEED = '2'


def _seeded_no_polish(seed=None):
    """splitflap seeded from the rows intent, WITHOUT the polish: the board
    the polish would start from (place_seed `--seed`)."""
    seed = SF_SEED if seed is None else seed
    key = f'sf_seed_{seed}'
    if key not in _CACHE:
        td = _workdir()
        _doc, ipath = _splitflap_intent()
        out = os.path.join(td, f'sf_seed_{seed}.kicad_pcb')
        s = _summary(_run_seed([SPLITFLAP, out, '--intent', ipath, '--force',
                                '--no-polish', '--clearance',
                                str(CLEARANCE), '--seed', str(seed)]).stdout)
        formed = {n for n, r in s['arrays_formed'].items()
                  if r['verdict'] == 'formed'}
        assert 'U4:47k' in formed, s['arrays_formed']
        _CACHE[key] = (out, formed)
    return _CACHE[key]


def _gate(intent, pcb):
    gate, _p = fp.resolve_intent_gate(intent, pcb, ('kicad', 'sheet'))
    return gate


def _offsets(pcb, refs):
    """Each member's (dx, dy, rot) from the first member: a rigid translate
    keeps every entry exactly."""
    f0 = pcb.footprints[refs[0]]
    return {r: (round(pcb.footprints[r].x - f0.x, 6),
                round(pcb.footprints[r].y - f0.y, 6),
                round((pcb.footprints[r].rotation or 0.0) % 360, 6))
            for r in refs}


def _shape(pcb, refs):
    """The block's occupied poses relative to its own corner, as a sorted
    multiset: what a rigid translate keeps, and what a same-footprint swap
    INSIDE the block keeps too (it exchanges which member sits where)."""
    x0 = min(pcb.footprints[r].x for r in refs)
    y0 = min(pcb.footprints[r].y for r in refs)
    return sorted((round(pcb.footprints[r].x - x0, 6),
                   round(pcb.footprints[r].y - y0, 6),
                   round((pcb.footprints[r].rotation or 0.0) % 360, 6))
                  for r in refs)


def _quench_to(board, gate, out, **kw):
    pcb = parse_kicad_pcb(board)
    m = {}
    args = dict(POLISH)
    args.update(kw)
    placements = _quiet(q.quench, pcb, board, metrics_out=m,
                        intent_gate=gate, **args)
    write_placed_output(board, out, placements)
    copy_siblings(board, out)
    return m, parse_kicad_pcb(out)


def _seed_polished_with_rows():
    """The SF_SEED board polished with its rows intent's own gate, POLISH
    knobs: `(metrics_out, written pcb)`. Two cases read this one quench --
    the unformed-row case's seeded control and the cross-group swap case --
    and each used to run it with identical arguments. The quench is
    deterministic and both only read the result."""
    if 'sf_seed_q' not in _CACHE:
        _doc, ipath = _splitflap_intent()
        seed, _formed = _seeded_no_polish()
        intent = fp.load_intent(ipath)
        _CACHE['sf_seed_q'] = _quench_to(
            seed, _gate(intent, parse_kicad_pcb(seed)),
            os.path.join(_workdir(), 'sf_seed_q.kicad_pcb'))
    return _CACHE['sf_seed_q']


# --------------------------------------------------------------------------
# rows and rigid blocks
# --------------------------------------------------------------------------

def test_seeded_row_keeps_formation_through_the_polish():
    td = _workdir()
    doc, ipath = _splitflap_intent()
    # The polish must actually MOVE a row for "formed after the polish" to
    # mean anything, and whether it does depends on the seed and on every
    # seeder stage: the first seed (of 6) where a row translates is taken,
    # and the control below is seeded the same (seed 0 moved no row once
    # stage 2.4 went opt-in; seeds 1-5 all did).
    for seed in range(6):
        out = os.path.join(td, f'sf_polished_{seed}.kicad_pcb')
        s = _summary(_run_seed([SPLITFLAP, out, '--intent', ipath, '--force',
                                '--clearance', str(CLEARANCE),
                                '--seed', str(seed)]).stdout)
        if sum(s['rigid']['moved_as_block'].values()) > 0:
            break
    rows = s['arrays_formed']
    assert rows and all(r['verdict'] == 'formed' for r in rows.values()), \
        {n: (r['verdict'], r['failed']) for n, r in rows.items()}
    assert 'U4:47k' in rows
    rigid = s['rigid']
    assert set(rigid['groups']) == {f"array:{n}" for n in rows}, rigid
    assert s['rigid_released'] == [] and rigid['released'] == []
    assert sum(rigid['moved_as_block'].values()) > 0, (
        "no row ever translated, so 'formed after the polish' would hold for "
        "a quench that simply froze them", rigid)

    # Control: the SAME seed, polished with the rows withheld from the gate.
    seed_board, formed_at_seed = _seeded_no_polish(seed)
    intent = fp.load_intent(ipath)
    pcb = parse_kicad_pcb(seed_board)
    gate = _gate(intent, pcb)
    assert gate['rigid_blocks'], gate['rigid_blocks']
    ctl_gate = dict(gate, rigid_blocks={})
    ctl_out = os.path.join(td, 'sf_ctl.kicad_pcb')
    _m, _p = _quench_to(seed_board, ctl_gate, ctl_out)
    g = fp.grade(intent, parse_kicad_pcb(ctl_out), ctl_out,
                 group_sources=('kicad', 'sheet'), clearance=CLEARANCE)
    broken = sorted(a['name'] for a in g.array_measured
                    if a['name'] in formed_at_seed and a['formed'] is False)
    assert broken, ("control: without the rigid groups the polish kept every "
                    "row, so the arm above proves nothing")
    print(f"  PASS: {len(rows)} row(s) formed after the polish, "
          f"{sum(rigid['moved_as_block'].values())} block move(s); the "
          f"control polish breaks {', '.join(broken)}")


def test_a_refused_array_is_no_rigid_group():
    """An array the intent check refuses (`array_problems` at error) is not
    one the seeder seats as a row, so the gate must not hand it to the
    quench as a rigid group either (review: esp_prog `mixed: [R3, C4]`
    came out `array:mixed` alongside its own array_conflict)."""
    esp = os.path.join(BOARDS, 'esp_prog.kicad_pcb')
    pcb = parse_kicad_pcb(esp)
    doc = fp.emit_intent(pcb, esp)
    doc['arrays'] = [{'name': 'mixed', 'members': ['R3', 'C4']}]
    intent = fp.intent_from_dict(doc, esp)
    gate, probs = fp.resolve_intent_gate(intent, pcb, ('kicad', 'sheet'))
    assert any(v.rule == 'array_conflict' and v.block == 'mixed'
               for v in probs), [v.rule for v in probs]
    assert 'array:mixed' not in gate['rigid_blocks'], gate['rigid_blocks']
    # Control: the same members as a legal row ARE a rigid group.
    doc['arrays'] = [{'name': 'ok', 'members': ['R3', 'R4']}]
    gate2, probs2 = fp.resolve_intent_gate(fp.intent_from_dict(doc, esp),
                                           pcb, ('kicad', 'sheet'))
    assert not [v for v in probs2 if v.block == 'ok'], probs2
    assert gate2['rigid_blocks'].get('array:ok') == ['R3', 'R4'], gate2
    print("  PASS: array_conflict 'mixed' is no rigid group; the legal "
          "'ok' row is")


def test_an_unformed_row_is_not_held_rigid():
    """The quench holds a declared array rigid only while it is a FORMED row
    at the poses it starts from -- the rule that makes a row the seeder
    could NOT seat (`array_unseated`, members seated one by one) move as
    single parts in the polish, and a row a later edit broke likewise. On
    the HUMAN splitflap none of the detector's four rows is formed
    (arrays.py's measurement), so none is held, each disclosed in
    `rigid.unformed` with its failed checks; on the seeded board (the
    control) the same rows are formed and held."""
    td = _workdir()
    doc, ipath = _splitflap_intent()
    intent = fp.load_intent(ipath)
    pcb = parse_kicad_pcb(SPLITFLAP)
    gate = _gate(intent, pcb)
    names = {f"array:{a['name']}" for a in doc['arrays']}
    assert names <= set(gate['rigid_blocks']), gate['rigid_blocks']
    m, _p = _quench_to(SPLITFLAP, gate,
                       os.path.join(td, 'sf_human_q.kicad_pcb'))
    rigid = m['rigid']
    held = {n for n in rigid['groups'] if n.startswith('array:')}
    assert not held, rigid['groups']
    assert set(rigid['unformed']) == names, rigid['unformed']
    assert all(rigid['unformed'][n] for n in names), rigid['unformed']
    m2, _p2 = _seed_polished_with_rows()
    assert names <= set(m2['rigid']['groups']), m2['rigid']
    assert m2['rigid']['unformed'] == {}, m2['rigid']['unformed']
    print(f"  PASS: {len(names)} unformed rows on the human board are not "
          f"held ({rigid['unformed']}); seeded, all are")


def test_portfolio_candidates_disclose_rigid_groups_and_tethers():
    """Review item 4: place_portfolio's quench ran with the rows and the
    tethers held and disclosed neither. Each candidate's `metrics` now
    carries `quench.disclosure`'s keys when the intent declares them:
    the rigid groups (the rows, formed on the seeded board), the released
    members, and the tether terms (decaps.max_distance_mm)."""
    td = _workdir()
    doc, _ip = _splitflap_intent()
    doc = dict(doc, decaps=dict(doc.get('decaps') or {},
                                max_distance_mm=3.0))
    ipath = os.path.join(td, 'pf_intent.json')
    with open(ipath, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    seed_board, _formed = _seeded_no_polish()
    out = os.path.join(td, 'pf')
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'py_placer', 'place_portfolio.py'),
                        seed_board, '--out-dir', out, '--seed', '0',
                        '--candidates', '2', '--keep', '1', '--route-top',
                        '0', '--intent', ipath, '--clearance',
                        str(CLEARANCE)], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', cwd=ROOT,
                       timeout=900)
    assert 'Traceback' not in r.stdout + r.stderr, (r.stdout + r.stderr)[-1500:]
    with open(os.path.join(out, 'portfolio.json'), encoding='utf-8') as fh:
        pf = json.load(fh)
    rows = {f"array:{a['name']}" for a in doc['arrays']}
    for c in pf['candidates']:
        m = c['metrics']
        for k in ('rigid', 'rigid_released', 'tethers'):
            assert k in m, (c['index'], k, sorted(m))
        assert rows <= set(m['rigid']['groups']) | set(
            m['rigid']['unformed']), m['rigid']
        assert 'decap_distance' in m['tethers']['armed'], m['tethers']
    print(f"  PASS: {len(pf['candidates'])} portfolio candidate(s) disclose "
          f"rigid / rigid_released / tethers")


def test_route_loop_summary_carries_each_rounds_quench_disclosure():
    """place_route_loop's JSON_SUMMARY gains `quench_disclosure` -- one entry
    per round whose quench held rows, rigid blocks or tethers -- and stays
    exactly as before when no round did. Unit level, through the two
    helpers `main` calls, because the loop itself routes."""
    sys.path.insert(0, os.path.join(ROOT, 'py_placer'))
    import place_route_loop as prl
    sink = []
    prl.record_quench_disclosure(sink, {'after': {'crossings': 3}}, 1)
    assert sink == [], sink                       # nothing to disclose
    m = {'rigid': {'groups': {'array:x': ['R1', 'R2']}, 'unformed': {}},
         'rigid_released': [], 'tethers': {'armed': ['decap_distance']},
         'after': {'crossings': 3}}
    prl.record_quench_disclosure(sink, m, 2)
    assert sink == [{'rigid': m['rigid'], 'rigid_released': [],
                     'tethers': m['tethers'], 'round': 2}], sink
    summary = {'rounds': 2}
    prl.add_quench_disclosure(summary, sink)
    assert summary['quench_disclosure'] == sink, summary
    bare = {'rounds': 2}
    prl.add_quench_disclosure(bare, [])
    assert bare == {'rounds': 2}, bare
    print("  PASS: the route loop records each round's disclosure and adds "
          "`quench_disclosure` only when a round had one")


def test_member_leaves_only_through_a_disclosed_release():
    td = _workdir()
    doc, ipath = _splitflap_intent()
    seed, _formed = _seeded_no_polish()
    pcb = parse_kicad_pcb(seed)
    row = next(a for a in doc['arrays'] if a['name'] == 'U4:47k')
    members = list(row['members'])
    victim = members[len(members) // 2]
    # A part outside the row, dropped ONTO the victim and locked: the
    # victim's incumbent pose now fails legality, and at a 1mm cap no block
    # offset clears a full overlap of two 0402-class parts.
    intent = fp.load_intent(ipath)
    held = {r for a in doc['arrays'] for r in a['members']}
    intruder = next(r for r in sorted(pcb.footprints)
                    if r.startswith('C') and r not in held)
    v = pcb.footprints[victim]
    bad = os.path.join(td, 'sf_release.kicad_pcb')
    write_placed_output(seed, bad, [{'reference': intruder, 'new_x': v.x,
                                     'new_y': v.y,
                                     'new_rotation': v.rotation or 0.0}])
    copy_siblings(seed, bad)
    before = _offsets(parse_kicad_pcb(bad), members)
    gate = _gate(intent, parse_kicad_pcb(bad))
    out = os.path.join(td, 'sf_release_out.kicad_pcb')
    m, after_pcb = _quench_to(bad, gate, out, max_displacement=1.0,
                              lock_refs=[intruder])
    rel = m['rigid_released']
    assert [{k: r[k] for k in ('ref', 'group', 'clause')} for r in rel] == [
        {'ref': victim, 'group': 'array:U4:47k', 'clause': 'legality'}], rel
    # Released at the END of pass 1, after every other part had its turn.
    assert rel[0]['pass'] == 1, rel
    assert m['rigid']['released'] == rel
    after = _offsets(after_pcb, [r for r in members if r != victim])
    before_rest = _offsets(parse_kicad_pcb(bad),
                           [r for r in members if r != victim])
    assert after == before_rest, (after, before_rest)
    # And the control: nothing dropped on the row, nothing released, every
    # member (the victim included) keeps its place in the row.
    ctl = os.path.join(td, 'sf_release_ctl.kicad_pcb')
    m2, ctl_pcb = _quench_to(seed, _gate(intent, pcb), ctl,
                             max_displacement=1.0)
    assert m2['rigid_released'] == [], m2['rigid_released']
    assert _offsets(ctl_pcb, members) == before, "control row sheared"
    # The intruder UNLOCKED, with room to move: it can clear the clash from
    # its own side, so the row keeps its member (the verifier's case -- a
    # first-sight release broke R6 out for good here). The reach is 5 mm, not
    # the polish's 3: on the seed of the MST wiring target the intruder's
    # nearest legal pose is 3.61 mm off, and within 3 mm it has none, which
    # is the locked case above.
    free = os.path.join(td, 'sf_release_free.kicad_pcb')
    m3, free_pcb = _quench_to(bad, gate, free, max_displacement=5.0)
    assert m3['rigid_released'] == [], m3['rigid_released']
    assert _offsets(free_pcb, members) == before, "row sheared"
    moved = math.hypot(free_pcb.footprints[intruder].x - v.x,
                       free_pcb.footprints[intruder].y - v.y)
    assert moved > 0.5, "the intruder did not move off, so nothing was tested"
    print(f"  PASS: {intruder} locked on {victim} releases {victim} alone "
          f"after pass 1 (legality); the other {len(members) - 1} keep their "
          f"offsets; unlocked, {intruder} moves {moved:.2f}mm off and nobody "
          f"is released; the control releases nobody")


def test_a_released_member_rejoins_when_clean_and_in_its_slot():
    """Release is not a verdict for the rest of the run: a released member
    that is clean again and still in its row slot rejoins; one that moved
    away stays released."""
    doc, _ip = _splitflap_intent()
    seed, _formed = _seeded_no_polish()
    pcb = parse_kicad_pcb(seed)
    row = next(a['members'] for a in doc['arrays'] if a['name'] == 'U4:47k')
    st = _quiet(q.QuenchState, pcb, seed, CLEARANCE, EDGE, 30.0, 0.5, 0.15,
                2.0, 2.0, 2.0, 0.1, 0.3)
    st.build_neighbor_lists(3.1)
    name = 'array:U4:47k'
    info = {'groups': {name: list(row)}, 'anchored': {}}
    victim, anchor, other = row[3], row[0], row[5]
    held = {r: name for r in row if r not in (victim, other)}
    blocks = {name: [r for r in row if r not in (victim, other)]}
    recs = [{'ref': r, 'group': name, 'clause': 'legality', 'pass': 1,
             '_anchor': anchor, '_slot': q._slot_of(st, r, anchor)}
            for r in (victim, other)]
    released, rejoined = list(recs), []
    p = st.parts[other]
    # 2mm ACROSS the row (along it, it would land on a sibling and release
    # that one too): the row's axis is read off the fixture, not assumed.
    xs = [st.parts[r].x for r in row]
    ys = [st.parts[r].y for r in row]
    across_y = (max(xs) - min(xs)) >= (max(ys) - min(ys))
    st.apply_move(other, p.x + (0.0 if across_y else 2.0),
                  p.y + (2.0 if across_y else 0.0), p.rot)  # it moved alone
    # Hysteresis: not in the pass after the release (2), from pass 3 on.
    assert not _quiet(q._update_releases, st, held, blocks, info, released,
                      rejoined, 2, 3.0, 1.0, 0.1) and not rejoined
    changed = _quiet(q._update_releases, st, held, blocks, info, released,
                     rejoined, 3, 3.0, 1.0, 0.1)
    assert changed and [r['ref'] for r in rejoined] == [victim], rejoined
    assert [r['ref'] for r in released] == [other], released
    assert held[victim] == name and victim in blocks[name]
    assert blocks[name] == [r for r in row if r != other], blocks
    print(f"  PASS: {victim} (clean, in its slot) rejoins {name}; {other} "
          f"(moved 2mm alone) stays released")


def test_a_rejoin_re_forms_a_two_member_row_with_its_sibling():
    """A release that leaves ONE member held drops the block (one part is no
    formation to translate); when the released member rejoins, the block is
    rebuilt from every member still held. It used to be rebuilt from the
    rejoining member alone: that member was then translated by itself, out
    of the row it had just rejoined, and the sibling that stayed held was in
    no block and never moved again."""
    doc, _ip = _splitflap_intent()
    seed, _formed = _seeded_no_polish()
    pcb = parse_kicad_pcb(seed)
    row = next(a['members'] for a in doc['arrays'] if a['name'] == 'U4:47k')
    st = _quiet(q.QuenchState, pcb, seed, CLEARANCE, EDGE, 30.0, 0.5, 0.15,
                2.0, 2.0, 2.0, 0.1, 0.3)
    st.build_neighbor_lists(3.1)
    name = 'array:U4:47k'
    a, b, intruder = row[2], row[3], row[5]
    info = {'groups': {name: [a, b]}, 'anchored': {}}
    held = {a: name, b: name}
    blocks = {name: [a, b]}
    released, rejoined = [], []
    assert q._clause_failing(st, a, exclude={a, b}) is None, "A starts dirty"
    home = st.parts[intruder]
    home = (home.x, home.y, home.rot)
    pa = st.parts[a]
    st.apply_move(intruder, pa.x, pa.y, pa.rot)     # dropped onto A
    assert _quiet(q._update_releases, st, held, blocks, info, released,
                  rejoined, 2, 0.3, 1.0, 0.1)
    assert [r['ref'] for r in released] == [a], released
    assert held == {b: name} and name not in blocks, (held, blocks)
    st.apply_move(intruder, *home)                  # and taken off again
    assert not _quiet(q._update_releases, st, held, blocks, info, released,
                      rejoined, 3, 0.3, 1.0, 0.1)   # hysteresis
    assert _quiet(q._update_releases, st, held, blocks, info, released,
                  rejoined, 4, 0.3, 1.0, 0.1)
    assert [r['ref'] for r in rejoined] == [a], rejoined
    assert held == {a: name, b: name}, held
    assert blocks == {name: [a, b]}, blocks
    print(f"  PASS: {a} released from the 2-member row {a}+{b} drops the "
          f"block; on rejoin the block is [{a}, {b}] again, not [{a}]")


def test_release_and_rejoin_do_not_oscillate():
    """The re-verifier's osc.py: a released sibling that moved alone ONTO a
    member. Release and rejoin must judge that member with ONE exclusion set
    (the formation minus its released members): with the release counting the
    sibling and the rejoin ignoring it, the member flipped released ->
    rejoined -> released every pass, `changed` never went False (so the
    `moves == 0` stop never fired) and `rejoined` filled with duplicates.
    Here, with nothing moving between passes, only the first call changes
    anything and nobody rejoins."""
    doc, _ip = _splitflap_intent()
    seed, _formed = _seeded_no_polish()
    pcb = parse_kicad_pcb(seed)
    row = next(a['members'] for a in doc['arrays'] if a['name'] == 'U4:47k')
    name = 'array:U4:47k'
    for md in (0.3, 3.0):
        st = _quiet(q.QuenchState, pcb, seed, CLEARANCE, EDGE, 30.0, 0.5,
                    0.15, 2.0, 2.0, 2.0, 0.1, 0.3)
        st.build_neighbor_lists(3.1)
        info = {'groups': {name: list(row)}, 'anchored': {}}
        victim, anchor, other = row[3], row[0], row[5]
        held = {r: name for r in row if r != other}
        blocks = {name: [r for r in row if r != other]}
        released = [{'ref': other, 'group': name, 'clause': 'legality',
                     'pass': 1, '_anchor': anchor,
                     '_slot': q._slot_of(st, other, anchor)}]
        rejoined = []
        pv = st.parts[victim]
        st.apply_move(other, pv.x + 0.2, pv.y, pv.rot)  # onto the victim
        seen = []
        for pn in range(2, 8):
            ch = _quiet(q._update_releases, st, held, blocks, info, released,
                        rejoined, pn, md, 1.0, 0.1)
            seen.append((ch, tuple(sorted(r['ref'] for r in released))))
        assert seen[0][0] is True and victim in seen[0][1], seen[0]
        assert all(ch is False for ch, _r in seen[1:]), seen
        assert len({r for _c, r in seen}) == 1, seen
        assert rejoined == [], [r['ref'] for r in rejoined]
    print(f"  PASS: {other} moved onto {victim}: released once after pass 2, "
          f"then stable for 5 passes at both caps, nobody rejoins")


def _rigid_block_intent(doc, refs, rigid):
    d = copy.deepcopy(doc)
    d['arrays'] = []
    d['blocks'] = list(d.get('blocks') or []) + [
        dict({'name': 'drv_row', 'refs': list(refs)},
             **({'rigid': True} if rigid else {}))]
    return fp.intent_from_dict(d)


def test_rigid_true_block_moves_as_one():
    td = _workdir()
    doc, _ip = _splitflap_intent()
    seed0, _formed = _seeded_no_polish()
    refs = next(a['members'] for a in doc['arrays'] if a['name'] == 'U4:47k')
    # The row is shifted 2 mm off its seat, so the polish has a reason to
    # move it: the seeder can seat it where its wiring wants it (the MST
    # wiring target does), and then neither arm moves it and the test
    # proves nothing.
    p0 = parse_kicad_pcb(seed0)
    seed = os.path.join(td, 'rigid_seed_shifted.kicad_pcb')
    write_placed_output(seed0, seed, [
        {'reference': r, 'new_x': p0.footprints[r].x + 2.0,
         'new_y': p0.footprints[r].y,
         'new_rotation': p0.footprints[r].rotation or 0.0} for r in refs])
    copy_siblings(seed0, seed)
    pcb = parse_kicad_pcb(seed)
    before = _shape(pcb, refs)
    on = _rigid_block_intent(doc, refs, True)
    gate = _gate(on, pcb)
    assert set(gate['rigid_blocks']) == {'block:drv_row'} and \
        set(gate['rigid_blocks']['block:drv_row']) == set(refs), gate
    m, got = _quench_to(seed, gate, os.path.join(td, 'rigid_on.kicad_pcb'))
    assert m['rigid']['moved_as_block'].get('block:drv_row', 0) > 0, m['rigid']
    assert m['rigid']['released'] == [], m['rigid']['released']
    assert _shape(got, refs) == before, (_shape(got, refs), before)
    moved = math.hypot(
        min(got.footprints[r].x for r in refs)
        - min(pcb.footprints[r].x for r in refs),
        min(got.footprints[r].y for r in refs)
        - min(pcb.footprints[r].y for r in refs))
    off = _rigid_block_intent(doc, refs, False)
    _m, ctl = _quench_to(seed, _gate(off, pcb),
                         os.path.join(td, 'rigid_off.kicad_pcb'))
    assert _shape(ctl, refs) != before, \
        "control: the plain block kept its shape, so the arm proves nothing"
    print(f"  PASS: rigid block of {len(refs)} moved "
          f"{m['rigid']['moved_as_block']['block:drv_row']} time(s), "
          f"{moved:.2f}mm, shape exact; the plain block is pulled apart")


def test_a_ref_in_two_groups_is_deduped_and_disclosed():
    doc, ipath = _splitflap_intent()
    seed, _formed = _seeded_no_polish()
    intent = fp.load_intent(ipath)
    pcb = parse_kicad_pcb(seed)
    gate = _gate(intent, pcb)
    row = next(a['members'] for a in doc['arrays'] if a['name'] == 'U4:47k')
    caller = {'sheet:test': list(row[:3]) + ['U4']}
    m = {}
    _quiet(q.quench, pcb, seed, metrics_out=m, intent_gate=gate,
           groups=caller, max_passes=1, **POLISH)
    got = {d['ref']: d for d in m['groups_deduped']}
    assert set(got) == set(row[:3]), m['groups_deduped']
    for r in row[:3]:
        assert got[r]['kept'] == 'array:U4:47k', got[r]
        assert got[r]['dropped_from'] == ['sheet:test'], got[r]
    # The unit: claim order is rigid, then tether clusters, then the caller.
    blocks, info = q.merge_groups(
        {'caller': ['A', 'B', 'X']}, {'array:r': ['A', 'B', 'C']},
        {'tether:U1': ['U1', 'B', 'D']}, {'A', 'B', 'C', 'D', 'U1', 'X'},
        {r: None for r in 'ABCDX'} | {'U1': None})
    assert blocks == {'array:r': ['A', 'B', 'C'], 'tether:U1': ['U1', 'D']}, \
        blocks
    assert info['deduped'] == [
        {'ref': 'A', 'kept': 'array:r', 'dropped_from': ['caller']},
        {'ref': 'B', 'kept': 'array:r',
         'dropped_from': ['caller', 'tether:U1']}], info['deduped']
    assert info['held'] == {'A': 'array:r', 'B': 'array:r', 'C': 'array:r'}
    # A tether cluster whose IC an array claimed is dropped, not left as a
    # caps-only block that would translate the caps off their IC.
    blocks2, info2 = q.merge_groups(
        {}, {'array:r': ['U1', 'A']}, {'tether:U1': ['U1', 'C1', 'C2']},
        {'U1', 'A', 'C1', 'C2'}, {r: None for r in ('U1', 'A', 'C1', 'C2')})
    assert 'tether:U1' not in blocks2, blocks2
    assert info2['clusters_dropped'] == ['tether:U1'], info2
    print(f"  PASS: {len(got)} ref(s) deduped into array:U4:47k and "
          f"disclosed; merge order rigid > tether > caller")


# --------------------------------------------------------------------------
# tethers (#1043)
# --------------------------------------------------------------------------

def _glasgow_state(intent=None, pcb=None, board=GLASGOW):
    pcb = pcb or parse_kicad_pcb(board)
    intent = intent or fp.load_intent(RUN32_INTENT)
    gate = _gate(intent, pcb)
    st = _quiet(q.QuenchState, pcb, board, CLEARANCE, EDGE, 30.0, 0.5,
                0.15, 2.0, 2.0, 2.0, 0.1, 0.3, tethers=gate['tethers'])
    return pcb, intent, gate, st


def _glasgow_unlocked():
    """glasgow with every file lock removed: a term none of whose refs can
    move is dropped (nothing can change it), so the every-pair comparison
    needs every pair to have a mover."""
    if 'gl_unlocked' not in _CACHE:
        from placement import seeder
        out = os.path.join(_workdir(), 'glasgow_unlocked.kicad_pcb')
        write_placed_output(GLASGOW, out, [])
        copy_siblings(GLASGOW, out)
        seeder.stamp_unlocked(out, sorted(parse_kicad_pcb(GLASGOW).footprints))
        _CACHE['gl_unlocked'] = out
    return _CACHE['gl_unlocked']


def test_an_ic_that_would_strand_its_caps_is_refused_the_cluster_moves():
    pcb, intent, gate, st = _glasgow_state()
    assert st._tether_active
    st.build_neighbor_lists(5.1)
    clusters = derive_groups(pcb, ('decap',), movable=set(st.parts))
    found = None
    for name, refs in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        ic = name.split(':', 1)[1]
        if ic not in refs or len(refs) < 3:
            continue
        idx = [i for i in st._tethers_of.get(ic, ())
               if st._tether_terms[i].rule == 'decap_distance']
        if not idx or any(st._incumbent_tether(i)
                          > st._tether_terms[i].threshold for i in idx):
            continue
        p = st.parts[ic]
        for dx, dy in q._group_offsets(st, refs, 5.0, 1.0, 0.1):
            if not st.group_move_valid(refs, dx, dy):
                continue
            pose = (p.x + dx, p.y + dy, p.rot)
            st._tether_active = False
            legal_alone = st.candidate_valid(ic, *pose)
            st._tether_active = True
            if not legal_alone:
                continue
            fails = st.tether_failures({ic: pose})
            if any(f[0] == 'decap_distance' for f in fails):
                found = (ic, refs, dx, dy, pose, fails)
                break
        if found:
            break
    assert found, "no IC offset that is legal alone and strands a cap"
    ic, refs, dx, dy, pose, fails = found
    n0 = st.intent_rejected.get('decap_distance', 0)
    assert st.candidate_valid(ic, *pose) is False
    assert st.intent_rejected.get('decap_distance', 0) > n0, \
        "the refusal was not tallied under decap_distance"
    # The cluster takes the SAME offset, tethers intact.
    assert st.group_move_valid(refs, dx, dy)
    st._tether_override = {r: (st.parts[r].x + dx, st.parts[r].y + dy,
                               st.parts[r].rot) for r in refs}
    try:
        assert st.tether_failures({ic: pose}) == []
    finally:
        st._tether_override = None
    print(f"  PASS: {ic} alone at ({dx:+.2f},{dy:+.2f}) is refused on "
          f"{fails[0][0]} ({fails[0][1]} {fails[0][2]} > "
          f"{fails[0][3]}); its cluster of {len(refs)} takes the offset")


#: Three proximity claims on glasgow, one per shape the rule grades:
#: declared subject pads (net-matched, one term per pad), part adjacency
#: (one term for the pair), and the body basis.
PROX = [
    {'ref': 'Y1', 'near': 'U1', 'max_mm': 3.0, 'pads': {'Y1': ['1', '3']}},
    {'ref': 'U20', 'near': 'U30', 'max_mm': 5.0},
    {'ref': 'U20', 'near': 'U1', 'max_mm': 4.0, 'basis': 'body'},
]


#: A limit every measurement exceeds, so the grade reports a number for EVERY
#: pair -- overlapping pads (negative gaps) and caps inside the inflated bbox
#: (distance 0) included. The loader refuses a limit <= 0, so it is set on the
#: LOADED intent, which the rules read at grade time.
PROBE_LIMIT = -1e9


def _probe_intent():
    """The run-32 intent, the proximity claims, every limit at PROBE_LIMIT."""
    with open(RUN32_INTENT, encoding='utf-8') as fh:
        d = json.load(fh)
    d['proximity'] = [dict(c) for c in PROX]
    it = fp.intent_from_dict(d)
    it.decaps['max_distance_mm'] = PROBE_LIMIT
    it.decaps['max_pin_distance_mm'] = PROBE_LIMIT
    for c in it.proximity:
        c['max_mm'] = PROBE_LIMIT
    return it


def _grader_readings(intent, pcb, path):
    g = fp.grade(intent, pcb, path, group_sources=('kicad', 'sheet'),
                 clearance=CLEARANCE)
    dd = sorted((v.ref, v.measured['ic'], v.measured['distance_mm'])
                for v in g.violations if v.rule == 'decap_distance')
    dp = sorted((v.ref, v.measured['pad'], v.measured['net'],
                 v.measured['gap_mm'])
                for v in g.violations if v.rule == 'decap_pin_distance')
    px = sorted((m['claim'].split(':')[0] + ']', m['gap_mm'])
                for m in g.proximity_measured)
    return dd, dp, px


def _term_readings(st, limit):
    dd, dp, px = [], [], []
    for i, t in enumerate(st._tether_terms):
        v = st._tether_value(i)
        if t.rule == 'decap_distance':
            if t.data['graded'] and v > limit:
                dd.append((t.data['cap'], t.data['ic'], round(v, 4)))
        elif t.rule == 'decap_pin_distance':
            if v > limit:
                pad = st._posed_fp(t.data['ic']).pads[t.data['pad_index']]
                dp.append((t.data['ic'], pad.pad_number, pad.net_name,
                           round(v, 4)))
        else:
            px.append((t.name.split('#')[0], round(v, 4)))
    return sorted(dd), sorted(dp), sorted(px)


def test_tether_terms_equal_the_grader_on_every_pair():
    limit = PROBE_LIMIT
    board = _glasgow_unlocked()
    pcb = parse_kicad_pcb(board)
    intent = _probe_intent()
    _pcb, _i, gate, st = _glasgow_state(intent, pcb, board)
    assert not any(p.locked for p in st.parts.values() if p.pin_count),         "the unlocked copy still locks a connected part"
    assert set(gate['tethers']) == set(fp.TETHER_RULES), gate['tethers']
    want = _grader_readings(intent, pcb, board)
    got = _term_readings(st, limit)
    for name, w, g in zip(('decap_distance', 'decap_pin_distance',
                           'proximity'), want, got):
        assert w == g, (name, sorted(set(w) ^ set(g))[:6])
        assert w, f"{name}: nothing measured"
    # At the run-32 limits the terms past their limit ARE the grade's errors.
    pcb2, it2, _g2, st2 = _glasgow_state()
    over = q._tether_over_limit(st2)
    errs = fp.grade(it2, pcb2, GLASGOW, group_sources=('kicad', 'sheet'),
                    clearance=CLEARANCE).errors
    for rule in ('decap_distance', 'decap_pin_distance'):
        assert over.get(rule, 0) == sum(1 for v in errs if v.rule == rule), \
            (rule, over)

    # Perturbed poses: move ICs and caps IN THE STATE -- every cap elected
    # BEYOND the radius among them -- pose the board the way
    # `floorplan.PoseGrader` does, and ask the grader's own election on it.
    # Every decap term equals it: a pair graded at build reads the live
    # distance, one elected beyond the radius reads it once inside the
    # radius and 0 outside (the grade's `decap_ungraded`).
    import random
    from placement import groups as _groups
    rng = random.Random(1043)
    dterms = [t for t in st._tether_terms if t.rule == 'decap_distance']
    ungraded = sorted({t.data['cap'] for t in dterms if not t.data['graded']})
    assert ungraded, "no cap elected beyond the radius: the arm is vacuous"
    movers = sorted({t.data['cap'] for t in dterms}
                    | {t.data['ic'] for t in dterms})
    moved = sorted(set(rng.sample(movers, 25)) | set(ungraded))
    for ref in moved:
        p = st.parts[ref]
        st.apply_move(ref, p.x + rng.uniform(-3, 3), p.y + rng.uniform(-3, 3),
                      p.rot)
    # ...and one cap elected beyond the radius put INSIDE another chip's
    # radius (random moves of 3mm never did): the case where a frozen pair
    # and the live election disagree.
    placed_in = _walk_into_another_radius(st)
    assert placed_in, "no beyond-radius cap could be put in another radius"
    view = fp._PosedState(st).board()
    wdd, wdp, wpx = _grader_readings(intent, view, board)
    gdd, gdp, gpx = _term_readings(st, limit)
    assert wdp == gdp, sorted(set(wdp) ^ set(gdp))[:6]
    assert wpx == gpx, (wpx, gpx)
    radius = dterms[0].data['radius']
    live = {cap: d for cap, _ic, d in _groups._elect_tethers(view)}
    n_in = 0
    for i, t in enumerate(st._tether_terms):
        if t.rule != 'decap_distance':
            continue
        d = live[t.data['cap']]
        want_v = d if (t.data['graded'] or d <= radius + 1e-9) else 0.0
        n_in += (not t.data['graded']) and d <= radius
        assert abs(st._tether_value(i) - want_v) < 1e-9, (t.name, d, want_v)
    assert n_in >= 1, "no beyond-radius cap ended inside a radius"
    print(f"  PASS: {len(want[0])} cap->IC distances, {len(want[1])} supply "
          f"pins and {len(want[2])} proximity reaches equal the grade; after "
          f"{len(moved)}-part perturbation every decap "
          f"term ({len(dterms)}, {len(ungraded)} elected beyond the radius, "
          f"{n_in} of them now inside it) equals the grader's live election")


def _walk_into_another_radius(st):
    """Move the first cap elected beyond the radius to a pose inside the
    radius of a DIFFERENT chip on its rail (and beyond its elected IC's).
    Returns (cap, chip, live distance) or None."""
    from placement import groups as _groups
    for t in st._tether_terms:
        if t.rule != 'decap_distance' or t.data['graded']:
            continue
        cap, ic, rad = t.data['cap'], t.data['ic'], t.data['radius']
        cx, cy = _groups._centroid(st._posed_fp(cap))
        p = st.parts[cap]
        ib = st._chip_bounds(ic)
        rail = [(r, st._chip_bounds(r)) for r in t.data['rail']]
        for other in t.data['rail']:
            if other == ic:
                continue
            b = st._chip_bounds(other)
            mx, my = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
            for off in (1.0, 2.0, 3.0, 4.0):
                for tx, ty in ((b[2] + off, my), (b[0] - off, my),
                               (mx, b[3] + off), (mx, b[1] - off)):
                    w, d = _groups.nearest_chip((tx, ty), rail)
                    if (w != ic and d <= rad - 0.2 and
                            _groups._point_to_bounds((tx, ty), ib) > rad):
                        st.apply_move(cap, p.x + tx - cx, p.y + ty - cy,
                                      p.rot)
                        return cap, w, d
    return None


def test_a_cap_elected_beyond_the_radius_may_not_walk_into_another_ics():
    """#1043 verifier's C26 case, on a tracked board. Run 32: C26 was elected
    to U15 at 7.20mm (beyond the 5mm radius, so ungraded), the quench walked
    it to 3.07mm from U36, and the grade charged a NEW decap_distance error.
    Here: a glasgow cap elected beyond the radius is offered a pose 3.0mm
    from a DIFFERENT chip on its rail (limit 2.5): the gate refuses it, and
    the grader on that pose does charge the error."""
    from placement import groups as _groups
    board = _glasgow_unlocked()
    pcb = parse_kicad_pcb(board)
    intent = fp.load_intent(RUN32_INTENT)
    _p, _i, gate, st = _glasgow_state(intent, pcb, board)
    base = {(v.rule, v.ref) for v in fp.grade(
        intent, pcb, board, group_sources=('kicad', 'sheet'),
        clearance=CLEARANCE).errors}
    case = None
    for t in st._tether_terms:
        if t.rule != 'decap_distance' or t.data['graded']:
            continue
        cap, ic = t.data['cap'], t.data['ic']
        if ('decap_distance', cap) in base:
            continue
        cfp = st._posed_fp(cap)
        cx, cy = _groups._centroid(cfp)
        p = st.parts[cap]
        ib = st._chip_bounds(ic)
        rail = [(r, st._chip_bounds(r)) for r in t.data['rail']]
        lim, rad = t.threshold, t.data['radius']
        for other in t.data['rail']:
            if other == ic:
                continue
            b = st._chip_bounds(other)
            mx, my = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
            # A centroid off a side of the other chip's inflated bbox whose
            # LIVE election lands in (limit, radius] -- a new error -- while
            # the frozen IC is beyond the radius, so the frozen pair would
            # have read the pose as 0.
            for off in (3.0, 3.5, 4.0, 4.5):
                for tx, ty in ((b[2] + off, my), (b[0] - off, my),
                               (mx, b[3] + off), (mx, b[1] - off)):
                    _w, d = _groups.nearest_chip((tx, ty), rail)
                    if (lim + 0.2 < d <= rad - 0.2 and
                            _groups._point_to_bounds((tx, ty), ib) > rad):
                        case = (cap, ic, _w,
                                (p.x + tx - cx, p.y + ty - cy, p.rot))
                        break
                if case:
                    break
            if case:
                break
        if case:
            break
    assert case, "no ungraded cap with a second chip on its rail"
    cap, ic, other, pose = case
    fails = st.tether_failures({cap: pose})
    assert any(f[0] == 'decap_distance' for f in fails), fails
    st.apply_move(cap, *pose)
    view = fp._PosedState(st).board()
    after = fp.grade(intent, view, board, group_sources=('kicad', 'sheet'),
                     clearance=CLEARANCE).errors
    hit = [v for v in after if v.rule == 'decap_distance' and v.ref == cap]
    assert hit and hit[0].measured['ic'] == other, [
        (v.ref, v.measured) for v in hit]
    print(f"  PASS: {cap} (elected to {ic} beyond the radius) offered "
          f"{hit[0].measured['distance_mm']}mm from {other}: the gate refuses, "
          f"and the grade there charges the new error")


def test_a_swap_that_strands_a_cap_is_refused_on_the_tether():
    """Swaps skip `candidate_valid`, so `swap_intent_ok`'s tether conjunct is
    the ONLY tether check a swap gets. Two same-footprint glasgow caps, one
    within its limit of its IC and one far from it: exchanging them strands
    the first. The zone/keep-out halves admit the swap (the run-32 intent
    has no keep-out), so the refusal is the tether's."""
    from collections import defaultdict
    board = _glasgow_unlocked()
    pcb = parse_kicad_pcb(board)
    _p, _i, _g, st = _glasgow_state(fp.load_intent(RUN32_INTENT), pcb, board)
    by_fp = defaultdict(list)
    for t in st._tether_terms:
        if t.rule == 'decap_distance' and t.data['graded']:
            by_fp[st.parts[t.data['cap']].footprint_name].append(t)
    # A pair whose exchange strands one of them: on DIFFERENT rails, since
    # two caps on one rail trade ICs legitimately (the grade re-elects).
    found = None
    for fp_name, ts in sorted(by_fp.items()):
        caps = sorted({t.data['cap'] for t in ts})
        for i, ra in enumerate(caps):
            for rb in caps[i + 1:]:
                pa, pb = st.parts[ra], st.parts[rb]
                if st.tether_failures({ra: (pb.x, pb.y, pb.rot),
                                       rb: (pa.x, pa.y, pa.rot)}):
                    found = (ra, rb)
                    break
            if found:
                break
        if found:
            break
    assert found, "no same-footprint cap pair whose swap strands one"
    ra, rb = found
    pa, pb = st.parts[ra], st.parts[rb]
    assert st.intent_ok(ra, pb.x, pb.y, pb.rot) and         st.intent_ok(rb, pa.x, pa.y, pa.rot), "a zone term refused first"
    assert st.swap_intent_ok(ra, rb) is False
    fails = st.tether_failures({ra: (pb.x, pb.y, pb.rot),
                                rb: (pa.x, pa.y, pa.rot)})
    assert any(f[0] == 'decap_distance' for f in fails), fails
    print(f"  PASS: swapping {ra} <-> {rb} is refused on "
          f"{fails[0][1]} ({fails[0][2]}mm > limit)")


def test_the_cli_gate_is_not_called_inert_when_it_holds_tethers_or_rows():
    """`resolve_intent_gate_for_cli` prints "the gate is inert" only for an
    intent the quench cannot gate on at all. A tethers-only or rows-only
    intent is gated (the tether terms; the rigid rows), so it gets the
    summary line, naming the tether rules and the rigid groups."""
    from contextlib import redirect_stderr
    from placement.cli_gates import resolve_intent_gate_for_cli
    pcb = parse_kicad_pcb(SPLITFLAP)
    base = {'schema': 1, 'kind': fp.KIND, 'units': 'mm'}
    row = next(a for a in _splitflap_intent()[0]['arrays']
               if a['name'] == 'U4:47k')
    cases = (('nothing', dict(base), 'the gate is inert'),
             ('tethers', dict(base, decaps={'max_distance_mm': 3.0}),
              'tethers (decap_distance)'),
             ('rows', dict(base, arrays=[row], min_reader=7),
              '1 rigid group(s)'))
    for what, doc, want in cases:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()) as out:
            resolve_intent_gate_for_cli(fp.intent_from_dict(doc, SPLITFLAP),
                                        pcb, (), 'x.json')
        said = err.getvalue() + out.getvalue()
        assert want in said, (what, said)
        if what != 'nothing':
            assert 'the gate is inert' not in said, (what, said)
    print("  PASS: an empty intent is inert; tethers-only and rows-only "
          "intents are summarised with what they hold")


def test_rigid_swap_rule_stays_inside_one_group():
    """`_rigid_swap_ok` is the only thing between a held member and a swap
    with a part outside its group (the swap phase never calls the nudge's
    `held` check)."""
    held = {'A': 'array:r', 'B': 'array:r', 'C': 'array:s',
            'P': 'block:b', 'Q': 'block:b'}
    order = {'array:r': {'A'}, 'array:s': set()}
    ok = q._rigid_swap_ok
    assert ok(held, order, 'C', 'A') is False      # across two arrays
    assert ok(held, order, 'A', 'X') is False      # member with non-member
    assert ok(held, order, 'X', 'P') is False
    assert ok(held, order, 'P', 'C') is False      # block with array
    assert ok(held, order, 'P', 'Q') is True       # inside a rigid block
    assert ok(held, order, 'A', 'B') is False      # A has a row position
    held2 = dict(held, D='array:s')
    assert ok(held2, order, 'C', 'D') is True      # order unknown: may trade
    # And in the quench: two arrays of ONE footprint (splitflap's U9:220R and
    # U5:220R), side by side, never exchange members.
    doc, _ipath = _splitflap_intent()
    seed, _formed = _seeded_no_polish()
    rows = {a['name']: set(a['members']) for a in doc['arrays']}
    assert {'U9:220R', 'U5:220R'} <= set(rows)
    pcb = parse_kicad_pcb(seed)
    before = {r: (pcb.footprints[r].x, pcb.footprints[r].y)
              for r in rows['U9:220R'] | rows['U5:220R']}
    m, got = _seed_polished_with_rows()
    for name in ('U9:220R', 'U5:220R'):
        spots = {before[r] for r in rows[name]}
        dx = got.footprints[sorted(rows[name])[0]].x -             before[sorted(rows[name])[0]][0]
        dy = got.footprints[sorted(rows[name])[0]].y -             before[sorted(rows[name])[0]][1]
        now = {(round(got.footprints[r].x - dx, 6),
                round(got.footprints[r].y - dy, 6)) for r in rows[name]}
        assert now == {(round(x, 6), round(y, 6)) for x, y in spots}, name
    print(f"  PASS: cross-group swaps refused (unit); {m['rigid']['swaps_refused']}"
          f" rigid swap(s) refused in the polish, both 220R rows intact")


def test_the_tether_shortcuts_change_no_decision():
    """`_tether_value`'s bound shortcuts (a static cap or chip already within
    the limit ends the term) skip measurement, never a decision: a quench
    with them and one measuring every term exactly (`_exact_tethers`) take
    the same moves, refuse the same poses and end at the same cost. watchy,
    both decap rules armed (37 terms), two passes."""
    board = os.path.join(BOARDS, 'watchy.kicad_pcb')
    pcb = parse_kicad_pcb(board)
    d = fp.emit_intent(pcb, board)
    d['decaps'] = dict(d.get('decaps') or {}, max_distance_mm=2.5,
                       max_pin_distance_mm=2.5)
    gate = _gate(fp.intent_from_dict(d), pcb)
    got = {}
    try:
        for exact in (False, True):
            q.QuenchState._exact_tethers = exact
            m = {}
            pl = _quiet(q.quench, parse_kicad_pcb(board), board,
                        clearance=CLEARANCE, board_edge_clearance=EDGE,
                        metrics_out=m, intent_gate=gate, max_passes=2)
            got[exact] = (sorted((p['reference'], p['new_x'], p['new_y'],
                                  p['new_rotation']) for p in pl),
                          m['intent_gate'], m['after'], m['tethers'])
    finally:
        q.QuenchState._exact_tethers = False
    assert set(got[False][3]['terms']) == {'decap_distance',
                                           'decap_pin_distance'}, got[False][3]
    assert got[False][1]['rejected'] > 0, "the gate refused nothing"
    assert got[False] == got[True], "the shortcuts changed a decision"
    print(f"  PASS: {len(got[False][0])} moves, "
          f"{got[False][1]['rejected']} refusals, identical with and without "
          f"the shortcuts")


def _run(script, args):
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'py_placer', script)] + args,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=ROOT, timeout=900)
    out = r.stdout + r.stderr
    assert 'Traceback' not in out, out[-2000:]
    return r, out


def test_every_caller_hands_the_quench_the_resolved_rows_and_tethers():
    """The merge lives INSIDE `quench()`, fed from `intent_gate`, so a caller
    receives it exactly when it passes the gate `resolve_intent_gate_for_cli`
    built. Run: place_optimize and place_portfolio (the polish is the first
    test above). Read: place_route_loop, whose quench only runs after a
    route, so its call site is checked in the source instead."""
    import ast
    td = _workdir()
    doc, _ip = _splitflap_intent()
    seed, _formed = _seeded_no_polish()
    d = copy.deepcopy(doc)
    d['decaps'] = dict(d.get('decaps') or {}, max_distance_mm=3.0)
    ipath = os.path.join(td, 'sf_tethers.intent.json')
    with open(ipath, 'w', encoding='utf-8') as fh:
        json.dump(d, fh, indent=1)
    r, out = _run('place_optimize.py',
                  [seed, os.path.join(td, 'opt.kicad_pcb'), '--intent', ipath,
                   '--max-passes', '1', '--clearance', str(CLEARANCE)])
    s = _summary(r.stdout)
    assert set(s['rigid']['groups']) == {f"array:{a['name']}"
                                         for a in doc['arrays']}, s['rigid']
    assert 'decap_distance' in s['tethers']['armed'], s.get('tethers')
    assert 'decap_distance' in s['intent_rules_enforced']
    r, out = _run('place_portfolio.py',
                  [seed, '--out-dir', os.path.join(td, 'slate'),
                   '--candidates', '1', '--keep', '0', '--intent', ipath,
                   '--max-passes', '1', '--clearance', str(CLEARANCE),
                   '--route-top', '0'])
    assert 'Rigid groups (#1051/#1052): array:' in out, out[-2000:]
    assert 'Tethers (#1043):' in out, out[-2000:]
    # place_route_loop: the quench call passes `intent_gate=intent_gate`, and
    # that name is bound only from resolve_intent_gate_for_cli.
    src = open(os.path.join(ROOT, 'py_placer', 'place_route_loop.py'),
               encoding='utf-8').read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, 'id', None) == 'quench']
    assert calls and all(
        any(k.arg == 'intent_gate' and getattr(k.value, 'id', '') ==
            'intent_gate' for k in c.keywords) for c in calls), \
        "place_route_loop's quench call does not pass the resolved gate"
    binds = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Tuple) and any(
                 getattr(e, 'id', '') == 'intent_gate' for e in t.elts)
                 for t in n.targets)]
    assert binds and all(getattr(b.value.func, 'id', '') ==
                         'resolve_intent_gate_for_cli' for b in binds), binds
    print("  PASS: place_optimize and place_portfolio quench with the rows "
          "and the tether gate; place_route_loop passes the resolver's gate")


def test_unarmed_quench_is_bit_identical_to_the_pre_phase4_quench():
    with open(UNARMED, encoding='utf-8') as fh:
        base = json.load(fh)
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(FIX, 'record_quench_unarmed.py')],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=ROOT, timeout=900)
    assert r.returncode == 0, r.stderr[-2000:]
    now = json.loads(r.stdout)
    assert [c['board'] for c in now] == [c['board'] for c in base]
    for a, b in zip(base, now):
        assert a['placements'] == b['placements'], a['board']
        assert a['metrics'] == b['metrics'], (
            a['board'], sorted(set(a['metrics']) ^ set(b['metrics'])))
        for key in q.DISCLOSURE_KEYS:
            assert key not in b['metrics'], (a['board'], key)
    n = sum(len(c['placements']) for c in now)
    assert n > 0, "the recorded quench moved nothing, so identity is vacuous"
    print(f"  PASS: {len(now)} unarmed quench(es), {n} placement(s), "
          f"metrics and poses equal to the recorded quench's")


TESTS = [
    test_seeded_row_keeps_formation_through_the_polish,
    test_a_refused_array_is_no_rigid_group,
    test_an_unformed_row_is_not_held_rigid,
    test_portfolio_candidates_disclose_rigid_groups_and_tethers,
    test_route_loop_summary_carries_each_rounds_quench_disclosure,
    test_member_leaves_only_through_a_disclosed_release,
    test_a_released_member_rejoins_when_clean_and_in_its_slot,
    test_a_rejoin_re_forms_a_two_member_row_with_its_sibling,
    test_release_and_rejoin_do_not_oscillate,
    test_rigid_true_block_moves_as_one,
    test_a_ref_in_two_groups_is_deduped_and_disclosed,
    test_an_ic_that_would_strand_its_caps_is_refused_the_cluster_moves,
    test_tether_terms_equal_the_grader_on_every_pair,
    test_a_cap_elected_beyond_the_radius_may_not_walk_into_another_ics,
    test_a_swap_that_strands_a_cap_is_refused_on_the_tether,
    test_the_tether_shortcuts_change_no_decision,
    test_rigid_swap_rule_stays_inside_one_group,
    test_the_cli_gate_is_not_called_inert_when_it_holds_tethers_or_rows,
    test_every_caller_hands_the_quench_the_resolved_rows_and_tethers,
    test_unarmed_quench_is_bit_identical_to_the_pre_phase4_quench,
]


if __name__ == '__main__':
    only = sys.argv[1:]
    for t in TESTS:
        if only and not any(o in t.__name__ for o in only):
            continue
        print(f"--- {t.__name__}")
        t()
    print('ALL PASS')
