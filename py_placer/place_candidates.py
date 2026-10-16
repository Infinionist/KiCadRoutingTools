#!/usr/bin/env python3
"""Place a grouped board several ways AT ONCE and keep the best candidate.

Grouped placement has two flows over the same regions (`plan_regions.py`'s
intent): one pass (`place_seed --intent`) and group by group
(`place_groups.py`). Neither wins on every board, and each takes seconds, so
`/pcb-group-place` places a few seeds of each and keeps the best. This runs
those candidates in parallel and ranks them by the skill's own order:

  1. buildable (`check_assembly`);
  2. then floorplan errors on parts the run placed;
  3. then airwire crossings;
  4. then HPWL.

    python3 py_placer/place_candidates.py fixed.kicad_pcb placed.kicad_pcb \\
        --groups groups.json --intent regions_intent.json

The best candidate is copied to the output with its siblings; every candidate
stays in the work directory (`<output>.candidates/` by default) with its log,
and `--json` writes the ranking. Each candidate is graded the way
`place_groups.py` grades its own board (`place_groups.grade`), so the flows are
ranked on one measure. The input board is never modified.

Candidates run as separate processes, `--jobs` at a time (default 4: a
placement is one process of a few hundred MB, and the default keeps four of
them under 2 GB).

Exit codes: 0 the best candidate is buildable with no floorplan error;
2 bad input; 3 no candidate wrote a board; 4 the best candidate was written,
but its grade found something.
"""
#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement', 'combined'], 'kind': 'actor'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
FLOWS = ('one', 'loop')
#: `--seeds auto`: three seeds of each flow up to this many movable parts,
#: one above it. A candidate's time grows faster than the part count, and
#: on four performance cores more candidates than cores mostly wait: a
#: 272-part board (glasgow_revC) takes 96 s for one seed of each flow and
#: 172 s for two, against 30 s for three seeds on a 92-part one (tigard).
AUTO_SEEDS_MAX_PARTS = 150


def auto_seeds(board):
    """`--seeds auto` for `board`: (seeds, movable part count)."""
    from kicad_parser import parse_kicad_pcb
    from placement.grouping import BoardView
    view = BoardView(parse_kicad_pcb(board))
    movable = len(set(view.parts) - set(view.locked))
    return ('0,1,2' if movable <= AUTO_SEEDS_MAX_PARTS else '0'), movable


def _summary(stdout):
    for line in stdout.splitlines():
        if line.startswith('JSON_SUMMARY: '):
            try:
                return json.loads(line[len('JSON_SUMMARY: '):])
            except ValueError:
                return {}
    return {}


def _grade_cmd(board, intent, baseline):
    """A subprocess that prints `place_groups.grade` of `board` as JSON."""
    code = ("import json, sys; sys.path.insert(0, %r); import _path; "
            "import place_groups; "
            "print('GRADE_JSON: ' + json.dumps(place_groups.grade(%r, %r, %r)))"
            % (HERE, board, intent, baseline))
    return [sys.executable, '-X', 'utf8', '-c', code]


def run_candidate(flow, seed, a, workdir):
    """Place one candidate and grade it; a dict for the ranking."""
    out = os.path.join(workdir, f'{flow}_s{seed}.kicad_pcb')
    log = os.path.join(workdir, f'{flow}_s{seed}.log')
    if flow == 'one':
        cmd = [sys.executable, '-X', 'utf8', os.path.join(HERE, 'place_seed.py'),
               a.board, out, '--intent', a.intent, '--seed', str(seed)]
    else:
        cmd = [sys.executable, '-X', 'utf8', os.path.join(HERE, 'place_groups.py'),
               a.board, out, '--groups', a.groups, '--intent', a.intent,
               '--seed', str(seed), '--workdir', os.path.join(workdir, f'loop_s{seed}.steps')]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    row = {'flow': flow, 'seed': seed, 'board': out, 'place_exit': r.returncode,
           'place_s': round(time.time() - t0, 2)}
    text = ' '.join(cmd) + '\n' + r.stdout + r.stderr
    if not os.path.isfile(out):
        row['error'] = f"no board written (exit {r.returncode})"
        open(log, 'w', encoding='utf-8').write(text)
        return row
    summ = _summary(r.stdout) if flow == 'loop' else {}
    if flow == 'loop' and 'buildable' in summ:
        # place_groups graded its own board with `place_groups.grade`.
        grade = {'buildable': summ['buildable'],
                 'floorplan_errors': summ.get('floorplan_errors', 0),
                 'crossings': summ.get('crossings'), 'hpwl': summ.get('hpwl')}
    else:
        g = subprocess.run(_grade_cmd(out, a.intent, a.board), capture_output=True, text=True)
        text += '\n--- grade ---\n' + g.stdout + g.stderr
        got = next((ln[len('GRADE_JSON: '):] for ln in g.stdout.splitlines()
                    if ln.startswith('GRADE_JSON: ')), None)
        if got is None:
            row['error'] = f"grade failed (exit {g.returncode})"
            open(log, 'w', encoding='utf-8').write(text)
            return row
        full = json.loads(got)
        grade = {'buildable': full['buildable'],
                 'floorplan_errors': len(full['floorplan_errors']),
                 'crossings': full['crossings'], 'hpwl': full['hpwl']}
    open(log, 'w', encoding='utf-8').write(text)
    row.update(grade)
    return row


def rank_key(row):
    """The skill's order: buildable, floorplan errors, crossings, HPWL; then
    the flow and seed, so equal candidates rank the same way every time."""
    return (not row.get('buildable', False), row.get('floorplan_errors', 1 << 30),
            row.get('crossings') if row.get('crossings') is not None else 1 << 30,
            row.get('hpwl') if row.get('hpwl') is not None else float('inf'),
            FLOWS.index(row['flow']), row['seed'])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('board', help='the board: fixed parts placed and locked, the rest anywhere')
    ap.add_argument('output', help='where the best candidate is written')
    ap.add_argument('--groups', required=True, help='the groups JSON file')
    ap.add_argument('--intent', required=True,
                    help="the intent with one zone per group (plan_regions.py's output)")
    ap.add_argument('--seeds', default='auto',
                    help="comma-separated seeds, each placed by every flow; 'auto' (the "
                         "default) is 0,1,2 up to %d movable parts and 0 above"
                         % AUTO_SEEDS_MAX_PARTS)
    ap.add_argument('--flows', default='one,loop',
                    help="comma-separated flows: 'one' (place_seed --intent, one pass) and "
                         "'loop' (place_groups.py, group by group) (default: %(default)s)")
    ap.add_argument('--jobs', type=int, default=4,
                    help='candidates placed at once (default: %(default)s)')
    ap.add_argument('--workdir', help='where the candidates go (default: <output>.candidates/)')
    ap.add_argument('--json', dest='json_out', help='write the ranking here')
    a = ap.parse_args(argv)
    if a.seeds == 'auto' and os.path.isfile(a.board):
        a.seeds, movable = auto_seeds(a.board)
        print(f"  seeds auto: {a.seeds} ({movable} movable part(s))")
    try:
        seeds = [int(s) for s in a.seeds.split(',') if s.strip() != '']
    except ValueError:
        print(f"place_candidates: --seeds {a.seeds!r} is not a list of integers", file=sys.stderr)
        return 2
    flows = [f.strip() for f in a.flows.split(',') if f.strip()]
    bad = [f for f in flows if f not in FLOWS]
    if bad or not flows or not seeds:
        print(f"place_candidates: --flows takes {', '.join(FLOWS)}, and --seeds at least one "
              f"seed (got flows {flows}, seeds {seeds})", file=sys.stderr)
        return 2
    for path in (a.board, a.groups, a.intent):
        if not os.path.isfile(path):
            print(f"place_candidates: no such file {path}", file=sys.stderr)
            return 2
    workdir = a.workdir or (os.path.splitext(a.output)[0] + '.candidates')
    os.makedirs(workdir, exist_ok=True)
    jobs = [(f, s) for f in flows for s in seeds]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        rows = list(pool.map(lambda fs: run_candidate(fs[0], fs[1], a, workdir), jobs))
    seconds = round(time.time() - t0, 2)
    ok = sorted((r for r in rows if 'error' not in r), key=rank_key)
    for r in sorted(rows, key=lambda r: (FLOWS.index(r['flow']), r['seed'])):
        if 'error' in r:
            print(f"  {r['flow']:<4} s{r['seed']}: {r['error']} ({r['place_s']}s)")
        else:
            print(f"  {r['flow']:<4} s{r['seed']}: {'buildable' if r['buildable'] else 'NOT buildable'}, "
                  f"floorplan errors {r['floorplan_errors']}, crossings {r['crossings']}, "
                  f"hpwl {r['hpwl']} mm ({r['place_s']}s)")
    if not ok:
        print("place_candidates: no candidate wrote a board -- see the logs in " + workdir,
              file=sys.stderr)
        return 3
    best = ok[0]
    from copy_board import copy_board
    copy_board(best['board'], a.output)
    print(f"  kept {best['flow']} s{best['seed']} -> {a.output} ({len(rows)} candidate(s), "
          f"{a.jobs} at a time, {seconds}s)")
    report = {'best': best, 'candidates': rows, 'seconds': seconds, 'workdir': workdir}
    if a.json_out:
        with open(a.json_out, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=1)
    print('JSON_SUMMARY: ' + json.dumps({
        'best': {k: best[k] for k in ('flow', 'seed', 'buildable', 'floorplan_errors',
                                      'crossings', 'hpwl')},
        'candidates': len(rows), 'failed': len(rows) - len(ok), 'seconds': seconds},
        sort_keys=True))
    return 0 if best['buildable'] and not best['floorplan_errors'] else 4


if __name__ == '__main__':
    import cli_banner
    cli_banner.install()   # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
