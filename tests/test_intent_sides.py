"""The floorplan's side declarations can be applied before ``place_seed``.

The seeder correctly refuses to interpret front-side geometry as back-side
geometry, but until this actor existed there was no intent-driven way to make
its input agree with a two-sided plan.  A board could therefore seed every
X/Y zone and still leave all footprints on F.Cu.

Refusals are asserted by their REASON (`run_utils.check`), never by a bare
exit code, and each one is checked to have written nothing.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))

import run_utils                                            # noqa: E402
from kicad_parser import parse_kicad_pcb                    # noqa: E402
from placement.legality import footprint_side               # noqa: E402

TOOL = os.path.join(ROOT, 'py_placer', 'place_intent_sides.py')

#: R1 is the part the plan sends to B.Cu; `lock` stamps `(locked yes)` on it.
BOARD = '''(kicad_pcb
  (version 20241229)
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") (uuid "edge"))
  (footprint "test:R" (layer "F.Cu")%(lock)s (uuid "fp-r1") (at 5 5 90)
    (property "Reference" "R1" (at 0 0 90) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1 -0.5) (end 1 0.5) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
    (pad "1" smd rect (at -0.5 0 90) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask")))
  (footprint "test:C" (layer "F.Cu") (uuid "fp-c1") (at 10 5)
    (property "Reference" "C1" (at 0 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_rect (start -1 -0.5) (end 1 0.5) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu" "F.Paste" "F.Mask")))
%(copper)s)
'''
SEGMENT = ('  (segment (start 2 2) (end 4 2) (width 0.2) (layer "F.Cu") '
           '(net 0) (uuid "s1"))\n')


def _board(path, lock=False, copper=False):
    path.write_text(BOARD % {'lock': ' (locked yes)' if lock else '',
                             'copper': SEGMENT if copper else ''},
                    encoding='utf-8')


def _intent(path, c_side='F', **extra):
    doc = {
        'schema': 1,
        'kind': 'floorplan-intent',
        'units': 'mm',
        'blocks': [
            {'name': 'rear', 'refs': ['R1'], 'zone': [1, 1, 9, 9], 'side': 'B'},
            {'name': 'front', 'refs': ['C1'], 'zone': [9, 1, 19, 9], 'side': c_side},
        ],
    }
    doc.update(extra)
    path.write_text(json.dumps(doc), encoding='utf-8')


def _argv(board, output, intent):
    return [sys.executable, '-X', 'utf8', TOOL, str(board), str(output),
            '--intent', str(intent), '--group-by', 'none']


def _summary(run):
    line = [x for x in run.stdout.splitlines()
            if x.startswith('JSON_SUMMARY:')][-1]
    return json.loads(line.split('JSON_SUMMARY: ', 1)[1])


def test_cli_mirrors_only_mismatched_declared_sides():
    with tempfile.TemporaryDirectory() as temp:
        tmp_path = Path(temp)
        board = tmp_path / 'in.kicad_pcb'
        intent = tmp_path / 'intent.json'
        output = tmp_path / 'out.kicad_pcb'
        _board(board)
        (tmp_path / 'in.kicad_pro').write_text('{}', encoding='utf-8')
        _intent(intent)

        run = run_utils.check(_argv(board, output, intent), accept=True)
        assert 'mirrored 1 footprint(s)' in run.stdout, run.stdout
        assert _summary(run)['flipped_refs'] == ['R1'], run.stdout
        pcb = parse_kicad_pcb(run_utils.evidence(str(output)))
        assert footprint_side(pcb.footprints['R1']) == 'B'
        assert footprint_side(pcb.footprints['C1']) == 'F'
        assert (pcb.footprints['R1'].x, pcb.footprints['R1'].y,
                pcb.footprints['R1'].rotation) == (5.0, 5.0, 90.0)
        # The DRC floor travels with the board (#441).
        assert (tmp_path / 'out.kicad_pro').is_file()
    print('  PASS: R1 mirrored to B.Cu at its pose, C1 untouched, the '
          '.kicad_pro carried')


def test_every_declared_claim_is_applied():
    """An edge connector's and a fixed pose's declared face are applied like
    a block's: the same three claims `plan_check` prices the plan on."""
    with tempfile.TemporaryDirectory() as temp:
        tmp_path = Path(temp)
        board = tmp_path / 'in.kicad_pcb'
        intent = tmp_path / 'intent.json'
        output = tmp_path / 'out.kicad_pcb'
        _board(board)
        intent.write_text(json.dumps({
            'schema': 1, 'kind': 'floorplan-intent', 'units': 'mm',
            'edge_connectors': [{'ref': 'C1', 'side': 'B'}],
            'fixed_poses': [{'ref': 'R1', 'x': 5, 'y': 5, 'side': 'B',
                             'basis': 'declared'}]}), encoding='utf-8')
        run = run_utils.check(_argv(board, output, intent), accept=True)
        assert _summary(run)['flipped_refs'] == ['C1', 'R1'], run.stdout
        pcb = parse_kicad_pcb(run_utils.evidence(str(output)))
        assert footprint_side(pcb.footprints['R1']) == 'B'
        assert footprint_side(pcb.footprints['C1']) == 'B'
    print('  PASS: edge_connectors[].side and fixed_poses[].side are applied')


def test_cli_refuses_contradictory_side_claims():
    with tempfile.TemporaryDirectory() as temp:
        tmp_path = Path(temp)
        board = tmp_path / 'in.kicad_pcb'
        intent = tmp_path / 'intent.json'
        output = tmp_path / 'out.kicad_pcb'
        _board(board)
        _intent(intent, fixed_poses=[{
            'ref': 'R1', 'x': 5, 'y': 5, 'side': 'F',
            'basis': 'declared', 'why': 'negative control'}])
        run = run_utils.check(_argv(board, output, intent),
                              refuse='contradictory side declarations',
                              code=2)
        assert 'R1: B from blocks[rear], F from fixed_poses[R1]' in (
            run.stderr), run.stderr
        assert _summary(run)['refused'] == 'contradictory_sides'
        assert not output.exists()
    print('  PASS: a ref declared on both faces is refused, both claims '
          'named, nothing written')


def test_cli_refuses_to_flip_a_locked_part():
    """A KiCad lock is a decision, and no placement tool lifts one: R1 is
    locked on F.Cu and the plan declares B.Cu, so nothing is written. The
    same lock on a part already on its declared face is no obstacle."""
    with tempfile.TemporaryDirectory() as temp:
        tmp_path = Path(temp)
        board = tmp_path / 'in.kicad_pcb'
        intent = tmp_path / 'intent.json'
        output = tmp_path / 'out.kicad_pcb'
        _board(board, lock=True)
        _intent(intent)
        run = run_utils.check(_argv(board, output, intent),
                              refuse='locked in the board', code=2)
        assert _summary(run)['locked'] == ['R1'], run.stdout
        assert not output.exists()

        # R1 already on its declared face: locked, and not moved.
        _intent(intent, blocks=[
            {'name': 'front', 'refs': ['R1', 'C1'], 'side': 'F'}])
        run = run_utils.check(_argv(board, output, intent), accept=True)
        assert _summary(run)['flipped'] == 0, run.stdout
    print('  PASS: a locked part on the undeclared face is refused; on its '
          'declared face it is left alone')


def test_cli_refuses_a_board_with_copper():
    with tempfile.TemporaryDirectory() as temp:
        tmp_path = Path(temp)
        board = tmp_path / 'in.kicad_pcb'
        intent = tmp_path / 'intent.json'
        output = tmp_path / 'out.kicad_pcb'
        _board(board, copper=True)
        _intent(intent)
        run = run_utils.check(_argv(board, output, intent),
                              refuse='would strand routed copper', code=3)
        assert _summary(run)['refused'] == 'has_copper'
        assert not output.exists()
    print('  PASS: a routed board is refused at exit 3, nothing written')


def test_the_tool_is_a_registered_board_writer():
    """#892's registries: a pose writer outside LEVER_REGISTRY raises
    UnaidedViolation in an unaided work dir; one outside REFUSED_TOOLS
    converts silently and breaks a replayed chain; one that does not
    self-record is missing from a redo manifest."""
    import krt_registry
    from placement import provenance
    sys.path.insert(0, os.path.join(ROOT, 'tests', 'stress'))
    import manifest_to_plan

    assert 'place_intent_sides.py' in provenance.LEVER_REGISTRY
    assert 'placement step' in manifest_to_plan.REFUSED_TOOLS[
        'place_intent_sides.py']
    assert krt_registry.self_records(ROOT, 'py_placer/place_intent_sides.py')
    print('  PASS: in LEVER_REGISTRY and REFUSED_TOOLS, and self-records')


TESTS = [
    test_cli_mirrors_only_mismatched_declared_sides,
    test_every_declared_claim_is_applied,
    test_cli_refuses_contradictory_side_claims,
    test_cli_refuses_to_flip_a_locked_part,
    test_cli_refuses_a_board_with_copper,
    test_the_tool_is_a_registered_board_writer,
]


if __name__ == '__main__':
    for test in TESTS:
        print(f'--- {test.__name__}')
        test()
    print('ALL PASS')
