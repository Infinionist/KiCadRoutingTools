#!/usr/bin/env python3
"""`converge poses` grades pose legality at the BOARD's floor, and says so.

Run-7 S4: `poses` ran at fixed argparse defaults (clearance 0.25, edge 0.55)
on a board routed to 0.15/0.3. candidate_valid then vetoed rotations the
router would happily route -- including flip-in-place, which WAS enumerated
(the offset list starts at (0,0)) and then silently dropped. The run read
"no legal pose" as a fact about the part when it was a fact about the knobs.

Contract under test:
  * unset knobs resolve from the board's own Default netclass /
    min_copper_edge_clearance (explicit CLI values still win);
  * the JSON discloses the resolved knobs AND their source;
  * dropped poses are censused (`dropped_total`, `dropped_in_place`), so an
    empty ranking is diagnosable as a knob problem vs a genuinely stuck part.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # placement split

import converge  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')


def _cv(args, **kw):
    return subprocess.run([sys.executable, '-X', 'utf8',
                           os.path.join(ROOT, 'py_placer', 'converge.py')] + args,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', cwd=ROOT, **kw)


def test_cli_values_always_win():
    c, e, knobs = converge._pose_knobs(BOARD, 0.2, 0.3)
    assert (c, e) == (0.2, 0.3)
    assert knobs['clearance']['source'] == 'cli'
    assert knobs['board_edge_clearance']['source'] == 'cli'
    print("  PASS: explicit CLI knobs are untouched")


def test_unset_knobs_resolve_from_the_board_project():
    with tempfile.TemporaryDirectory() as td:
        b = os.path.join(td, 'b.kicad_pcb')
        shutil.copy(BOARD, b)
        with open(os.path.join(td, 'b.kicad_pro'), 'w', encoding='utf-8') as f:
            json.dump({'net_settings': {'classes': [
                           {'name': 'Default', 'clearance': 0.15}]},
                       'board': {'design_settings': {'rules': {
                           'min_copper_edge_clearance': 0.3}}}}, f)
        c, e, knobs = converge._pose_knobs(b, None, None)
        assert c == 0.15, f"expected the board's 0.15, got {c}"
        assert e == 0.3, f"expected the board's 0.3, got {e}"
        assert knobs['clearance']['source'] == 'board netclass'
        assert knobs['board_edge_clearance']['source'] == 'board constraint'
    print("  PASS: unset knobs resolve from the board's own floors")


def test_projectless_board_falls_back_and_says_so():
    assert not os.path.exists(os.path.splitext(BOARD)[0] + '.kicad_pro'), (
        "fixture grew a .kicad_pro -- update this test")
    c, e, knobs = converge._pose_knobs(BOARD, None, None)
    # #1301: the edge falls back to the fab edge floor routing writes back.
    assert (c, e) == (0.25, 0.2)
    assert knobs['clearance']['source'] == 'fixed default'
    assert (knobs['board_edge_clearance']['source']
            == 'fab edge floor (what routing writes)')
    print("  PASS: a projectless board falls back, labeled")


def test_poses_json_discloses_knobs_and_drops():
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    r = _cv(['poses', BOARD, '--ref', 'C1', '--radius', '0.5',
             '--step', '0.5', '--limit', '3'])
    assert r.returncode == 0, r.stderr[-800:]
    d = json.loads(r.stdout)
    assert 'knobs' in d and 'clearance' in d['knobs'], d.keys()
    assert 'source' in d['knobs']['clearance']
    assert 'dropped_total' in d and 'dropped_in_place' in d, (
        "the dropped-pose census must be in the JSON")
    print("  PASS: poses JSON carries knobs + dropped-pose census")


def test_impossible_knobs_report_dropped_in_place():
    """A clearance no pose can satisfy: the ranking is empty, and the census
    must say flip-in-place was vetoed -- the S4 diagnosis in one field."""
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    r = _cv(['poses', BOARD, '--ref', 'C1', '--radius', '0.5',
             '--step', '0.5', '--clearance', '50'])
    assert r.returncode == 1, (r.returncode, r.stdout[:400])
    d = json.loads(r.stdout)
    assert d['poses'] == []
    assert d['dropped_in_place'], (
        "with an impossible clearance, the part's own rotations must appear "
        "in dropped_in_place -- silence here is exactly the S4 failure")
    assert d['knobs']['clearance']['value'] == 50.0
    print("  PASS: empty ranking is diagnosable (dropped_in_place populated)")


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        print(f"--- {fn.__name__}")
        fn()
    print("ALL PASS")
