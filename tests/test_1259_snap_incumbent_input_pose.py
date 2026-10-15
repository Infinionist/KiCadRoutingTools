#!/usr/bin/env python3
"""#1259 (2): the snap ranker's off-board escape branch measures the
INCUMBENT at the part's INPUT pose, not at the refused request.

#1256 (72b9821d) made `pose_ops._ranker_state` judge the moved part from its
pose on the input board, then `apply_move` it to the request so the sweep
centres there. `QuenchState.candidate_valid`'s off-board escape branch read
its incumbent at the part's CURRENT pose, which is that request -- so an
off-board request licensed every pose merely LESS off the board than it,
and the re-grade, which compares with the input (where the part was on the
board), refused each one. The #1257 pad-copper conjunct in the same branch
read the request as its incumbent too.

The fix: `QuenchState.incumbent_at[ref]`, the pose both incumbent measures
read (`_incumbent_violation`, and `_pad_copper_past` in the escape branch).
`_ranker_state` sets it to the input pose before the move; every other
caller leaves it empty, so the incumbent stays the part's current pose.

Measured on esp_prog, `set R3 146.0 101.5 --snap` (R3 at 141.0 101.5 on the
input; the request puts a pad 1.008 mm past the east edge):

  --radius 2: before 21 ranked (+175 lattice), all 12 re-graded refused,
      exit 4; after 0 ranked (+196 lattice), all 6 refused, exit 4.
  --radius 4: before 3 ranked, all 9 refused, exit 4; after 9 ranked, the
      7th try, (143.25, 104.25) at 3.89 mm, passes the re-grade, exit 0.

What each case pins:

* the ranker's state pins R3's incumbent at its input pose, and its
  `_incumbent_violation` is the input pose's, not the request's; a plain
  `make_state` leaves `incumbent_at` empty;
* the escape branch's pad-copper conjunct reads `incumbent_at` (test_1257's
  `_scoped` R2, whose pad reaches 4 mm past its courtyard): the same
  candidate is refused from an incumbent whose pads are on the board and
  admitted from one whose pads are further out, whatever the current pose;
* every pose the snap ranker returns passes the caller's own re-grade, at
  radius 2 and 4 (before: 21 and 3 ranked, every one refused);
* the CLI: radius 2 refuses with 0 ranked, exit 4; radius 4 snaps to a
  ranked pose the re-grade passes, exit 0 (before: exit 4).

    python3 tests/test_1259_snap_incumbent_input_pose.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_utils import check, evidence, tool    # noqa: E402

RUN_ALL_TIMEOUT = 900

ESP = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
POSE = tool('place_pose.py')
REF = 'R3'
#: R3's pose on the input board.
HOME = (141.0, 101.5, 180.0)
#: The issue's request: R3's east pad 1.008 mm past the east edge.
REQ = (146.0, 101.5, 180.0)

_TMP = tempfile.TemporaryDirectory(prefix='t1259s_')


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **k)


def _knobs(board):
    from placement import pose_ops
    clr, bec, _tw, _k = pose_ops.resolve_knobs(board)
    return clr, bec


def _regrade(board, before, pose, clr, bec):
    """`apply_poses`' own re-grade of one candidate: stage it, grade it,
    compare with the input. Returns the categories that got worse."""
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    from placement.portfolio import copy_siblings
    from placement.writer import write_placed_output
    cand = os.path.join(_TMP.name, 'cand.kicad_pcb')
    _quiet(write_placed_output, board, cand,
           [{'reference': REF, 'new_x': pose['x'], 'new_y': pose['y'],
             'new_rotation': pose['rot']}])
    copy_siblings(board, cand)
    g = _quiet(pose_ops.grade, _quiet(parse_kicad_pcb, cand), cand, clr, bec,
               ())
    return pose_ops.worsened(before, g)


def test_the_ranker_pins_the_incumbent_at_the_input_pose():
    import pose_score
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    board = evidence(ESP, 'esp_prog board')
    clr, bec = _knobs(board)
    pcb = _quiet(parse_kicad_pcb, board)
    plain = _quiet(pose_score.make_state, pcb, board, clearance=clr,
                   board_edge_clearance=bec)
    assert plain.incumbent_at == {}, ('every other caller leaves it empty',
                                      plain.incumbent_at)
    st = _quiet(pose_ops._ranker_state, pcb, board, REF, REQ, clearance=clr,
                board_edge_clearance=bec)
    part = st.parts[REF]
    assert [round(v, 4) for v in (part.x, part.y, part.rot)] == list(REQ), (
        'the sweep centres on the request', part.x, part.y, part.rot)
    inc = st.incumbent_at.get(REF)
    assert inc is not None and [round(v, 4) for v in inc] == list(HOME), inc
    got = st._incumbent_violation(REF)
    at_home = st.violation_parts(REF, *HOME)
    at_req = st.violation_parts(REF, *REQ)
    assert got == at_home, ('the incumbent is the input pose', got, at_home)
    # The request is off the board; the input pose is not. Measured at the
    # request, the escape branch had a board term to improve on.
    assert at_req[0] > 1e-6 and at_home[0] <= 1e-6, (at_req, at_home)
    print('  PASS: incumbent %s, violation %s (the request measures %s)'
          % (inc, got, at_req))


def test_the_pad_conjunct_reads_the_pinned_incumbent():
    """test_1257's Q2 geometry, with the incumbent pinned AWAY from the
    current pose: the verdict follows `incumbent_at`, not `part.x`."""
    from test_1174_emit_round_trip import _synthetic
    from test_1241_seed_gate_off_board import _scoped
    from test_1257_seat_pad_copper import _state, _veto
    home = (37.0, 20.0, 0.0)          # courtyard inside, pad 2.5 mm past
    on_board = (39.6, 20.0, 90.0)     # pad pointing north, on the board
    far_out = (39.5, 20.0, 0.0)       # pad 5 mm past
    with tempfile.TemporaryDirectory() as td:
        st = _state(_synthetic(td, _scoped()))
        assert st._pad_copper_past('R2', *on_board) == 0.0
        assert abs(st._pad_copper_past('R2', *far_out) - 5.0) < 1e-6
        # Current pose far out (which alone admits `home`), incumbent pinned
        # where the pads are on the board: refused, by the pad conjunct.
        st.apply_move('R2', *far_out)
        assert _veto(st, 'R2', home) is None, 'control: unpinned admits it'
        st.incumbent_at['R2'] = on_board
        st.apply_move('R2', *far_out)          # clears the incumbent cache
        v = _veto(st, 'R2', home)
        assert v == ('pad_copper_outline', None), v
        # And the converse: current pose on the board, incumbent far out.
        st.incumbent_at['R2'] = far_out
        st.apply_move('R2', *on_board)
        v = _veto(st, 'R2', home)
        assert v is None, v
    print('  PASS: the pad conjunct follows incumbent_at (refused from an '
          'on-board incumbent, admitted from a farther-out one)')


def test_every_ranked_pose_passes_the_regrade():
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    board = evidence(ESP, 'esp_prog board')
    clr, bec = _knobs(board)
    pcb = _quiet(parse_kicad_pcb, board)
    before = _quiet(pose_ops.grade, pcb, board, clr, bec, ())
    req_bad = _regrade(board, before, dict(x=REQ[0], y=REQ[1], rot=REQ[2]),
                       clr, bec)
    assert 'oob_pad_count' in req_bad, ('the request must be refused',
                                        req_bad)
    got = {}
    for radius in (2.0, 4.0):
        poses, census = _quiet(
            pose_ops.snap_candidates, board, REF, rot=REQ[2], clearance=clr,
            board_edge_clearance=bec, radius=radius, step=0.25, pcb_data=pcb,
            at=REQ[:2])
        ranked = [p for p in poses if p['rung'] == 'ranked']
        assert census['ranked'] == len(ranked), census
        refused = [(p['x'], p['y'], b) for p in ranked
                   for b in [_regrade(board, before, p, clr, bec)] if b]
        # Before #1259: 21 ranked at radius 2 and 3 at radius 4, every one
        # refused for the pad copper the request put past the edge.
        assert not refused, ('ranked poses the re-grade refuses', radius,
                             refused[:4], len(refused))
        got[radius] = len(ranked)
    assert got[4.0] >= 1, ('radius 4 has poses the re-grade passes', got)
    print('  PASS: ranked %s, every one passes the re-grade' % got)


def _summary(r):
    rows = [ln for ln in r.stdout.splitlines()
            if ln.startswith('JSON_SUMMARY:')]
    assert len(rows) == 1, (r.returncode, r.stdout[-600:], r.stderr[-600:])
    return json.loads(rows[0].split(':', 1)[1])


def _set(radius):
    out = os.path.join(_TMP.name, 'o.kicad_pcb')
    return [sys.executable, '-X', 'utf8', POSE, evidence(ESP, 'esp_prog'),
            out, '--dry-run', 'set', REF, str(REQ[0]), str(REQ[1]), '--snap',
            '--radius', radius]


def test_the_cli_snaps_from_radius_4():
    r = check(_set('2'), refuse='oob_pad_count 0 -> 1', code=4)
    sc = _summary(r)['snap_census']
    assert sc['ranked'] == 0, ('radius 2 has no pose the re-grade passes',
                               sc['ranked'])
    r = check(_set('4'), accept=True)
    s = _summary(r)
    assert s['no_worse'] is True, s.get('refused')
    sn = s['snapped']
    assert sn and sn['rung'] == 'ranked' and sn['dist_mm'] <= 4.0, sn
    assert s['snap_census']['ranked'] >= sn['candidates_tried'], (
        'the snap was found on the ranked rung', s['snap_census'], sn)
    print('  PASS: radius 2 refuses with 0 ranked; radius 4 snaps to %s '
          '(%.2f mm, try %d)' % (sn['to'], sn['dist_mm'],
                                 sn['candidates_tried']))


TESTS = [
    test_the_ranker_pins_the_incumbent_at_the_input_pose,
    test_the_pad_conjunct_reads_the_pinned_incumbent,
    test_every_ranked_pose_passes_the_regrade,
    test_the_cli_snaps_from_radius_4,
]


if __name__ == '__main__':
    only = sys.argv[1:]
    ran = 0
    for t in TESTS:
        if only and not any(o in t.__name__ for o in only):
            continue
        print(f"--- {t.__name__}")
        t()
        ran += 1
    if only and not ran:
        print(f"NO TEST matches {only}")
        sys.exit(2)
    print('ALL PASS')
