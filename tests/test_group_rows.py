#!/usr/bin/env python3
"""Indicator banks: LEDs with their series resistors, read off the nets.

`placement/group_rows.py` finds an LED CHANNEL -- an LED and the one resistor
it shares a net with that no other pad is on -- and lays the channels of one
zone, one LED footprint and one RAIL (an outer net common to them, each with
a signal of its own) out as ONE row alternating LED and resistor.
`plan_regions.py` writes those rows into the regions intent (`--no-arrays`
writes none). This holds:

* the channels on tracked boards (tigard's five, glasgow_revC's twelve) and
  none on boards without an LED;
* a bank alternates LED, resistor, each resistor its LED's own;
* a zone splits a bank (an LED and its resistor in two zones is no channel of
  a bank), and two rails in one zone make two banks (ulx3s);
* a net an LED shares with a resistor AND another part is no channel;
* the intent `plan_regions` writes loads and plan-checks with its banks, and
  `--no-arrays` writes none.

    python3 tests/test_group_rows.py
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import group_rows as G  # noqa: E402

FAILS = []


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}")
    if not ok:
        FAILS.append(what)


def board(name):
    return parse_kicad_pcb(os.path.join(ROOT, 'kicad_files', name))


def alternates(pcb, chans, members):
    leds, res = members[0::2], members[1::2]
    return (len(leds) == len(res)
            and all(G.is_led(pcb.footprints[l], l) for l in leds)
            and all(chans[l]['res'] == r for l, r in zip(leds, res)))


def main():
    tig = board('tigard.kicad_pcb')
    ch = G.led_channels(tig)
    check({l: c['res'] for l, c in ch.items()}
          == {'D1': 'R4', 'D2': 'R5', 'D3': 'R8', 'D4': 'R9', 'D5': 'R11'},
          "tigard's five LED channels, each with its own resistor")
    rows = G.led_rows(tig, {r: 'ui' for r in tig.footprints})
    check(len(rows) == 1 and alternates(tig, ch, rows[0]['members'])
          and rows[0].get('allow_mixed') is True and rows[0]['order'] == 'declared',
          f"one alternating bank of all five ({rows and rows[0]['members']})")
    zones = {r: 'ui' for r in tig.footprints}
    zones['R8'] = 'core'
    rows = G.led_rows(tig, zones)
    check(rows and 'D3' not in rows[0]['members'] and 'R8' not in rows[0]['members'],
          "an LED whose resistor sits in another zone leaves the bank")
    check(G.led_rows(tig, {}) == [], "no zone, no bank")

    gla = board('glasgow_revC.kicad_pcb')
    check(len(G.led_channels(gla)) == 12, "glasgow_revC: twelve LED channels")

    ulx = board('ulx3s.kicad_pcb')
    rows = G.led_rows(ulx, {r: 'z' for r in ulx.footprints})
    chu = G.led_channels(ulx)
    check(len(rows) == 2 and all(alternates(ulx, chu, r['members']) for r in rows),
          f"ulx3s: two rails in one zone make two banks "
          f"({[len(r['members']) // 2 for r in rows]} LEDs)")

    esp = board('esp_prog.kicad_pcb')
    check(G.led_channels(esp) == {} and G.led_rows(esp, {r: 'z' for r in esp.footprints}) == [],
          "esp_prog: no LED, no bank")

    # A third pad on the LED-resistor net: no channel.
    led, c = next(iter(G.led_channels(tig).items()))
    other = next(r for r, fp in tig.footprints.items() if r not in (led, c['res'])
                 and any(p.net_id > 0 for p in fp.pads))
    pad = next(p for p in tig.footprints[other].pads if p.net_id > 0)
    saved = pad.net_id
    pad.net_id = c['net']
    try:
        check(led not in G.led_channels(tig),
              "an LED net shared with a resistor and a third part is no channel")
    finally:
        pad.net_id = saved

    # plan_regions writes the banks into an intent that plan-checks.
    with tempfile.TemporaryDirectory() as tmp:
        fixed = os.path.join(ROOT, 'kicad_files', 'tigard.kicad_pcb')
        groups = os.path.join(tmp, 'groups.json')
        refs = sorted(tig.footprints)
        json.dump({'schema': 1, 'groups': [{'name': 'all', 'refs': refs}]},
                  open(groups, 'w'))
        out = {}
        for flag in ([], ['--no-arrays']):
            path = os.path.join(tmp, f"regions{len(flag)}.json")
            r = subprocess.run([sys.executable, '-X', 'utf8',
                                os.path.join(ROOT, 'py_placer', 'plan_regions.py'),
                                fixed, '--groups', groups, '--out', path] + flag,
                               capture_output=True, text=True, cwd=ROOT)
            if not os.path.isfile(path):
                check(False, f"plan_regions {flag} wrote no intent (exit "
                             f"{r.returncode}): {r.stderr[-300:]}")
                continue
            out[len(flag)] = json.load(open(path))
            if not flag:
                pc = subprocess.run([sys.executable, '-X', 'utf8',
                                     os.path.join(ROOT, 'py_tools', 'check_floorplan.py'),
                                     fixed, '--intent', path, '--plan-only'],
                                    capture_output=True, text=True, cwd=ROOT)
                check(pc.returncode in (0, 4) and 'Traceback' not in pc.stderr,
                      f"the intent with its bank plan-checks (exit {pc.returncode})")
        if 0 in out:
            banks = [a for a in out[0].get('arrays') or [] if a['name'].startswith('leds:')]
            check(len(banks) == 1 and len(banks[0]['members']) == 10
                  and out[0].get('min_reader', 0) >= 7,
                  "plan_regions writes tigard's bank (min_reader 7)")
        if 1 in out:
            check(not out[1].get('arrays'), "--no-arrays writes no bank")
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
