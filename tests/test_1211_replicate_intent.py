#!/usr/bin/env python3
"""#1211: the intent's `replicate[]` declaration -- the AI declares which
sheet instance is the template, the tools do the geometry.

One declaration names a SOURCE sheet instance and its TARGETS and carries
`place_pose replicate`'s own settings. Three readers, each pinned here:

* the LOADER refuses what needs no board to refuse (a target twice, `from`
  among its own `to`, `only` with `skip`, a waiver that is not REF.PAD, the
  numbers), each by its reason; reader 11 is required;
* the GRADE (`replicate`, WARN by default) measures, per target, each part's
  distance from the best translation of the source arrangement (the median
  offset, so the anchor does not matter and one stray part reads as
  itself), and reports a declaration the board cannot resolve as
  `replicate_unresolved` with the verb's own reason; check_floorplan prints
  and `--json`s every target;
* `place_pose replicate --intent` (no --from/--to) applies the declarations;
* `place_seed` applies them after its polish, through the same engine and
  verdict: a copy that fits is written (the targets then grade as copies), a
  copy that grades worse is not, and the seed stands with the colliding
  members named. An undeclared intent seeds exactly as before (the base
  commit comparison is in the commit message; here: no stage record).

Run: python3 -X utf8 tests/test_1211_replicate_intent.py [case ...]
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_utils  # noqa: E402
import test_1211_place_pose_replicate as fx  # noqa: E402  (the fixture)

ROOT = run_utils.ROOT_DIR
RUN_ALL_TIMEOUT = 600

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import floorplan as fp  # noqa: E402

PLACE_SEED = os.path.join(ROOT, 'py_placer', 'place_seed.py')
CHECK_FLOORPLAN = os.path.join(ROOT, 'py_tools', 'check_floorplan.py')
DECL = {'from': 'S1', 'to': ['S2', 'S3']}


def _raw(rows, **extra):
    d = {'schema': 1, 'kind': 'floorplan-intent', 'units': 'mm',
         'replicate': rows}
    d.update(extra)
    return d


def _write_intent(td, rows, name='intent.json', **extra):
    p = os.path.join(td, name)
    with open(p, 'w', encoding='utf-8') as fh:
        json.dump(_raw(rows, **extra) if rows is not None else
                  {'schema': 1, 'kind': 'floorplan-intent', 'units': 'mm'},
                  fh)
    return p


def _grade(board, rows, **extra):
    intent = fp.intent_from_dict(_raw(rows, **extra))
    return fp.grade(intent, parse_kicad_pcb(board), board)


def _findings(g, rule):
    return [v for v in g.violations if v.rule == rule]


def test_the_loader_refuses_each_malformed_row():
    cases = [
        ({'from': 'S1'}, 'replicate[0].to: expected a list of non-empty'),
        ({'from': 'S1', 'to': []}, 'expected a list of non-empty strings'),
        ({'to': ['S2']}, 'replicate[0].from: expected a sheet name or path'),
        ({'from': '/', 'to': ['S2']}, 'replicate[0].from'),
        ({'from': 'S1', 'to': ['/S1/']}, "is the `from` sheet itself"),
        ({'from': 'S1', 'to': ['S2', 's2']}, 'is already a target of '
                                              'replicate[0]'),
        ({'from': 'S1', 'to': ['S2'], 'only': ['U1'], 'skip': ['R1']},
         '`only` and `skip` together'),
        ({'from': 'S1', 'to': ['S2'], 'allow_net_mismatch': ['U19']},
         "'U19' is not REF.PAD"),
        ({'from': 'S1', 'to': ['S2'], 'search_mm': 0},
         'search_mm: expected a positive number'),
        ({'from': 'S1', 'to': ['S2'], 'tolerance_mm': -1},
         'tolerance_mm: expected a non-negative number'),
        ({'from': 'S1', 'to': ['S2'], 'tolerance_mm': True},
         'tolerance_mm: expected'),
        ({'from': 'S1', 'to': ['S2'], 'offset': [1, 2]},
         'unknown key(s) offset'),
        ({'from': 'S1', 'to': ['S2'], 'anchor': ''}, 'anchor: expected'),
        ({'from': 'S1', 'to': ['S2'], 'context': []}, 'context'),
    ]
    for row, why in cases:
        try:
            fp.intent_from_dict(_raw([row]))
        except fp.IntentError as exc:
            assert why in str(exc), (row, str(exc))
        else:
            raise AssertionError('loaded: %r' % (row,))
    # Across rows: one target, two copies.
    try:
        fp.intent_from_dict(_raw([DECL, {'from': 'S2', 'to': ['S3']}]))
    except fp.IntentError as exc:
        assert 'replicate[1].to' in str(exc) and 'replicate[0]' in str(exc)
    else:
        raise AssertionError('a target declared twice loaded')
    try:
        fp.intent_from_dict({'schema': 1, 'kind': 'floorplan-intent',
                             'replicate': {'from': 'S1'}})
    except fp.IntentError as exc:
        assert 'replicate: expected a list of rows' in str(exc), str(exc)
    else:
        raise AssertionError('a dict loaded as replicate[]')
    # A good row loads, and a chain (S2 is a source after being a target in
    # NO row) is fine.
    i = fp.intent_from_dict(_raw([dict(DECL, anchor='U1', search_mm=2,
                                       tolerance_mm=0, skip=['R1'],
                                       allow_net_mismatch=['C1.1'])]))
    assert i.replicate[0]['to'] == ['S2', 'S3'], i.replicate
    print('  %d malformed rows refused by reason; a full row loads'
          % (len(cases) + 2))


def test_reader_11_names_the_key():
    assert fp.READER_VERSION == 11, fp.READER_VERSION
    fp.intent_from_dict(_raw([DECL], min_reader=11))
    saved = fp.READER_VERSION
    try:
        fp.READER_VERSION = 10
        try:
            fp.intent_from_dict(_raw([DECL], min_reader=11))
        except fp.IntentError as exc:
            assert 'this build is reader 10' in str(exc), str(exc)
        else:
            raise AssertionError('a reader-10 build loaded min_reader 11')
    finally:
        fp.READER_VERSION = saved
    print('  min_reader 11 loads here and is refused by a reader-10 build')


def test_the_grade_measures_each_target():
    with tempfile.TemporaryDirectory() as td:
        b = fx.board(td)
        g = _grade(b, [DECL])
        found = _findings(g, 'replicate')
        assert len(found) == 2 and all(v.severity == fp.WARN for v in found)
        m = {r['to']: r for r in g.replicate_measured}
        # S2: R2 and C2 off and both rotated (R2 90 vs 0, C2 0 vs 90); U2
        # is the median.
        assert (m['S2']['off'], m['S2']['pairs'], m['S2']['rotated'],
                m['S2']['fit_offset']) == (2, 3, 2, [30.0, 0.0]), m['S2']
        assert m['S2']['parts'][0]['ref'] in ('R2', 'C2'), m['S2']['parts']
        assert 'S2 has 2 of 3 part(s) off S1' in found[0].message
        # The severity map promotes it.
        g = _grade(b, [DECL], severity={'replicate': 'error'})
        assert {v.severity for v in _findings(g, 'replicate')} == {fp.ERROR}
        # After the copy: every target a copy, 0 mm.
        out = os.path.join(td, 'copied.kicad_pcb')
        fx.pose(b, out, 'replicate', '--from', 'S1', '--to', 'S2', '--to',
                'S3', accept=True)
        g = _grade(out, [DECL])
        assert not _findings(g, 'replicate'), g.violations
        assert all(r['passes'] and r['max_residual_mm'] == 0.0
                   for r in g.replicate_measured), g.replicate_measured
        # Laid around ANOTHER anchor, still a copy: the fit is the median.
        out2 = os.path.join(td, 'anchorR.kicad_pcb')
        fx.pose(b, out2, 'replicate', '--from', 'S1', '--to', 'S2',
                '--anchor', 'R2', accept=True)
        m = {r['to']: r for r in _grade(out2, [DECL]).replicate_measured}
        assert m['S2']['passes'] and m['S2']['fit_offset'] == [36.0, 13.0], m
        # One stray part reads as itself, by its own distance.
        stray = fx.board(td, 'stray', at={'U2': (45, 20, 0), 'R2': (46, 14, 0),
                                          'C2': (40, 20, 90),
                                          'U3': (75, 20, 0), 'R3': (75, 14, 0),
                                          'C3': (70, 20, 90)})
        g = _grade(stray, [DECL])
        (v,) = _findings(g, 'replicate')
        assert v.ref == 'R2' and v.measured['off'] == 1, v
        assert v.measured['max_residual_mm'] == 1.0, v.measured
        # A tolerance above the stray's distance passes it.
        g = _grade(stray, [dict(DECL, tolerance_mm=1.5)])
        assert not _findings(g, 'replicate'), g.violations
        # A part in place but TURNED is off too: no translation fixes it.
        turned = fx.board(td, 'turned', at={'R2': (45, 14, 0),
                                            'C2': (40, 20, 0),
                                            'R3': (75, 14, 0),
                                            'C3': (70, 20, 90)})
        g = _grade(turned, [DECL])
        (v,) = _findings(g, 'replicate')
        assert v.ref == 'C2' and v.measured['rotated'] == 1, v.measured
        assert v.measured['max_residual_mm'] == 0.0, v.measured
        print('  unarranged: 2 WARN findings (S2 rotated 2); promoted to '
              'error; a copy reads 0 whatever its anchor; a 1 mm stray is '
              'named alone')


def test_a_declaration_the_board_cannot_resolve_is_named():
    with tempfile.TemporaryDirectory() as td:
        b = fx.board(td, 'strap', nets={('C2', '1'): '/S2/IN'})
        g = _grade(b, [DECL])
        (v,) = _findings(g, 'replicate_unresolved')
        assert v.severity == fp.WARN and 'not wired alike' in v.message, v
        assert '--allow-net-mismatch C1.1' in v.message, v.message
        assert g.replicate_measured[0]['state'] == 'unresolved'
        # The declaration's own waiver resolves it.
        g = _grade(b, [dict(DECL, allow_net_mismatch=['C1.1'])])
        assert not _findings(g, 'replicate_unresolved'), g.violations
        assert len(g.replicate_measured) == 2
        g = _grade(fx.board(td), [{'from': 'S1', 'to': ['S9']}])
        (v,) = _findings(g, 'replicate_unresolved')
        assert 'is neither a sheet name nor a sheet path' in v.message
        # A partial declaration measures only its parts.
        g = _grade(fx.board(td, 'p'), [dict(DECL, skip=['R1'])])
        m = {r['to']: r for r in g.replicate_measured}
        assert (m['S2']['pairs'], m['S2']['left_in_place']) == (2, ['R2']), m
        print('  not wired alike / unknown sheet -> replicate_unresolved with '
              'the verb\'s reason; a declared waiver resolves; skip measured '
              'apart')


def test_check_floorplan_reports_every_target():
    with tempfile.TemporaryDirectory() as td:
        b = fx.board(td)
        out = os.path.join(td, 'copied.kicad_pcb')
        fx.pose(b, out, 'replicate', '--from', 'S1', '--to', 'S2', accept=True)
        ip = _write_intent(td, [DECL])
        js = os.path.join(td, 'g.json')
        r = run_utils.check([sys.executable, '-X', 'utf8', CHECK_FLOORPLAN,
                             out, '--intent', ip, '--json', js], accept=True,
                            allow=('[warn ]',))
        assert 'replicate[0] S1 -> S2: a copy, max 0.00mm' in r.stdout, (
            r.stdout[-1500:])
        assert 'replicate[0] S1 -> S3: 2 of 3 part(s) off' in r.stdout
        with open(run_utils.evidence(js, 'check_floorplan --json')) as fh:
            doc = json.load(fh)
        assert [x['to'] for x in doc['replicate']] == ['S2', 'S3'], doc.get(
            'replicate')
        assert doc['replicate'][0]['passes'] is True
        assert 'replicate' in doc['rules_run'], doc['rules_run']
        print('  check_floorplan prints and --jsons every target, passing '
              'ones too')


def test_place_pose_applies_the_declarations():
    with tempfile.TemporaryDirectory() as td:
        b = fx.board(td)
        ip = _write_intent(td, [DECL])
        out = os.path.join(td, 'x.kicad_pcb')
        r = fx.pose(b, out, '--intent', ip, 'replicate', accept=True)
        got = fx.poses(out)
        for ref, w in {'R2': (45, 14, 0), 'C2': (40, 20, 90),
                       'R3': (75, 14, 0), 'C3': (70, 20, 90)}.items():
            assert fx.near(got[ref], w), (ref, got[ref], w)
        assert fx.summary(r)['replicate'][0]['from']['name'] == 'S1'
        none = _write_intent(td, None, 'none.json')
        argparse_exit = ('usage:',)
        for argv, why, code in (
                (['replicate'], 'this call passes no --intent', 2),
                (['--intent', none, 'replicate'], 'declares none', 2),
                (['--intent', ip, 'replicate', '--anchor', 'U1'],
                 'would override them silently', 2),
                (['replicate', '--from', 'S1'],
                 'needs both --from and --to', 2)):
            fx.pose(b, out + '2', *argv, refuse=why, code=code,
                    allow=argparse_exit)
        assert not os.path.exists(out + '2')
        print('  `replicate --intent` copies both targets; no intent, no '
              'declaration, a flag beside it, half a pair -> 2')


def _pile(td, name='pile', *, extra_at=None, locked=('U1', 'R1', 'C1',
                                                     'U2', 'U3', 'T1')):
    """S1 laid out and locked, U2 / U3 locked at their channel spots (the
    mixer's front-panel parts), T1 locked clear of every copy, and R2 / C2 /
    R3 / C3 piled off the board -- so the seeder's choices for those four
    (which the copy overwrites) are all the seed decides, and no change to
    the seeder's search can make this fixture's copy collide."""
    at = {'R2': (120, 10, 0), 'C2': (130, 10, 0), 'R3': (120, 20, 0),
          'C3': (130, 20, 0), 'T1': (60, 35, 0)}
    at.update(extra_at or {})
    return fx.board(td, name, at=at, locked=locked)


def _seed(td, board, rows, name):
    ip = _write_intent(td, rows, name + '.json')
    out = os.path.join(td, name + '.kicad_pcb')
    r = run_utils.check([sys.executable, '-X', 'utf8', PLACE_SEED, board,
                         out, '--intent', ip, '--seed', '1'], accept=True)
    return out, fx.summary(r), r


def test_place_seed_copies_the_declared_channels():
    with tempfile.TemporaryDirectory() as td:
        b = _pile(td)
        off, s_off, _ = _seed(td, b, None, 'off')
        on, s_on, r = _seed(td, b, [DECL], 'on')
        assert s_off['replicate'] == [], s_off['replicate']
        (rec,) = s_on['replicate']
        assert rec['applied'] is True, rec
        assert rec['moved'] == ['C2', 'C3', 'R2', 'R3'], rec
        got = fx.poses(on)
        for ref, w in {'R2': (45, 14, 0), 'C2': (40, 20, 90),
                       'R3': (75, 14, 0), 'C3': (70, 20, 90)}.items():
            assert fx.near(got[ref], w), (ref, got[ref], w)
        # Without the declaration the seeder put them elsewhere: the stage,
        # not the seed, made the copy.
        assert not fx.near(fx.poses(off)['R2'], (45, 14, 0)), fx.poses(off)
        g = _grade(on, [DECL])
        assert all(m['passes'] for m in g.replicate_measured), (
            g.replicate_measured)
        assert 'replicate[0] S1 -> S2, S3: copied, 4 part(s) moved' in r.stdout
        # The source and the locked parts are where they were.
        for ref in ('U1', 'R1', 'C1', 'U2', 'U3', 'T1'):
            assert fx.near(got[ref], fx.poses(off)[ref]), ref
        print('  seeded with the declaration: S2 / S3 are copies of S1 '
              '(R2/C2/R3/C3 moved); without it they are not')


def test_place_seed_keeps_the_seed_when_the_copy_does_not_fit():
    with tempfile.TemporaryDirectory() as td:
        # T1 locked exactly where S2's copy of R1 lands.
        b = _pile(td, 'blocked', extra_at={'T1': (45, 14, 0)},
                  locked=('U1', 'R1', 'C1', 'U2', 'U3', 'T1'))
        off, _s, _ = _seed(td, b, None, 'off')
        on, s_on, r = _seed(td, b, [DECL], 'on')
        (rec,) = s_on['replicate']
        assert rec['applied'] is False and rec['code'] == 4, rec
        assert 'WORSE' in rec['reason'], rec['reason']
        assert any('T1' in c for c in rec['new_conflicts'].get('R2', ())), rec
        assert fx.poses(on) == fx.poses(off), 'a refused copy changed the seed'
        assert 'NOT copied' in r.stdout
        print('  a copy that stacks R2 on T1 is not written: the seed '
              'stands, R2 named')


TESTS = [
    test_the_loader_refuses_each_malformed_row,
    test_reader_11_names_the_key,
    test_the_grade_measures_each_target,
    test_a_declaration_the_board_cannot_resolve_is_named,
    test_check_floorplan_reports_every_target,
    test_place_pose_applies_the_declarations,
    test_place_seed_copies_the_declared_channels,
    test_place_seed_keeps_the_seed_when_the_copy_does_not_fit,
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
