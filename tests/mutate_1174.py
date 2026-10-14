"""The #1174 mutation battery: an emitted intent grades clean on its own board.

One row per load-bearing line, each reverting it; every row names the test
that must fail. **THE ROWS TO LOOK AT FIRST if this file ever goes red**:

  * `zone-not-clamped-to-the-written-envelope` -- CM5_MINIMA_3's x-min
    75.0504 rounded to 75.05, 0.4 um outside the 4-decimal envelope, and the
    emitted intent failed its own board;
  * `open-side-ignored` -- rp2350's J1 (USB-C, north) failed its block zone
    by 1.93 mm while the edge rule passed it.

NOT named `test_*.py`, so `tests/run_all.py` does not collect it: it REWRITES
the sources in place. One writer per tree. It refuses to start on a dirty
target, and it runs every witness UNMUTATED first -- a witness that already
fails would score every row as killed.

    python3 tests/mutate_1174.py
    python3 tests/mutate_1174.py --row open-side-ignored

A row is KILLED by a failure or an error. An anchor that does not match
EXACTLY ONCE is BROKEN, never skipped; `preflight()` runs right after `ROWS`.
Edits are `str.replace(old, new, 1)`; anchors are LF and translated to the
target's own ending.
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)
_PL = os.path.join(_ROOT, 'py_placer', 'placement')

TARGETS = {
    'fp': os.path.join(_PL, 'floorplan.py'),
    'quench': os.path.join(_PL, 'quench.py'),
    'seeder': os.path.join(_PL, 'seeder.py'),
    'ps': os.path.join(_ROOT, 'py_placer', 'place_seed.py'),
}

T1174 = os.path.join(_TESTS, 'test_1174_emit_round_trip.py')
T1241 = os.path.join(_TESTS, 'test_1241_seed_gate_off_board.py')
T1117 = os.path.join(_TESTS, 'test_1117_reseat_declared_rotation.py')

# (name, target, old, new, tests, expect)
ROWS = [
    ('zone-not-clamped-to-the-written-envelope', 'fp',
     "    env = [round(v, 4) for v in bounds]\n",
     "    return z\n",
     (T1174,), 'KILLED'),
    ('emitter-rounds-the-zone-on-its-own', 'fp',
     "            entry['zone'] = emitted_zone_rect(rect, bounds)\n",
     "            entry['zone'] = [round(v, 3) for v in rect]\n",
     (T1174,), 'KILLED'),
    ('open-side-ignored', 'fp',
     "        zone_rect = open_zone_rect(zone_rect, open_sides)\n",
     "        pass\n",
     (T1174,), 'KILLED'),
    ('any-declared-or-not-side-opens', 'fp',
     "                 if e in edges and on[e])\n",
     "                 if on[e])\n",
     (T1174,), 'KILLED'),
    ('a-zone-side-inside-the-board-opens', 'fp',
     "                 if e in edges and on[e])\n",
     "                 if e in edges)\n",
     (T1174,), 'KILLED'),
    ('connector-affinity-opens-a-side', 'fp',
     "    for c in intent.edge_claims():\n"
     "        e = c.get('edge')\n",
     "    for c in intent.edge_connectors:\n"
     "        e = c.get('edge')\n",
     (T1174,), 'KILLED'),
    ('the-grade-does-not-open', 'fp',
     "                out, axis = zone_escape(z.rect, part.rect, True, opened)\n",
     "                out, axis = zone_escape(z.rect, part.rect, True)\n",
     (T1174,), 'KILLED'),
    ('the-grade-does-not-open-a-region', 'fp',
     "            out, axis = zone_escape(z.rect, part.rect, False, opened)\n",
     "            out, axis = zone_escape(z.rect, part.rect, False)\n",
     (T1174,), 'KILLED'),
    ('zone-rows-carry-no-open-sides', 'fp',
     "    claimed = claimed_edges(intent) if bounds else {}\n",
     "    claimed = {}\n",
     (T1174,), 'KILLED'),
    ('the-quench-term-drops-the-open-side', 'quench',
     "                    tuple((_z.get('open_sides') or {}).get(_ref, ()))))\n",
     "                    ()))\n",
     (T1174,), 'KILLED'),
    ('the-quench-measures-without-it', 'quench',
     "            out.append(_fp.zone_escape(t.rect, rects[0], t.anchor,\n"
     "                                       t.open_sides)[0])\n",
     "            out.append(_fp.zone_escape(t.rect, rects[0], t.anchor)[0])\n",
     (T1174,), 'KILLED'),
    ('feasibility-keeps-the-side-closed', 'fp',
     "        zone_rect = _reach_open_zone(zone_rect, open_sides)\n",
     "        pass\n",
     (T1174,), 'KILLED'),
    ('the-seeder-note-reads-the-closed-zone', 'seeder',
     "            out, _axis = _rect_escape(_oz, r)\n",
     "            out, _axis = _rect_escape(z.rect, r)\n",
     (T1174,), 'KILLED'),
    # -- the phase-1 verifier's fixes, and the second verifier's --
    ('a-claim-without-a-band-max-opens-a-side', 'fp',
     "                and (c.get('overhang_mm') or {}).get('max') is not None):\n",
     "                and True):\n",
     (T1174,), 'KILLED'),
    ('plan-row5-charges-an-open-side-member', 'fp',
     "            if zone_open_sides(z.rect, ctx.outline_bounds,\n",
     "            if False and zone_open_sides(z.rect, ctx.outline_bounds,\n",
     (T1174,), 'KILLED'),
    ('total-coverage-ignores-the-open-side', 'fp',
     "                and not zone_open_sides(z.rect, _bounds, _claimed.get(r))}\n",
     "                and True}\n",
     (T1174,), 'KILLED'),
    ('an-open-side-lets-the-pads-leave', 'fp',
     "            if opened:\n",
     "            if False:\n",
     (T1174,), 'KILLED'),
    # -- the round-2 verifier's: each SURVIVED test_1174 before its test --
    ('the-posed-view-has-no-pad-model', 'fp',
     "        self.legality_ctx = getattr(state, 'legality_ctx', None)\n",
     "        self.legality_ctx = None\n",
     (T1174,), 'KILLED'),
    ('the-posed-view-grades-the-current-pose', 'fp',
     "        if callable(getattr(st, 'pose', None)):\n",
     "        if False:\n",
     (T1174,), 'KILLED'),
    ('a-small-pad-escape-passes', 'fp',
     "                if pout > tol:\n",
     "                if pout > tol + 1.0:\n",
     (T1174,), 'KILLED'),
    ('the-west-side-is-unmeasured', 'fp',
     "            'west': zx0 - x0, 'east': x1 - zx1}\n",
     "            'west': 0.0, 'east': x1 - zx1}\n",
     (T1174,), 'KILLED'),
    ('the-east-side-is-unmeasured', 'fp',
     "            'west': zx0 - x0, 'east': x1 - zx1}\n",
     "            'west': zx0 - x0, 'east': 0.0}\n",
     (T1174,), 'KILLED'),
    ('the-south-side-is-unmeasured', 'fp',
     "    past = {'north': zy0 - y0, 'south': y1 - zy1,\n",
     "    past = {'north': zy0 - y0, 'south': 0.0,\n",
     (T1174,), 'KILLED'),
    ('a-pad-error-also-grades-the-courtyard', 'fp',
     "                    continue\n"
     "            if not zone_fits_courtyard(z.rect, part.rect, tol):\n",
     "                    pass\n"
     "            if not zone_fits_courtyard(z.rect, part.rect, tol):\n",
     (T1174,), 'KILLED'),
    ('pads-measured-at-the-zone-side-not-the-board', 'fp',
     "        zx0, zy0 = max(zx0, bounds[0]), max(zy0, bounds[1])\n",
     "        pass\n",
     (T1174,), 'KILLED'),
    ('the-grade-passes-no-outline-to-the-pads', 'fp',
     "                    ctx.outline_bounds)\n",
     "                    None)\n",
     (T1174,), 'KILLED'),
    ('pads-measured-at-the-zone-side-not-the-board-east-south', 'fp',
     "        zx1, zy1 = min(zx1, bounds[2]), min(zy1, bounds[3])\n",
     "        pass\n",
     (T1174,), 'KILLED'),
    # ---- follow-ups folded in: #1241 (c)/(d) and #1240's wording ----------
    ('the-gate-forgets-pad-copper-off-the-board', 'ps',
     "                          oob=[r for r, _a in _oob_seeded])\n",
     "                          oob=[])\n",
     (T1241,), 'KILLED'),
    ('the-gate-reason-ignores-oob-alone', 'ps',
     "    if not (unseated or own or my_pads or hole_delta or band or oob):\n",
     "    if not (unseated or own or my_pads or hole_delta or band):\n",
     (T1241,), 'KILLED'),
    ('the-gate-charges-copper-the-seed-did-not-place', 'ps',
     "                   if r in _seeded and r not in result['unseated']]\n",
     "                   if r not in result['unseated']]\n",
     (T1241,), 'KILLED'),
    ('the-floor-note-calls-off-board-copper-the-floor', 'seeder',
     "    off = pad['gap_mm'] is not None and pad['gap_mm'] < 0\n",
     "    off = False\n",
     (T1241, T1174), 'KILLED'),
    ('the-reseat-always-blames-the-polish', 'ps',
     "                               if _polish_moved(f['reference'])]\n",
     "                               if True]\n",
     (T1174,), 'KILLED'),
    ('the-reseat-never-blames-the-polish', 'ps',
     "                        return fp is None or (\n",
     "                        return False and (\n",
     (T1117,), 'KILLED'),
]

sys.path.insert(0, _TESTS)
from mutation_anchors import preflight   # noqa: E402
preflight(__file__)


def _dirty(path):
    p = subprocess.run(['git', 'status', '--porcelain', '--', path],
                       capture_output=True, text=True, cwd=_ROOT)
    return bool(p.stdout.strip())


def _run_tests(tests):
    failed = []
    for t in tests:
        p = subprocess.run([sys.executable, '-X', 'utf8', t],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=2400, cwd=_ROOT)
        if p.returncode not in (0,):
            failed.append((os.path.basename(t), p.returncode,
                           [ln.strip()[:90] for ln in
                            ((p.stdout or '') + (p.stderr or '')).splitlines()
                            if 'FAIL' in ln or 'Error' in ln][:2]))
    return failed


def run(only=None):
    rows = [r for r in ROWS if only is None or r[0] == only]
    if not rows:
        print('no row named %r' % only)
        return 1
    for path in TARGETS.values():
        if _dirty(path):
            print('REFUSING: %s has uncommitted changes. Commit or stash '
                  'first -- this battery restores by overwriting.'
                  % os.path.basename(path))
            return 2
    # THE UNMUTATED BASELINE: every witness must pass as the code stands,
    # or a row it "kills" proves nothing.
    witnesses = sorted({t for r in rows for t in r[4]})
    base_fail = _run_tests(witnesses)
    if base_fail:
        print('REFUSING: witnesses fail UNMUTATED -- %s' % base_fail)
        return 2
    print('baseline: %d witnesses pass unmutated' % len(witnesses))
    orig = {k: io.open(v, encoding='utf-8', newline='').read()
            for k, v in TARGETS.items()}
    results = []
    try:
        for name, tgt, old, new, tests, expect in rows:
            path = TARGETS[tgt]
            base = orig[tgt]
            o, n = old, new
            if '\r\n' in base:
                o, n = o.replace('\n', '\r\n'), n.replace('\n', '\r\n')
            if base.count(o) != 1 or o == n:
                results.append((name, 'BROKEN', expect,
                                ['anchor matched %d times' % base.count(o)]))
                continue
            io.open(path, 'w', encoding='utf-8', newline='').write(
                base.replace(o, n, 1))
            try:
                failed = _run_tests(tests)
            finally:
                io.open(path, 'w', encoding='utf-8', newline='').write(base)
            got = 'KILLED' if failed else (
                'EQUIVALENT' if expect == 'EQUIVALENT' else 'SURVIVED')
            results.append((name, got, expect,
                            [str(f)[:150] for f in failed[:2]]))
            print('%-44s %s' % (name, results[-1][1]), flush=True)
    finally:
        for k, v in TARGETS.items():
            io.open(v, 'w', encoding='utf-8', newline='').write(orig[k])
    wrong = [r for r in results if r[1] != r[2]]
    print('')
    for name, verdict, expect, why in results:
        print('%-44s %-10s%s' % (name, verdict, '' if verdict == expect else
                                 '   <-- WRONG, expected %s' % expect))
        for w in why:
            print('      %s' % w)
    print('\n%d rows: %d killed, %d survived, %d equivalent, %d broken'
          % (len(results), sum(r[1] == 'KILLED' for r in results),
             sum(r[1] == 'SURVIVED' for r in results),
             sum(r[1] == 'EQUIVALENT' for r in results),
             sum(r[1] == 'BROKEN' for r in results)))
    return 1 if wrong else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--row', default=None, help='run only this row')
    a = ap.parse_args()
    return run(a.row)


if __name__ == '__main__':
    sys.exit(main())
