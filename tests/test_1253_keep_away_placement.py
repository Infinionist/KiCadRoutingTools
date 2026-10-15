"""#1253: one keep-away declaration, graded on the placement and priced by the
router.

The routing keep-away (#1146, `route.py --keep-away AGG:VICTIM:GAP`) measures
TRACKS, so on a placed, unrouted board it checks nothing. The same rule may now
be declared once, in the board's design brief (`keep_away`), and it drives
both halves: check_floorplan grades it pad edge to pad edge on the placement
(`keep_away`, WARN unless the intent's `severity` promotes it), and the routing
engines read the same key off the sibling brief and price it, with no flag.

Rows:
  - the brief key's one reader (`keep_away.brief_rule_rows`): a string or an
    object row, "unknown", and each malformed shape refused with the rule
    parser's own message -- by the brief loader, the intent loader and
    route.py alike (route.py exits 2, before parsing the board);
  - py_router's brief extension is design_brief's;
  - the watchy demo from the issue, end to end through the CLIs: the brief
    beside the board, place_pose moves Y1 beside L2, the emitted intent
    carries the rule, the moved board WARNs on the exact pad pair with both
    sides, the original passes, and the WARN changes no exit code;
  - severity ERROR, and an unresolved side;
  - the ulx3s cross-face pair: Y1 (F.Cu) over buck U4's SW pin (B.Cu), both
    sides named and the plan-view caveat stated;
  - drift and clause coverage: a brief rule the intent lacks, and a
    brief-sourced intent rule the brief no longer declares;
  - the reverse: a row the intent carries and the brief beside the board
    does not is graded and priced by no router, so check_floorplan warns
    (console, --json `keep_away_unpriced`, JSON_SUMMARY), and a row compiled
    from that brief does not; a --brief naming another file is caught at
    --emit-intent too;
  - the routing side: `resolve_keep_away` merges the brief's rules after the
    caller's, without duplicates, and changes NOTHING for a board with no
    brief or a brief without the key; both engines call it; batch_route
    handed a PCBData with only a source path (the GUI's path, no CLI) prices
    and reports the brief's rule; and route_diff routes the same copper from
    the brief as from the flag.

    python3 tests/test_1253_keep_away_placement.py
"""

import ast
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_tools', 'py_placer', 'py_router', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from run_utils import check, evidence  # noqa: E402

import keep_away as ka  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import design_brief as db  # noqa: E402
from placement import floorplan as fp  # noqa: E402

RUN_ALL_TIMEOUT = 900

WATCHY = os.path.join(ROOT, 'kicad_files', 'watchy.kicad_pcb')
ULX3S = os.path.join(ROOT, 'kicad_files', 'ulx3s.kicad_pcb')
RULE = 'Net-(D5-A):/32K_P,/32K_N:1'
DEMO_LINE = ("Y1 pad 1 (F.Cu) on /32K_P is 0.91mm from L2 pad 1 (F.Cu) on "
             "Net-(D5-A), inside the 1.00mm keep-away")
BRIEF = {'schema': 1, 'kind': 'design-brief', 'units': 'mm',
         'keep_away': [{'rule': RULE,
                        'why': '32 kHz crystal vs the boost switch node'}]}


def _py(*argv):
    return [sys.executable, '-X', 'utf8'] + [str(a) for a in argv]


def _run(argv):
    """Run a CLI that may exit non-zero for reasons this test does not own;
    a traceback is never one of them."""
    r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=600)
    out = r.stdout + r.stderr
    assert 'Traceback' not in out, out[-2000:]
    return r.returncode, out


def _intent(rules, **extra):
    raw = {'schema': 1, 'kind': 'floorplan-intent',
           'keep_away': [{'rule': r} for r in rules]}
    raw.update(extra)
    return fp.intent_from_dict(raw, '')


def _write_brief(board, doc):
    path = os.path.splitext(board)[0] + '.design-brief.json'
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)
    return path


def test_brief_ext_is_design_briefs():
    assert ka.BRIEF_EXT == db.BRIEF_EXT, (ka.BRIEF_EXT, db.BRIEF_EXT)


def test_the_key_has_one_reader():
    rows = ka.brief_rule_rows([RULE, {'rule': 'A:B:0.5', 'why': 'w'}])
    assert [r['rule'] for r in rows] == [RULE, 'A:B:0.5'], rows
    assert rows[1]['why'] == 'w' and rows[0]['row'] == 0, rows
    assert ka.brief_rule_rows('unknown') == [] and ka.brief_rule_rows(None) == []
    bad = {
        'A:B': "keep-away rule 'A:B': expected AGGRESSOR:VICTIM:GAP",
        'A:B:50': 'GAP must be > 0 and <= 10 mm',
        'A:B:1 C:D:1': 'holds 2 rules',
    }
    for text, why in bad.items():
        for refuse in (lambda: ka.brief_rule_rows([text]),
                       lambda: db.brief_from_dict(
                           dict(BRIEF, keep_away=[text])),
                       lambda: _intent([text])):
            try:
                refuse()
            except ValueError as exc:     # BriefError / IntentError are both
                assert why in str(exc), (text, str(exc))
            else:
                raise AssertionError(f"{text!r} was accepted")
    for shape, why in (([RULE, RULE], 'repeats keep_away[0]'),
                       ([{'rule': RULE, 'min_mm': 1}], 'unknown key(s) min_mm'),
                       ('A:B:1', 'expected a list')):
        try:
            ka.brief_rule_rows(shape)
        except ValueError as exc:
            assert why in str(exc), (shape, str(exc))
        else:
            raise AssertionError(f"{shape!r} was accepted")
    # "unknown" is reported, not compiled.
    frag, rep = db.compile_brief(db.brief_from_dict(
        dict(BRIEF, keep_away='unknown')))
    assert 'keep_away' not in frag and 'keep_away' in rep['unknown'], rep


def test_route_refuses_a_malformed_brief_rule():
    with tempfile.TemporaryDirectory() as td:
        board = os.path.join(td, 'w.kicad_pcb')
        shutil.copy(WATCHY, board)
        _write_brief(board, dict(BRIEF, keep_away=['Net-(D5-A)/32K_P:1']))
        for script in ('py_router/route.py', 'py_router/route_diff.py'):
            check(_py(script, board, '--output', os.path.join(td, 'o.kicad_pcb'),
                      '--nets', '/32K_P'),
                  refuse="keep_away[0]: keep-away rule 'Net-(D5-A)/32K_P:1': "
                         "expected AGGRESSOR:VICTIM:GAP",
                  code=2)


def test_watchy_demo_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, 'base.kicad_pcb')
        moved = os.path.join(td, 'y1.kicad_pcb')
        intent = os.path.join(td, 'intent.json')
        rc, out = _run(_py('py_router/copy_board.py', WATCHY, base))
        assert rc == 0, out
        _write_brief(base, BRIEF)
        rc, out = _run(_py('py_placer/place_pose.py', base, moved, 'set', 'Y1',
                           '--near', '78.5', '106.4', '--rot', '270', '--snap'))
        evidence(moved, 'the moved board')
        assert os.path.isfile(os.path.splitext(moved)[0]
                              + '.design-brief.json'), 'brief not carried'
        rc, out = _run(_py('py_tools/check_floorplan.py', base, '--emit-intent',
                           intent, '--declare-classes', '--declare-decaps'))
        assert rc == 0, out[-2000:]
        assert '1 keep-away rule(s)' in out, out[:2000]
        doc = json.load(open(evidence(intent, 'the emitted intent') or intent))
        assert [k['rule'] for k in doc['keep_away']] == [RULE], doc['keep_away']
        assert doc['min_reader'] >= 10, doc['min_reader']
        got = {}
        for name, board in (('base', base), ('y1', moved)):
            js = os.path.join(td, f'{name}.json')
            rc, out = _run(_py('py_tools/check_floorplan.py', board, '--intent',
                               intent, '--json', js))
            got[name] = (rc, out, json.load(open(evidence(js, f'{name} grade')
                                                 or js)))
        rc_b, out_b, js_b = got['base']
        rc_y, out_y, js_y = got['y1']
        assert DEMO_LINE in out_y, out_y[-3000:]
        assert DEMO_LINE not in out_b
        ka_y = [v for v in js_y['violations'] if v['rule'] == 'keep_away']
        assert len(ka_y) == 1 and ka_y[0]['severity'] == 'warn', ka_y
        m = ka_y[0]['measured']
        assert (m['victim']['ref'], m['victim']['pad'], m['victim']['side'],
                m['aggressor']['ref'], m['aggressor']['pad'],
                m['aggressor']['side']) == ('Y1', '1', 'F.Cu', 'L2', '1',
                                            'F.Cu'), m
        assert abs(m['gap_mm'] - 0.91) < 0.005 and m['cross_face'] is False, m
        assert not [v for v in js_b['violations'] if v['rule'] == 'keep_away']
        (row_b,) = js_b['keep_away']
        assert row_b['state'] == 'pass' and row_b['passes'] is True, row_b
        assert row_b['min_gap_mm'] > 15.0, row_b      # 15.34: C18 pad 1 to D5
        assert row_b['not_measured'] == list(fp.KEEP_AWAY_NOT_MEASURED)
        assert 'cannot see tracks' in out_b, out_b[-3000:]
        # WARN by default: the finding changes no exit code.
        assert rc_b == rc_y, (rc_b, rc_y)
        # Both clauses graded; the moved board's reads graded_warn.
        for js in (js_b, js_y):
            (cl,) = [c for c in js['brief_coverage']['clauses']
                     if c['kind'] == 'keep_away']
            assert cl['state'] == 'graded' and cl['keep_away_rule'] == RULE, cl
        led = {r['id']: r['status'] for r in js_y['declaration_ledger']}
        assert led['keep_away[0].rule'] == 'graded_warn', led
        led = {r['id']: r['status'] for r in js_b['declaration_ledger']}
        assert led['keep_away[0].rule'] == 'graded_pass', led

        # Severity ERROR, asked for by the intent's map.
        doc['severity'] = dict(doc.get('severity') or {}, keep_away='error')
        it = fp.intent_from_dict(doc, intent)
        res = fp.grade(it, parse_kicad_pcb(moved), moved)
        errs = [v for v in res.errors if v.rule == 'keep_away']
        assert len(errs) == 1 and DEMO_LINE in errs[0].message, res.violations

        # The router reads the SAME declaration off the brief beside the
        # moved board: see test_gui_engine_path_reads_the_brief.


UNPRICED = ('keep_away rule(s) in this intent are graded on the placement '
            'but will NOT be priced by route.py, route_diff.py or the GUI')


def _summary(out):
    line = next(l for l in out.splitlines() if l.startswith('JSON_SUMMARY:'))
    return json.loads(line.split(':', 1)[1])


def test_intent_only_rows_are_said_to_go_unrouted():
    """A keep_away row the intent carries and the brief beside the board
    does not is graded and priced by no router, so check_floorplan warns,
    on its console and in --json; a row compiled from that brief does not.
    Asked of the router's own reader (`keep_away.unpriced_rules`)."""
    other = 'Net-(D5-A):/40M_P,/40M_N:2'
    with tempfile.TemporaryDirectory() as td:
        board = os.path.join(td, 'w.kicad_pcb')
        rc, out = _run(_py('py_router/copy_board.py', WATCHY, board))
        assert rc == 0, out
        # The reader: canonical spelling, so `1.0` is RULE.
        got = ka.unpriced_rules([RULE, other + '.0'], board)
        assert got['rules'] == [RULE, other] and got['brief'] == '', got
        brief = _write_brief(board, BRIEF)
        got = ka.unpriced_rules(['Net-(D5-A):/32K_P,/32K_N:1.0', other], board)
        assert got['rules'] == [other] and got['brief_declares'] == [RULE], got
        assert os.path.samefile(got['brief'], brief), got

        # Brief-compiled: no warning, and the JSON says it checked.
        intent = os.path.join(td, 'intent.json')
        rc, out = _run(_py('py_tools/check_floorplan.py', board,
                           '--emit-intent', intent))
        assert rc == 0 and UNPRICED not in out, out[-3000:]
        js = os.path.join(td, 'brief.json')
        rc, out = _run(_py('py_tools/check_floorplan.py', board, '--intent',
                           evidence(intent, 'the emitted intent'), '--json',
                           js))
        assert 'keep-away Net-(D5-A):/32K_P,/32K_N:1: closest' in out, \
            out[-3000:]                  # graded -- the run reached the rule
        assert UNPRICED not in out, out[-3000:]
        with open(evidence(js, 'the brief-compiled grade'),
                  encoding='utf-8') as fh:
            rec = json.load(fh)['keep_away_unpriced']
        assert rec['rules'] == [] and rec['checked'] == 1 and \
            rec['warning'] is None, rec
        assert _summary(out)['keep_away_unpriced'] == 0

        # Intent-only: the same intent plus a row written by hand.
        doc = json.load(open(intent, encoding='utf-8'))
        doc['keep_away'].append({'rule': other})
        hand = os.path.join(td, 'hand.json')
        with open(hand, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        js = os.path.join(td, 'hand_grade.json')
        rc, out = _run(_py('py_tools/check_floorplan.py', board, '--intent',
                           hand, '--json', js))
        assert f"keep-away {other}: closest" in out, out[-3000:]
        assert f"1 {UNPRICED}" in out, out[-3000:]
        assert 'the design brief beside the board (w.design-brief.json) ' \
               'does not declare it' in out, out[-3000:]
        assert f"- keep_away[1] '{other}'" in out, out[-3000:]
        assert f"fix: declare it in the `keep_away` list of {brief}" in out, \
            out[-3000:]
        with open(evidence(js, 'the hand-edited grade'),
                  encoding='utf-8') as fh:
            rec = json.load(fh)['keep_away_unpriced']
        assert rec['rules'] == [{'index': 1, 'rule': other, 'source': None}], \
            rec
        assert rec['router_brief_declares'] == [RULE] and rec['fix'], rec
        assert _summary(out)['keep_away_unpriced'] == 1

        # No brief beside the board: the brief-compiled row is now unpriced
        # too, and a --brief naming another file is caught at emit time.
        moved = os.path.join(td, 'elsewhere.json')
        os.replace(brief, moved)
        rc, out = _run(_py('py_tools/check_floorplan.py', board, '--intent',
                           intent, '--no-brief'))
        assert f"1 {UNPRICED}" in out and 'no design brief sits beside the ' \
            'board (w.design-brief.json)' in out, out[-3000:]
        assert f"- keep_away[0] '{RULE}' (source brief)" in out, out[-3000:]
        rc, out = _run(_py('py_tools/check_floorplan.py', board, '--brief',
                           moved, '--emit-intent',
                           os.path.join(td, 'other.json')))
        assert rc == 0 and f"1 {UNPRICED}" in out, out[-3000:]


def test_unresolved_side():
    pcb = parse_kicad_pcb(WATCHY)
    res = fp.grade(_intent(['NO_SUCH_NET:/32K_P:1']), pcb, WATCHY)
    (v,) = [v for v in res.violations if v.rule == 'keep_away_unresolved']
    assert v.severity == 'warn' and 'aggressor side matches no net' in \
        v.message, v
    assert res.keep_away_measured[0]['state'] == 'unresolved'


def test_ulx3s_cross_face():
    pcb = parse_kicad_pcb(ULX3S)
    res = fp.grade(_intent(['/power/L2:CLK_25MHz:1.5']), pcb, ULX3S)
    (v,) = [v for v in res.violations if v.rule == 'keep_away']
    assert v.message.startswith(
        'Y1 pad 3 (F.Cu) on CLK_25MHz is 1.05mm from U4 pad 3 (B.Cu) on '
        '/power/L2, inside the 1.50mm keep-away; the pads are on opposite '
        'faces'), v.message
    assert v.measured['cross_face'] is True, v.measured
    assert abs(v.measured['gap_mm'] - 1.055) < 1e-3, v.measured
    (row,) = res.keep_away_measured
    assert row['closest']['cross_face'] is True, row
    assert 'opposite faces, plan view' in fp.format_text(res)


def test_drift_and_coverage():
    frag, rep = db.compile_brief(db.brief_from_dict(BRIEF))
    assert rep['declared'] == ['keep_away[0].rule'], rep['declared']
    doc = db.merge_into_intent({'schema': 1, 'kind': 'floorplan-intent'},
                               frag, rep)
    assert db.drift_pairs(doc, frag) == []
    # A respelling of the same rule is not drift.
    respelt = dict(doc, keep_away=[{'rule': 'Net-(D5-A):/32K_P,/32K_N:1.0'}])
    assert db.drift_pairs(respelt, frag) == []
    lost = dict(doc, keep_away=[])
    assert [c for c, _l in db.drift_pairs(lost, frag)] == \
        ['keep_away[0].rule']
    cov = db.clause_coverage(rep, lost, rules_run=('keep_away',))
    assert cov['uncovered'] == 1 and not cov['complete'], cov
    cov = db.clause_coverage(rep, doc, rules_run=('keep_away',))
    assert cov['graded'] == 1, cov
    # A brief-sourced rule the brief stopped declaring: the router has
    # stopped pricing it, so the grade must say so.
    stale = dict(doc, keep_away=doc['keep_away'] + [
        {'rule': 'A:B:1', 'source': 'brief'}])
    lines = db.drift(stale, frag)
    assert any("'A:B:1'" in l and 'no longer declares' in l
               for l in lines), lines


def test_resolve_changes_nothing_without_a_declaration():
    with tempfile.TemporaryDirectory() as td:
        board = os.path.join(td, 'w.kicad_pcb')
        shutil.copy(WATCHY, board)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert ka.resolve_keep_away(None, board) is None
            assert ka.resolve_keep_away(['A:B:1.0'], board) == ['A:B:1']
            # A brief without the key, and one that says "unknown".
            _write_brief(board, {'schema': 1, 'kind': 'design-brief'})
            assert ka.resolve_keep_away(None, board) is None
            assert ka.resolve_keep_away(['A:B:1'], board) == ['A:B:1']
            _write_brief(board, dict(BRIEF, keep_away='unknown'))
            assert ka.resolve_keep_away(None, board) is None
        assert buf.getvalue() == '', buf.getvalue()
        # Declared: after the caller's rules, never twice, said once.
        _write_brief(board, BRIEF)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert ka.resolve_keep_away(['A:B:1'], board) == ['A:B:1', RULE]
            assert ka.resolve_keep_away([RULE], board) == [RULE]
            assert ka.resolve_keep_away(None, '', parse_kicad_pcb(board)) \
                == [RULE]                  # found through PCBData.source_path
        log = buf.getvalue()
        assert log.count('read from the design brief') == 2, log
        assert 'all already given' in log, log


def test_both_engines_resolve():
    """batch_route and batch_route_diff_pairs each call resolve_keep_away
    with their own input_file and pcb_data -- a CLI main() that did it would
    be invisible to the GUI, which calls the engines directly."""
    for rel, fn in (('py_router/route.py', 'batch_route'),
                    ('py_router/route_diff.py', 'batch_route_diff_pairs')):
        tree = ast.parse(open(os.path.join(ROOT, rel), encoding='utf-8').read())
        (node,) = [n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == fn]
        calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                 and getattr(c.func, 'id', None) == 'resolve_keep_away']
        assert calls, f"{rel}:{fn} does not call resolve_keep_away"
        args = [getattr(a, 'id', None) for a in calls[0].args]
        assert args == ['keep_away', 'input_file', 'pcb_data'], (rel, args)


def _copper(path):
    p = parse_kicad_pcb(evidence(path))
    name = {n: x.name for n, x in p.nets.items()}
    return (sorted((round(s.start_x, 4), round(s.start_y, 4), round(s.end_x, 4),
                    round(s.end_y, 4), s.layer, name.get(s.net_id))
                   for s in p.segments),
            sorted((round(v.x, 4), round(v.y, 4), name.get(v.net_id))
                   for v in p.vias))


def test_route_diff_reads_the_brief():
    """route_diff on #1146's own diff-pair case: the rule declared in the
    brief routes the copper the same rule given by flag routes, and both
    runs report it."""
    rule = '/CLK+,/CLK-:/DATA+,/DATA-:1'
    src = os.path.join(ROOT, 'kicad_files', 'lvds_converter_dualclk.kicad_pcb')
    env = dict(os.environ, MSYS2_ARG_CONV_EXCL='*')
    with tempfile.TemporaryDirectory() as td:
        outs = {}
        for arm in ('flag', 'brief'):
            board = os.path.join(td, arm, 'b.kicad_pcb')
            os.makedirs(os.path.dirname(board))
            shutil.copy(evidence(src), board)
            extra = ['--keep-away-cost', '1']
            if arm == 'flag':
                extra += ['--keep-away', rule]
            else:
                _write_brief(board, dict(BRIEF, keep_away=[rule]))
            outs[arm] = os.path.join(td, arm, 'o.kicad_pcb')
            r = subprocess.run(_py('py_router/route_diff.py', board, outs[arm],
                                   '--nets', '/CLK+', '/CLK-', '/DATA+',
                                   '/DATA-', *extra),
                               cwd=ROOT, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', env=env,
                               timeout=900)
            assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
            assert 'Keep-away (1 rule(s)' in r.stdout, (arm, r.stdout[-2000:])
            assert ('read from the design brief' in r.stdout) == \
                (arm == 'brief'), arm
        assert _copper(outs['flag']) == _copper(outs['brief'])


def test_gui_engine_path_reads_the_brief():
    """The GUI's path: batch_route handed a PCBData (source path set, as
    build_pcb_data_from_board sets it) and no input file, no CLI. The rule
    in the brief beside the board is priced and reported."""
    from route import batch_route
    with tempfile.TemporaryDirectory() as td:
        board = os.path.join(td, 'w.kicad_pcb')
        shutil.copy(WATCHY, board)
        _write_brief(board, BRIEF)
        pcb = parse_kicad_pcb(board)
        assert pcb.source_path == os.path.abspath(board)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _ok, _fail, _t, rd = batch_route(
                '', '', ['/32K_P', '/32K_N', 'Net-(D5-A)'],
                layers=['F.Cu', 'B.Cu'], return_results=True, pcb_data=pcb)
        log = buf.getvalue()
        assert f"read from the design brief w.design-brief.json (#1253): " \
               f"'{RULE}'" in log, log[-3000:]
        assert f"Keep-away rule 1 '{RULE}': 1 net(s) vs 2 net(s)" in log, \
            log[-3000:]
        # Measured by the report the brief's rule armed: no flag was given.
        assert 'Keep-away (1 rule(s)' in log, log[-3000:]
        assert isinstance(rd.get('keep_away'), list), rd.keys()


TESTS = [test_brief_ext_is_design_briefs, test_the_key_has_one_reader,
         test_route_refuses_a_malformed_brief_rule, test_unresolved_side,
         test_ulx3s_cross_face, test_drift_and_coverage,
         test_resolve_changes_nothing_without_a_declaration,
         test_both_engines_resolve, test_gui_engine_path_reads_the_brief,
         test_route_diff_reads_the_brief, test_watchy_demo_end_to_end,
         test_intent_only_rows_are_said_to_go_unrouted]


if __name__ == '__main__':
    fails = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL {t.__name__}: {e}")
    print('ALL PASS' if not fails else f'{fails} FAILED')
    sys.exit(1 if fails else 0)
