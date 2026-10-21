#!/usr/bin/env python3
"""Below blocking 0, open connections outrank vias and copper (#1340).

`blocking` counts an unrouted net as 1 whatever its size, and a `blocking`
tie fell to vias, then copper. On StickHub, board_score gave a board missing the whole
17-pad +3.3V rail BLOCKING=1 / 117 vias / 704.04 mm and a board with one
stranded pad BLOCKING=1 / 132 vias / 773.42 mm: the rule kept the rail-less
board, its missing copper reading as quality. board_score published no pad
count for an unrouted net that could break the tie; check_connected prints
it and board_score dropped it.

Checks:
  1. board_score keeps each unrouted net's pad count and publishes
     `joins_needed` = (pads - 1) per unrouted net + the broken joins;
  2. end to end, board_score on a real unrouted board publishes the pads and
     a `joins_needed` that adds up.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _d in ('', 'py_router', 'py_tools', 'py_placer', 'kicad_routing_plugin'):
    _p = os.path.join(ROOT, _d) if _d else ROOT
    if _p not in sys.path:
        sys.path.insert(0, _p)
sys.path.insert(0, HERE)

import board_score as bs                                      # noqa: E402
from run_utils import check, evidence, tool                   # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')

# check_connected's report: the 17-pad rail unrouted, a 2-pad net unrouted,
# a signal split in two, and a poured GND split in three
REPORT = """Found 1330 segments, 108 vias, 210 pads, 1 zones
FOUND 4 ISSUES:
  Unrouted nets (2):
    +3.3V (17 pads)
    /SCL (2 pads)

  Connectivity issues (2):

  /CS (net 2):
    Segments: 9, Vias: 0, Pads: 3
    Disconnected components: 2
    Disconnected pads:
      (120.49, 78.50) on F.Cu [R1]

  GND (net 4):
    Segments: 618, Vias: 38, Pads: 55
    Disconnected components: 3
    Disconnected pads:
      (137.72, 66.05) on F.Cu [SW1]
"""


def conn(report=REPORT, poured=('GND',)):
    real_run, real_poured = bs.run_tool, bs.poured_net_names
    bs.run_tool = lambda root, tool, board, *a, **k: (1, report)
    bs.poured_net_names = lambda board: set(poured)
    try:
        return bs.score_connectivity('.', 'board.kicad_pcb')
    finally:
        bs.run_tool, bs.poured_net_names = real_run, real_poured


def test_board_score_counts_the_joins():
    c = conn()
    assert c['unrouted_pads'] == {'+3.3V': 17, '/SCL': 2}, c['unrouted_pads']
    assert c['unrouted'] == 2 and c['broken'] == 3, (c['unrouted'], c['broken'])
    assert c['joins_needed'] == 16 + 1 + 1 + 2, c['joins_needed']
    clean = conn('ALL NETS FULLY CONNECTED!\n')
    assert clean['joins_needed'] == 0 and clean['unrouted_pads'] == {}, clean
    print('  PASS: board_score keeps each unrouted net\'s pads; joins_needed 20 '
          '(16 + 1 unrouted, 1 + 2 broken)')


def test_board_score_end_to_end():
    evidence(BOARD, 'fixture board')
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'score.json')
        check([sys.executable, '-X', 'utf8', tool('board_score.py'), BOARD,
               '--json', out, '--quiet'], refuse='joins_needed=', code=4)
        doc = json.load(open(evidence(out, 'board_score JSON'), encoding='utf-8'))
    pads = doc['components']['unrouted']['pads']
    assert len(pads) == doc['blocking_by']['unrouted'], (len(pads), doc['blocking_by'])
    want = sum(n - 1 for n in pads.values()) + doc['blocking_by']['broken']
    assert doc['joins_needed'] == want, (doc['joins_needed'], want)
    assert doc['joins_needed'] > doc['blocking_by']['unrouted'], doc['joins_needed']
    print(f'  PASS: board_score on a real board: {len(pads)} unrouted nets, '
          f'joins_needed {doc["joins_needed"]}')


def main():
    test_board_score_counts_the_joins()
    test_board_score_end_to_end()
    print('test_1340_joins_tiebreak: all passed')


if __name__ == '__main__':
    main()
