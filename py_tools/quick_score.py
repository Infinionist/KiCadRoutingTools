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
`quality`), so a seed's `s<S>_fast.score.json` reads the same either way, and
what it leaves out is listed in `ungraded`, by board_score's own component
names (#1323): a converge ledger holds quick and full rows side by side, and
it compares two rows' graded sets before it compares their `blocking`.
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


#: What this score leaves out of board_score's `blocking`, by board_score's
#: component names, and why. `converge.ungraded_set` reads a payload's
#: `ungraded` list and `components[k].reason`; without them a quick row read
#: as grading everything, and the first board_score row after it -- which
#: honestly lists impedance, length and net_widths -- was refused as an
#: improvement that "graded FEWER components" (#1323). `assembly.courtyard_
#: gating` is board_score's own entry for check_assembly's moved-part gate;
#: `connectivity.poured_nets` is the half of `unrouted`/`broken` this score
#: sets apart (`poured`).
UNGRADED = {
    'drc': '--quick 2 leaves the grazes for the full route\'s cleanup; DRC is '
           'graded on the full route',
    'undersized': '--quick 2 leaves the copper polish for the full route; '
                  'sizes are graded on the full route',
    'floorplan': 'quick_score grades the route; check_floorplan --intent '
                 'grades the placement',
    'assembly': 'quick_score grades the route; check_assembly grades the '
                'placement',
    'assembly.courtyard_gating': 'quick_score runs no check_assembly',
    'impedance': 'quick_score grades no impedance',
    'length': 'quick_score grades no length matching',
    'net_widths': 'quick_score grades no per-net widths',
    'connectivity.poured_nets': '--quick 2 skips the plane finalize that joins '
                                'a poured net\'s pads, so their open pads are '
                                'reported as `poured`, not graded',
}


def quick_score(board):
    from board_score import quality
    blk, by, poured = connectivity(board)
    ungraded = sorted(UNGRADED)
    return {'blocking': blk, 'blocking_by': by, 'poured': poured, 'quality': quality(board),
            'ungraded': ungraded,
            'components': {k: {'ran': False, 'reason': UNGRADED[k]} for k in ungraded},
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
    print(f"UNGRADED (the full route's board_score grades these): {', '.join(sc['ungraded'])}")
    return 2 if sc['blocking'] is None else (4 if sc['blocking'] else 0)


if __name__ == '__main__':
    sys.exit(main())
