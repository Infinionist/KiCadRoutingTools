#!/usr/bin/env python3
"""#1105: stage 3.5 is ON by default when the input board is a pile, and a
board that is not a pile seeds exactly as before.

The default is the maintainer's call on the second pile pilot
(tests/1105_pile_ab_prereg_v2.json), scoped to the population that pilot
measured: `stage_unaided` piles, which `PlacementState.pile` -- the one pile
test, and the verdict `emit_intent` withholds pose claims on -- reads as
piles. What each case pins:

* on the committed run-29 pile and on the pilot's own esp_prog pile, a seed
  with the flag omitted arms stage 3.5 (`late['armed_by'] == 'pile'`),
  claims caps at it, and writes exactly the board the A/B rows' ON arm
  writes -- the pilot's measured candidate, within-limit at
  `after_last_owner` (`late['variant']`); the explicit OFF arm claims none
  there and says it was turned off;
* the EXPLICIT flag is still after-ics, on a pile too: on the run-29 pile
  it keeps the two far seats the pile default undoes;
* on placed boards (esp_prog, watchy), seeded whole as `place_seed --force`
  does, the omitted flag writes exactly the board the explicit OFF arm
  writes, and `late` says the input is not a pile;
* `decap_pin_forecast` arms stage 3.5 by the same verdict, and an explicit
  value still wins either way;
* place_seed with the flag omitted on the pile prints that 3.5 ran by the
  pile default, and `--no-decap-claim-after-ics` turns it off.

    python3 -X utf8 tests/test_1105_pile_default.py [name-substring ...]
"""
import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, TESTS_DIR)

RUN_ALL_TIMEOUT = 900

BOARDS = os.path.join(ROOT, 'kicad_files')
ESP = os.path.join(BOARDS, 'esp_prog.kicad_pcb')
WATCHY = os.path.join(BOARDS, 'watchy.kicad_pcb')
PILE = os.path.join(TESTS_DIR, 'fixtures', '959', 'run29_pile.kicad_pcb')
PLACE_SEED = os.path.join(ROOT, 'py_placer', 'place_seed.py')
PREREG_V2 = os.path.join(TESTS_DIR, '1105_pile_ab_prereg_v2.json')
SOURCES = ('kicad', 'sheet')


def _doc(board):
    """The intent a run would seed from: a pile's own, with esp_prog as the
    --decaps-from reference (a pile withholds the limit); a placed board's
    own, armed with a 3 mm decap limit."""
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan as fp
    with contextlib.redirect_stdout(io.StringIO()):
        if board == PILE:
            return fp.emit_intent(parse_kicad_pcb(PILE), PILE,
                                  decaps_from=ESP)
        doc = fp.emit_intent(parse_kicad_pcb(board), board)
    doc['decaps'] = dict(doc.get('decaps') or {}, max_distance_mm=3.0)
    return doc


def _seed(board, intent, **kw):
    """(poses, unseated, late) of one seed, seed '0'."""
    from kicad_parser import parse_kicad_pcb
    from placement import seeder
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        pcb = parse_kicad_pcb(board)
        res = seeder.seed_from_intent(
            pcb, board, intent, random.Random('0'), group_sources=SOURCES,
            clearance=0.2, **kw)
    poses = {p['reference']: (p['new_x'], p['new_y'], p['new_rotation'])
             for p in res['placements']}
    return poses, res['unseated'], res['decap_stage']['late']


def _intent(doc, board):
    from placement import floorplan as fp
    return fp.intent_from_dict(doc, board)


def _candidate():
    with open(PREREG_V2, encoding='utf-8') as fh:
        cand = json.load(fh)['candidate']
    return {k: v for k, v in cand.items() if k.isupper()}


class _knobs:
    """Set seeder module knobs for one block, and restore them."""

    def __init__(self, **kw):
        self.kw, self.saved = kw, {}

    def __enter__(self):
        from placement import seeder
        for k, v in self.kw.items():
            self.saved[k] = getattr(seeder, k)
            setattr(seeder, k, v)

    def __exit__(self, *exc):
        from placement import seeder
        for k, v in self.saved.items():
            setattr(seeder, k, v)
        return False


def _assert_pile_default(board, intent, label, **kw):
    """The omitted flag on a pile is the A/B rows' ON arm (the explicit
    claim with the pre-registered candidate's knobs), bit for bit, and
    claims; the explicit OFF arm claims nothing at 3.5."""
    from placement import seeder
    cand = _candidate()
    assert seeder.DECAP_PILE_WITHIN_LIMIT == cand['DECAP_LATE_WITHIN_LIMIT'], (
        seeder.DECAP_PILE_WITHIN_LIMIT, cand)
    assert seeder.DECAP_LATE_AT == cand['DECAP_LATE_AT'], cand
    dflt = _seed(board, intent, **kw)
    with _knobs(**cand):
        on = _seed(board, intent, decap_claim_after_ics=True, **kw)
    off = _seed(board, intent, decap_claim_after_ics=False, **kw)
    late = dflt[2]
    assert late['armed'] is True and late['armed_by'] == 'pile', late
    assert late['variant'] == 'within-limit', late
    assert late['claimed'] > 0 and late['caps'], late
    assert dflt[:2] == on[:2], (
        label, sorted(r for r in dflt[0] if dflt[0][r] != on[0].get(r)))
    assert {k: v for k, v in late.items() if k != 'armed_by'} == \
        {k: v for k, v in on[2].items() if k != 'armed_by'}, (late, on[2])
    assert off[2]['armed'] is False and off[2]['claimed'] == 0, off[2]
    assert 'off for this run' in off[2]['reason'], off[2]
    assert dflt[0] != off[0], f"{label}: the default seeded as OFF"
    return late


def test_the_run29_pile_claims_by_default():
    late = _assert_pile_default(PILE, _intent(_doc(PILE), PILE), 'run29')
    print(f"  PASS: run-29 pile -- the omitted flag is the ON arm "
          f"(3.5 claimed {late['claimed']}: {', '.join(late['caps'])}); "
          f"OFF claims 0")


def test_the_explicit_flag_is_after_ics_on_a_pile():
    """`--decap-claim-after-ics` runs the variant it always ran: on the
    run-29 pile it keeps the seats past the decap limit that the pile
    default undoes."""
    intent = _intent(_doc(PILE), PILE)
    dflt = _seed(PILE, intent)
    flag = _seed(PILE, intent, decap_claim_after_ics=True)
    lf, ld = flag[2], dflt[2]
    assert lf['armed_by'] == 'set' and lf['variant'] == 'after-ics', lf
    assert ld['variant'] == 'within-limit' and ld['declined'], ld
    assert not lf['declined'] and set(ld['declined']) <= set(lf['caps']), (
        lf, ld)
    assert flag[0] != dflt[0], "the flag seeded as the pile default"
    print(f"  PASS: run-29 pile -- the flag (after-ics) claims "
          f"{', '.join(lf['caps'])}; the pile default (within-limit) "
          f"declines {', '.join(ld['declined'])}")


def test_the_pilots_esp_prog_pile_claims_by_default():
    """The pilot's own basis: `_pile_inputs` stages the pile, the CLI emits
    the intent, and the seed takes place_seed's scope."""
    import test_placement_ab as AB
    with tempfile.TemporaryDirectory() as td, \
            contextlib.redirect_stdout(io.StringIO()):
        pile, intent, _doc_, refs = AB._pile_inputs(ESP, td)
        kw = {'seed_refs': refs} if refs is not None else {}
        late = _assert_pile_default(pile, intent, 'esp_prog pile', **kw)
    print(f"  PASS: the pilot's esp_prog pile -- the omitted flag is the ON "
          f"arm (3.5 claimed {late['claimed']}); OFF claims 0")


def test_a_placed_board_seeds_as_before():
    from placement import seeder
    for board in (ESP, WATCHY):
        intent = _intent(_doc(board), board)
        dflt = _seed(board, intent)
        off = _seed(board, intent, decap_claim_after_ics=False)
        assert dflt[:2] == off[:2], (
            board, sorted(r for r in dflt[0] if dflt[0][r] != off[0].get(r)))
        late = dflt[2]
        assert late['armed'] is False and late['armed_by'] == 'not a pile', \
            late
        assert late['claimed'] == 0 and 'not a pile' in late['reason'], late
        # The forecast for the same board says the same.
        from kicad_parser import parse_kicad_pcb
        from placement import floorplan as fp
        pcb = parse_kicad_pcb(board)
        blocks, _p = fp.resolve_blocks(intent, pcb, SOURCES)
        f = seeder.decap_pin_forecast(pcb, intent, blocks)
        assert f['late_armed'] is False and \
            f['late_armed_by'] == 'not a pile', f
    print("  PASS: esp_prog and watchy seed bit-identically with the flag "
          "omitted and with it off; 3.5 says the input is not a pile")


def test_the_forecast_reads_the_pile_and_an_explicit_value_wins():
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan as fp, seeder
    from placement.placement_state import assess_placement
    pcb = parse_kicad_pcb(PILE)
    intent = _intent(_doc(PILE), PILE)
    blocks, _p = fp.resolve_blocks(intent, pcb, SOURCES)
    st = assess_placement(pcb, PILE)
    assert st.pile, st.reasons
    got = {}
    for name, kw in (('omitted', {}), ('assessed', {'assessment': st}),
                     ('off', {'claim_after_ics': False}),
                     ('on', {'claim_after_ics': True})):
        f = seeder.decap_pin_forecast(pcb, intent, blocks, **kw)
        got[name] = (f['late_armed'], f['late_armed_by'])
    assert got == {'omitted': (True, 'pile'), 'assessed': (True, 'pile'),
                   'off': (False, 'set'), 'on': (True, 'set')}, got
    # The resolver itself: a bool is taken as given, the sentinel reads
    # the verdict.
    assert seeder.decap_claim_resolved(True) == (True, 'set')
    assert seeder.decap_claim_resolved(False, st) == (False, 'set')
    assert seeder.decap_claim_resolved(seeder.DECAP_ON_PILE, st) == \
        (True, 'pile')
    print("  PASS: the forecast arms 3.5 on the pile's verdict; an explicit "
          "value wins")


def _summary(stdout):
    m = re.search(r'^JSON_SUMMARY: (.*)$', stdout, re.M)
    assert m, stdout[-1500:]
    return json.loads(m.group(1))


def test_place_seed_on_a_pile():
    with tempfile.TemporaryDirectory() as td:
        ipath = os.path.join(td, 'pile.json')
        with open(ipath, 'w', encoding='utf-8') as fh:
            json.dump(_doc(PILE), fh)
        got = {}
        for flag in (None, '--no-decap-claim-after-ics'):
            out = os.path.join(td, f'{flag or "default"}.kicad_pcb')
            argv = [sys.executable, '-X', 'utf8', PLACE_SEED, PILE, out,
                    '--intent', ipath, '--seed', '0', '--no-polish']
            r = subprocess.run(argv + ([flag] if flag else []),
                               capture_output=True, text=True,
                               encoding='utf-8', errors='replace', cwd=ROOT)
            assert r.returncode in (0, 4), r.stdout[-800:] + r.stderr[-800:]
            got[flag] = (_summary(r.stdout)['decap_stage']['late'], r.stdout)
    late, so = got[None]
    assert late['armed'] is True and late['armed_by'] == 'pile', late
    assert late['claimed'] > 0, late
    assert '(3.5, on by default for a pile, within-limit: ' in so, \
        so[-1500:]
    late_off, so_off = got['--no-decap-claim-after-ics']
    assert late_off['armed'] is False and late_off['claimed'] == 0, late_off
    assert ('stage 3.5 is off (--no-decap-claim-after-ics; '
            '--decap-claim-after-ics arms it)') in so_off, so_off[-1500:]
    print(f"  PASS: place_seed on the pile -- omitted, 3.5 claimed "
          f"{late['claimed']} by the pile default; --no-... turns it off")


TESTS = [
    test_the_run29_pile_claims_by_default,
    test_the_explicit_flag_is_after_ics_on_a_pile,
    test_the_pilots_esp_prog_pile_claims_by_default,
    test_a_placed_board_seeds_as_before,
    test_the_forecast_reads_the_pile_and_an_explicit_value_wins,
    test_place_seed_on_a_pile,
]


if __name__ == '__main__':
    want = sys.argv[1:]
    for t in TESTS:
        if want and not any(w in t.__name__ for w in want):
            continue
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
