#!/usr/bin/env python3
"""#1105 PILOT: stage 3.5's decap claim on the issue's own basis, a pile.

The go/no-go of a pre-registration's `pilot`: `--prereg` names it, by default
`tests/1105_pile_ab_prereg_v2.json`, the one written for #1142's per-cap
signal; `tests/1105_pile_ab_prereg.json` is the first, whose candidate
(after_queue + within_limit) was NO-GO at 39affafb (#1144, numbers in #1105).
Each pre-registered board is staged as an unaided pile and its intent emitted
by the CLI with `--decaps-from` the board itself (`test_placement_ab.
_pile_inputs`, the same helper the pile rows use); then the pile is seeded
once with stage 3.5 OFF and once per variant ON, and every arm is graded with
the A/B's own `_run_seed` / `_grade_row` against the one intent. Nothing here
is a copy of the harness: the seed, the write, the grade and the verdict are
the harness's functions.

The GO rule is the pre-registration's: its CANDIDATE (one of `VARIANTS`, read
from the file) must improve -- the signal falls, no arbiter guard rises,
intent errors do not rise -- on at least max(1, N-1) of the N ELIGIBLE boards,
N >= 3. The other variants are diagnostics: they cannot change the verdict.
Eligibility is decided from the emitted intent alone, before any seed: the
decap limit is armed, the forecast's scope is at least 1, and -- when the
file's `eligibility.held_list_required` says so -- the intent holds at least
one cap within the radius (`decaps.within_radius_refs`, #1142), without which
the board is graded on the old signal.

Each arm also prints DIAGNOSTICS the go rule does not read: its decap_* errors
by rule (`decap_ungraded` errors are the held caps it strands, #1142), and how
many caps the written board leaves beyond the 5 mm decap search radius of
their chip, with their summed distance (held or not).

Not collected by run_all (no `test_` prefix): about 30-45 minutes.

    python3 -X utf8 tests/measure_1105_pile_variants.py [--prereg PATH]
        [--workdir DIR] [--json-out PATH] [--board esp_prog.kicad_pcb ...]

Exit 0 = GO, 1 = NO-GO, 2 = the measurement could not be taken, 3 = a
partial (--board) run, which gives no verdict.
"""
import argparse
import json
import os
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
import test_placement_ab as AB                      # noqa: E402

#: name -> the seeder module knobs of one stage-3.5 variant (the corpus rows'
#: three families, `test_placement_ab` decap-after-ics/-within-limit/-after-queue).
VARIANTS = {
    'after-ics': {'DECAP_LATE_WITHIN_LIMIT': False,
                  'DECAP_LATE_AT': 'after_last_owner'},
    'within-limit': {'DECAP_LATE_WITHIN_LIMIT': True,
                     'DECAP_LATE_AT': 'after_last_owner'},
    'after-queue': {'DECAP_LATE_WITHIN_LIMIT': True,
                    'DECAP_LATE_AT': 'after_queue'},
}


#: #1142's per-cap signal; the pilot's default since the first one's NO-GO.
PREREG_V2 = os.path.join(TESTS_DIR, '1105_pile_ab_prereg_v2.json')


def _prereg(path=PREREG_V2):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def _candidate_name(pre):
    cand = {k: v for k, v in pre['candidate'].items() if k.isupper()}
    names = [n for n, f in VARIANTS.items() if f == cand]
    if len(names) != 1:
        raise ValueError(f"the pre-registered candidate {cand} is not exactly "
                         f"one of the variants {VARIANTS}")
    return names[0]


def _decap_by_rule(m):
    """{rule: count} of the arm's decap_* intent ERRORS."""
    return {r: n for r, n in sorted((m.get('intent_errors_by_rule')
                                     or {}).items()) if r.startswith('decap_')}


def _decap_str(m):
    by = _decap_by_rule(m)
    return (f"decap {sum(by.values())}"
            + (f" [{', '.join(f'{r[6:]} {n}' for r, n in by.items())}]"
               if by else ''))


def _beyond(path):
    """(count, summed mm) of the caps `path` leaves beyond the decap search
    radius of the chip they tether to -- `groups.decap_populations`'
    `beyond`, the population `decap_ungraded` reports."""
    import contextlib
    import io
    from kicad_parser import parse_kicad_pcb
    from placement import groups
    with contextlib.redirect_stdout(io.StringIO()):
        _near, beyond, _o = groups.decap_populations(parse_kicad_pcb(path))
    return len(beyond), round(sum(d for _c, _ic, d in beyond), 1)


def _late(m):
    late = ((m.get('decap_stage') or {}).get('late') or {})
    return {k: late.get(k) for k in ('claimed', 'declined', 'owners',
                                     'reason')}


def _unread(off, on, pre):
    """The unread guards at their pre-registered tolerances: a list of the
    ones that rose past them. Reported only -- the pilot's go rule is the
    arbiter's."""
    rose = []
    for g, tol in pre['unread_guards'].items():
        if g == 'why':
            continue
        a, b = off.get(g), on.get(g)
        if a is None or b is None:
            continue
        allow = (abs(a) * tol['rel']) if 'rel' in tol else tol['abs']
        if b > a + allow + 1e-9:
            rose.append(f"{g} {a} -> {b}")
    return rose


def measure(boards, workdir, pre):
    row = {'signal': pre['signal'], 'guard': tuple(pre['arbiter_guards'])}
    ign = list(pre['ignore_nets'])
    out = {}
    for b in boards:
        d = os.path.join(workdir, os.path.splitext(b)[0])
        os.makedirs(d, exist_ok=True)
        rec = out[b] = {}
        t0 = time.time()
        try:
            pile, intent, doc, refs = AB._pile_inputs(
                os.path.join(AB.BOARDS, b), d)
        except AB.PileIneligible as exc:
            rec.update(eligible=False, why=str(exc))
            print(f"{b}: INELIGIBLE -- {exc}", flush=True)
            continue
        f = AB._pile_forecast(doc)
        dec = doc.get('decaps') or {}
        held = dec.get('within_radius_refs')
        rec.update(eligible=bool(f.get('scope')),
                   scope=f.get('scope'), early=len(f.get('early') or ()),
                   late=len(f.get('late') or ()),
                   ownerless=len(f.get('ownerless') or ()),
                   limit=dec.get('max_distance_mm'),
                   pin_limit=dec.get('max_pin_distance_mm'),
                   held=None if held is None else len(held),
                   held_radius=dec.get('within_radius_mm'),
                   seed_refs=None if refs is None else len(refs))
        if not rec['eligible']:
            rec['why'] = 'the forecast scope is 0: no cap is charged'
        elif (pre.get('eligibility') or {}).get('held_list_required') \
                and not held:
            rec['eligible'] = False
            rec['why'] = ('the intent holds no cap within the radius '
                          '(no decaps.within_radius_refs): the old signal')
        scope = {'seed_refs': refs} if refs is not None else {}
        with AB._seeder_flags({}):
            off = AB._run_seed(pile, os.path.join(d, 'off.kicad_pcb'), intent,
                               dict(decap_claim_after_ics=False, **scope),
                               ignore_nets=ign, grade_intent=intent)
        rec['off'] = off
        rec['off_beyond'] = _beyond(os.path.join(d, 'off.kicad_pcb'))
        for name, flags in VARIANTS.items():
            with AB._seeder_flags(flags):
                on = AB._run_seed(pile, os.path.join(d, f'{name}.kicad_pcb'),
                                  intent,
                                  dict(decap_claim_after_ics=True, **scope),
                                  ignore_nets=ign, grade_intent=intent)
            mark, notes = AB._verdict(off, on, row)
            rec[name] = {'mark': mark, 'notes': notes, 'on': on,
                         'unread_rose': _unread(off, on, pre),
                         'beyond': _beyond(os.path.join(d,
                                                        f'{name}.kicad_pcb'))}
        rec['seconds'] = round(time.time() - t0, 1)
        print(f"{b}: {'eligible' if rec['eligible'] else 'INELIGIBLE'} "
              f"scope {rec['scope']} (early {rec['early']}, late "
              f"{rec['late']}, ownerless {rec['ownerless']}), limit "
              f"{rec['limit']}, pin limit {rec['pin_limit']}, held "
              f"{rec['held']} within {rec['held_radius']} mm, "
              f"{rec['seconds']}s"
              + (f" -- {rec['why']}" if rec.get('why') else ''), flush=True)
        print(f"    OFF  intent_errors {off['intent_errors']} "
              f"({_decap_str(off)}) crossings {off['crossings']} hpwl "
              f"{off['hpwl']} unseated {off['unseated']} body_blocking "
              f"{off['body_blocking']} | beyond 5 mm {rec['off_beyond'][0]} "
              f"({rec['off_beyond'][1]} mm)", flush=True)
        for name in VARIANTS:
            on = rec[name]['on']
            print(f"    {name:<13} {rec[name]['mark']:<8} intent_errors "
                  f"{on['intent_errors']} ({_decap_str(on)}) "
                  f"crossings {on['crossings']} hpwl {on['hpwl']} unseated "
                  f"{on['unseated']} body_blocking {on['body_blocking']} | "
                  f"beyond 5 mm {rec[name]['beyond'][0]} "
                  f"({rec[name]['beyond'][1]} mm) | 3.5 {_late(on)}",
                  flush=True)
            for n in rec[name]['notes']:
                print(f"        {n}", flush=True)
            if rec[name]['unread_rose']:
                print(f"        unread guards past tolerance: "
                      f"{'; '.join(rec[name]['unread_rose'])}", flush=True)
    return out


def verdict(out, cand):
    elig = [b for b, r in out.items() if r.get('eligible')]
    n = len(elig)
    improve = [b for b in elig if out[b][cand]['mark'] == 'improve']
    need = max(1, n - 1)
    go = n >= 3 and len(improve) >= need
    return go, n, improve, need


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--prereg', default=PREREG_V2,
                    help='The pre-registration whose pilot this runs '
                         '(default: %(default)s)')
    ap.add_argument('--workdir', default=None)
    ap.add_argument('--json-out', default=None)
    ap.add_argument('--board', action='append', default=None,
                    help='Only these pre-registered boards (repeatable); a '
                         'partial run prints its table but no GO verdict')
    args = ap.parse_args(argv)
    try:
        pre = _prereg(args.prereg)
    except Exception as exc:                      # noqa: BLE001 - exit 2
        print(f"MEASUREMENT NOT TAKEN: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    boards = list(pre['boards'])
    if args.board:
        bad = sorted(set(args.board) - set(boards))
        if bad:
            print(f"not pre-registered: {bad}", file=sys.stderr)
            return 2
        boards = [b for b in boards if b in args.board]
    try:
        cand = _candidate_name(pre)
        work = args.workdir or tempfile.mkdtemp(prefix='m1105_')
        print(f"pre-registration {os.path.basename(args.prereg)}; candidate "
              f"{cand}; workdir {work}", flush=True)
        out = measure(boards, work, pre)
        if args.json_out:
            with open(args.json_out, 'w', encoding='utf-8') as fh:
                json.dump({'candidate': cand, 'boards': out}, fh, indent=1,
                          default=str)
        go, n, improve, need = verdict(out, cand)
    except Exception as exc:                      # noqa: BLE001 - exit 2
        # A broken measurement is not a NO-GO: an AssertionError from
        # `_pile_inputs` (not a pile) used to exit 1, the NO-GO code.
        print(f"MEASUREMENT NOT TAKEN: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    print()
    for name in VARIANTS:
        marks = [out[b][name]['mark'] for b in boards
                 if out[b].get('eligible')]
        print(f"{name:<13} improve {marks.count('improve')} / neutral "
              f"{marks.count('neutral')} / regress {marks.count('regress')}"
              + ('   <- the pre-registered candidate' if name == cand
                 else ''))
    if args.board:
        print("partial run: no GO verdict")
        return 3
    print(f"\n{'GO' if go else 'NO-GO'}: {cand} improves {len(improve)} of "
          f"{n} eligible pile(s) {sorted(improve)}; the pre-registered rule "
          f"needs >= {need} (and N >= 3)")
    return 0 if go else 1


if __name__ == '__main__':
    sys.exit(main())
