#!/usr/bin/env python3
"""The copper-to-hole floor is read FRESH: from the project as it is now, and
on the GUI from the live board (#1038, #1238).

`obstacle_map.resolve_hole_clearance` prices NPTH holes at the board's declared
`min_hole_clearance` (raised to its `fab_floor_origin`), and since #1038 any
value above 0 also turns on the via-copper keep-out around them. Two ways it
served a value that was no longer the board's:

1. Its cache was keyed on the board PATH for the life of the process. A CLI
   step is a fresh process, so it always read the current file; the GUI
   plugin is ONE process for a whole KiCad session, so a user who saved a new
   Board Setup min hole clearance kept routing at the first value read.

2. The GUI never saw the live board's rule at all. It read the file beside
   the board -- stale until saved, and absent on an unsaved board (#1238).

Now the file read is keyed on the project's stamp (mtime, size, inode), and a
PCBData built from pcbnew answers the rule from its in-memory design settings
through `live_rules_provider`, at call time. A live value AT pcbnew's factory
default on a board whose project does not declare the key reads as "declares
nothing" -- what the file reads -- so the GUI and the CLI price the same
floor on a board pcbnew just loaded.

wx-free (fake providers). The real-pcbnew half is
tests/gui_parity/test_live_hole_clearance.py.

Standalone, no pytest:  python3 tests/test_1038_hole_floor_fresh.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'py_router'))

import obstacle_map                                              # noqa: E402
from obstacle_map import resolve_hole_clearance                   # noqa: E402

FACTORY = 0.25          # pcbnew's m_HoleClearance default (KiCad 10)
_checks = 0
_fails = 0


def check(cond, label, detail=''):
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        _fails += 1
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ''))


def _reset():
    obstacle_map._HOLE_CLR_CACHE.clear()
    obstacle_map._HOLE_CLR_ANNOUNCED.clear()
    obstacle_map._HOLE_CLR_ORIGIN.clear()


def _board(tmp, name):
    pcb = os.path.join(tmp, name + '.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as fh:
        fh.write('(kicad_pcb (version 20240108))\n')
    return pcb


def _project(pcb, rule=None, origin=None, atomic=True):
    """Write `pcb`'s sibling project: atomically (tmp + os.replace, how every
    writer here does it) or in place (same inode)."""
    proj = {"board": {"design_settings": {"rules": {}}}}
    if rule is not None:
        proj["board"]["design_settings"]["rules"]["min_hole_clearance"] = rule
    if origin is not None:
        proj["kicad_routing_tools"] = {
            "fab_floor_origin": {"min_hole_clearance": origin}}
    pro = os.path.splitext(pcb)[0] + '.kicad_pro'
    if atomic:
        with open(pro + '.tmp', 'w', encoding='utf-8') as fh:
            json.dump(proj, fh)
        os.replace(pro + '.tmp', pro)
    else:
        with open(pro, 'w', encoding='utf-8') as fh:
            json.dump(proj, fh)


def _resolve(pcb_data, config=None, pcb_file=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        v = resolve_hole_clearance(pcb_data, config, pcb_file)
    return v, buf.getvalue()


def _live(path, rules, defaults=None):
    """A PCBData-shaped stand-in for build_pcb_data_from_board's output: the
    provider is a callable read when asked (it closes over `rules`, so a test
    can edit the 'live board' after building), the defaults are pcbnew's."""
    return SimpleNamespace(
        source_path=path,
        live_rules_provider=lambda: dict(rules),
        live_rule_defaults=({'min_hole_clearance': FACTORY}
                            if defaults is None else defaults))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        print("the file read: the same process sees the project as it is NOW")
        _reset()
        p = _board(tmp, 'saved')
        _project(p, rule=0.25)
        v1, out1 = _resolve(SimpleNamespace(source_path=p))
        check(v1 == 0.25, "first read: the declared 0.25", f"{v1}")
        _project(p, rule=0.30)                      # a KiCad save of Board Setup
        v2, out2 = _resolve(SimpleNamespace(source_path=p))
        check(v2 == 0.30, "after the project is re-written, the NEW 0.30 "
              "(a path-keyed cache served 0.25 for the rest of the session)",
              f"{v2}")
        check('Copper-to-hole 0.3mm' in out2,
              "and the new floor is announced", out2.strip())
        _project(p, rule=0.35, atomic=False)        # in place: same inode
        v3, _ = _resolve(SimpleNamespace(source_path=p))
        check(v3 == 0.35, "an in-place rewrite (same inode) is seen too", f"{v3}")

        q = _board(tmp, 'late')
        v4, _ = _resolve(SimpleNamespace(source_path=q))
        check(v4 == 0.0, "no project yet: declares nothing", f"{v4}")
        _project(q, rule=0.30)
        v5, _ = _resolve(SimpleNamespace(source_path=q))
        check(v5 == 0.30, "a project created after the first read is read",
              f"{v5}")
        os.remove(os.path.splitext(q)[0] + '.kicad_pro')
        v6, _ = _resolve(SimpleNamespace(source_path=q))
        check(v6 == 0.0, "and a removed one stops counting", f"{v6}")

        o = _board(tmp, 'origin')
        _project(o, rule=0.15, origin=0.40)
        v7, _ = _resolve(SimpleNamespace(source_path=o))
        check(v7 == 0.40 and o in obstacle_map._HOLE_CLR_ORIGIN,
              "the origin outranks a relaxed rule, and is recorded as the source",
              f"{v7}")
        _project(o, rule=0.30)
        v8, out8 = _resolve(SimpleNamespace(source_path=o))
        check(v8 == 0.30 and o not in obstacle_map._HOLE_CLR_ORIGIN,
              "an origin that is gone stops being the source", f"{v8}")
        check("board's own min_hole_clearance" in out8,
              "and the banner names the rule, not the origin", out8.strip())

        print("\nthe cache still caches: an unchanged project is not re-read")
        import list_nets
        real = list_nets.board_constraint
        reads = []
        list_nets.board_constraint = lambda *a, **k: (reads.append(a),
                                                      real(*a, **k))[1]
        try:
            _reset()
            for _ in range(50):
                _resolve(SimpleNamespace(source_path=p))
            check(len(reads) == 1, "50 resolves of one unchanged board read "
                  "the project once (the read measured 0.07-3.4 ms on the "
                  "tracked boards; the obstacle map is rebuilt per net)",
                  f"{len(reads)} reads")
        finally:
            list_nets.board_constraint = real

        print("\nthe live board (a PCBData from build_pcb_data_from_board)")
        _reset()
        bare = _board(tmp, 'bare')                     # no project at all
        v, _ = _resolve(_live(bare, {'min_hole_clearance': FACTORY}))
        check(v == 0.0, "project-less board at pcbnew's factory 0.25 declares "
              "nothing -- what the CLI reads from the same file", f"{v}")
        v, _ = _resolve(_live(bare, {'min_hole_clearance': 0.30}))
        check(v == 0.30, "a Board Setup edit on it counts before any save",
              f"{v}")
        v, _ = _resolve(_live(bare, {'min_hole_clearance': 0.20}))
        check(v == 0.20, "so does a GUI step's writeback lowering it (the CLI's "
              "next step reads its written rule the same way)", f"{v}")

        d25 = _board(tmp, 'declares25')
        _project(d25, rule=0.25)
        v, _ = _resolve(_live(d25, {'min_hole_clearance': FACTORY}))
        check(v == 0.25, "a project that DECLARES the factory value is "
              "honoured at it", f"{v}")
        d30 = _board(tmp, 'declares30')
        _project(d30, rule=0.30)
        v, _ = _resolve(_live(d30, {'min_hole_clearance': FACTORY}))
        check(v == 0.25, "an unsaved edit of a declared 0.30 down to exactly "
              "0.25 is the live 0.25, not the file's 0.30", f"{v}")
        v, _ = _resolve(_live(d30, {'min_hole_clearance': 0.40}))
        check(v == 0.40, "an unsaved edit up to 0.40 is priced at 0.40", f"{v}")
        d0 = _board(tmp, 'declares0')
        _project(d0, rule=0.0)
        v, _ = _resolve(_live(d0, {'min_hole_clearance': 0.0}))
        check(v == 0.0, "a declared 0 (not configured) stays 0", f"{v}")

        og = _board(tmp, 'liveorigin')
        _project(og, rule=0.25, origin=0.25)
        v, _ = _resolve(_live(og, {'min_hole_clearance': 0.15}))
        check(v == 0.25 and og in obstacle_map._HOLE_CLR_ORIGIN,
              "a live rule a GUI step lowered is raised to the project's "
              "fab_floor_origin, as the CLI's step 2 is", f"{v}")

        v, _ = _resolve(_live("", {'min_hole_clearance': 0.30}))
        check(v == 0.30, "an UNSAVED board (#1238: no file at all) is priced "
              "from its live rule", f"{v}")
        v, _ = _resolve(_live("", {'min_hole_clearance': FACTORY}))
        check(v == 0.0, "and at the factory value it declares nothing", f"{v}")

        rules = {'min_hole_clearance': 0.30}
        built = _live(bare, rules)
        v_before, _ = _resolve(built)
        rules['min_hole_clearance'] = 0.35          # edited after the build
        v_after, _ = _resolve(built)
        check((v_before, v_after) == (0.30, 0.35),
              "the provider is asked at CALL time, so a PCBData built before "
              "an edit prices the edit", f"{v_before} -> {v_after}")

        v, _ = _resolve(_live(d30, {'min_hole_clearance': FACTORY}, defaults={}))
        check(v == 0.30, "without pcbnew's factory value the live rule is not "
              "guessed at: the file decides, as before", f"{v}")
        v, _ = _resolve(_live(bare, {'min_track_width': 0.1}))
        check(v == 0.0, "a provider that does not answer the key leaves the "
              "file to decide", f"{v}")
        v, _ = _resolve(_live(bare, {'min_hole_clearance': 0.30}),
                        SimpleNamespace(hole_clearance=0.5))
        check(v == 0.5, "an explicit config.hole_clearance still wins", f"{v}")
        v, _ = _resolve(_live(os.path.join(tmp, 'elsewhere.kicad_pcb'),
                              {'min_hole_clearance': FACTORY}), None, d30)
        check(v == 0.25, "pcb_file names the file whose project is asked "
              "whether it declares the key", f"{v}")

    print(f"\n{_checks - _fails}/{_checks} checks passed")
    return 1 if _fails else 0


if __name__ == '__main__':
    sys.exit(main())
