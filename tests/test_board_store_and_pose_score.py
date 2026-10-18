#!/usr/bin/env python3
"""The two things a surgical convergence loop needs and did not have.

1. A step back that is a CHECKOUT, not a reconstruction. A prose ledger records
   `reverted_to: "grid back to 0.1"` and `reverted_to: "wk/seed.kicad_pcb"` --
   one unusable by a program, the other naming a path three iterations wrote in
   turn. Content-addressing the board makes it mechanical.

2. A pose ranked as ONE decision. Position and rotation were never scored
   together: the existing multi-pose scorer holds position fixed and varies
   rotation only, so a part needing 1.5 mm and 90 degrees has no evidence
   behind it.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # placement split
from board_store import BoardStore, Ledger, sha256_file  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')


# ---------------------------------------------------------------- board store

def test_same_content_is_one_entry_and_round_trips():
    with tempfile.TemporaryDirectory() as td:
        s = BoardStore(os.path.join(td, 'store'))
        a = os.path.join(td, 'a.kicad_pcb')
        b = os.path.join(td, 'b.kicad_pcb')
        shutil.copyfile(BOARD, a)
        shutil.copyfile(BOARD, b)          # same bytes, different path
        sha1, sha2 = s.put(a), s.put(b)
        assert sha1 == sha2, "content-addressed: the path must not matter"
        out = s.get(sha1, os.path.join(td, 'out.kicad_pcb'))
        assert sha256_file(out) == sha1, "checkout must be byte-identical"
    print("  PASS: same content -> one entry; checkout is byte-identical")


def test_siblings_travel_with_the_board():
    """A board without its .kicad_pro grades differently -- the project carries
    the DRC floor and the net classes. A store that drops it hands back
    something that is not the board that was stored."""
    with tempfile.TemporaryDirectory() as td:
        brd = os.path.join(td, 'x.kicad_pcb')
        shutil.copyfile(BOARD, brd)
        with open(os.path.join(td, 'x.kicad_pro'), 'w') as f:
            f.write('{"net_settings": {"classes": [{"name": "Default"}]}}')
        with open(os.path.join(td, 'x.kicad_dru'), 'w') as f:
            f.write('(version 1)\n')
        s = BoardStore(os.path.join(td, 'store'))
        sha = s.put(brd)
        dest = os.path.join(td, 'sub', 'y.kicad_pcb')
        s.get(sha, dest)
        assert os.path.isfile(os.path.join(td, 'sub', 'y.kicad_pro')), \
            "the project sibling did not travel"
        assert os.path.isfile(os.path.join(td, 'sub', 'y.kicad_dru')), \
            "the custom-rules sibling did not travel"
    print("  PASS: .kicad_pro and .kicad_dru travel with the board")


def test_an_unknown_sha_raises_rather_than_returning_nothing():
    with tempfile.TemporaryDirectory() as td:
        s = BoardStore(os.path.join(td, 'store'))
        assert not s.has('0' * 64)
        try:
            s.get('0' * 64, os.path.join(td, 'z.kicad_pcb'))
        except KeyError:
            print("  PASS: an unknown sha raises")
            return
        raise AssertionError("a missing board must not fail silently")


# --------------------------------------------------------------------- ledger

def test_the_last_accepted_row_is_the_one_to_step_back_to():
    with tempfile.TemporaryDirectory() as td:
        lg = Ledger(os.path.join(td, 'ledger.jsonl'))
        lg.append({'iteration': 1, 'kind': 'completion', 'parent_sha': 'a' * 64,
                   'result_sha': 'b' * 64, 'accepted': True})
        lg.append({'iteration': 2, 'kind': 'systemic', 'accepted': False,
                   'lever': 'restored the net classes'})
        assert lg.last_accepted()['iteration'] == 1, \
            "step back goes to the last ACCEPTED entry, not the last entry"
    print("  PASS: the last accepted row is found past a rejected one")


def test_a_torn_line_does_not_lose_the_ledger():
    """JSONL so a killed run keeps everything written before it."""
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, 'l.jsonl')
        lg = Ledger(p)
        lg.append({'iteration': 1, 'accepted': True})
        with open(p, 'a', encoding='utf-8') as f:
            f.write('{"iteration": 2, "acce')      # killed mid-write
        assert len(lg.entries()) == 1
        assert lg.last_accepted()['iteration'] == 1
    print("  PASS: a torn final line costs only that line")


# ----------------------------------------------------------------- pose score

def test_poses_are_ranked_and_legal_only():
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    from kicad_parser import parse_kicad_pcb
    import pose_score
    pcb = parse_kicad_pcb(BOARD)
    st = pose_score.make_state(pcb, BOARD)
    ref = sorted(st.parts)[0]
    poses = pose_score.rank_poses(pcb, BOARD, ref, radius=1.0, step=0.5, state=st)
    assert poses, "no legal pose at all, including staying put"
    dists = [p['dist_mm'] for p in poses]
    assert dists == sorted(dists), "poses must come back nearest first"
    assert all(p['legal'] for p in poses), "an illegal pose must be dropped, not ranked"
    assert {'x', 'y', 'rot', 'dist_mm', 'legal'} <= set(poses[0])
    # position AND rotation vary -- the gap this fills
    assert len({p['rot'] for p in poses}) >= 1
    assert len({(p['x'], p['y']) for p in poses}) > 1, \
        "position must vary, not just rotation"
    print(f"  PASS: {len(poses)} legal poses, nearest first, position+rotation together")


def test_scoring_leaves_the_board_where_it_found_it():
    """Ranking only asks the gate. If it moved a part, every later
    decision would be made against a board nobody chose."""
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    from kicad_parser import parse_kicad_pcb
    import pose_score
    pcb = parse_kicad_pcb(BOARD)
    st = pose_score.make_state(pcb, BOARD)
    ref = sorted(st.parts)[0]
    before = (st.parts[ref].x, st.parts[ref].y, st.parts[ref].rot)
    wire_before = st.wire_metrics()
    pose_score.rank_poses(pcb, BOARD, ref, radius=1.0, step=0.5, state=st)
    after = (st.parts[ref].x, st.parts[ref].y, st.parts[ref].rot)
    assert before == after, f"ranking moved the part: {before} -> {after}"
    assert st.wire_metrics() == wire_before
    print("  PASS: ranking is side-effect free")


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f"--- {k}")
            v()
    print("ALL PASS")
