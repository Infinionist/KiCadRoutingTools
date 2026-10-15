#!/usr/bin/env python3
"""A multipoint pair whose coupled chain failed is not reported electrically
short.

Issue #165 defers a multipoint pair to single-ended when no coupled chain
routes and relocation does not help, and signals it with the same return as
the all-legs-electrically-short case. The loop then recorded reason
'electrically-short' and printed "All N legs electrically short" for it.

Fixture: tigard /USB_D (USB-C J1 -> U3) takes exactly that path today: the
log carries "Coupled routing failed and relocation unavailable". Its
pair_reports entry must say 'coupled-route-failed'.

    python3 tests/test_multipoint_coupled_fail_reason.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, 'kicad_files', 'tigard.kicad_pcb')
FAILS = []


def check(name, cond, detail=''):
    print(('  PASS  ' if cond else '  FAIL  ') + name + (f'  [{detail}]' if detail else ''))
    if not cond:
        FAILS.append(name)


def main():
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'out.kicad_pcb')
        r = subprocess.run([sys.executable, 'py_router/route_diff.py', BOARD,
                            '--output', out, '--nets', '/USB_D*'],
                           cwd=ROOT, capture_output=True, text=True)
    log = r.stdout + r.stderr
    check('route_diff exits 0', r.returncode == 0, f'exit {r.returncode}')
    check('fixture: the coupled chain failed and the pair was deferred',
          'Coupled routing failed and relocation unavailable' in log)
    m = re.search(r'JSON_SUMMARY:\s*(\{.*\})', log)
    check('JSON_SUMMARY present', m is not None)
    if m:
        reps = {p['pair']: p for p in json.loads(m.group(1)).get('pair_reports', [])}
        rep = reps.get('/USB_D', {})
        check('/USB_D deferred', rep.get('outcome') == 'deferred', str(rep.get('outcome')))
        check("/USB_D reason is 'coupled-route-failed', not 'electrically-short'",
              rep.get('failure_reason') == 'coupled-route-failed',
              str(rep.get('failure_reason')))
    check('the log does not call the failed legs electrically short',
          'legs electrically short - whole pair' not in log)
    if FAILS:
        print(f'\nFAIL: {len(FAILS)} check(s): ' + '; '.join(FAILS))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
