#!/usr/bin/env python3
"""Off-board is measured against the OUTLINE, not the bounding rectangle.

The reconstruct gate's third term is "did this candidate push copper off the
board". It used to compute that from the board's bounding RECTANGLE while every
other instrument used the real Edge.Cuts outline. On a rectangular board the two
agree; on any board with a notch, a cutout or a non-convex edge they do not, and
the gate is blind exactly where evacuation is easiest.

Measured on two recorded run-7 boards (bounding-rect term -> outline-aware term
at the same poses):

    one board:     0.0000 mm  ->  6.7177 mm
    another:       0.0000 mm  ->  5.5190 mm
    a rectangular board:  0.0000 mm  ->  0.0000 mm   (no false positive)

That 0.0 is what let a candidate sweeping nine parts into a notch pass the
"oob may not worsen" conjunct: the term could not see it.

Run: python3 -X utf8 tests/test_run8_oob_outline.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522/py_placer layout
os.environ.setdefault('KRT_NO_BANNER', '1')

from kicad_parser import parse_kicad_pcb                     # noqa: E402
from placement import reconstruct                            # noqa: E402
import pose_score                                            # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def bbox_term(state, bands=None):
    """The old bounding-rectangle term, kept here as the comparison."""
    bands = bands or {}
    total = 0.0
    for ref, pp in state.legality_ctx.parts.items():
        p = state.parts.get(ref)
        if p is None:
            continue
        ext = pp.extent(p.x, p.y, p.rot)
        if ext is not None:
            total += max(0.0, reconstruct._bbox_outside(ext, state.board)
                         - bands.get(ref, 0.0))
    return total


def state_for(path, clearance):
    pcb = parse_kicad_pcb(path)
    return pose_score.make_state(pcb, path, clearance=clearance,
                                 board_edge_clearance=0.2)


def main():
    board = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
    st = state_for(board, 0.1)
    check('the legality context is available', st.legality_ctx is not None)
    check('the outline-aware term exists and is used by the gate',
          'ctx.pad_oob_amount' in open(
              os.path.join(ROOT, 'py_placer', 'placement', 'reconstruct.py'),
              encoding='utf-8').read())

    # On a rectangular, healthy board the two definitions must agree at 0 --
    # this is the no-false-positive half, and it is why the corpus sweep did
    # not move when the term changed.
    old, new = bbox_term(st), reconstruct.pad_oob_amount(st)
    check('a healthy rectangular board reads 0 both ways',
          abs(old) < 1e-9 and abs(new) < 1e-9, f'old={old} new={new}')

    # Push one part far off the board: BOTH definitions must see it, or the
    # test proves nothing about the outline case below.
    ref = sorted(st.parts)[0]
    p = st.parts[ref]
    p.x, p.y = st.board[0] - 25.0, st.board[1] - 25.0
    check('a part moved well off the board is charged by the outline term',
          reconstruct.pad_oob_amount(st) > 1.0,
          str(reconstruct.pad_oob_amount(st)))

    # The real case, on a recorded non-rectangular board when one is present.
    notched = os.path.join(ROOT, 'wk', 'run7', 'glasgow_revC',
                           'perturbed.kicad_pcb')
    if os.path.isfile(notched):
        st2 = state_for(notched, 0.09)
        old2, new2 = bbox_term(st2), reconstruct.pad_oob_amount(st2)
        check('on a non-rectangular board the bounding rect sees nothing',
              abs(old2) < 1e-9, f'bbox term = {old2}')
        check('...while the outline term charges the real overhang',
              new2 > 1.0, f'outline term = {new2}')
    else:
        print('  SKIP  recorded non-rectangular board not present '
              '(wk/ is gitignored); the measured values are in the docstring')

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
