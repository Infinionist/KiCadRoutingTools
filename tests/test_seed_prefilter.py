#!/usr/bin/env python3
"""The seat search's prefilter refuses only poses `pose_ok` refuses.

`seeder._pose_prefilter` marks, for many positions at once, the poses whose
grade rect overshoots the usable bbox or that overlap a seated part
(`_overlap_rejector`'s grid, which now serves a drilled part being seated
and marks a far-side drilled part's hole boxes). `_try_place` and
`count_legal_poses` drop those positions unasked, so the search can keep
exactly the pose it always found only if every refusal is one `pose_ok`
makes too. This samples poses over and around two boards -- drilled parts
included, a random quarter of the board excluded as the not-yet-placed --
and asks `pose_ok` about every refused one.

It also holds `refuse.batch` to the per-pose `refuse` it vectorises, and
`_unrefused` to keeping its input's order.

    python3 tests/test_seed_prefilter.py
"""
import os
import random
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import quench as Q, seeder as S  # noqa: E402

BOARDS = ('glasgow_revC.kicad_pcb', 'tigard.kicad_pcb')
FAILS = []


def check(ok, what):
    if not ok:
        FAILS.append(what)
        print(f"  FAIL {what}")


def run(name):
    path = os.path.join(ROOT, 'kicad_files', name)
    st = Q.QuenchState(parse_kicad_pcb(path), path, 0.2, 0.3, 30.0, 0.3, 0.15,
                       1.0, 0.5, 1.0, 0.5, 0.3)
    rng = random.Random(3)
    refs = [r for r, p in st.parts.items() if not p.locked]
    rng.shuffle(refs)
    drilled = [r for r in refs if st.parts[r].has_tht][:8]
    pick = drilled + [r for r in refs if r not in drilled][:24]
    x0, y0, x1, y1 = st.board
    total = refused = wrong = 0
    for ref in pick:
        part = st.parts[ref]
        excl = set(rng.sample(refs, k=min(len(refs) // 4, 40))) - {ref}
        pre = S._pose_prefilter(st, ref, excl)
        over = S._overlap_rejector(st, ref, excl)
        for rot in (0.0, 90.0, 180.0, 270.0):
            part.ensure_rotation(rot)
            xs = np.round(np.array([rng.uniform(x0 - 3, x1 + 3) for _ in range(150)]), 3)
            ys = np.round(np.array([rng.uniform(y0 - 3, y1 + 3) for _ in range(150)]), 3)
            bad = pre(xs, ys, rot)
            if over is not None:
                one = [over(x, y, rot) for x, y in zip(xs.tolist(), ys.tolist())]
                check(one == over.batch(xs, ys, rot).tolist(),
                      f"{name} {ref} rot {rot}: refuse.batch differs from refuse")
            for x, y, b in zip(xs.tolist(), ys.tolist(), bad.tolist()):
                total += 1
                if b:
                    refused += 1
                    if S.pose_ok(st, ref, x, y, rot, excl):
                        wrong += 1
                        check(False, f"{name} {ref} ({x}, {y}, {rot}): refused "
                                     f"by the prefilter, seated by pose_ok")
    check(len(drilled) >= 2, f"{name}: only {len(drilled)} drilled part(s) sampled")
    check(refused > total // 3, f"{name}: the prefilter refused only "
                                f"{refused}/{total} (vacuous)")
    print(f"  {name}: {len(pick)} parts ({len(drilled)} drilled), {total} poses, "
          f"{refused} refused, {wrong} wrongly")


def test_unrefused_order():
    pts = [(float(i), float(i % 7)) for i in range(2000)]

    def pre(xs, ys, rot):
        return (xs.astype(int) % 3) == 0
    got = list(S._unrefused(pre, iter(pts), 0.0))
    check(got == [p for p in pts if int(p[0]) % 3 != 0],
          "_unrefused changed the order or dropped the wrong positions")
    print("  _unrefused: order kept across chunks")


def main():
    for name in BOARDS:
        run(name)
    test_unrefused_order()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
