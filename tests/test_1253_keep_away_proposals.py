#!/usr/bin/env python3
"""#1253 follow-up: board_context PROPOSES keep-away rules, ready for the brief.

`emc_roles` (#1254) already reads which parts are switching converters and
which are crystals or oscillators. Since #1253 the design brief's `keep_away`
holds a rule both check_floorplan and the routers read -- and nothing wrote one.
`emc_roles.keep_away` now does: per crystal or oscillator, its clock nets kept
away from every switch node the pins name, in route.py's own `--keep-away`
syntax, at a DERIVED DEFAULT gap (`KEEP_AWAY_GAP_MM`), with check_floorplan's
own reading of that rule beside it and a `brief_row` to paste. A proposal, not
a declaration: nothing reads it until the author pastes it.

Cases:
* watchy: the e-paper boost's switch node `Net-(D5-A)` -- a DISCRETE switch,
  Q3's drain (pin function D) on inductor L2's undecoupled end -- against
  the 32 kHz crystal Y1 and the 40 MHz crystal Y2; both pass on the human
  layout (15.34 / 16.58 mm);
* ulx3s: the three TLV62569 bucks carry no pin function, so their switch
  nets are CANDIDATES read off the inductor ends; so is the oscillator Y1's
  CLK_25MHz (its only undecoupled signal net). The rule is already inside on
  the human layout, across the board: Y1 pad 3 (F.Cu) 1.055 mm from U4 pad 3
  (B.Cu);
* rp2350_fpga_eensy: both sides read by pin function (U6 VREG_LX, U7 SW;
  crystal Y1, oscillator Y2);
* every proposed rule is the router's: `parse_keep_away_rules` gives back the
  same spelling, `brief_rule_rows` accepts the brief row, and
  `resolve_rule_sides` names exactly the nets the proposal lists;
* the gap: twice route.py's --keep-away-free, and every pair it puts inside
  the band on these human layouts is on opposite faces;
* pasted into a brief beside a copy of the board, check_floorplan grades
  each rule with the very reading printed beside it, and route.py's reader
  reads the same rules off the same file;
* #1253's watchy move (Y1 beside L2): the proposal reads it inside;
* no proposal without both a switch node and a clock; the CLI's --json and
  --md carry the section.

    python3 -X utf8 tests/test_1253_keep_away_proposals.py [case ...]
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_utils  # noqa: E402

ROOT = run_utils.ROOT_DIR
for _sub in ('py_router', 'py_tools', 'py_placer'):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

RUN_ALL_TIMEOUT = 900

BOARDS = os.path.join(ROOT, 'kicad_files')
WATCHY = os.path.join(BOARDS, 'watchy.kicad_pcb')
ULX = os.path.join(BOARDS, 'ulx3s.kicad_pcb')
RP = os.path.join(BOARDS, 'rp2350_fpga_eensy_prePlane.kicad_pcb')
FLAT = os.path.join(BOARDS, 'flat_hierarchy.kicad_pcb')
ESP = os.path.join(BOARDS, 'esp_prog.kicad_pcb')
CONTEXT = os.path.join(ROOT, 'py_tools', 'board_context.py')

WATCHY_RULES = ['Net-(D5-A):/32K_P,/32K_N:3', 'Net-(D5-A):/40M_P,/40M_N:3']
ULX_RULES = ['/power/L1,/power/L2,/power/L3:CLK_25MHz:3',
             '/power/L1,/power/L2,/power/L3:/power/OSCO_32k,/power/OSCI_32k:3']
RP_RULES = ['Net-(U6-VREG_LX),Net-(U7-SW):Net-(U6-XOUT),Net-(U6-XIN):3',
            'Net-(U6-VREG_LX),Net-(U7-SW):Net-(U2-GPIOL_20_PLLIN):3']
_CACHE = {}


def _parse(path):
    if path not in _CACHE:
        from kicad_parser import parse_kicad_pcb
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            _CACHE[path] = parse_kicad_pcb(path)
    return _CACHE[path]


def _sec(path, pcb=None):
    from placement.emc_roles import emc_roles
    return emc_roles(pcb if pcb is not None else _parse(path))


def _py(*argv):
    return [sys.executable, '-X', 'utf8'] + [str(a) for a in argv]


def _run(argv):
    """A CLI that may exit non-zero for reasons this test does not own (the
    floorplan grade exits 4 on any finding); a traceback is never one."""
    r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=600)
    out = r.stdout + r.stderr
    assert 'Traceback' not in out, out[-2000:]
    return r.returncode, out


def _closest(p):
    c = p['measured']['closest']
    v, a = c['victim'], c['aggressor']
    return (f"{v['ref']}.{v['pad']}", v['side'], f"{a['ref']}.{a['pad']}",
            a['side'], c['cross_face'])


def test_watchy_discrete_boost_against_both_crystals():
    from placement.emc_roles import KEEP_AWAY_GAP_MM
    sec = _sec(WATCHY)
    props = sec['keep_away']
    assert [p['rule'] for p in props] == WATCHY_RULES, \
        [p['rule'] for p in props]
    for p in props:
        (sw,) = p['switch_nodes']
        assert (sw['net'], sw['kind'], sw['part'], sw['inductor'],
                sw['read']) == ('Net-(D5-A)', 'discrete switch', 'Q3', 'L2',
                                'pin function'), sw
        assert p['gap_mm'] == KEEP_AWAY_GAP_MM == 3.0, p['gap_mm']
        assert p['gap_source'] == 'derived_default', p
        assert p['candidate'] is False and p['inside_now'] is False, p
        assert p['clock']['role'] == 'crystal', p['clock']
        assert 'derived default, not a datasheet value' in \
            p['brief_row']['why'], p['brief_row']
    y1, y2 = props
    assert y1['clock']['part'] == 'Y1' and y2['clock']['part'] == 'Y2'
    assert y1['measured']['min_gap_mm'] == 15.3399, y1['measured']
    assert _closest(y1) == ('C18.1', 'F.Cu', 'D5.2', 'F.Cu', False), \
        _closest(y1)
    assert y2['measured']['min_gap_mm'] == 16.5806, y2['measured']
    assert sec['keep_away_gap']['source'] == 'derived_default', \
        sec['keep_away_gap']
    print("  PASS: watchy Net-(D5-A) (Q3 drain, L2) vs Y1 15.34 / Y2 16.58 mm, "
          "gap 3 derived_default")


def test_ulx3s_candidates_and_the_cross_face_pair():
    sec = _sec(ULX)
    props = sec['keep_away']
    assert [p['rule'] for p in props] == ULX_RULES, [p['rule'] for p in props]
    for p in props:
        assert p['candidate'] is True, p
        assert {(s['part'], s['inductor'], s['read'])
                for s in p['switch_nodes']} == {
            ('U3', 'L1', 'candidate'), ('U4', 'L2', 'candidate'),
            ('U5', 'L3', 'candidate')}, p['switch_nodes']
        assert 'read as a candidate off the nets' in p['brief_row']['why']
    osc, xtal = props
    assert (osc['clock']['part'], osc['clock']['read'],
            osc['clock']['nets']) == ('Y1', 'candidate', ['CLK_25MHz']), \
        osc['clock']
    assert osc['inside_now'] is True and \
        osc['measured']['min_gap_mm'] == 1.055, osc['measured']
    assert _closest(osc) == ('Y1.3', 'F.Cu', 'U4.3', 'B.Cu', True), \
        _closest(osc)
    assert (xtal['clock']['part'], xtal['clock']['read']) == \
        ('Y2', 'role table'), xtal['clock']
    assert xtal['inside_now'] is False and \
        xtal['measured']['min_gap_mm'] == 12.4251, xtal['measured']
    # The role records carry the nets the proposals were built from.
    unk = {r['part']: r for r in sec['roles'] if r['role'] == 'unknown'}
    assert unk['U4']['candidate_switch_nets'] == [
        {'net': '/power/L2', 'inductor': 'L2'}], unk['U4']
    print("  PASS: ulx3s bucks + Y1 read as candidates; Y1.3 (F.Cu) 1.055 mm "
          "from U4.3 (B.Cu), inside, opposite faces")


def test_rp2350_both_sides_by_pin_function():
    props = _sec(RP)['keep_away']
    assert [p['rule'] for p in props] == RP_RULES, [p['rule'] for p in props]
    for p in props:
        assert p['candidate'] is False and p['inside_now'] is False, p
        assert [(s['part'], s['read']) for s in p['switch_nodes']] == [
            ('U6', 'pin function'), ('U7', 'pin function')], p
    assert [(p['clock']['part'], p['clock']['role']) for p in props] == [
        ('Y1', 'crystal'), ('Y2', 'oscillator')]
    assert [p['measured']['min_gap_mm'] for p in props] == [8.4143, 8.9096]
    print("  PASS: rp2350 VREG_LX + SW vs Y1 8.41 / Y2 8.91 mm, all by pin "
          "function")


def test_every_rule_is_the_routers():
    import keep_away as ka
    n = 0
    for path in (WATCHY, ULX, RP):
        pcb = _parse(path)
        ids = {net.name: nid for nid, net in pcb.nets.items() if nid}
        for p in _sec(path)['keep_away']:
            (rule,) = ka.parse_keep_away_rules(p['rule'])
            assert rule.spec == p['rule'] and rule.gap == p['gap_mm'], p
            (row,) = ka.brief_rule_rows([p['brief_row']])
            assert row['rule'] == p['rule'] and row['why'], row
            with contextlib.redirect_stdout(io.StringIO()):
                ((agg, vic, _g),), notes, unmatched = ka.resolve_rule_sides(
                    pcb, (rule,))
            assert not notes and not unmatched, (notes, unmatched)
            assert agg == {ids[s['net']] for s in p['switch_nodes']}, p
            assert vic == {ids[x] for x in p['clock']['nets']}, p
            n += 1
    assert n == 6, n
    print(f"  PASS: {n} proposed rules parse, load as brief rows and resolve "
          f"to exactly their nets")


def test_the_gap_is_derived_and_flags_no_same_face_human_pair():
    import routing_defaults
    from placement import floorplan as fp
    from placement.emc_roles import KEEP_AWAY_GAP_MM
    assert KEEP_AWAY_GAP_MM == 2 * routing_defaults.KEEP_AWAY_FREE, \
        (KEEP_AWAY_GAP_MM, routing_defaults.KEEP_AWAY_FREE)
    inside = []
    for path in (WATCHY, ULX, RP):
        props = _sec(path)['keep_away']
        intent = fp.intent_from_dict(
            {'schema': 1, 'kind': fp.KIND, 'units': 'mm',
             'keep_away': [{'rule': p['rule']} for p in props]}, path)
        with contextlib.redirect_stdout(io.StringIO()):
            res = fp.grade(intent, _parse(path), path)
        inside += [(os.path.basename(path), v.message)
                   for v in res.violations if v.rule == 'keep_away'
                   and not v.measured['cross_face']]
        n_inside = sum(1 for v in res.violations if v.rule == 'keep_away')
        assert n_inside == (2 if path == ULX else 0), (path, n_inside)
    assert not inside, inside
    print("  PASS: gap = 2 x keep_away_free; the only pairs inside it on the "
          "human layouts are ulx3s Y1 over U4 / L2, on opposite faces")


def test_pasted_into_a_brief_it_is_graded_and_routed():
    import keep_away as ka
    for src, warn in ((WATCHY, None),
                      (ULX, "Y1 pad 3 (F.Cu) on CLK_25MHz is 1.05mm from U4 "
                            "pad 3 (B.Cu) on /power/L2, inside the 3.00mm "
                            "keep-away; the pads are on opposite faces")):
        with tempfile.TemporaryDirectory() as td:
            board = os.path.join(td, 'b.kicad_pcb')
            rc, out = _run(_py('py_router/copy_board.py', src, board))
            assert rc == 0, out[-1500:]
            ctx = os.path.join(td, 'ctx.json')
            rc, out = _run(_py(CONTEXT, board, '--json', '-o', ctx))
            assert rc == 0, out[-1500:]
            with open(run_utils.evidence(ctx, 'the context document'),
                      encoding='utf-8') as fh:
                props = json.load(fh)['emc_roles']['keep_away']
            assert len(props) == 2, props
            brief = os.path.join(td, 'b.design-brief.json')
            with open(brief, 'w', encoding='utf-8') as fh:
                json.dump({'schema': 1, 'kind': 'design-brief',
                           'units': 'mm',
                           'keep_away': [p['brief_row'] for p in props]}, fh)
            intent = os.path.join(td, 'intent.json')
            rc, out = _run(_py('py_tools/check_floorplan.py', board,
                               '--emit-intent', intent))
            assert rc == 0, out[-2000:]
            assert '2 keep-away rule(s)' in out, out[:2000]
            js = os.path.join(td, 'grade.json')
            rc, out = _run(_py('py_tools/check_floorplan.py', board,
                               '--intent', run_utils.evidence(intent, 'intent'),
                               '--json', js))
            with open(run_utils.evidence(js, 'the grade'),
                      encoding='utf-8') as fh:
                g = json.load(fh)
            got = {m['rule']: m for m in g['keep_away']}
            for p in props:
                m = got[p['rule']]
                assert m['source'] == 'brief', m
                for k in ('state', 'min_gap_mm', 'closest',
                          'part_pairs_inside'):
                    assert m.get(k) == p['measured'].get(k), (k, m, p)
            cl = [c for c in g['brief_coverage']['clauses']
                  if c['kind'] == 'keep_away']
            assert [c['state'] for c in cl] == ['graded', 'graded'], cl
            if warn:
                assert warn in out, out[-3000:]
            else:
                assert '[warn ] keep_away:' not in out, out[-3000:]
            rules, path = ka.read_brief_keep_away(board)
            assert list(rules) == [p['rule'] for p in props] and \
                os.path.samefile(path, brief), (rules, path)
    print("  PASS: pasted into a brief, check_floorplan grades each rule with "
          "the reading printed beside it, and route.py's reader reads them")


def test_the_watchy_move_reads_inside():
    with tempfile.TemporaryDirectory() as td:
        base = os.path.join(td, 'base.kicad_pcb')
        moved = os.path.join(td, 'y1.kicad_pcb')
        rc, out = _run(_py('py_router/copy_board.py', WATCHY, base))
        assert rc == 0, out[-1500:]
        rc, out = _run(_py('py_placer/place_pose.py', base, moved, 'set', 'Y1',
                           '--near', '78.5', '106.4', '--rot', '270',
                           '--snap'))
        from kicad_parser import parse_kicad_pcb
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            pcb = parse_kicad_pcb(run_utils.evidence(moved, 'the moved board'))
        y1 = _sec(moved, pcb)['keep_away'][0]
    assert y1['rule'] == WATCHY_RULES[0], y1['rule']
    assert y1['inside_now'] is True, y1['measured']
    assert _closest(y1) == ('Y1.1', 'F.Cu', 'L2.1', 'F.Cu', False), \
        _closest(y1)
    assert abs(y1['measured']['min_gap_mm'] - 0.91) < 0.005, y1['measured']
    print(f"  PASS: #1253's move reads inside: Y1.1 "
          f"{y1['measured']['min_gap_mm']} mm from L2.1")


def test_no_proposal_without_both_sides():
    for path, why in ((FLAT, 'a boost, no clock'),
                      (ESP, 'a crystal, no switch node')):
        sec = _sec(path)
        assert sec['keep_away'] == [] and sec['keep_away_skipped'] == [], \
            (path, why, sec['keep_away'], sec['keep_away_skipped'])
    print("  PASS: flat_hierarchy (no clock) and esp_prog (no switch node) "
          "propose nothing")


def test_the_cli_carries_the_proposals():
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'ctx.json')
        rc, log = _run(_py(CONTEXT, WATCHY, '--json', '-o', out))
        assert rc == 0, log[-1500:]
        with open(run_utils.evidence(out, 'the --json document'),
                  encoding='utf-8') as fh:
            doc = json.load(fh)
    sec = doc['emc_roles']
    assert [p['rule'] for p in sec['keep_away']] == WATCHY_RULES
    assert 'keep_away' in doc['sources']['emc_roles'], doc['sources']
    rc, md = _run(_py(CONTEXT, ULX, '--md'))
    assert rc == 0, md[-1500:]
    assert '### Keep-away proposals (derived default gap -- NOT declared)' \
        in md, md[-3000:]
    assert ('| `/power/L1,/power/L2,/power/L3:CLK_25MHz:3` | Y1 crystal or '
            'oscillator? (candidate) |') in md, md[-3000:]
    assert '1.055 mm, Y1.3 (F.Cu) to U4.3 (B.Cu), opposite faces -- INSIDE ' \
        'the proposed gap' in md, md[-3000:]
    assert '"rule": "/power/L1,/power/L2,/power/L3:CLK_25MHz:3"' in md
    print("  PASS: --json carries emc_roles.keep_away; --md prints the "
          "proposals and the brief rows")


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
