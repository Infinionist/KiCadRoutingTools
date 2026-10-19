#!/usr/bin/env python3
"""Score a `route.py --quick 2` board: the seed judge's score.

    quick_score.py FAST.kicad_pcb --route ROUTE [--json OUT]

ROUTE is the route step's summary: the file its `--json-out` wrote, or a log
holding its `JSON_SUMMARY_MIN:` line (the last one counts).

- **blocking** is the route step's own tally -- failed singles + open singles
  + the multipoint pad deficit, the verdict CLAUDE.md defines -- not a
  re-grade. `--quick 2` skips the passes that fix grazes and the final
  re-grade, so DRC on its board measures the polish it skipped, not the
  placement; this score leaves DRC, assembly and the floorplan out.
- **quality** (vias, copper_mm, segments) is `board_score`'s own, counted on
  FAST.kicad_pcb.

The keys are `board_score --json`'s (`blocking`, `blocking_by`, `quality`),
so a seed's `s<S>_fast.score.json` reads the same either way. Rank seeds by
blocking, then vias, copper_mm, segments. Never grade a board to ship with
it: route that one in full and run `board_score`.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))


def route_tally(path):
    """Blocking from a route step's summary: a --json-out file, or the last
    JSON_SUMMARY_MIN line of a log. None when neither is there."""
    text = open(path, encoding='utf-8', errors='replace').read()
    try:
        doc = json.loads(text)
        deficit = (int(doc.get('multipoint_pads_total') or 0)
                   - int(doc.get('multipoint_pads_connected') or 0))
    except ValueError:
        mins = [x for x in text.split('\n') if x.startswith('JSON_SUMMARY_MIN: ')]
        if not mins:
            return None
        doc = json.loads(mins[-1].split(': ', 1)[1])
        deficit = int(doc.get('multipoint_deficit') or 0)
    return (len(doc.get('failed_single') or []) + len(doc.get('open_single') or [])
            + deficit)


def quick_score(board, route):
    from board_score import quality
    blk = route_tally(route)
    return {'blocking': blk, 'blocking_by': {'broken': blk}, 'quality': quality(board),
            'score': 'quick_score: the route step tally (route.py --quick 2)'}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('board')
    ap.add_argument('--route', required=True,
                    help="the route step's --json-out file, or its log")
    ap.add_argument('--json', help='write the score here')
    a = ap.parse_args(argv)
    sc = quick_score(a.board, a.route)
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as fh:
            json.dump(sc, fh, indent=1)
    q = sc['quality']
    print(f"blocking={sc['blocking']} vias={q.get('vias')} copper_mm={q.get('copper_mm')} "
          f"segments={q.get('segments')}")
    return 2 if sc['blocking'] is None else (4 if sc['blocking'] else 0)


if __name__ == '__main__':
    sys.exit(main())
