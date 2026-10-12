#!/usr/bin/env python3
"""A board with no file on disk routes with Via Size / Track Width unchecked (#1238).

The GUI passes `input_file=self.board_filename`. A freshly imported EAGLE .brd
has never been saved, so that path names a .kicad_pcb that does not exist, and
batch_route skips its netclass-width block (it reads the sibling .kicad_pro).
The per-layer width map and the per-net via map were bound only INSIDE that
block, so the readers further down raised UnboundLocalError: `_per_net_via`
with Via Size/Drill unchecked, `_per_layer` first when Track Width was
unchecked too.

Checks, through the real engine on a tiny board, mirroring the GUI's unsaved
state (input_file and pcb_data.source_path both name a path that is not there):
  1. via_from_class=True, track_width_from_class=False (the issue's case);
  2. via_from_class=True, track_width_from_class=True (both unchecked);
each must route every net and lay real copper, not merely not raise.

    python3 tests/test_1238_unsaved_board.py
"""
import contextlib
import io
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'py_router'))
sys.path.insert(0, os.path.join(REPO, 'tests'))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from route import batch_route  # noqa: E402
from run_utils import evidence  # noqa: E402

BOARD = evidence(os.path.join(REPO, 'kicad_files', 'cap_chain.kicad_pcb'), 'board')
NETS = ['DPA_P', 'DPA_N', 'DPB_P', 'DPB_N']
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f" ({detail})" if detail else ''))
    if not ok:
        failures.append(label)


with tempfile.TemporaryDirectory() as tmp:
    unsaved = os.path.join(tmp, 'imported_brd.kicad_pcb')
    check('the unsaved board path really is absent (nor its project)',
          not os.path.exists(unsaved)
          and not os.path.exists(os.path.splitext(unsaved)[0] + '.kicad_pro'))
    for twc in (False, True):
        pcb = parse_kicad_pcb(BOARD)
        pcb.source_path = unsaved
        label = (f'via_from_class, track_width_from_class={twc}: '
                 f'routes with no file on disk')
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ok_n, fail_n, _t, data = batch_route(
                    unsaved, '', NETS, pcb_data=pcb, return_results=True,
                    via_from_class=True, track_width_from_class=twc)
        except Exception as e:                                 # noqa: BLE001
            check(label, False, f'{type(e).__name__}: {e}')
            continue
        segs = sum(len(r.get('new_segments') or []) for r in data['results'])
        check(label, ok_n == len(NETS) and fail_n == 0 and segs > 0,
              f'{ok_n} routed, {fail_n} failed, {segs} new segments')

if failures:
    print(f'FAIL: {len(failures)}')
    sys.exit(1)
print('ALL PASS')
