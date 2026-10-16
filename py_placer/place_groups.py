#!/usr/bin/env python3
"""Place a board ONE FUNCTIONAL GROUP AT A TIME, each group in its own region.

The groups (`placement/grouping.py`'s file, checked by
`py_tools/check_groups.py`) say which parts belong together; the regions are
the zones `py_placer/plan_regions.py` wrote into an intent. This tool runs the
loop:

  for each group, in order (the file's `order`, else the anchor group first
  -- the core -- then the group most wired to what is already placed):
    1. build a STEP board from the board so far: the groups not yet placed
       are DELETED from it (they do not exist yet: no obstacle, no centroid
       partner, no pile pulling a seat off the board), and the groups already
       placed are stamped (locked yes) -- on the step board only;
    2. trim the intent to the parts the step board has;
    3. run `place_seed` on the step board with `--decap-owners` naming this
       group (a cap is claimed at its own group's IC, never at a chip of a
       group placed earlier), its polish gated by the zones;
    4. copy the group's poses back onto the board.

Then the whole board is graded against the FULL intent (`floorplan.grade`),
`check_assembly`, and the airwire crossings and HPWL the optimizer prices
(`render_placement.PlacementModel`). `--final-polish MM` runs
`place_optimize --intent` over the assembled board and KEEPS it only if it
is no worse on buildable, floorplan errors, and (crossings, hpwl).

    python3 py_placer/place_groups.py board.kicad_pcb placed.kicad_pcb \\
        --groups groups.json --intent regions_intent.json

The input board is never modified, and every lock this tool stamps lives on a
step board: the output carries the input's own locks and no others.

Exit codes: 0 placed and graded clean; 2 bad input (unreadable, groups that
cannot be placed: check_groups' errors); 3 a step's place_seed did not write
a board; 4 placed, but the grade found something (floorplan errors on parts
this tool placed, not buildable, or a part left unseated) -- the board is
written for inspection. A floorplan error on a part locked in the input
board is printed and counted (`floorplan_errors_pinned`) and does not fail
the run, as in place_seed: no group placed it.
"""
#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement', 'combined'], 'kind': 'actor'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time

from kicad_parser import iter_footprint_blocks, parse_kicad_pcb  # noqa: E402
from place_seed import _split_pinned  # noqa: E402
from placement.grouping import (BoardView, GroupsError, analyze,  # noqa: E402
                                fold_followers, load_groups, placement_order)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


# ---------------------------------------------------------------------------
# step boards and step intents
# ---------------------------------------------------------------------------

def write_step_board(src: str, dst: str, drop, lock) -> None:
    """`src` without the footprint blocks keyed in `drop`, with
    `(locked yes)` stamped on `lock`, written to `dst` with its siblings."""
    from copy_board import copy_siblings
    from placement import seeder
    text = open(src, encoding='utf-8').read()
    drop = set(drop)
    cut = []
    for start, end, _txt, _raw, key in iter_footprint_blocks(text):
        if key in drop:
            # take the block's own indentation and its line break with it
            a = start
            while a > 0 and text[a - 1] in ' \t':
                a -= 1
            b = end
            while b < len(text) and text[b] in ' \t':
                b += 1
            if b < len(text) and text[b] == '\n':
                b += 1
            cut.append((a, b))
    for a, b in sorted(cut, reverse=True):
        text = text[:a] + text[b:]
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(text)
    copy_siblings(src, dst)
    if lock:
        seeder.stamp_locked(dst, sorted(lock))


def trim_intent(intent: dict, present: set) -> dict:
    """`intent` naming only the refs in `present`: a row about an absent
    part is dropped (a block keeps its present refs), so a step board's grade
    neither refuses a block that resolves to nothing nor reports a part that
    is simply not placed yet."""
    out = copy.deepcopy(intent)
    blocks = []
    for b in out.get('blocks') or []:
        refs = [r for r in b.get('refs') or [] if r in present]
        if b.get('refs') is not None:
            if not refs:
                continue
            b['refs'] = refs
        blocks.append(b)
    out['blocks'] = blocks
    for key, fields in (('edge_connectors', ('ref',)), ('fixed_poses', ('ref',)),
                        ('proximity', ('ref', 'near'))):
        if out.get(key):
            out[key] = [row for row in out[key]
                        if all(row.get(f) in present for f in fields if row.get(f))]
    for row in out.get('fixed_poses') or []:
        if row.get('accept_courtyard_overlap'):
            row['accept_courtyard_overlap'] = [r for r in row['accept_courtyard_overlap']
                                               if r in present]
    if out.get('arrays'):
        out['arrays'] = [a for a in out['arrays']
                         if all(m in present for m in a.get('members') or [])
                         and (a.get('serves') in (None, 'unknown') or a['serves'] in present)]
    if out.get('replicate'):
        out['replicate'] = []
    if out.get('overlap_waivers'):
        out['overlap_waivers'] = [w for w in out['overlap_waivers']
                                  if all(r in present for r in w.get('pair') or [])]
    for k in out.get('keepouts') or []:
        if k.get('allow'):
            k['allow'] = [r for r in k['allow'] if r in present]
    dec = out.get('decaps')
    if dec and dec.get('within_radius_refs') is not None:
        dec['within_radius_refs'] = [r for r in dec['within_radius_refs'] if r in present]
    return out


def run_place_seed(step_in, step_out, intent_path, seed, owners, extra, log):
    """One group's step. `owners` go to `--decap-owners`: the decap pin
    stages seat a cap only at this group's ICs, never at a chip of a group
    placed earlier that shares the rail (esp_prog: C3 on +3.3V belongs at the
    LDO, not the CH340 placed before it -- exempting it from the stage
    instead sent it through the zone pack, onto the LDO's tab copper)."""
    cmd = [sys.executable, '-X', 'utf8', os.path.join(HERE, 'place_seed.py'),
           step_in, step_out, '--intent', intent_path, '--seed', str(seed),
           '--decap-owners'] + sorted(owners) + list(extra)
    r = subprocess.run(cmd, capture_output=True, text=True)
    with open(log, 'w', encoding='utf-8') as f:
        f.write(' '.join(cmd) + '\n' + r.stdout + r.stderr)
    summary = {}
    for line in r.stdout.splitlines():
        if line.startswith('JSON_SUMMARY: '):
            try:
                summary = json.loads(line[len('JSON_SUMMARY: '):])
            except ValueError:
                pass
    return r.returncode, summary


# ---------------------------------------------------------------------------
# grading the assembled board
# ---------------------------------------------------------------------------

def grade(board: str, intent_path: str, baseline: str) -> dict:
    """buildable, floorplan errors, crossings and hpwl for `board`."""
    from placement import floorplan
    from placement.groups import parse_sources
    from render_placement import PlacementModel
    pcb = parse_kicad_pcb(board)
    intent = floorplan.load_intent(intent_path)
    # Blocks resolve as place_seed resolves them (its --group-by default), so
    # an emitted intent's `sheet:` block is found on a hierarchical board.
    g = floorplan.grade(intent, pcb, board, group_sources=parse_sources('auto'))
    own, pinned = _split_pinned(g, board, intent)
    errors = [f"[{v.rule}] {v.message}" for v in own]
    m = PlacementModel(parse_kicad_pcb(board), board).metrics
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(REPO, 'py_tools', 'check_assembly.py'), board,
                        '--baseline', baseline], capture_output=True, text=True)
    verdict = next((ln.split('VERDICT:', 1)[1].strip() for ln in r.stdout.splitlines()
                    if 'VERDICT:' in ln), '')
    return {'buildable': verdict.startswith('buildable'), 'verdict': verdict,
            'floorplan_errors': errors,
            'floorplan_errors_pinned': [f"[{v.rule}] {v.message}" for v in pinned],
            'crossings': m.get('crossings'),
            'hpwl': round(float(m.get('hpwl') or 0.0), 2)}


def not_worse(after: dict, before: dict) -> bool:
    """`after` keeps buildable and floorplan errors, and improves
    (crossings, hpwl) lexicographically."""
    if before['buildable'] and not after['buildable']:
        return False
    if len(after['floorplan_errors']) > len(before['floorplan_errors']):
        return False
    return (after['crossings'], after['hpwl']) < (before['crossings'], before['hpwl'])


# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('board', help='the board: fixed parts placed and locked, the rest anywhere')
    ap.add_argument('output', help='the placed board to write')
    ap.add_argument('--groups', required=True, help='the groups JSON file')
    ap.add_argument('--intent', required=True,
                    help="the intent with one zone per group (plan_regions.py's output)")
    ap.add_argument('--order', help='comma-separated group order, overriding the '
                    "file's `order` and the computed one")
    ap.add_argument('--seed', type=int, default=0,
                    help='place_seed --seed for every step (default: %(default)s)')
    ap.add_argument('--seed-arg', action='append', default=[], metavar='ARG',
                    help='an extra place_seed argument for every step, repeatable '
                         "(e.g. --seed-arg=--no-polish)")
    ap.add_argument('--final-polish', type=float, default=0.0, metavar='MM',
                    help='after the last group, run place_optimize --intent with '
                         'this --max-displacement over the whole board and keep it '
                         'only if no worse (0 = off, the default)')
    ap.add_argument('--workdir', help='where step boards go (default: <output>.steps/)')
    ap.add_argument('--keep-steps', action='store_true',
                    help='keep the step boards and logs (default: deleted when the run succeeds)')
    ap.add_argument('--json', dest='json_out', help='write the run report here')
    a = ap.parse_args(argv)

    try:
        doc = load_groups(a.groups)
        intent = json.load(open(a.intent, encoding='utf-8'))
        pcb = parse_kicad_pcb(a.board)
    except (GroupsError, OSError, ValueError) as ex:
        print(f"place_groups: {ex}", file=sys.stderr)
        return 2
    rep = analyze(doc, pcb)
    if rep['errors']:
        for e in rep['errors']:
            print(f"place_groups: {e}", file=sys.stderr)
        print("place_groups: the groups cannot be placed as written -- see "
              "py_tools/check_groups.py", file=sys.stderr)
        return 2
    # A claim's subject is seated beside its partner (the seeder's followers
    # stage), so it is placed in its partner's step: plan_regions folded it
    # into that group's region by the same rule.
    names_before = {g['name'] for g in doc['groups']}
    doc, moved = fold_followers(doc, intent.get('proximity') or [])
    for ref, (src, dst) in sorted(moved.items()):
        print(f"  {ref} follows its proximity partner: placed with group "
              f"{dst!r}, not {src!r}")
    gone = names_before - {g['name'] for g in doc['groups']}
    view = BoardView(pcb)
    order = a.order.split(',') if a.order else placement_order(doc, view)
    order = [n for n in order if n not in gone]
    unknown = [n for n in order if n not in {g['name'] for g in doc['groups']}]
    if unknown:
        print(f"place_groups: --order names no group {unknown}", file=sys.stderr)
        return 2
    movable = {g['name']: [r for r in g['refs'] if r in view.parts and r not in view.locked]
               for g in doc['groups']}
    order = [n for n in order if movable.get(n)]
    workdir = a.workdir or (os.path.splitext(a.output)[0] + '.steps')
    os.makedirs(workdir, exist_ok=True)

    from copy_board import copy_board
    from placement.provenance import declare_lever
    from placement.writer import write_placed_output

    cur = os.path.join(workdir, 'board_00.kicad_pcb')
    copy_board(a.board, cur)
    steps = []
    placed_refs: set = set()
    t_all = time.time()
    with declare_lever('place_groups.py', sys.argv):
        for k, name in enumerate(order, 1):
            t0 = time.time()
            refs = movable[name]
            future = {r for n in order[k:] for r in movable[n]}
            present = (set(view.parts) | set(pcb.footprints)) - future
            tag = f"{k:02d}_{re.sub(r'[^A-Za-z0-9_]+', '_', name)}"
            step_in = os.path.join(workdir, f"step_{tag}.in.kicad_pcb")
            step_out = os.path.join(workdir, f"step_{tag}.kicad_pcb")
            step_intent = os.path.join(workdir, f"step_{tag}.intent.json")
            write_step_board(cur, step_in, future, placed_refs)
            si = trim_intent(intent, present)
            with open(step_intent, 'w', encoding='utf-8') as f:
                json.dump(si, f, indent=1)
            owners = [r for g in doc['groups'] if g['name'] == name
                      for r in g['refs'] if r in view.parts]
            rc, summ = run_place_seed(step_in, step_out, step_intent, a.seed,
                                      owners, a.seed_arg,
                                      os.path.join(workdir, f"step_{tag}.log"))
            if not os.path.isfile(step_out):
                print(f"place_groups: step {k} ({name}) wrote no board "
                      f"(place_seed exit {rc}; see {workdir})", file=sys.stderr)
                return 3
            out_pcb = parse_kicad_pcb(step_out)
            moves = []
            for r in refs:
                fp = out_pcb.footprints.get(r)
                if fp is not None:
                    moves.append({'reference': r, 'new_x': fp.x, 'new_y': fp.y,
                                  'new_rotation': fp.rotation or 0.0})
            nxt = os.path.join(workdir, f"board_{k:02d}.kicad_pcb")
            write_placed_output(cur, nxt, moves)
            from copy_board import copy_siblings
            copy_siblings(cur, nxt)
            cur = nxt
            placed_refs |= set(refs)
            unseated = [r for r in (summ.get('unseated_refs') or []) if r in refs]
            steps.append({'group': name, 'refs': refs, 'seconds': round(time.time() - t0, 2),
                          'place_seed_exit': rc, 'unseated': unseated})
            print(f"  [{k}/{len(order)}] {name:<12} {len(refs)} part(s) in "
                  f"{time.time() - t0:.1f}s, place_seed exit {rc}"
                  + (f", UNSEATED {', '.join(unseated)}" if unseated else ''))

        final = grade(cur, a.intent, a.board)
        polish = None
        if a.final_polish > 0:
            pol = os.path.join(workdir, 'board_polished.kicad_pcb')
            r = subprocess.run([sys.executable, '-X', 'utf8',
                                os.path.join(HERE, 'place_optimize.py'), cur, pol,
                                '--intent', a.intent,
                                '--max-displacement', str(a.final_polish)],
                               capture_output=True, text=True)
            polish = {'exit': r.returncode, 'kept': False}
            if os.path.isfile(pol):
                after = grade(pol, a.intent, a.board)
                polish['after'] = after
                if not_worse(after, final):
                    cur, final, polish['kept'] = pol, after, True
            print(f"  final polish (max {a.final_polish} mm): "
                  f"{'KEPT' if polish['kept'] else 'not kept'}")
        copy_board(cur, a.output)

    seconds = round(time.time() - t_all, 2)
    unseated_all = [r for s in steps for r in s['unseated']]
    print(f"  placed {sum(len(s['refs']) for s in steps)} part(s) in {len(steps)} "
          f"group(s), {seconds}s: {final['verdict']}; floorplan errors "
          f"{len(final['floorplan_errors'])}; crossings {final['crossings']}; "
          f"hpwl {final['hpwl']} mm")
    for e in final['floorplan_errors']:
        print(f"    {e}")
    if final['floorplan_errors_pinned']:
        print(f"  {len(final['floorplan_errors_pinned'])} floorplan error(s) sit on "
              f"part(s) locked in the input board, which no group placed -- "
              f"reported, not counted against the run:")
        for e in final['floorplan_errors_pinned']:
            print(f"    (pinned) {e}")
    report = {'board': a.board, 'output': a.output, 'order': order, 'steps': steps,
              'final': final, 'polish': polish, 'seconds': seconds,
              'unseated': unseated_all}
    if a.json_out:
        with open(a.json_out, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=1)
    if not a.keep_steps and os.path.isdir(workdir) and not unseated_all:
        shutil.rmtree(workdir, ignore_errors=True)
    print('JSON_SUMMARY: ' + json.dumps({
        'buildable': final['buildable'], 'floorplan_errors': len(final['floorplan_errors']),
        'floorplan_errors_pinned': len(final['floorplan_errors_pinned']),
        'crossings': final['crossings'], 'hpwl': final['hpwl'], 'order': order,
        'unseated': unseated_all, 'seconds': seconds,
        'polish_kept': (polish or {}).get('kept')}, sort_keys=True))
    bad = unseated_all or final['floorplan_errors'] or not final['buildable']
    return 4 if bad else 0


if __name__ == '__main__':
    import cli_banner
    cli_banner.install()   # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
