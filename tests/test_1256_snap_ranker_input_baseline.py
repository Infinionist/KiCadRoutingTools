#!/usr/bin/env python3
"""#1256: place_pose's snap ranker judges the moved part from its INPUT pose.

`snap_candidates` and `nearest_legal` built the ranker's QuenchState from the
STAGED board, which already carries the requested pose. The ranker's pad and
hole check (`LegalityContext.pads_ok`) is relative to each part's SEED, so
the request was its own baseline: it passed at offset 0, and so did every
nearby pose carrying the same new conflict, while the re-grade -- which
compares against the INPUT board -- refused them all.

The fixture is the issue's: esp_prog with C1's pads given `(clearance 1.5)`,
and `set C2 140.06 96.23 --rot 90`, refused for `pad_conflicts 4 -> 6`
(new pairs C1/C2, C2/Ref*). Measured before the fix: `--snap --radius 2`
ranked 8 poses, the request among them at 0.000 mm, and the re-grade refused
all 8; at radius 4 it ranked 16 and the re-grade refused 10; the no-snap
refusal suggested x=140.06 y=96.73, which the re-grade refuses too. After:
radius 2 ranks none, radius 4 ranks 10, every one passing. What each case
pins:

* the ranker's state seeds the moved part at its INPUT pose and puts it at
  the request (the mechanism);
* every pose the snap ranker returns passes the caller's own re-grade, and
  at radius 2 there is honestly none;
* neither function returns the request itself, even from a ranker that
  offers it (forced, so a gate the ranker does not model is covered too);
* the CLI: radius 2 refuses with 0 ranked, radius 4 writes a ranked pose,
  and the no-snap suggestion is a pose the re-grade passes;
* "the knobs veto even staying put" is said about the INPUT pose, not about
  the request the grade has just refused.

    python3 tests/test_1256_snap_ranker_input_baseline.py
"""
import json
import os
import re
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
REF = 'C2'
REQ = (140.06, 96.23, 90.0)
#: C2's pose on the input board (KiCad writes -90; normalised).
HOME = (131.7, 103.9, 270.0)

_TMP = tempfile.TemporaryDirectory(prefix='t1256_')


def _override_board():
    """esp_prog with C1's pads given a 1.5 mm clearance override (#1256's
    recipe), written once per run."""
    dst = os.path.join(_TMP.name, 'ovr.kicad_pcb')
    if not os.path.exists(dst):
        evidence(ESP, 'esp_prog board')
        t = open(ESP, encoding='utf-8').read()
        i = t.index('(property "Reference" "C1"')
        s = t.rfind('(footprint', 0, i)
        e = t.index('\n\t)', i)
        blk, n = re.subn(r'(\(layers [^)]*\))', r'\1 (clearance 1.5)', t[s:e])
        assert n == 2, ('expected C1 to carry two pads', n)
        with open(dst, 'w', encoding='utf-8') as f:
            f.write(t[:s] + blk + t[e:])
    return evidence(dst, 'C1-override board')


def _knobs(board):
    from placement import pose_ops
    clr, bec, _tw, _k = pose_ops.resolve_knobs(board)
    return clr, bec


def _regrade(board, pcb, before, pose, clr, bec):
    """`apply_poses`' own re-grade of one candidate: stage it, grade it,
    compare with the input. Returns the categories that got worse."""
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    from placement.portfolio import copy_siblings
    from placement.writer import write_placed_output
    cand = os.path.join(_TMP.name, 'cand.kicad_pcb')
    write_placed_output(board, cand, [{'reference': REF, 'new_x': pose['x'],
                                       'new_y': pose['y'],
                                       'new_rotation': pose['rot']}])
    copy_siblings(board, cand)
    g = pose_ops.grade(parse_kicad_pcb(cand), cand, clr, bec, ())
    return pose_ops.worsened(before, g)


def _is_request(p):
    return abs(p['x'] - REQ[0]) < 1e-4 and abs(p['y'] - REQ[1]) < 1e-4


def test_the_ranker_judges_the_part_from_its_input_pose():
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    board = _override_board()
    clr, bec = _knobs(board)
    st = pose_ops._ranker_state(parse_kicad_pcb(board), board, REF, REQ,
                                clearance=clr, board_edge_clearance=bec)
    seed = st.legality_ctx.seed_of(REF)
    pose = st.legality_ctx.pose_of(REF)
    assert [round(v, 4) for v in seed] == list(HOME), seed
    assert [round(v, 4) for v in pose] == list(REQ), pose
    # Seeded at the request (the old state), pads_ok passed it at offset 0.
    veto = st.candidate_veto(REF, *REQ)
    assert veto == ('pads', 'C1'), veto
    print('  PASS: seed %s, centred on %s, request vetoed by %s'
          % (seed, pose, veto))


def test_every_ranked_pose_passes_the_regrade():
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    board = _override_board()
    clr, bec = _knobs(board)
    pcb = parse_kicad_pcb(board)
    before = pose_ops.grade(pcb, board, clr, bec, ())
    req_bad = _regrade(board, pcb, before, dict(x=REQ[0], y=REQ[1],
                                                rot=REQ[2]), clr, bec)
    assert 'pad_conflicts' in req_bad, ('the request must be refused', req_bad)
    got = {}
    for radius in (2.0, 4.0):
        poses, census = pose_ops.snap_candidates(
            board, REF, rot=REQ[2], clearance=clr, board_edge_clearance=bec,
            radius=radius, step=0.25, pcb_data=pcb, at=REQ[:2])
        ranked = [p for p in poses if p['rung'] == 'ranked']
        assert census['ranked'] == len(ranked), census
        assert not any(_is_request(p) for p in poses), 'request offered'
        refused = [(p['x'], p['y'], b) for p in ranked
                   for b in [_regrade(board, pcb, before, p, clr, bec)] if b]
        assert not refused, ('ranked poses the re-grade refuses', radius,
                             refused)
        got[radius] = len(ranked)
    # Before #1256: 8 ranked at radius 2 (all refused), 16 at radius 4 (10
    # refused). Radius 2 has no pose the re-grade passes, so none is ranked.
    assert got[2.0] == 0, got
    assert got[4.0] >= 1, got
    print('  PASS: ranked %s, every one passes the re-grade' % got)


def test_the_request_itself_is_never_returned():
    """Forced: a ranker that offers the request (as one would for a refusal
    the ranker does not model -- graphic copper, pad-edge coverage)."""
    import pose_score
    from kicad_parser import parse_kicad_pcb
    from placement import pose_ops
    board = _override_board()
    clr, bec = _knobs(board)
    pcb = parse_kicad_pcb(board)
    real = pose_score.rank_poses
    other = {'x': 150.0, 'y': 100.0, 'rot': REQ[2], 'dist_mm': 1.0,
             'legal': True, 'cost': 1.0}

    def ranker_that_offers_the_request(_pcb, _path, ref, **kw):
        part = kw['state'].parts[ref]
        return [{'x': part.x, 'y': part.y, 'rot': REQ[2], 'dist_mm': 0.0,
                 'legal': True, 'cost': 0.0}, dict(other)]

    pose_score.rank_poses = ranker_that_offers_the_request
    try:
        poses, census = pose_ops.snap_candidates(
            board, REF, rot=REQ[2], clearance=clr, board_edge_clearance=bec,
            radius=2.0, step=0.25, pcb_data=pcb, at=REQ[:2])
        sugg, diag = pose_ops.nearest_legal(
            board, REF, clearance=clr, board_edge_clearance=bec, radius=2.0,
            step=0.25, rotations=(REQ[2],), pcb_data=pcb, at=REQ)
    finally:
        pose_score.rank_poses = real
    assert census['requested_dropped'] == 1, census
    assert not any(_is_request(p) for p in poses), poses[:3]
    assert [p for p in poses if p['rung'] == 'ranked'][0]['x'] == 150.0
    assert diag['requested_dropped'] == 1, diag
    assert [(p['x'], p['y']) for p in sugg] == [(150.0, 100.0)], sugg
    print('  PASS: the request is dropped from both (snap census and '
          'suggestion)')


def _summary(r):
    rows = [ln for ln in r.stdout.splitlines() if ln.startswith('JSON_SUMMARY:')]
    assert len(rows) == 1, (r.returncode, r.stdout[-600:], r.stderr[-600:])
    return json.loads(rows[0].split(':', 1)[1])


def _refusal(r):
    return next((ln for ln in r.stderr.splitlines()
                 if ln.startswith('place_pose REFUSED')), '')


def _set(*extra):
    return ['set', REF, str(REQ[0]), str(REQ[1]), '--rot', '90', *extra]


def test_the_cli_snap_ranks_only_poses_it_can_write():
    board = _override_board()
    out = os.path.join(_TMP.name, 'o2.kicad_pcb')
    r = check([sys.executable, '-X', 'utf8', POSE, board, out, '--snap',
               '--radius', '2', *_set()],
              refuse='pad_conflicts 4 -> 6', code=4)
    assert not os.path.exists(out), 'a refusal wrote the board'
    s = _summary(r)
    sc = s['snap_census']
    assert sc['ranked'] == 0 and sc['ranked_before_radius'] == 0, sc
    assert s['nearest_legal'] is None, s['nearest_legal']
    # Staying put is legal for the ranker, so the knobs are not blamed: the
    # request's own spot being vetoed only agrees with the grade.
    assert sc['dropped_in_place'] == [90.0], sc
    assert sc['input_pose_vetoed'] is None, sc
    assert 'knobs are stricter' not in _refusal(r), _refusal(r)

    out = os.path.join(_TMP.name, 'o4.kicad_pcb')
    r = check([sys.executable, '-X', 'utf8', POSE, board, out, '--snap',
               '--radius', '4', *_set()], accept=True)
    assert r.returncode == 0, (r.returncode, _refusal(r))
    s = _summary(r)
    assert s['snapped']['rung'] == 'ranked', s['snapped']
    assert s['no_worse'] is True, s
    from kicad_parser import parse_kicad_pcb
    fp = parse_kicad_pcb(evidence(out, 'snapped board')).footprints[REF]
    assert [round(fp.x, 4), round(fp.y, 4)] == s['snapped']['to'][:2], (
        fp.x, fp.y, s['snapped'])
    print('  PASS: radius 2 ranks none; radius 4 wrote %s (rung ranked)'
          % (s['snapped']['to'],))


def test_the_cli_suggestion_is_a_pose_the_regrade_passes():
    board = _override_board()
    for radius in ('2', '4'):
        r = check([sys.executable, '-X', 'utf8', POSE, board,
                   os.path.join(_TMP.name, 'n.kicad_pcb'), '--dry-run',
                   '--radius', radius, *_set()],
                  refuse='pad_conflicts 4 -> 6', code=4)
        nl = _summary(r)['nearest_legal']
        if nl is None:
            # Before #1256: x=140.06 y=96.73, which the re-grade refuses.
            assert radius == '2', ('radius 4 has passing poses', radius)
            continue
        assert not _is_request(nl), nl
        assert 'try it with --snap' in _refusal(r), _refusal(r)
        check([sys.executable, '-X', 'utf8', POSE, board,
               os.path.join(_TMP.name, 'n.kicad_pcb'), '--dry-run', 'set',
               REF, str(nl['x']), str(nl['y']), '--rot', '90'], accept=True)
        print('  PASS: --radius %s suggests %s, which grades no worse'
              % (radius, (nl['x'], nl['y'])))


def test_the_knob_sentence_reads_the_input_pose():
    from placement.pose_ops import _snap_census_sentence
    sc = {'candidates_tried': 0, 'ranked': 0, 'lattice': 0, 'radius_mm': 1.0,
          'ranked_before_radius': 0, 'dropped_in_place': [90.0],
          'dropped_by': {'pads': {'count': 3, 'blockers': [['C1', 3]]}},
          'input_pose_vetoed': None}
    assert 'knobs' not in _snap_census_sentence(sc), _snap_census_sentence(sc)
    sc['input_pose_vetoed'] = {'check': 'courtyard', 'blocker': 'Q1'}
    text = _snap_census_sentence(sc)
    assert "vetoes the part's pose on the input board too (courtyard vs Q1)" \
        in text, text
    print('  PASS: %s' % text.strip()[:110])


TESTS = [
    test_the_ranker_judges_the_part_from_its_input_pose,
    test_every_ranked_pose_passes_the_regrade,
    test_the_request_itself_is_never_returned,
    test_the_knob_sentence_reads_the_input_pose,
    test_the_cli_snap_ranks_only_poses_it_can_write,
    test_the_cli_suggestion_is_a_pose_the_regrade_passes,
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
