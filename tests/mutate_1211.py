#!/usr/bin/env python3
"""The #1211 mutation battery: `place_pose replicate`, its intent
declaration, and the parsed sheet fields it reads.

One row per load-bearing line, each reverting or bending it, next to the
test that must fail:

* `placement/replicate.py` -- THE NET RULE arm by arm (function, one-to-one,
  a shared net mapping to itself from either side, net against no net), the
  `--allow-net-mismatch` waiver (left out of the rule AND of sharedness,
  reported with whether it was needed, scoped to the target a target ref
  names, spelled in the refusal), the partial copy (`--only` / `--skip`, the
  anchor it implies, sharedness over the whole instance), the grade's
  median fit, and `apply_declared`'s verdict;
* `placement/pose_ops.py` -- `--search` (nearest first, inside the radius,
  never moving a locked part, the later targets left out, run only when the
  copy as asked is refused) and the per-part findings a refused copy names;
* `place_pose.py` -- the declared mode's refusals and `--search`'s bound;
* `placement/floorplan.py` -- the loader, the rule's severity and its
  unresolved finding;
* `place_seed.py` -- the stage itself;
* `kicad_parser.py` -- the sheet fields, both parse paths and the
  comparator (the pcbnew rows' killer is the KiCad-python parity gate,
  which exits 2 -- BROKEN here, never SURVIVED -- where pcbnew is absent).

NOT named `test_*.py`, so `tests/run_all.py` does not collect it: it
REWRITES the sources in place. One writer per tree. It refuses to start on a
dirty target, and it runs every witness UNMUTATED first -- a witness that
already fails would score every row as killed. A witness is a test file,
optionally with the case names its `__main__` filters on, so a row pays for
the cases that can see it and no more.

    python3 -X utf8 tests/mutate_1211.py
    python3 -X utf8 tests/mutate_1211.py --row the-net-rule-drops-the-function-arm
    python3 -X utf8 tests/mutate_1211.py --list

THE MEASURED RESULT is recorded here from the run, never predicted.

Measured on KiCad 10.0.0 python present (macOS), at the commit that added
this file: 38 rows, 37 KILLED, 1 SURVIVED as expected
(`only-and-skip-together-at-the-cli`: the engine refuses the same pair in
the same words), 0 BROKEN. Writing the battery found four arms no test saw,
each now with a case: the one-to-one arm (two source nets onto one target
net, `merge`), a target pad on a SOURCE channel net (`onto`, through
`--only U1`), a pad with no net against one with a net (`nonet`), and the
default anchor a partial copy implies (`--skip U1`). The parity gate gained
its comparator witness (a sheet name changed on one side is reported) for
the `compare_pcb_data-skips-the-sheet-fields` row.

#1277 added two rows for the net map (`the-net-map-is-first-come`,
`a-pair-that-breaks-under-any-map-votes`). Measured on Windows at the
commit that added them, with the twelve rows that mutate `correspond`
(the five net-rule arms, three waiver rows on it, the refusal's
flags, sharedness over the whole instance, and the two new rows), run
one by one: 12 KILLED, 0 SURVIVED, 0 BROKEN.
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

TARGETS = {'rep': os.path.join(_PL, 'replicate.py'),
           'ops': os.path.join(_PL, 'pose_ops.py'),
           'cli': os.path.join(_ROOT, 'py_placer', 'place_pose.py'),
           'fp': os.path.join(_PL, 'floorplan.py'),
           'seed': os.path.join(_ROOT, 'py_placer', 'place_seed.py'),
           'kp': os.path.join(_ROOT, 'py_router', 'kicad_parser.py')}

_T = os.path.join(_TESTS, 'test_1211_place_pose_replicate.py')
_I = os.path.join(_TESTS, 'test_1211_replicate_intent.py')
GATE = os.path.join(_TESTS, 'gui_parity', 'test_1211_sheet_name_parity.py')


def _w(path, *cases):
    """A witness: a test file and the cases to run (none = all)."""
    return (path,) + cases


NETS = _w(_T, 'nets_not')
NETMAP = _w(_T, 'net_map')
WAIVE = _w(_T, 'waived_per_pad')
GLASGOW = _w(_T, 'glasgow')
PARTIAL = _w(_T, 'partial')
SEARCH = _w(_T, 'search_keeps')
ORDER = _w(_T, 'leaving_later')
SHEETS = _w(_T, 'spellings')
LOADER = _w(_I, 'loader')
GRADE = _w(_I, 'grade_measures', 'cannot_resolve')
POSE_I = _w(_I, 'place_pose_applies')
SEED_I = _w(_I, 'place_seed')

#: (name, target, old, new, witnesses, expect)
ROWS = [
    # ---- THE NET RULE, arm by arm ------------------------------------------
    ('the-net-rule-drops-the-function-arm', 'rep',
     "        if a in fwd and fwd[a][0] != b:",
     "        if False and a in fwd and fwd[a][0] != b:",
     (NETS,), 'KILLED'),
    ('the-net-rule-drops-the-one-to-one-arm', 'rep',
     "        if b in bwd and bwd[b][0] != a:",
     "        if False and b in bwd and bwd[b][0] != a:",
     (NETS,), 'KILLED'),
    ('a-shared-source-net-may-be-renamed', 'rep',
     "        if a != b and a in t_nets:",
     "        if False and a != b and a in t_nets:",
     (NETS,), 'KILLED'),
    ('a-source-net-may-land-on-the-target', 'rep',
     "        if a != b and b in s_nets:",
     "        if False and a != b and b in s_nets:",
     (NETS,), 'KILLED'),
    ('a-pad-with-no-net-matches-one-with-a-net', 'rep',
     "        if (a == 0) != (b == 0):",
     "        if False:",
     (NETS,), 'KILLED'),
    # #1277: the map the two map arms judge against. First-come within a
    # group of competing pairings named the pads wired alike; a pair that
    # breaks under any map voting lets a netless pad claim its net.
    ('the-net-map-is-first-come', 'rep',
     "        if got + votes[e] + _best(rest) == target:",
     "        if True:",
     (NETMAP,), 'KILLED'),
    ('a-pair-that-breaks-under-any-map-votes', 'rep',
     "        if _why(a, b) is None:",
     "        if True:",
     (NETMAP,), 'KILLED'),
    # ---- the waiver -----------------------------------------------------------
    ('a-waived-pad-still-breaks-the-rule', 'rep',
     "            (waived_rows if given is not None else rows).append(",
     "            (rows if given is not None else rows).append(",
     (WAIVE, GLASGOW), 'KILLED'),
    ('a-waived-pad-still-makes-a-net-shared', 'rep',
     "                if (k, p.pad_number) not in waive}",
     "                if True}",
     (WAIVE,), 'KILLED'),
    ('every-waiver-reads-as-needed', 'rep',
     "               'needed': _why(sp.net_id, tp.net_id) is not None}",
     "               'needed': True}",
     (WAIVE,), 'KILLED'),
    ('a-target-ref-applies-to-every-target', 'rep',
     "            return hit[0], (list(dsts) if prefix == src else [prefix])",
     "            return hit[0], list(dsts)",
     (WAIVE, PARTIAL), 'KILLED'),
    ('the-refusal-spells-no-waiver', 'rep',
     "                flags.append(b['source_pad'])",
     "                pass",
     (WAIVE, GLASGOW), 'KILLED'),
    ('a-waiver-naming-no-pad-is-accepted', 'rep',
     "        if pad not in numbers:",
     "        if False:",
     (WAIVE,), 'KILLED'),
    # ---- the partial copy -----------------------------------------------------
    ('skip-leaves-nothing-out', 'rep',
     "    return {d: (sm | set(members(pcb, d))) - named[d] for d in dsts}",
     "    return {d: (sm | set(members(pcb, d))) for d in dsts}",
     (PARTIAL,), 'KILLED'),
    ('the-selection-is-ignored', 'rep',
     "    sel = set(sm) | set(tm) if keys is None else set(keys)",
     "    sel = set(sm) | set(tm)",
     (PARTIAL,), 'KILLED'),
    ('sharedness-only-over-the-copied-parts', 'rep',
     "    s_nets, t_nets = _instance_nets(sm), _instance_nets(tm)",
     "    s_nets, t_nets = ({sp.net_id for _s, sp, _t, _tp, _g in rows}, "
     "{tp.net_id for _s, _sp, _t, tp, _g in rows})",
     (PARTIAL,), 'KILLED'),
    ('the-default-anchor-ignores-the-selection', 'rep',
     "        pool = copied or everywhere",
     "        pool = everywhere",
     (PARTIAL,), 'KILLED'),
    # ---- the grade's measurement ---------------------------------------------
    ('the-fit-is-the-mean-not-the-median', 'rep',
     "        fx, fy = _median([a for a, _b in d]), _median([b for _a, b in d])",
     "        fx, fy = (sum(a for a, _b in d) / len(d), "
     "sum(b for _a, b in d) / len(d))",
     (GRADE,), 'KILLED'),
    ('a-turned-part-reads-as-a-copy', 'rep',
     "                'rot_differs': _rot_differs(float(fs.rotation or 0.0),",
     "                'rot_differs': False and _rot_differs(float(fs.rotation "
     "or 0.0),",
     (GRADE,), 'KILLED'),
    # ---- place_seed's stage: apply_declared ----------------------------------
    ('a-refused-declared-copy-is-written-anyway', 'rep',
     "                intent=intent, group_sources=group_sources)",
     "                intent=intent, group_sources=group_sources, force=True)",
     (SEED_I,), 'KILLED'),
    ('the-seed-stage-never-runs', 'seed',
     "    if getattr(intent, 'replicate', ()):",
     "    if False:",
     (SEED_I,), 'KILLED'),
    # ---- --search -------------------------------------------------------------
    ('the-search-is-farthest-first', 'ops',
     "    out.sort()",
     "    out.sort(reverse=True)",
     (SEARCH,), 'KILLED'),
    ('the-search-lattice-ignores-the-radius', 'ops',
     "            if dist <= radius + 1e-9:",
     "            if True:",
     (SEARCH,), 'KILLED'),
    ('the-search-moves-a-locked-part', 'ops',
     "            if guarded & {r['ref'] for r in moves}:",
     "            if False:",
     (SEARCH,), 'KILLED'),
    ('a-later-target-blocks-an-earlier-one', 'ops',
     "        later = {r['ref'] for u in targets[k + 1:] for r in u['rows']}",
     "        later = set()",
     (ORDER,), 'KILLED'),
    ('the-search-never-runs', 'ops',
     "        if search_targets and (bad or (strict and not is_clean(after))):",
     "        if False:",
     (SEARCH, ORDER), 'KILLED'),
    ('an-off-board-member-is-not-named', 'ops',
     "            for rk in ('oob_pad_copper_refs', 'oob_keepout_copper_refs',",
     "            for rk in ('oob_keepout_copper_refs',",
     (PARTIAL,), 'KILLED'),
    ('an-edge-band-pad-is-not-named', 'ops',
     "                    fresh.setdefault(f['pad_ref'].partition('.')[0], "
     "[]).append(",
     "                    (lambda *a: None)(",
     (PARTIAL,), 'KILLED'),
    # ---- the verb -----------------------------------------------------------------
    ('the-declared-mode-takes-an-overriding-flag', 'cli',
     "                if given:",
     "                if False:",
     (POSE_I,), 'KILLED'),
    ('a-zero-search-radius-is-accepted', 'cli',
     "            if a.search is not None and not a.search > 0:",
     "            if False:",
     (SEARCH,), 'KILLED'),
    # Survives BY DESIGN: `replicate.resolve_selection` refuses the same pair
    # with the same words and exit code, so removing the CLI's early copy of
    # the check changes nothing a caller can see. Kept as the record that the
    # engine, not the CLI, owns this refusal.
    ('only-and-skip-together-at-the-cli', 'cli',
     "            if only and skip:",
     "            if False:",
     (PARTIAL,), 'SURVIVED'),
    # ---- the intent ----------------------------------------------------------
    ('the-loader-takes-a-target-twice', 'fp',
     "            if key in targets:",
     "            if False:",
     (LOADER,), 'KILLED'),
    ('the-loader-takes-only-with-skip', 'fp',
     "        if 'only' in r and 'skip' in r:",
     "        if False:",
     (LOADER,), 'KILLED'),
    ('the-rule-grades-at-error', 'fp',
     "    sev = ctx.intent.severity_of('replicate', default=WARN)",
     "    sev = ctx.intent.severity_of('replicate', default=ERROR)",
     (GRADE,), 'KILLED'),
    ('an-unresolved-row-reads-as-a-distance', 'fp',
     "                rule='replicate_unresolved', severity=sev_unres,",
     "                rule='replicate', severity=sev_unres,",
     (GRADE,), 'KILLED'),
    # ---- the parsed sheet fields --------------------------------------------
    ('a-sheet-name-is-read-past-the-first-pad', 'kp',
     "    head = fp_text[:first_pad] if first_pad > 0 else fp_text",
     "    head = fp_text",
     (SHEETS,), 'KILLED'),
    ('a-sheet-name-keeps-its-escapes', 'kp',
     "        out.append(_unescape_kicad_string(m.group(1)) if m else '')",
     "        out.append(m.group(1) if m else '')",
     (SHEETS,), 'KILLED'),
    ('the-pcbnew-path-reads-no-sheet-name', 'kp',
     "            fp_sheet_name = str(fp.GetSheetname())",
     "            fp_sheet_name = ''",
     (_w(GATE),), 'KILLED'),
    ('the-pcbnew-path-reads-no-sheet-file', 'kp',
     "            fp_sheet_file = str(fp.GetSheetfile())",
     "            fp_sheet_file = ''",
     (_w(GATE),), 'KILLED'),
    ('compare_pcb_data-skips-the-sheet-fields', 'kp',
     "        for _sheet in ('sheet_path', 'sheet_name', 'sheet_file'):",
     "        for _sheet in ():",
     (_w(GATE),), 'KILLED'),
]

# Every anchor must match its target exactly once BEFORE anything is
# rewritten (#877).
from mutation_anchors import preflight   # noqa: E402
preflight(__file__)


def _uncache(path):
    """Delete the target's cached bytecode: CPython trusts a `.pyc` on
    (mtime seconds, size), and several rows are same-size edits."""
    import importlib
    import importlib.util
    try:
        cached = importlib.util.cache_from_source(path)
        if os.path.exists(cached):
            os.remove(cached)
    except (OSError, ValueError, NotImplementedError):
        pass
    importlib.invalidate_caches()


def _dirty(path):
    p = subprocess.run(['git', 'status', '--porcelain', '--', path],
                       capture_output=True, text=True, cwd=_ROOT)
    return bool(p.stdout.strip())


def _run_test(witness):
    return subprocess.run([sys.executable, '-X', 'utf8'] + list(witness),
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=3600, cwd=_ROOT)


def _label(witness):
    return ' '.join([os.path.basename(witness[0])] + list(witness[1:]))


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

    # The battery is only evidence if every killer passes UNMUTATED first.
    # The parity gate exits 2 without pcbnew: that is BROKEN here, so its
    # rows can never be scored SURVIVED on an environment accident.
    for path in TARGETS.values():
        _uncache(path)
    for w in sorted({w for row in rows for w in row[4]}):
        p = _run_test(w)
        if p.returncode:
            print('BROKEN: %s fails on the UNMUTATED tree (exit %d); every '
                  'verdict below would be meaningless.'
                  % (_label(w), p.returncode))
            return 2
        print('  unmutated %-50s passes' % _label(w))

    orig = {k: io.open(v, encoding='utf-8', newline='').read()
            for k, v in TARGETS.items()}
    results = []
    try:
        for name, tgt, old, new, witnesses, expect in rows:
            path, base = TARGETS[tgt], orig[tgt]
            n = base.count(old)
            if n != 1:
                results.append((name, 'BROKEN', expect,
                                'anchor matched %d times' % n, []))
                print('  ran %-52s BROKEN' % name)
                continue
            io.open(path, 'w', encoding='utf-8', newline='').write(
                base.replace(old, new, 1))
            _uncache(path)
            killed, failed = False, []
            for w in witnesses:
                p = _run_test(w)
                out = (p.stderr or '') + (p.stdout or '')
                if p.returncode:
                    killed = True
                failed += [l.strip()[:90] for l in out.splitlines()
                           if l.strip().startswith(('FAIL', 'AssertionError'))]
                if 'Traceback' in out:
                    failed.append('raised: '
                                  + out.strip().splitlines()[-1][:70])
                if killed:
                    break
            io.open(path, 'w', encoding='utf-8', newline='').write(base)
            _uncache(path)
            results.append((name, 'KILLED' if killed else 'SURVIVED', expect,
                            '%d' % len(failed), failed[:2]))
            print('  ran %-52s %s' % (name, results[-1][1]))
    finally:
        for k, v in TARGETS.items():
            io.open(v, 'w', encoding='utf-8', newline='').write(orig[k])
            _uncache(v)

    print()
    width = max(len(r[0]) for r in results)
    wrong = 0
    for name, verdict, expect, cnt, failed in results:
        mark = ''
        if verdict != expect:
            mark = '   <-- WRONG, expected %s' % expect
            wrong += 1
        print('%-*s  %-9s  %-3s%s' % (width, name, verdict, cnt, mark))
        for f in failed:
            print('%s      %s' % (' ' * width, f))
    killed = sum(1 for r in results if r[1] == 'KILLED')
    survived = sum(1 for r in results if r[1] == 'SURVIVED')
    broken = sum(1 for r in results if r[1] == 'BROKEN')
    print('\n%d rows: %d killed, %d survived (%d of them expected), %d broken'
          % (len(results), killed, survived,
             sum(1 for r in results if r[1] == r[2] == 'SURVIVED'), broken))
    if wrong or broken:
        print('%d row(s) did not match their expectation' % (wrong + broken))
    return 1 if (wrong or broken) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--row', default=None, help='run one row by name')
    ap.add_argument('--list', action='store_true', help='list the row names')
    a = ap.parse_args()
    if a.list:
        for r in ROWS:
            print('%-52s %-4s %s' % (r[0], r[1], r[5]))
        return 0
    return run(a.row)


if __name__ == '__main__':
    sys.exit(main())
