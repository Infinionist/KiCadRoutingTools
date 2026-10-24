#!/usr/bin/env python3
"""#1432: the length-window grade is calibrated to KiCad 10.0.0 and said so
nowhere.

check_drc measures net length as kicad-cli 10.0.0 does; 10.0.7's
LENGTH_DELAY_CALCULATION joins a via to track ends anywhere inside its pad and
clips track inside via pads, so a net within about one via barrel of a
window limit grades differently there (DATA_13 15.149 vs 15.378 mm against a
15.25 mm max). The measure is not re-derived for other releases -- nothing
here can be checked against one without its kicad-cli -- so it now STATES the
release it reproduces, and the installed one when that differs.

  1. net_queries.LENGTH_MEASURE_CALIBRATED_TO names the release.
  2. length_measure_note() names it and, for another installed kicad-cli,
     says which one and what differs; it is silent about the installed
     version when it matches (or is unknown).
  3. check_drc's length-rule summary carries `calibrated_to_kicad` and prints
     the note when it graded a net.

    python3 tests/test_1432_length_measure_calibration.py
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT)
for _p in ('py_router', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import net_queries                                             # noqa: E402
import kicad_oracle                                            # noqa: E402
from copy_board import copy_board                              # noqa: E402

failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def main():
    check('the calibrated release is named',
          net_queries.LENGTH_MEASURE_CALIBRATED_TO == '10.0.0')
    real = kicad_oracle.kicad_cli_version
    try:
        kicad_oracle.kicad_cli_version = lambda cli=None: '10.0.7'
        n = net_queries.length_measure_note()
        check('another installed release is named, with what differs',
              'KiCad 10.0.0' in n and 'installed kicad-cli is 10.0.7' in n
              and 'inside the via pad' in n, n)
        kicad_oracle.kicad_cli_version = lambda cli=None: '10.0.0'
        n = net_queries.length_measure_note()
        check('the calibrated release installed: no warning',
              'installed' not in n and 'KiCad 10.0.0' in n, n)
        kicad_oracle.kicad_cli_version = lambda cli=None: None
        n = net_queries.length_measure_note()
        check('no kicad-cli: the calibration is still stated',
              'installed' not in n and 'KiCad 10.0.0' in n, n)
    finally:
        kicad_oracle.kicad_cli_version = real

    tmp = tempfile.mkdtemp(prefix='t1432_')
    try:
        dst = os.path.join(tmp, 'ro.kicad_pcb')
        with contextlib.redirect_stdout(io.StringIO()):
            copy_board(os.path.join(ROOT, 'kicad_files', 'routed_output.kicad_pcb'),
                       dst)
        with open(os.path.join(tmp, 'ro.kicad_dru'), 'w', encoding='utf-8') as fh:
            fh.write('(version 1)\n(rule "d13"\n'
                     '  (condition "A.NetName == \'Net-(U2A-DATA_13)\'")\n'
                     '  (constraint length (max 15.25mm)))\n')
        r = subprocess.run([sys.executable, '-X', 'utf8',
                            os.path.join(ROOT, 'py_router', 'check_drc.py'), dst,
                            '--clearance-margin', '0.1'], cwd=ROOT,
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace')
        out = r.stdout + r.stderr
        check('check_drc prints the calibration beside the length summary',
              'Length rules (.kicad_dru' in out
              and '(length) lengths are measured as KiCad 10.0.0' in out,
              out[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('FAILED: ' + ', '.join(failures) if failures else 'PASS')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
