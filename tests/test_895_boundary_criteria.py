#!/usr/bin/env python3
"""#895: the numbers the boundary-criteria worked example quoted, re-derived.

The worked example (`references/boundary-criteria.md` of the retired
plan-pcb-placement-and-routing skill, #1009) quoted a figure per criterion from
one tracked fixture. Two of them started life as journal figures -- a seam of
0.183mm and a pair of 9.3mm -- that re-measured as -0.133mm and 8.10mm with the
shipped instruments. The page is gone; the measurements it pinned are
properties of the instruments, so they stay pinned here, as constants, against
the same fixture: a number that stops being true fails the suite instead of
ageing quietly.

    python3 tests/test_895_boundary_criteria.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, ROOT)

RUN_ALL_TIMEOUT = 600

#: The board the example was drawn from.
FIXTURE = os.path.join(ROOT, 'tests', 'fixtures', 'run25',
                       'esp_prog_lap5.kicad_pcb')
#: The declared clauses criterion 3 is worked from.
BRIEF = os.path.join(ROOT, 'tests', 'fixtures', '902',
                     'esp_prog_proximity.design-brief.json')

#: The figures the worked example quoted, as it quoted them.
PAIR_SPAN = '8.10'
IFACE_SPAN = '12.70'
#: ref -> (body extents, the rung they came from)
BODIES = {'U1': ({'8.51', '7.62'}, 'silk'), 'USB1': ({'7.12', '7.40'}, 'fab')}
SEAM = -0.133
SEAM_SOURCES = {'silk', 'fab'}
COLD_TOP_AREA = 24.0
CENTROID_PCT = 3.0
#: (subject, near, pad) -> the gap criterion 3 quoted, two decimals.
PROXIMITY = {('Y1', 'U1', '1'): '2.69', ('Y1', 'U1', '2'): '1.62',
             ('C1', 'U2', '1'): '1.12', ('C3', 'U2', '1'): '0.29'}
#: The regulator's own caps and the partner the tether election gives them.
#: Criterion 3 cited C3 -> U1 1.83 mm and C1 -> USB1 2.03 mm as WRONG
#: partners: U2 is a 3-pad SOT-89 regulator, and before #1249 a part under
#: four copper pads could never be elected. It is a supply owner now
#: (`groups.supply_owners`), so both caps elect the regulator they serve.
REGULATOR_PARTNERS = {'C1': ('U2', '0.95'), 'C3': ('U2', '0.95')}

FAILURES = []


def check(name, cond, detail=''):
    if cond:
        print(f"  PASS: {name}")
    else:
        FAILURES.append(f"{name} -- {detail}")
        print(f"  FAIL: {name} -- {detail}")


def _context():
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'py_tools',
                                                     'board_context.py'),
                        FIXTURE, '--json'],
                       capture_output=True, text=True)
    check('board_context exits 0', r.returncode == 0, r.stderr[-300:])
    return json.loads(r.stdout)


def test_criterion_1_and_2_are_what_the_instrument_reports():
    """`span_mm` and `verdict`, re-derived from the tracked fixture."""
    doc = _context()
    rows = {(x['a'], x['b'], x['scope']): x for x in doc['pin_order']['rows']}
    pair = rows.get(('U1', 'USB1', 'pair /D_P//D_N'))
    iface = rows.get(('U1', 'USB1', 'interface'))
    check('the pair row exists', pair is not None, sorted(rows))
    if pair:
        check(f'the pair span is {PAIR_SPAN} mm',
              f"{pair['span_mm']:.2f}" == PAIR_SPAN,
              f"measured {pair['span_mm']}")
        check('...and the pair reads CROSSED', pair['verdict'] == 'CROSSED',
              pair['verdict'])
        check('...with one inversion', pair['inversions'] == 1,
              pair['inversions'])
    check('the interface row exists', iface is not None, sorted(rows))
    if iface:
        check(f'the interface span is {IFACE_SPAN} mm',
              f"{iface['span_mm']:.2f}" == IFACE_SPAN,
              f"measured {iface['span_mm']}")


def test_criterion_1_bodies():
    """The denominator, not only the span: both bodies and their rungs."""
    parts = {p['ref']: p for p in _context().get('parts', [])}
    for who, (extents, rung) in BODIES.items():
        body = (parts.get(who) or {}).get('body_mm')
        check(f'{who} reports a body', bool(body), sorted(parts))
        if not body:
            continue
        check(f'{who} body extents are {sorted(extents)} mm',
              {f'{mm:.2f}' for mm in body} == extents, f'measured {body}')
        src = parts[who].get('body_source')
        check(f'...from the {rung} rung', str(src) == rung, str(src))


def test_criterion_5_is_what_the_instrument_reports():
    """The tightest body seam, signed, with its sources."""
    from kicad_parser import parse_kicad_pcb
    from placement import legality
    pcb = parse_kicad_pcb(FIXTURE)
    found = legality.grade_body_overlap(pcb, 0.15, (), FIXTURE)
    seam = found.get('body_seam') or {}
    check('a seam was measured', bool(seam), found.keys())
    if seam:
        check(f'the seam is {SEAM} mm (an OVERLAP)', seam['mm'] == SEAM,
              f"measured {seam['mm']}")
        check('...between a silk and a fab rung',
              {seam['source_a'], seam['source_b']} == SEAM_SOURCES,
              f"{seam['source_a']}/{seam['source_b']}")


def test_criterion_6_is_what_the_instrument_reports():
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'py_tools',
                                                     'check_pockets.py'),
                        FIXTURE, '--bin', '5'],
                       capture_output=True, text=True)
    line = [x for x in r.stdout.splitlines() if x.startswith('JSON_SUMMARY:')]
    check('check_pockets emitted a summary', bool(line), r.stderr[-200:])
    if not line:
        return
    s = json.loads(line[0].split('JSON_SUMMARY: ', 1)[1])
    check(f'the emptiest region is {COLD_TOP_AREA} mm2',
          s['cold_top_area_mm2'] == COLD_TOP_AREA, s['cold_top_area_mm2'])
    check(f'the centroid offset is {CENTROID_PCT} %',
          round(s['centroid_offset_frac'] * 100, 1) == CENTROID_PCT,
          s['centroid_offset_frac'])


def test_the_criteria_keys_are_emitted():
    """Every dotted path the criteria read resolves against the real output.

    Criterion 6 once shipped reading `hot[].ratio`, a LOCAL inside
    check_pockets; the emitted key is `windows[].ratio`.
    """
    ctx_doc = _context()
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        pk_path = os.path.join(tmp, 'pockets.json')
        subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'py_tools', 'check_pockets.py'),
                        FIXTURE, '--bin', '5', '--json', pk_path],
                       capture_output=True, text=True)
        check('check_pockets wrote a document', os.path.isfile(pk_path))
        if not os.path.isfile(pk_path):
            return
        with open(pk_path, encoding='utf-8') as fh:
            pk_doc = json.load(fh)

    paths = (
        ('pin_order.rows[].span_mm', ctx_doc,
         lambda d: d['pin_order']['rows'][0]['span_mm']),
        ('pin_order.rows[].verdict', ctx_doc,
         lambda d: d['pin_order']['rows'][0]['verdict']),
        ('parts[].body_mm', ctx_doc, lambda d: d['parts'][0]['body_mm']),
        ('parts[].pads_by_face', ctx_doc,
         lambda d: d['parts'][0]['pads_by_face']),
        ('parts[].partners', ctx_doc, lambda d: d['parts'][0]['partners']),
        ('cold_regions[0].area_mm2', pk_doc,
         lambda d: d['cold_regions'][0]['area_mm2']),
        ('windows[0].ratio', pk_doc, lambda d: d['windows'][0]['ratio']),
        ('arrangement.sides[<layer>].offset_mm', pk_doc,
         lambda d: next(iter(d['arrangement']['sides'].values()))['offset_mm']),
    )
    for path, doc, resolve in paths:
        try:
            resolve(doc)
            check(f'the instrument emits `{path}`', True)
        except (KeyError, IndexError, TypeError, StopIteration) as exc:
            check(f'the instrument emits `{path}`', False,
                  f'{type(exc).__name__}: {exc}')

    # The seam comes from render_placement, which is too slow to run here; its
    # key is asserted at the emit site instead of by re-rendering the board.
    with open(os.path.join(ROOT, 'py_tools', 'render_placement.py'),
              encoding='utf-8') as fh:
        rp = fh.read()
    check("render_placement emits 'b_body_seam'", "'b_body_seam':" in rp)


def test_criterion_3_is_what_the_grader_reports():
    """Every distance criterion 3 quoted, re-derived through the real grader.

    The brief is re-read with every `max_mm` tightened to 0.001, so each
    declared row reports its measured gap instead of only the row that exceeds
    its own limit. A limit decides what is REPORTED, never what is measured.
    """
    import tempfile

    from kicad_parser import parse_kicad_pcb
    from placement import groups

    with open(BRIEF, encoding='utf-8') as fh:
        brief = json.load(fh)
    for row in brief['proximity']:
        row['max_mm'] = 0.001

    with tempfile.TemporaryDirectory() as tmp:
        tight = os.path.join(tmp, 'tight.json')
        intent = os.path.join(tmp, 'intent.json')
        graded = os.path.join(tmp, 'graded.json')
        with open(tight, 'w', encoding='utf-8') as fh:
            json.dump(brief, fh)
        cf = os.path.join(ROOT, 'py_tools', 'check_floorplan.py')
        emit = subprocess.run([sys.executable, '-X', 'utf8', cf, FIXTURE,
                               '--brief', tight, '--emit-intent', intent],
                              capture_output=True, text=True)
        check('check_floorplan emitted an intent', os.path.isfile(intent),
              emit.stderr[-300:])
        if not os.path.isfile(intent):
            return
        subprocess.run([sys.executable, '-X', 'utf8', cf, FIXTURE,
                        '--brief', tight, '--intent', intent,
                        '--json', graded], capture_output=True, text=True)
        check('check_floorplan wrote a graded document',
              os.path.isfile(graded))
        if not os.path.isfile(graded):
            return
        with open(graded, encoding='utf-8') as fh:
            doc = json.load(fh)

    # Keyed on the PAIR, not on the number: two measured gaps round to the
    # same two decimals (0.292 and the transistor pair's 0.295).
    gaps = {}
    for v in doc['violations']:
        m = v.get('measured') or {}
        if v.get('rule') == 'proximity' and 'gap_mm' in m:
            gaps[(v.get('ref'), m.get('near'), m.get('pad'))] = m['gap_mm']
    check('the rule measured every declared row', len(gaps) >= 5, sorted(gaps))
    for key, want in PROXIMITY.items():
        gap = gaps.get(key)
        check(f'{key[0]} pad {key[2]} -> {key[1]} was measured', gap is not None,
              sorted(gaps))
        if gap is not None:
            check(f'...at {want}mm', f'{gap:.2f}' == want, f'measured {gap}')
    check('the body-basis pair was measured too',
          ('Q1', 'Q2', None) in gaps, sorted(gaps))

    # The two caps criterion 3 cited with WRONG partners (C3 -> U1 1.83,
    # C1 -> USB1 2.03) now elect the regulator they serve (#1249).
    tethers = groups.decap_tethers(parse_kicad_pcb(FIXTURE))
    got = {cap: (ic, f'{mm:.2f}') for ic, rows in (tethers or {}).items()
           for cap, mm in rows if cap in REGULATOR_PARTNERS}
    check('the regulator caps C1/C3 tether to U2 at 0.95mm each',
          got == REGULATOR_PARTNERS, got)


TESTS = [test_criterion_1_and_2_are_what_the_instrument_reports,
         test_criterion_1_bodies,
         test_criterion_3_is_what_the_grader_reports,
         test_criterion_5_is_what_the_instrument_reports,
         test_criterion_6_is_what_the_instrument_reports,
         test_the_criteria_keys_are_emitted]


def main():
    for t in TESTS:
        print(f'--- {t.__name__}')
        try:
            t()
        except Exception as exc:                             # noqa: BLE001
            check(f'{t.__name__} ran to completion', False,
                  f'{type(exc).__name__}: {exc}')
    print(f"\n{'FAIL' if FAILURES else 'PASS'}: #895 boundary criteria, "
          f"{len(FAILURES)} failure(s)")
    for f in FAILURES:
        print(f'  - {f}')
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
