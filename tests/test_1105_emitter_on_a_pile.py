#!/usr/bin/env python3
"""#1105, the emitter half: `check_floorplan --emit-intent --decaps-from` must
not promise a pin stage that cannot run on the board it emits from.

place_seed's per-supply-pin stage (2.5) serves the supply pins of owner ICs
seated BEFORE it -- placed, file-locked, or declared as a fixed pose, a
must_lock pattern or a zoned block. On a pile none is, so the stage claims
nothing; run 38's StickHub emitter still promised "will seat 38 cap(s)". The
line now says the stage will claim NO cap on this board, names the owners the
centroid stage seats after it, and names the three ways to declare them. It
reads the same owner set as the stage (#1249: `seeder.decap_owner_refs`, so
esp_prog's 3-pad regulator U2 is one of them).

Cases:
* a `make_unplaced` pile of esp_prog and of tigard, each with its own board
  as the `--decaps-from` reference: the line says NO cap and names the
  declaration routes, the forecast's early set is empty, and the seed on
  esp_prog's pile indeed claims 0 at stage 2.5 and 0 at 3.5;
* control: the same esp_prog intent with U1 and U2 declared as fixed poses
  at the reference's poses -- the forecast moves their caps to stage 2.5,
  the line switches to "can claim up to N", and the seed claims caps;
* with no forecast the line states the scope as a scope, never "will seat".

The issue itself stays OPEN: seating owner ICs before stage 2.5 by default
was measured NO-GO three times and is a policy call.

    python3 -X utf8 tests/test_1105_emitter_on_a_pile.py [case ...]
"""
import contextlib
import io
import json
import os
import random
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

ESP = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
TIGARD = os.path.join(ROOT, 'kicad_files', 'tigard.kicad_pcb')
MAKE_UNPLACED = os.path.join(ROOT, '.claude', 'skills', 'pcb-free-agent',
                             'scripts', 'make_unplaced.py')
FLOORPLAN = os.path.join(ROOT, 'py_tools', 'check_floorplan.py')
NO_CAP = 'will claim NO cap on this board'
_TMP = tempfile.TemporaryDirectory(prefix='t1105e_')
_PILES = {}


def _run(argv):
    r = subprocess.run([sys.executable, '-X', 'utf8'] + argv,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=ROOT)
    assert r.returncode == 0, (argv, r.stdout[-1500:], r.stderr[-1500:])
    return r.stdout


def _pile(board):
    """`(pile, emitted intent path, stdout)` -- make_unplaced, then the
    emitter with the board itself as the --decaps-from reference."""
    if board in _PILES:
        return _PILES[board]
    name = os.path.splitext(os.path.basename(board))[0]
    pile = os.path.join(_TMP.name, f'{name}_pile.kicad_pcb')
    intent = os.path.join(_TMP.name, f'{name}_pile.intent.json')
    _run([MAKE_UNPLACED, board, pile])
    run_utils.evidence(pile, 'the pile make_unplaced wrote')
    out = _run([FLOORPLAN, pile, '--emit-intent', intent, '--decaps-from',
                board, '--allow-unplaced'])
    run_utils.evidence(intent, 'the emitted intent')
    _PILES[board] = (pile, intent, out)
    return _PILES[board]


def _line(stdout):
    got = [ln for ln in stdout.splitlines() if 'READS this key' in ln]
    assert len(got) == 1, stdout[-2000:]
    return got[0]


def _forecast(intent):
    with open(intent, encoding='utf-8') as fh:
        doc = json.load(fh)
    return doc, doc['context']['decap_census']['seeder_forecast']


def _seed(pile, doc, seed='0'):
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan as fp, seeder
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        pcb = parse_kicad_pcb(pile)
        return seeder.seed_from_intent(
            pcb, pile, fp.intent_from_dict(doc, pile), random.Random(seed),
            group_sources=('kicad', 'sheet'), clearance=0.2)


def test_esp_prog_pile_says_the_stage_claims_nothing():
    from check_floorplan import DECLARE_OWNERS_HOW
    pile, intent, out = _pile(ESP)
    line = _line(out)
    assert NO_CAP in line, line
    assert DECLARE_OWNERS_HOW in line, line
    assert 'can claim up to' not in line and 'will seat' not in line, line
    doc, f = _forecast(intent)
    assert f['early'] == [] and f['scope'] == 4, f
    # #1249: the regulator is an owner the stage reads.
    assert {'U1', 'U2'} <= set(f['late_owners']), f
    assert 'U1, U2, USB1' in line, line
    res = _seed(pile, doc)
    ds = res['decap_stage']
    assert ds['claimed'] == 0 and ds['late']['claimed'] == 0, ds
    print(f"  PASS: esp_prog pile -- the line says {NO_CAP!r}; the seed "
          f"claimed {ds['claimed']} ({ds['reason'][:60]}...)")


def test_tigard_pile_says_the_stage_claims_nothing():
    from check_floorplan import DECLARE_OWNERS_HOW
    _pile_, intent, out = _pile(TIGARD)
    line = _line(out)
    assert NO_CAP in line and DECLARE_OWNERS_HOW in line, line
    _doc, f = _forecast(intent)
    assert f['early'] == [] and f['scope'] > 20, f
    print(f"  PASS: tigard pile -- {f['scope']} caps in scope, the line "
          f"says none can be claimed and how to declare the owners")


def test_declared_owners_turn_the_line_and_the_seed():
    """Control: U1 and U2 declared at the reference's poses."""
    import check_floorplan as CF
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan as fp, seeder
    pile, intent, _out = _pile(ESP)
    doc, f0 = _forecast(intent)
    ref = parse_kicad_pcb(ESP)
    doc = dict(doc, fixed_poses=[
        {'ref': r, 'x': ref.footprints[r].x, 'y': ref.footprints[r].y,
         'rot': ref.footprints[r].rotation or 0.0, 'basis': 'declared',
         'why': 'control: the owner seated before the pin stage'}
        for r in ('U1', 'U2')])
    pcb = parse_kicad_pcb(pile)
    it = fp.intent_from_dict(doc, pile)
    blocks, _p = fp.resolve_blocks(it, pcb, ('kicad', 'sheet'))
    f = seeder.decap_pin_forecast(pcb, it, blocks)
    assert {'U1', 'U2'} <= set(f['early_owners']), f
    assert f['early'], f
    line = CF._forecast_clause({'seeder_forecast': f})
    assert NO_CAP not in line, line
    assert f"can claim up to {len(f['early'])} of {f['scope']}" in line, line
    res = _seed(pile, doc)
    ds = res['decap_stage']
    assert ds['claimed'] > 0, ds
    print(f"  PASS: with U1/U2 declared the forecast holds "
          f"{len(f['early'])} cap(s) at stage 2.5 and the seed claimed "
          f"{ds['claimed']}")


def test_no_forecast_is_a_scope_not_a_promise():
    import check_floorplan as CF
    line = CF._forecast_clause({'seeder_pin_scope': 38,
                                'seeder_forecast': {'unavailable': 'x'}})
    assert 'will seat' not in line, line
    assert '38 cap(s) in scope' in line and 'could not be forecast' in line, \
        line
    print("  PASS: no forecast -> the scope, and that it was not forecast")


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
