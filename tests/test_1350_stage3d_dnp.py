#!/usr/bin/env python3
"""#1350: a DNP part is not a part whose 3D model fell back to a box.

The 3D board's GLB export runs `kicad-cli pcb export glb --no-dnp`, so a
Do-Not-Populate part has no model node by design. The scene still drew it
as a body box, and the GLB line counted it among the parts with no model:
on imx6ulz_4l 23 of the 25 "boxes" were DNP 0402s whose model resolves
(movie3d, gp14; main's stage3d has the same export and the same count).

Function level -- no kicad-cli and no render needed:
  1. the parser reads `(attr smd dnp)` and `dnp_refs` names the part;
  2. `build_scene` draws a DNP part as its pads alone, like the other
     not-assembled parts, and keeps every other part's body;
  3. `glb_summary` counts models over the parts that will be populated
     and names the DNP parts apart, never as missing models.

    python3 tests/test_1350_stage3d_dnp.py
"""
import os
import shutil
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, TESTS_DIR)

from kicad_parser import parse_kicad_pcb                  # noqa: E402
from stage3d import scene as SC                           # noqa: E402
from run_utils import evidence                            # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'cap_chain.kicad_pcb')
failures = []


def ok(label, cond, detail=''):
    print(f"  [{'ok' if cond else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


def with_dnp_c2(tmp):
    """cap_chain with C2's attr line marked dnp (C2's is the second
    `(attr smd)` in the file)."""
    with open(BOARD, encoding='utf-8') as f:
        text = f.read()
    first = text.index('(attr smd)')
    second = text.index('(attr smd)', first + 1)
    assert text.rfind('"C2"', 0, second) > text.rfind('"C1"', 0, second)
    text = text[:second] + '(attr smd dnp)' + text[second + len('(attr smd)'):]
    path = os.path.join(tmp, 'dnp.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    return evidence(path, 'the DNP board')


def main():
    tmp = tempfile.mkdtemp(prefix='t1350_')
    try:
        pcb = parse_kicad_pcb(with_dnp_c2(tmp))
        print("1: the DNP part is read")
        ok("C2 parses as DNP, C1 does not",
           pcb.footprints['C2'].dnp and not pcb.footprints['C1'].dnp)
        ok("dnp_refs names exactly C2", SC.dnp_refs(pcb) == {'C2'},
           str(SC.dnp_refs(pcb)))

        print("2: the scene")
        sc = SC.build_scene(pcb)
        ok("C2 is drawn as its pads alone", sc['parts']['C2']['body'] is None
           and sc['parts']['C2']['pads'], str(sc['parts']['C2'].get('body')))
        ok("C1 keeps its body", sc['parts']['C1']['body'] is not None)
        bare = SC.build_scene(parse_kicad_pcb(BOARD))
        ok("without dnp, C2 keeps its body too",
           bare['parts']['C2']['body'] is not None)

        print("3: the GLB line")
        line = SC.glb_summary(pcb, ['C1', 'J1'])
        ok("models are counted over the populated parts (3, not 4)",
           line.startswith('GLB: 2 of 3 parts have a model'), line)
        ok("the DNP part is named apart",
           '1 DNP part(s) not populated, so not drawn: C2' in line, line)
        line = SC.glb_summary(parse_kicad_pcb(BOARD), ['C1', 'J1'])
        ok("a board with no DNP part reads as before",
           line == 'GLB: 2 of 4 parts have a model', line)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\nFAIL ({len(failures)}): " + '; '.join(failures))
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(main())
