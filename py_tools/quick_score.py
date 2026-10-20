#!/usr/bin/env python3
"""Score a `route.py --quick 2` board: the seed judge's score.

    quick_score.py FAST.kicad_pcb [--json OUT]

- **blocking** is the board's own connectivity, graded the way `board_score`
  grades a full route (`check_connected`: unrouted nets plus the joins each
  broken net still needs), over the nets WITHOUT a zone. Level 2 skips the
  plane finalize, the step that taps a poured net's pads into its pour, so a
  poured net's open pads say what the fast route skipped, not what the
  placement allows: they are reported as `poured` and left out of blocking.
  The board is graded, not the route step's tally, because a chain's later
  route steps each tally only their own `--nets` (a one-net retry reports
  `already_connected` and nothing about the rest of the board).
- **quality** (vias, copper_mm, segments) is `board_score`'s own, counted on
  FAST.kicad_pcb.

DRC, assembly and the floorplan are left out: level 2 skips the passes that
fix grazes, so DRC on its board measures the polish it skipped, not the
placement. The keys are `board_score --json`'s (`blocking`, `blocking_by`,
`quality`), so a seed's `s<S>_fast.score.json` reads the same either way.
Rank seeds by blocking, then vias, copper_mm, segments. Never grade a board
to ship with it: route that one in full and run `board_score`.
"""

#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['placement', 'combined'], 'kind': 'instrument'}

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))


def connectivity(board):
    """(blocking, blocking_by, poured) from `board_score.score_connectivity`,
    the nets with a zone set apart. None for blocking when check_connected
    gave no summary."""
    from board_score import score_connectivity
    c = score_connectivity(ROOT, board)
    if not c.get('ran'):
        return None, {'unrouted': None, 'broken': None}, {'reason': c.get('reason')}
    zoned = set(c.get('poured_nets') or ())
    unrouted = [n for n in c.get('unrouted_net_names') or () if n not in zoned]
    detail = c.get('broken_detail') or {}
    broken = {n: v.get('joins_needed') or 0 for n, v in detail.items() if n not in zoned}
    poured = {n: v.get('joins_needed') or 0 for n, v in detail.items() if n in zoned}
    poured_unrouted = [n for n in c.get('unrouted_net_names') or () if n in zoned]
    blocking = len(unrouted) + sum(broken.values())
    return (blocking,
            {'unrouted': len(unrouted), 'broken': sum(broken.values()),
             'nets': sorted(set(unrouted) | {n for n, j in broken.items() if j})},
            {'unrouted': len(poured_unrouted), 'broken': sum(poured.values()),
             'nets': sorted(set(poured_unrouted) | {n for n, j in poured.items() if j})})


def quick_score(board):
    from board_score import quality
    blk, by, poured = connectivity(board)
    return {'blocking': blk, 'blocking_by': by, 'poured': poured, 'quality': quality(board),
            'score': 'quick_score: connectivity of the nets without a zone (route.py --quick 2)'}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('board')
    ap.add_argument('--json', help='write the score here')
    a = ap.parse_args(argv)
    sc = quick_score(a.board)
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as fh:
            json.dump(sc, fh, indent=1)
    q = sc['quality']
    p = sc['poured']
    print(f"blocking={sc['blocking']} vias={q.get('vias')} copper_mm={q.get('copper_mm')} "
          f"segments={q.get('segments')} (poured, not counted: {p.get('broken')} joins)")
    return 2 if sc['blocking'] is None else (4 if sc['blocking'] else 0)


if __name__ == '__main__':
    sys.exit(main())
