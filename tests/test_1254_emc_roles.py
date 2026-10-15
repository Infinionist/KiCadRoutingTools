#!/usr/bin/env python3
"""#1254: `board_context --json` proposes `proximity[]` rows for hot loops,
crystals and ESD arrays (`emc_roles`), REPORT ONLY.

`proximity[]` (#902) can grade the placement facts that decide EMC -- Cin to
VIN/PGND, L to SW, a crystal to its load pins, an ESD array to its connector
-- and nothing named those parts. `placement.emc_roles` reads them off PIN
FUNCTIONS (and the role table for crystals), measures every leg pad edge to
pad edge (`legality.pad_rect` + `rect_gap`), and writes a ready row with
`max_mm` null. It writes no intent key: #902's "never emitted" stands.

Cases:
* rp2350_fpga_eensy_prePlane, the issue's table: buck U7 (C26+ -> VIN
  0.495, C26- -> PGND 1.461, L2 -> SW 0.764), ESD U3 -> J1 (0.940 / 0.940),
  crystal Y1 -> U6 (XOUT 1.371, XIN 3.143), both pads' sides on each leg;
* the rows are the schema `rule_proximity` grades: with `max_mm` just above
  the worst leg every row passes, just below it the row fails, and the gaps
  the GRADE measured are the legs printed beside the row;
* ulx3s: the three TLV62569 carry no pin function and are `unknown` with no
  row (a pin number is never guessed); U8, an ADC fed through an LC filter,
  is not a candidate (its inductor net is decoupled, a switch node never is);
* watchy's 32.768 kHz crystal is reported on `/32K_P` / `/32K_N`, which
  `net_queries.is_power_net_name` reads as rails -- the trap the detector
  must not fall into;
* legs, never a loop area: no key in the section names an area;
* a part with I/O pins and any other named function (CLK) is not an ESD
  array;
* flat_hierarchy's LT1373 is a boost (its inductor runs from VIN to SW);
* the CLI: `--json` carries `emc_roles`, `--md` its section.

    python3 -X utf8 tests/test_1254_emc_roles.py [case ...]
"""
import contextlib
import copy
import io
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_utils  # noqa: E402

ROOT = run_utils.ROOT_DIR
for _sub in ('py_router', 'py_tools', 'py_placer'):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

RUN_ALL_TIMEOUT = 600

BOARDS = os.path.join(ROOT, 'kicad_files')
RP = os.path.join(BOARDS, 'rp2350_fpga_eensy_prePlane.kicad_pcb')
ULX = os.path.join(BOARDS, 'ulx3s.kicad_pcb')
WATCHY = os.path.join(BOARDS, 'watchy.kicad_pcb')
FLAT = os.path.join(BOARDS, 'flat_hierarchy.kicad_pcb')
CONTEXT = os.path.join(ROOT, 'py_tools', 'board_context.py')
_CACHE = {}


def _parse(path):
    if path not in _CACHE:
        from kicad_parser import parse_kicad_pcb
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            _CACHE[path] = parse_kicad_pcb(path)
    return _CACHE[path]


def _roles(path, pcb=None):
    from placement.emc_roles import emc_roles
    return emc_roles(pcb if pcb is not None else _parse(path))


def _legs(role):
    return {(l['from']['ref'] + '.' + l['from']['pad'],
             l['to']['ref'] + '.' + l['to']['pad']): l
            for pr in role['proximity'] for l in pr['legs']}


def _one(sec, role, part):
    got = [r for r in sec['roles'] if r['role'] == role and r['part'] == part]
    assert len(got) == 1, [(r['role'], r['part']) for r in sec['roles']]
    return got[0]


def test_rp2350_reports_the_issues_table():
    sec = _roles(RP)
    want = {('buck', 'U7'): {('C26.1', 'U7.2'): 0.495, ('C26.2', 'U7.1'): 1.461,
                             ('L2.1', 'U7.7'): 0.764},
            ('esd array', 'U3'): {('U3.6', 'J1.B6'): 0.94,
                                  ('U3.4', 'J1.B7'): 0.94},
            ('crystal', 'Y1'): {('Y1.1', 'U6.22'): 1.371,
                                ('Y1.3', 'U6.21'): 3.143}}
    for (role, part), legs in want.items():
        r = (_one(sec, 'switching converter', part) if role == 'buck'
             else _one(sec, role, part))
        if role == 'buck':
            assert r['topology'] == 'buck' and r['inductor'] == 'L2' and \
                r['input_cap'] == 'C26', r
            assert r['pins'] == {'sw': ['7'], 'vin': ['2'], 'gnd': ['1']}, r
        got = _legs(r)
        assert {k: got[k]['gap_mm'] for k in legs} == legs, \
            {k: v['gap_mm'] for k, v in got.items()}
        for k in legs:
            assert (got[k]['from']['side'], got[k]['to']['side']) == \
                ('F', 'F'), got[k]
        for pr in r['proximity']:
            assert pr['row']['max_mm'] is None, pr
    esd = _one(sec, 'esd array', 'U3')
    assert esd['connector'] == 'J1', esd
    assert esd['proximity'][0]['row']['pads'] == {
        'U3': ['4', '6'], 'J1': ['A6', 'A7', 'B6', 'B7']}, esd['proximity']
    print("  PASS: U7 buck 0.495/1.461/0.764, U3 -> J1 0.940/0.940, "
          "Y1 -> U6 1.371/3.143, every leg F/F, every max_mm null")


def test_the_rows_are_what_rule_proximity_grades():
    from placement import floorplan as fp
    pcb = _parse(RP)
    rows = [pr for r in _roles(RP)['roles'] for pr in r['proximity']]
    assert len(rows) >= 5, rows

    def grade(slack):
        claims = []
        for pr in rows:
            row = dict(pr['row'], max_mm=round(pr['worst_leg_mm'] + slack, 3))
            claims.append(row)
        intent = fp.intent_from_dict(
            {'schema': 1, 'kind': fp.KIND, 'units': 'mm',
             'proximity': claims}, RP)
        with contextlib.redirect_stdout(io.StringIO()):
            return fp.grade(intent, pcb, RP)
    ok = grade(+0.002)
    assert not [v for v in ok.violations if v.rule.startswith('proximity')], \
        [v.message for v in ok.violations if v.rule.startswith('proximity')]
    bad = grade(-0.01)
    failed = {v.ref for v in bad.violations if v.rule == 'proximity'}
    assert failed == {pr['row']['ref'] for pr in rows}, failed
    # The gaps the GRADE measured are the legs printed beside each row.
    measured = {}
    for m in bad.proximity_measured:
        measured.setdefault((m['ref'], m['near']), set()).add(
            round(m['gap_mm'], 3))
    for pr in rows:
        key = (pr['row']['ref'], pr['row']['near'])
        assert measured[key] == {l['gap_mm'] for l in pr['legs']}, (
            key, measured[key], pr['legs'])
    print(f"  PASS: {len(rows)} rows load as proximity[] claims, pass at "
          f"their worst leg and fail below it; the grade measured the "
          f"printed legs")


def test_ulx3s_bucks_without_pin_functions_are_unknown():
    sec = _roles(ULX)
    unk = {r['part']: r for r in sec['roles'] if r['role'] == 'unknown'}
    for u in ('U3', 'U4', 'U5'):
        r = unk.get(u)
        assert r and r['candidate'] == 'switching converter', sec['roles']
        assert r['value'].startswith('TLV62569'), r
        assert not r['proximity'], r
        assert 'never guessed' in r['why'], r
    assert not [r for r in sec['roles'] if r['role'] == 'switching converter'
                and r['part'] in ('U3', 'U4', 'U5')]
    # U8 (MAX11125) shares an inductor net, but that net is decoupled: an
    # LC filter's output, not a switch node.
    assert 'U8' not in unk, unk.get('U8')
    print("  PASS: ulx3s U3/U4/U5 unknown with no row; U8 not a candidate")


def test_a_crystal_on_rail_shaped_nets_is_still_a_crystal():
    from net_queries import is_power_net_name
    assert is_power_net_name('/32K_P') and is_power_net_name('/32K_N'), \
        'the trap is not armed: the name predicate no longer reads them as rails'
    y1 = _one(_roles(WATCHY), 'crystal', 'Y1')
    nets = {l['from']['net'] for l in y1['proximity'][0]['legs']}
    assert nets == {'/32K_P', '/32K_N'}, nets
    print(f"  PASS: watchy Y1 -> {y1['ic']} on {sorted(nets)}, which "
          f"is_power_net_name calls rails")


def test_legs_never_an_area():
    def keys(x):
        if isinstance(x, dict):
            for k, v in x.items():
                yield k
                yield from keys(v)
        elif isinstance(x, list):
            for v in x:
                yield from keys(v)
    for path in (RP, ULX, WATCHY):
        found = [k for k in keys(_roles(path)) if 'area' in str(k).lower()]
        assert not found, (path, found)
    print("  PASS: no key in emc_roles names an area")


def test_io_pins_beside_another_function_are_not_an_esd_array():
    pcb = copy.deepcopy(_parse(RP))
    pad = next(p for p in pcb.footprints['U3'].pads if p.pad_number == '5')
    pad.pinfunction = 'CLK'
    sec = _roles(RP, pcb)
    assert not [r for r in sec['roles'] if r['role'] == 'esd array'], \
        sec['roles']
    print("  PASS: U3 with a CLK pin is not an ESD array")


def test_flat_hierarchy_lt1373_is_a_boost():
    r = _one(_roles(FLAT), 'switching converter', 'U4')
    assert r['topology'] == 'boost' and r['inductor'] == 'L1', r
    assert r['pins']['sw'] == ['8'], r
    print("  PASS: LT1373 U4 is a boost (L1 runs from VIN to SW)")


def test_the_cli_carries_the_section():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'ctx.json')
        r = subprocess.run([sys.executable, '-X', 'utf8', CONTEXT, RP,
                            '--json', '-o', out], capture_output=True,
                           text=True, encoding='utf-8', errors='replace',
                           cwd=ROOT)
        assert r.returncode == 0, r.stderr[-1500:]
        run_utils.evidence(out, 'the --json document')
        with open(out, encoding='utf-8') as fh:
            doc = json.load(fh)
    sec = doc['emc_roles']
    assert sec['counts'].get('switching converter') == 2, sec['counts']
    assert 'emc_roles' in doc['sources'], sorted(doc['sources'])
    md = subprocess.run([sys.executable, '-X', 'utf8', CONTEXT, RP, '--md'],
                        capture_output=True, text=True, encoding='utf-8',
                        errors='replace', cwd=ROOT)
    assert md.returncode == 0, md.stderr[-1500:]
    assert '## EMC roles (report only)' in md.stdout, md.stdout[:500]
    assert '| Cin+ -> VIN | C26.1 | U7.2 (VIN) |' in md.stdout
    print(f"  PASS: --json emc_roles counts {sec['counts']}; --md prints "
          f"the section")


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
