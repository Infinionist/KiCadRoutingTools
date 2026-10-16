#!/usr/bin/env python3
"""Whole-board text is not kept past the call that read it.

`kicad_parser`'s three text-keyed memos (`_mask_pad_primitives`,
`_footprint_blocks_by_key`, `_footprint_edge_points_by_ref`) let one parse
ask each whole-file question once. Kept afterwards they hold the board's text
and a masked copy -- 216 MB for ddr4_rdimm_tester's 54 MB file, twice that
with `maxsize=2` -- so `parse_kicad_pcb` releases them as it returns, and so
does the writer's own block scan (`kicad_writer._functional_footprint_spans`).

`place_seed.read_written` is the other side of the same cost: a seed reads
its own output back up to nine times, and it now parses once per VERSION of
the file. It must hand back the same object while the file is unchanged and
a fresh parse after any rewrite.

    python3 tests/test_parser_text_caches.py
"""
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))

import kicad_parser as K  # noqa: E402
import kicad_writer  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'esp_prog.kicad_pcb')
FAILS = []
MEMOS = (K._mask_pad_primitives, K._footprint_blocks_by_key_cached,
         K._footprint_edge_points_by_ref_cached)


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILS.append(what)


def held():
    return [m.cache_info().currsize for m in MEMOS]


def main():
    # `cache_clear` resets the hit counts too, so count the scans themselves:
    # within one parse the block scan is asked for more often than it runs.
    asked, ran = [0], [0]
    wrapper, scan = K._footprint_blocks_by_key, K._footprint_blocks_by_key_uncached

    def counting_wrapper(content):
        asked[0] += 1
        return wrapper(content)

    def counting_scan(content):
        ran[0] += 1
        return scan(content)
    K._footprint_blocks_by_key = counting_wrapper
    K._footprint_blocks_by_key_uncached = counting_scan
    try:
        pcb = K.parse_kicad_pcb(BOARD)
    finally:
        K._footprint_blocks_by_key = wrapper
        K._footprint_blocks_by_key_uncached = scan
    check(bool(pcb.footprints), "the board parsed")
    check(asked[0] >= 2 and ran[0] < asked[0],
          f"the memo still serves repeat questions within one parse "
          f"(asked {asked[0]}, scanned {ran[0]})")
    check(held() == [0, 0, 0], f"no board text is held after a parse {held()}")
    text = K.read_board_text(BOARD)
    kicad_writer._functional_footprint_spans(text)
    check(held() == [0, 0, 0], f"none after the writer's block scan {held()}")

    from place_seed import read_written
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'b.kicad_pcb')
        shutil.copyfile(BOARD, path)
        a = read_written(path)
        check(read_written(path) is a, "an unchanged file is parsed once")
        time.sleep(0.01)
        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n')
        b = read_written(path)
        check(b is not a, "a file rewritten in place is parsed again")
        tmp2 = path + '.tmp'
        shutil.copyfile(BOARD, tmp2)
        os.replace(tmp2, path)
        check(read_written(path) is not b, "a file replaced by os.replace is parsed again")
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
