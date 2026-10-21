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

#1277, the converse: R3's INPUT pose off the board (staged at 146.0 101.5,
the issue's recipe). The escape branch is then open, and the poses merely
less off the board than the input filled the ranking's cheapest 24: `set R3
143.4 102.0 --rot 180 --snap --radius 4` wrote (142.9, 104.75), `legal:
false`, past a legal pose 0.49 mm nearer the request. The fix ranks the
poses the ordinary gate admits (`QuenchState.escape_closed`) FIRST, as the
`ranked_on_board` rung, and a nearer lattice pose may not trade that rung's
answer for a worse one. The cases:

* `escape_board_term` is the input's board term from the off-board input
  and None from the on-board one; closing the branch refuses a pose merely
  less off the board and still admits one on it;
* every `ranked_on_board` pose has no board term, and the first is the
  legal pose the issue names;
* the CLI writes (142.9, 104.25), legal (before: (142.9, 104.75), not);
* `set R3 145.0 97.5 --snap --radius 3` keeps the on-board answer, legal
  (before: the lattice traded it for (145.25, 97.5), a pad past the outline);
* the no-snap refusal suggests (142.9, 104.25) (before: (142.9, 105.75));
* controls, the same as before the fix: the issue's two requests the other
  proposed fix would have changed ((143.4, 101.0) radius 3 writes the
  ranked (145.9, 101.75); (147.5, 101.5) radius 4 refuses), and the
  on-board input above (no `ranked_on_board` rung; the 7th try).

Found reviewing #1277: `pose_score.rank_poses` labelled every pose it
ranked `legal: True`, the escape branch's too. With R3 off the board,
`converge poses --ref R3 --radius 3` called (146.0, 104.5) at 270 degrees
legal with both pads past the east edge; at step 0.5 the sweep ranks 6
poses the ordinary gate admits and 108 it does not. Now `legal` is the
ordinary gate's verdict and those carry `escape: True`:

* off the board, each pose's label agrees with `candidate_valid` with the
  branch closed, `diagnostics['escape_admitted']` counts them, and at home
  every pose is legal with no `escape` key (the control);
* `converge poses` reports the pose as an escape pose, in the JSON and on
  stderr, and place_pose's no-snap suggestion at (145.4, 103.75) says it is
  only less off the board; controls: the `--radius 4` suggestion (142.9,
  104.25) is legal and worded as before, and the snap from (143.4, 101.0)
  still writes (145.9, 101.75), its pose now marked `escape`.

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


#: The edge floor every case here was measured at: esp_prog declares no edge
#: rule, which #1301 reads as the 0.2 fab floor, not the old 0.55.
EDGE_MM = 0.55
EDGE = ['--board-edge-clearance', str(EDGE_MM)]


def _knobs(board):
    from placement import pose_ops
    clr, bec, _tw, _k = pose_ops.resolve_knobs(board,
                                               board_edge_clearance=EDGE_MM)
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
            out, *EDGE, '--dry-run', 'set', REF, str(REQ[0]), str(REQ[1]), '--snap',
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
    # #1277 control: an input ON the board gets one ranking, as before --
    # no on-board rung, and the same pose on the same try.
    assert s['snap_census'].get('ranked_on_board') is None, s['snap_census']
    assert (sn['to'], sn['candidates_tried']) == ([143.25, 104.25, 180.0],
                                                  7), sn
    print('  PASS: radius 2 refuses with 0 ranked; radius 4 snaps to %s '
          '(%.2f mm, try %d)' % (sn['to'], sn['dist_mm'],
                                 sn['candidates_tried']))


# ---------------------------------------------------------------------------
# #1277: the input pose OFF the board
# ---------------------------------------------------------------------------

#: R3's input pose in the #1277 cases: the #1259 request, written with
#: `place_pose set --force` in the issue.
OFF = (146.0, 101.5, 180.0)
#: The legal pose the issue names, 2.305 mm from its request.
LEGAL = (142.9, 104.25, 180.0)


def _off_board():
    dst = os.path.join(_TMP.name, 'r3off.kicad_pcb')
    if not os.path.exists(dst):
        from placement.portfolio import copy_siblings
        from placement.writer import write_placed_output
        esp = evidence(ESP, 'esp_prog')
        _quiet(write_placed_output, esp, dst,
               [{'reference': REF, 'new_x': OFF[0], 'new_y': OFF[1],
                 'new_rotation': OFF[2]}])
        copy_siblings(esp, dst)
    return evidence(dst, 'esp_prog with R3 off the board')


def _off_set(x, y, *extra):
    out = os.path.join(_TMP.name, 'o1277.kicad_pcb')
    return [sys.executable, '-X', 'utf8', POSE, _off_board(), out, *EDGE,
            '--dry-run', 'set', REF, str(x), str(y), '--rot', '180'] + [
                str(e) for e in extra]


def test_1277_closing_the_escape_branch():
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    board = _off_board()
    clr, bec = _knobs(board)
    pcb = _quiet(parse_kicad_pcb, board)
    st = _quiet(pose_ops._ranker_state, pcb, board, REF,
                (143.4, 102.0, 180.0), clearance=clr,
                board_edge_clearance=bec)
    assert st.escape_closed == set(), st.escape_closed
    term = st.escape_board_term(REF)
    assert term is not None and term > 1e-6, ('the input is off the board',
                                              term)
    assert term == st.violation_parts(REF, *OFF)[0], term
    # (142.9, 105.75): the cheapest pose the licensed ranking returns, merely
    # less off the board than the input. (142.9, 104.25): on the board.
    less_off = (142.9, 105.75, 180.0)
    assert st.violation_parts(REF, *less_off)[0] > 1e-6
    assert st.candidate_valid(REF, *less_off) is True
    assert st.candidate_valid(REF, *LEGAL) is True
    st.escape_closed.add(REF)
    assert st.escape_board_term(REF) is None
    v = st.candidate_veto(REF, *less_off)
    assert v == ('board_bbox', None), v
    assert st.candidate_valid(REF, *LEGAL) is True
    st.escape_closed.discard(REF)
    assert st.candidate_valid(REF, *less_off) is True, 'reopened'
    # From the input on the board (R3 at home), the branch is closed already.
    esp = evidence(ESP, 'esp_prog')
    st2 = _quiet(pose_ops._ranker_state, _quiet(parse_kicad_pcb, esp), esp,
                 REF, REQ, clearance=clr, board_edge_clearance=bec)
    assert st2.escape_board_term(REF) is None, st2.escape_board_term(REF)
    print('  PASS: board term %.3f from the off-board input, None closed and '
          'None from home; closing refuses %s, still admits %s'
          % (term, less_off[:2], LEGAL[:2]))


def test_1277_the_board_is_ranked_first():
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    board = _off_board()
    clr, bec = _knobs(board)
    pcb = _quiet(parse_kicad_pcb, board)
    poses, census = _quiet(
        pose_ops.snap_candidates, board, REF, rot=180.0, clearance=clr,
        board_edge_clearance=bec, radius=4.0, step=0.25, pcb_data=pcb,
        at=(143.4, 102.0))
    first = [p for p in poses if p.get('rung') == 'ranked_on_board']
    assert first and census.get('ranked_on_board') == len(first), census
    assert poses[:len(first)] == first, 'the on-board rung comes first'
    assert (first[0]['x'], first[0]['y']) == LEGAL[:2], first[0]
    st = _quiet(pose_ops._ranker_state, pcb, board, REF,
                (143.4, 102.0, 180.0), clearance=clr,
                board_edge_clearance=bec)
    off = [(p['x'], p['y']) for p in first
           if st.violation_parts(REF, p['x'], p['y'], p['rot'])[0] > 1e-6]
    assert not off, ('ranked on the board, but off it', off)
    # Before the fix the snap wrote (142.9, 104.75), `legal: false`.
    r = check(_off_set(143.4, 102.0, '--snap', '--radius', '4'), accept=True)
    s = _summary(r)
    sn = s['snapped']
    assert s['legal'] is True, (s['legal'], sn)
    assert sn['to'] == list(LEGAL) and sn['rung'] == 'ranked_on_board', sn
    print('  PASS: %d poses ranked on the board first, none off it; the snap '
          'writes %s, legal' % (len(first), sn['to']))


def test_1277_the_lattice_keeps_the_on_board_answer():
    # Before the fix the lattice traded the ranked answer for (145.25, 97.5),
    # 0.25 mm from the request with R3's pad past the outline.
    r = check(_off_set(145.0, 97.5, '--snap', '--radius', '3'), accept=True)
    s = _summary(r)
    sn = s['snapped']
    assert s['legal'] is True and s['oob_pad_count_after'] == 0, (
        s['legal'], sn)
    assert sn['rung'] == 'ranked_on_board' and sn['to'] == [143.5, 95.0,
                                                             180.0], sn
    print('  PASS: --snap from (145.0, 97.5) writes %s (%s), legal'
          % (sn['to'], sn['rung']))


def test_1277_the_suggestion_takes_the_same_order():
    # No --snap: the refusal's suggestion. Before the fix (142.9, 105.75),
    # 3.783 mm away and still off the board.
    r = check(_off_set(143.4, 102.0, '--radius', '4'),
              refuse='pad_stack_count 0 -> 1', code=4)
    s = _summary(r)
    nl = s['nearest_legal']
    assert (nl['x'], nl['y']) == LEGAL[:2], nl
    assert (s['nearest_legal_census'].get('ranked_on_board') or 0) >= 1, (
        s['nearest_legal_census'])
    assert 'x=142.9 y=104.25' in s['refused'], s['refused']
    print('  PASS: the no-snap refusal suggests x=142.9 y=104.25')


def test_1277_controls_unchanged():
    """The issue's two requests the other proposed fix (pin the input only
    when it is on the board) would have changed, answered as before."""
    r = check(_off_set(143.4, 101.0, '--snap', '--radius', '3'), accept=True)
    s = _summary(r)
    sn = s['snapped']
    assert s['snap_census'].get('ranked_on_board', 0) == 0, s['snap_census']
    assert sn['to'] == [145.9, 101.75, 180.0] and sn['rung'] == 'ranked', sn
    r = check(_off_set(147.5, 101.5, '--snap', '--radius', '4'),
              refuse='pad_edge_shortfall 2.1 -> 5.1', code=4)
    sc = _summary(r)['snap_census']
    assert (sc.get('ranked_on_board', 0), sc['ranked']) == (0, 0), sc
    print('  PASS: (143.4, 101.0) r3 writes %s as before; (147.5, 101.5) r4 '
          'refuses as before' % sn['to'])


def test_rank_poses_labels_escape_poses():
    """`rank_poses` labelled every pose it ranked `legal: True`, the ones
    only the escape branch admits included (found reviewing #1277): with R3
    off the board, `converge poses` called (146.0, 104.5) at 270 degrees --
    both pads past the east edge -- legal. Now `legal` is the ordinary
    gate's verdict, and an escape pose carries `escape: True`."""
    import pose_score
    from kicad_parser import parse_kicad_pcb
    board = _off_board()
    clr, bec = _knobs(board)
    pcb = _quiet(parse_kicad_pcb, board)
    # converge's state: no incumbent pinned, R3's current pose off the board.
    st = _quiet(pose_score.make_state, pcb, board, clearance=clr,
                board_edge_clearance=bec)
    assert st.escape_board_term(REF) is not None, 'R3 is off the board'
    diag = {}
    poses = pose_score.rank_poses(pcb, board, REF, radius=3.0, step=0.5,
                                  limit=1000, state=st, diagnostics=diag)
    legal = [p for p in poses if p['legal']]
    esc = [p for p in poses if not p['legal']]
    by = {(p['x'], p['y'], p['rot']): p for p in poses}
    off = by.get((146.0, 104.5, 270.0))
    assert off is not None and off['legal'] is False and off.get('escape'), (
        'a pose with both pads past the edge is labelled legal', off and {
            k: off[k] for k in ('legal', 'escape') if k in off})
    assert legal and esc, (len(legal), len(esc))
    assert diag['escape_admitted'] == len(esc), (diag['escape_admitted'],
                                                 len(esc))
    # The label is the ordinary gate's verdict, pose by pose.
    st.escape_closed.add(REF)
    wrong = [(p['x'], p['y'], p['rot'], p['legal']) for p in poses
             if bool(st.candidate_valid(REF, p['x'], p['y'], p['rot']))
             != p['legal']]
    st.escape_closed.discard(REF)
    assert not wrong, ('labels the ordinary gate disagrees with', wrong[:4])
    assert all(p.get('escape') is True for p in esc), 'escape marks them all'
    assert not any('escape' in p for p in legal), 'and no legal pose'
    # Control: from the board's own poses (R3 at home) nothing changes --
    # every pose legal, no escape key, nothing escape-admitted.
    esp = evidence(ESP, 'esp_prog')
    pcb0 = _quiet(parse_kicad_pcb, esp)
    st0 = _quiet(pose_score.make_state, pcb0, esp, clearance=clr,
                 board_edge_clearance=bec)
    d0 = {}
    p0 = pose_score.rank_poses(pcb0, esp, REF, radius=3.0, step=0.5,
                               limit=1000, state=st0, diagnostics=d0)
    assert p0 and all(p['legal'] is True and 'escape' not in p for p in p0)
    assert d0['escape_admitted'] == 0, d0['escape_admitted']
    print('  PASS: off the board, %d legal + %d escape poses, each label the '
          'ordinary gate\'s verdict; at home %d poses, all legal'
          % (len(legal), len(esc), len(p0)))


def test_the_labels_reach_converge_and_the_suggestion():
    # converge poses: the escape pose is reported as one, and said so.
    conv = tool('converge.py')
    r = check([sys.executable, '-X', 'utf8', conv, 'poses', _off_board(),
               '--ref', REF, '--radius', '3', '--limit', '8', *EDGE],
              accept=True)
    d = json.loads(r.stdout)
    by = {(p['x'], p['y'], p['rot']): p for p in d['poses']}
    off = by.get((146.0, 104.5, 270.0))
    assert off is not None and off['legal'] is False and off.get('escape'), (
        off and {k: off[k] for k in ('legal', 'escape') if k in off})
    assert 'only LESS off it' in (d.get('escape_note') or ''), d.keys()
    assert 'escape: true, legal: false' in r.stderr, r.stderr[-400:]
    # place_pose's no-snap suggestion at the default sweep (2 mm): only an
    # escape pose is in reach, and the refusal no longer reads it as legal.
    r = check(_off_set(143.4, 102.0), refuse='pad_stack_count 0 -> 1',
              code=4)
    s = _summary(r)
    nl = s['nearest_legal']
    assert (nl['x'], nl['y']) == (145.4, 103.75), nl
    assert nl['legal'] is False and nl.get('escape') is True, nl
    assert ("2.658 mm away, only less off the board than the part's input "
            "pose) -- try it with --snap") in s['refused'], s['refused']
    # Control: --radius 4 reaches the legal pose, labelled and worded as
    # before.
    r = check(_off_set(143.4, 102.0, '--radius', '4'),
              refuse='pad_stack_count 0 -> 1', code=4)
    s = _summary(r)
    nl = s['nearest_legal']
    assert (nl['x'], nl['y'], nl['legal']) == (142.9, 104.25, True), nl
    assert 'escape' not in nl and 'only less off' not in s['refused'], nl
    # Control, the snap: an escape pose the re-grade passes is still written
    # (the ranked rung decides nothing on the label), and the summary's pose
    # now says what it is.
    r = check(_off_set(143.4, 101.0, '--snap', '--radius', '3'), accept=True)
    s = _summary(r)
    assert s['snapped']['to'] == [145.9, 101.75, 180.0], s['snapped']
    assert s['legal'] is False, s['legal']
    assert s['nearest_legal'].get('escape') is True, s['nearest_legal']
    print('  PASS: converge and the no-snap suggestion report the escape pose '
          'as one; the legal suggestion and the snap are unchanged')


TESTS = [
    test_the_ranker_pins_the_incumbent_at_the_input_pose,
    test_the_pad_conjunct_reads_the_pinned_incumbent,
    test_every_ranked_pose_passes_the_regrade,
    test_the_cli_snaps_from_radius_4,
    test_1277_closing_the_escape_branch,
    test_1277_the_board_is_ranked_first,
    test_1277_the_lattice_keeps_the_on_board_answer,
    test_1277_the_suggestion_takes_the_same_order,
    test_1277_controls_unchanged,
    test_rank_poses_labels_escape_poses,
    test_the_labels_reach_converge_and_the_suggestion,
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
