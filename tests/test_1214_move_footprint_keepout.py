#!/usr/bin/env python3
"""#1214: moving or rotating a footprint must carry its own keep-out zone.

A footprint-internal `(zone ...)` stores its `(polygon (pts (xy ...)))` in
ABSOLUTE board coordinates (the same fact `_FLIP_NAMED_REFUSALS['zone']` in
`py_placer/placement/writer.py` records for the mirror). The move and rotate
path of `write_placed_output` rewrites the footprint's `(at ...)`, rotates
pad and text angles, but leaves the zone's `(pts ...)` untouched, so moving a
footprint leaves its keep-out zone behind at the old absolute position.

This test injects a footprint-internal zone into a real footprint, moves and
rotates the footprint, and asserts the zone's `(pts ...)` land where the same
rigid transform (rotate about the old origin, then translate) puts them.

    python3 tests/test_1214_move_footprint_keepout.py
"""
from __future__ import annotations

import math
import os
import re
import shutil
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(REPO, 'py_router'))
sys.path.insert(0, os.path.join(REPO, 'py_placer'))
sys.path.insert(0, TESTS)

BOARD = os.path.join(REPO, 'kicad_files', 'tigard.kicad_pcb')
REF = 'J7'

# A 1 mm square zone with its lower-left corner on the footprint origin.
ZONE_PTS = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def _zone_sexpr(pts):
    """A footprint-internal keep-out zone with the given absolute (x, y) pts."""
    body = " ".join(f"(xy {x:.6f} {y:.6f})" for x, y in pts)
    return (
        "(zone\n"
        "\t\t\t(layer \"B.Cu\")\n"
        "\t\t\t(uuid \"12140000-0000-0000-0000-000000000000\")\n"
        "\t\t\t(hatch full 0.508)\n"
        "\t\t\t(connect_pads (clearance 0))\n"
        "\t\t\t(min_thickness 0.254)\n"
        "\t\t\t(keepout (tracks not_allowed) (vias not_allowed) "
        "(pads not_allowed) (copperpour not_allowed) (footprints not_allowed))\n"
        "\t\t\t(polygon (pts " + body + "))\n"
        "\t\t)"
    )


def _inject_zone(src, dst, ref, pts):
    """Copy `src` to `dst`, adding a keep-out zone to the footprint `ref`."""
    from kicad_parser import iter_footprint_blocks
    text = open(src, encoding='utf-8').read()
    out = None
    for s, e, fp_text, _raw, key in iter_footprint_blocks(text):
        if key != ref:
            continue
        close = fp_text.rindex(')')
        body = fp_text[:close] + '\n\t\t' + _zone_sexpr(pts) + '\n\t' + fp_text[close:]
        out = text[:s] + body + text[e:]
        break
    if out is None:
        raise AssertionError(f"{ref} not found in {src} -- fixture stale")
    with open(dst, 'w', encoding='utf-8') as fh:
        fh.write(out)
    return dst


def _zone_pts(path, ref):
    """Return the absolute (x, y) pts of the first zone in footprint `ref`."""
    from kicad_parser import iter_footprint_blocks
    text = open(path, encoding='utf-8').read()
    for _s, _e, fp_text, _raw, key in iter_footprint_blocks(text):
        if key != ref:
            continue
        m = re.search(r'\(pts\s+((?:\(xy\s+[-\d.]+\s+[-\d.]+\)\s*)+)\)', fp_text)
        if not m:
            return None
        pts = []
        for x, y in re.findall(r'\(xy\s+([-\d.]+)\s+([-\d.]+)\)', m.group(1)):
            pts.append((float(x), float(y)))
        return pts
    return None


def _transform(pts, cx, cy, delta_rot, dx, dy):
    """Rotate each point about (cx, cy) by delta_rot, then translate by (dx, dy)."""
    rad = math.radians(delta_rot)
    c, s = math.cos(rad), math.sin(rad)
    out = []
    for x, y in pts:
        rx = cx + (x - cx) * c - (y - cy) * s
        ry = cy + (x - cx) * s + (y - cy) * c
        out.append((rx + dx, ry + dy))
    return out


def main():
    from kicad_parser import parse_kicad_pcb
    from placement.writer import SideFlipUnsupported, write_placed_output

    if not os.path.isfile(BOARD):
        print(f"ERROR: board not found: {BOARD}")
        return 2

    tmp = tempfile.mkdtemp(prefix='krt1214')
    problems = []
    try:
        fp = parse_kicad_pcb(BOARD).footprints[REF]
        old_x, old_y, old_rot = fp.x, fp.y, fp.rotation or 0.0

        # A clear move plus a 90 degree rotation, so both halves of the
        # transform are exercised.
        dx, dy, delta_rot = 5.0, 3.0, 90.0
        new_x, new_y, new_rot = old_x + dx, old_y + dy, (old_rot + delta_rot) % 360

        src = _inject_zone(BOARD, os.path.join(tmp, 'in.kicad_pcb'), REF,
                           [(old_x + px, old_y + py) for px, py in ZONE_PTS])
        out = os.path.join(tmp, 'out.kicad_pcb')
        write_placed_output(src, out, [{
            'reference': REF, 'new_x': new_x, 'new_y': new_y,
            'new_rotation': new_rot}])

        got = _zone_pts(out, REF)
        if got is None:
            problems.append("the output footprint lost its keep-out zone")
        else:
            want = _transform([(old_x + px, old_y + py) for px, py in ZONE_PTS],
                              old_x, old_y, delta_rot, dx, dy)
            want = [(round(x, 6), round(y, 6)) for x, y in want]
            got = [(round(x, 6), round(y, 6)) for x, y in got]
            if got != want:
                problems.append(
                    f"zone pts did not move with the footprint\n"
                    f"  expected {want}\n"
                    f"  got      {got}\n"
                    f"  (footprint moved by ({dx}, {dy}) and rotated "
                    f"{delta_rot} deg about ({old_x}, {old_y}))")
            # The parser must read the moved zone back as the same keep-out.
            moved = parse_kicad_pcb(out).board_info.keepouts
            seen = [[(round(x, 6), round(y, 6)) for x, y in k['polygon']]
                    for k in moved if k.get('in_footprint')]
            if want not in seen:
                problems.append(f"the parser does not read the moved keep-out back: {seen}")

        # A fill is absolute too and this move does not carry it, so a zone
        # with one must refuse and say why rather than leave the fill behind.
        filled = os.path.join(tmp, 'filled.kicad_pcb')
        text = open(src, encoding='utf-8').read().replace(
            "\t\t\t(polygon (pts ",
            "\t\t\t(filled_polygon (layer \"B.Cu\") (pts (xy 0 0) (xy 1 0) (xy 1 1)))\n"
            "\t\t\t(polygon (pts ", 1)
        with open(filled, 'w', encoding='utf-8') as fh:
            fh.write(text)
        try:
            write_placed_output(filled, os.path.join(tmp, 'filled-out.kicad_pcb'), [{
                'reference': REF, 'new_x': new_x, 'new_y': new_y,
                'new_rotation': new_rot}])
            problems.append("a zone with a (filled_polygon ...) moved without refusing")
        except SideFlipUnsupported as e:
            if 'filled_polygon' not in str(e):
                problems.append(f"the fill refusal does not name its construct: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        print(f"FAIL: {len(problems)} problem(s)")
        for p in problems:
            print("  " + p)
        return 1
    print("PASS: a moved and rotated footprint carries its keep-out zone")
    return 0


if __name__ == '__main__':
    sys.exit(main())
