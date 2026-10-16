#!/usr/bin/env python3
"""Copper a `sense:` group holds, or KiCad locks, is never moved or re-emitted (#1279).

A `sense:<REF>.<PAD>` group names its member tracks and vias by uuid, so any
pass that deletes a member and writes it back -- even unchanged -- takes it out
of the group: the next parse refuses the branch ("member(s) ... are not tracks
or vias on the board") and every later step routes as if nothing were declared.
A KiCad lock (#521) says the same copper is pinned. Both are
`kicad_parser.is_immutable_copper`. These are the passes that moved such copper
anyway, with no pcbnew needed (the live-board twins are
tests/gui_parity/test_1279_sense_branch_live.py):

- the #313 via nudge (place_fanout_clearance's last resort for a boxed cap):
  it moved a KiCad-locked or held via and the placement writer deleted it and
  re-emitted it under a new uuid. Now the nudge leaves it and the writer
  refuses a move that names one.

Rig: tests/test_741_via_nudge_tenting.py's, whose cap C1 is boxed by via VF1
(nudged to (11.325, 9.5) there) and C2 by VF2 (the unlocked control).

Run:
    python3 tests/test_1279_sense_branch_gui.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import traceback

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
for _p in (TESTS_DIR, os.path.join(ROOT, 'py_router'), os.path.join(ROOT, 'py_placer'),
           os.path.join(ROOT, 'py_tools')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_741_via_nudge_tenting as R741  # noqa: E402  (the rig, not its tests)
from kicad_parser import parse_kicad_pcb  # noqa: E402
from run_utils import evidence  # noqa: E402

_TMP = []

#: VF1's via, as the rig writes it, and a lock token placed in it.
VF1_UUID_LINE = '\t\t(uuid "%s")\n\t)' % R741.UUID_VF1
J1_TRACK = 'dddddddd-0000-0000-0000-000000000001'


def _tmpdir():
    d = tempfile.mkdtemp(prefix='t1279g_')
    _TMP.append(d)
    return d


def _rig_text(lock_vf1=False, hold_vf1=False):
    """The #741 rig; VF1 KiCad-locked, or made a member of a `sense:J1.1`
    branch (VF1 + a member track to J1.1, a pad on VF1's net)."""
    txt = R741.RIG_BOARD
    assert txt.count(VF1_UUID_LINE) == 1 and txt.rstrip().endswith(')'), 'the #741 rig moved'
    if lock_vf1:
        txt = txt.replace(VF1_UUID_LINE, '\t\t(locked yes)\n' + VF1_UUID_LINE)
    if hold_vf1:
        j1 = '\n'.join([
            '\t(footprint "T:P"', '\t\t(layer "F.Cu")',
            '\t\t(uuid "eeeeeeee-0000-0000-0000-0000000000a1")', '\t\t(at 13 9.5)',
            '\t\t(property "Reference" "J1" (at 0 -1 0) (layer "F.SilkS") '
            '(uuid "eeeeeeee-0000-0000-0000-0000000000a2") '
            '(effects (font (size 0.5 0.5) (thickness 0.1))))',
            R741._pad('1', 0, 0, 0.6, 0.6, 5, 'VF1'), '\t)'])
        seg = ('\t(segment\n\t\t(start %s 9.5)\n\t\t(end 13 9.5)\n\t\t(width 0.2)\n'
               '\t\t(layer "F.Cu")\n\t\t(net "VF1")\n\t\t(uuid "%s")\n\t)' % (R741.VIA_X, J1_TRACK))
        grp = ('\t(group "sense:J1.1"\n\t\t(uuid "ffffffff-0000-0000-0000-000000000001")\n'
               '\t\t(members "%s" "%s")\n\t)' % (R741.UUID_VF1, J1_TRACK))
        txt = txt.rstrip()[:-1] + j1 + '\n' + seg + '\n' + grp + '\n)'
    return txt


def _nudge(text):
    """Stage `text`, run the REAL repair (nudge on, as #741 drives it) and the
    REAL writer; (input pcb, result dict, output path, console)."""
    d = _tmpdir()
    src = os.path.join(d, 'rig.kicad_pcb')
    with io.open(src, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    evidence(src, 'the staged #741 rig')
    from placement.fanout_clearance import repair_fanout_clearance
    from placement.writer import write_placed_output
    pcb = parse_kicad_pcb(src)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = repair_fanout_clearance(
            pcb, src, clearance=R741.CLEAR, cap_prefix='C', max_displacement=0.0,
            max_displacement_cap=0.0, max_passes=1, allow_rotations=False,
            via_clear_fallback=False)
        dst = os.path.join(d, 'out.kicad_pcb')
        write_placed_output(src, dst, res['placements'], via_moves=res.get('via_moves'),
                            new_segments=res.get('new_segments'), pcb_data=pcb)
    evidence(dst, 'the written rig')
    return pcb, res, dst, buf.getvalue()


def _via(pcb, net_name):
    hits = [v for v in pcb.vias if pcb.nets[v.net_id].name == net_name]
    assert len(hits) == 1, f'{len(hits)} vias on {net_name}'
    return hits[0]


def _moved_nets(pcb, res):
    return sorted(pcb.nets[m[2]['net_id']].name for m in (res.get('via_moves') or []))


def test_nudge_never_moves_a_locked_via():
    pcb, res, dst, log = _nudge(_rig_text(lock_vf1=True))
    assert _via(pcb, 'VF1').locked, 'VF1 is not KiCad-locked in the input'
    assert _moved_nets(pcb, res) == ['VF2'], \
        f'moved {_moved_nets(pcb, res)}; the locked VF1 must stay and the control VF2 move'
    out = parse_kicad_pcb(dst)
    vf1 = _via(out, 'VF1')
    assert (vf1.x, vf1.y, vf1.uuid, vf1.locked) == (R741.VIA_X, 9.5, R741.UUID_VF1, True), \
        f'VF1 written as ({vf1.x}, {vf1.y}) uuid {vf1.uuid} locked {vf1.locked}'
    assert 'is KiCad-locked -- not moved' in log, log[-1500:]


def test_nudge_never_moves_a_held_via():
    pcb, res, dst, log = _nudge(_rig_text(hold_vf1=True))
    br = [(b.name, b.protected) for b in pcb.sense_branches]
    assert br == [('sense:J1.1', True)] and _via(pcb, 'VF1').held_by == 'sense:J1.1', br
    assert _moved_nets(pcb, res) == ['VF2'], \
        f'moved {_moved_nets(pcb, res)}; the held VF1 must stay and the control VF2 move'
    out = parse_kicad_pcb(dst)
    vf1 = _via(out, 'VF1')
    assert (vf1.x, vf1.y, vf1.uuid) == (R741.VIA_X, 9.5, R741.UUID_VF1), \
        f'VF1 written as ({vf1.x}, {vf1.y}) uuid {vf1.uuid}'
    br = [(b.name, b.protected, b.reason) for b in out.sense_branches]
    assert br == [('sense:J1.1', True, None)], f'the written branch: {br}'
    assert 'is held by sense:J1.1 -- not moved' in log, log[-1500:]


def test_writer_refuses_a_move_of_a_locked_via():
    d = _tmpdir()
    src = os.path.join(d, 'rig.kicad_pcb')
    with io.open(src, 'w', encoding='utf-8', newline='') as f:
        f.write(_rig_text(lock_vf1=True))
    pcb = parse_kicad_pcb(src)
    v = _via(pcb, 'VF1')
    move = (v.x, v.y, {'x': v.x + 0.15, 'y': v.y, 'size': v.size, 'drill': v.drill,
                       'layers': v.layers, 'net_id': v.net_id, 'tenting_attrs': {}})
    from placement.writer import write_placed_output
    dst = os.path.join(d, 'out.kicad_pcb')
    try:
        write_placed_output(src, dst, [], via_moves=[move], pcb_data=pcb)
    except ValueError as e:
        assert 'never re-emitted (#1279)' in str(e), str(e)
    else:
        raise AssertionError('the writer re-emitted a KiCad-locked via')
    assert not os.path.exists(dst), 'the refused write left an output file'


TESTS = [
    test_nudge_never_moves_a_locked_via,
    test_nudge_never_moves_a_held_via,
    test_writer_refuses_a_move_of_a_locked_via,
]


def main():
    evidence(os.path.join(TESTS_DIR, 'test_741_via_nudge_tenting.py'), 'the #741 rig module')
    failed = []
    try:
        for t in TESTS:
            try:
                t()
                print(f'  PASS  {t.__name__}')
            except Exception:  # noqa: BLE001
                failed.append(t.__name__)
                print(f'  FAIL  {t.__name__}')
                traceback.print_exc()
    finally:
        for d in _TMP:
            shutil.rmtree(d, ignore_errors=True)
    print(f'{len(TESTS) - len(failed)}/{len(TESTS)} passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
