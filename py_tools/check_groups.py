#!/usr/bin/env python3
"""Check a groups file against its board, before `place_simple.py` places it.

The groups -- which parts belong together functionally -- are an agent's
judgement (see `.claude/skills/plan-pcb-placement/SKILL.md`). This instrument
refuses a file that cannot be placed and measures the judgement so it can be
checked before any part moves:

  ERRORS (exit 4)  a movable part in no group, a part in two groups, a ref the
                   board does not have, a group with no part on the board.
                   A part is fixed, not movable, when the board locks it or
                   the intent (`--intent`) seats it at a declared pose
                   (`fixed_poses[]`), as place_simple holds it.
  WARNINGS         a part with more SIGNAL links to another group than to its
                   own (rails left out: `placement.grouping.rail_net_ids`); a
                   decap whose IC is in another group and no IC of its own
                   group is on its rail (`place_simple` then leaves it out of
                   the decap term: right for a load or RC cap, wrong for a
                   real decap).
  REPORTED         bridges (2-pad parts linking two groups), the inter-group
                   signal-link matrix, and the rails.
  CLAIMS           the parts place_simple seats by a proximity claim -- the
                   intent's `proximity[]` rows, and each passive crystal with
                   its load caps at its IC's oscillator pins
                   (`placement/crystals.py`) -- and the group each follows
                   its partner into. A claimed cap is no decap conflict
                   (`--no-crystals`: checked as if no crystal claim were
                   written).

    python3 py_tools/check_groups.py board.kicad_pcb --groups groups.json --intent intent.json
    python3 py_tools/check_groups.py board.kicad_pcb --groups groups.json --json out.json

Exit codes: 0 placeable (warnings allowed), 2 unreadable input, 4 errors.
"""
#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement', 'combined'], 'kind': 'instrument'}

import _path  # noqa: F401  (py_tools -> py_router/py_placer on sys.path)

import argparse
import json
import sys

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement.grouping import (GroupsError, analyze, claim_rows,  # noqa: E402
                                fold_followers, follower_roots, load_groups)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('board', help='the board the groups are for')
    ap.add_argument('--groups', required=True, help='the groups JSON file')
    ap.add_argument('--json', dest='json_out', help='write the full report here')
    ap.add_argument('--intent', help="the intent place_simple will read (check_floorplan "
                    "--emit-intent): the parts it seats at a declared pose are "
                    "fixed, and its proximity claims are followed, as place_simple does")
    ap.add_argument('--no-crystals', action='store_true',
                    help="check as if no crystal claim were written")
    a = ap.parse_args(argv)
    try:
        doc = load_groups(a.groups)
    except GroupsError as ex:
        print(f"check_groups: {ex}", file=sys.stderr)
        return 2
    try:
        pcb = parse_kicad_pcb(a.board)
    except Exception as ex:  # an unreadable board is an input error, not a finding
        print(f"check_groups: cannot read {a.board}: {ex}", file=sys.stderr)
        return 2
    intent_doc, seated = {}, []
    if a.intent:
        from placement import floorplan
        try:
            with open(a.intent, encoding='utf-8') as f:
                intent_doc = json.load(f)
            intent = floorplan.load_intent(a.intent)
        except (OSError, ValueError) as ex:
            print(f"check_groups: cannot read {a.intent}: {ex}", file=sys.stderr)
            return 2
        seated = sorted({str(f['ref']) for f in intent.fixed_poses
                         if str(f['ref']) in (pcb.footprints or {})})
    rows, crystal_notes = claim_rows(pcb, intent_doc, crystals=not a.no_crystals)
    followers = sorted(follower_roots(rows))
    for line in crystal_notes:
        print(f"  {line} -- seated by its claim, beside the IC")
    if seated:
        print(f"  at a declared pose (fixed): {', '.join(seated)}")
    _folded, moved = fold_followers(doc, rows)
    if moved:
        print("  follow their claim into their partner's group: " + ', '.join(
            f"{r} ({f} -> {t})" for r, (f, t) in sorted(moved.items())))
    rep = analyze(doc, pcb, followers=followers, extra_fixed=seated)

    for g in rep['groups']:
        fixed = f"  fixed: {', '.join(g['fixed'])}" if g['fixed'] else ''
        print(f"  {g['name']:<14} {g.get('role') or '-':<11} "
              f"{len(g['movable'])} movable: {', '.join(g['movable'])}{fixed}")
    if rep['links']:
        print("  signal links between groups (rails excluded):")
        for row in rep['links']:
            print(f"    {row['a']} <-> {row['b']}: {row['signal_links']:.3g}")
    print(f"  rails: {', '.join(rep['rails']) or 'none'}")
    if rep['bridges']:
        print("  bridges: " + ', '.join(f"{b['ref']} ({b['group']} | {'/'.join(b['borders'])})"
                                        for b in rep['bridges']))
    for w in rep['warnings']:
        print(f"  WARN  {w}")
    for e in rep['errors']:
        print(f"  ERROR {e}")
    if a.json_out:
        with open(a.json_out, 'w', encoding='utf-8') as f:
            json.dump(rep, f, indent=1, sort_keys=True)
    summary = {'errors': len(rep['errors']), 'warnings': len(rep['warnings']),
               'groups': len(rep['groups']), 'bridges': len(rep['bridges']),
               'pulled': [p['ref'] for p in rep['pulled']],
               'decap_cross_group': [d['cap'] for d in rep['decap_cross_group']],
               'crystals': crystal_notes, 'followers': followers,
               'seated': seated}
    print('JSON_SUMMARY: ' + json.dumps(summary, sort_keys=True))
    return 4 if rep['errors'] else 0


if __name__ == '__main__':
    import cli_banner
    cli_banner.install()   # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
