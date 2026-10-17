"""Indicator banks -- LEDs with their series resistors -- as rows, for
grouped placement.

`placement/arrays.suggest_arrays` proposes rows of like parts: the same
footprint and value, each on its own pin of one part. Like VALUE is not like
FUNCTION: ft2232h_jtag's `U4:1k` row holds a reset pull-up and four LED
resistors, esp_prog's `U1:1K` the base resistors of two transistors, and a
decoupling cap belongs at its own pin, not in a row. So grouped placement
does not take the detector's rows; what does belong in a row is read off the
nets here:

* an LED CHANNEL is an LED and the one resistor it shares a net with that no
  other pad is on (`led_channels`);
* channels whose LEDs share a footprint and sit with their resistors in one
  group's zone, and that share one outer net (the rail) while the other is
  different for each (its signal), are an INDICATOR BANK. It is laid out as people lay one out:
  one row, LED and resistor alternating, each resistor beside its own LED
  (`led_rows`, an `arrays[]` row with `allow_mixed`).

Pose-blind, like the detector: it reads pads and nets, never a position.
Whether a bank's row FITS its region is a geometric question for the
caller, which `split_bank` asks through a callback: a bank too long for its
region becomes two or more rows side by side, never a row that cannot seat.
"""
from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

_RES_REF = re.compile(r'^R\d', re.IGNORECASE)
_LED_REF = re.compile(r'^(D|LED)\d', re.IGNORECASE)


def _nets(fp) -> Set[int]:
    return {p.net_id for p in fp.pads if p.net_id > 0}


def is_resistor(fp, ref: str) -> bool:
    """A two-terminal resistor: an `R` reference on two nets."""
    return fp is not None and bool(_RES_REF.match(ref)) and len(_nets(fp)) == 2


def is_led(fp, ref: str) -> bool:
    """An LED: a `D`/`LED` reference whose footprint or value says LED, on
    two nets."""
    if fp is None or not _LED_REF.match(ref):
        return False
    text = ' '.join(str(getattr(fp, k, '') or '') for k in
                    ('footprint_name', 'value')).upper()
    return 'LED' in text and len(_nets(fp)) == 2


def led_channels(pcb) -> Dict[str, dict]:
    """{LED: channel} for every LED that shares a net with exactly one
    resistor and nothing else: `res`, the shared `net`, and the two OUTER
    nets, `led_out` (the LED's other net) and `res_out` (the resistor's)."""
    fps = pcb.footprints or {}
    on_net: Dict[int, List[str]] = {}
    for ref, fp in fps.items():
        for p in fp.pads:
            if p.net_id > 0:
                on_net.setdefault(p.net_id, []).append(ref)
    out: Dict[str, dict] = {}
    for led, fp in sorted(fps.items()):
        if not is_led(fp, led):
            continue
        for net in sorted(_nets(fp)):
            refs = on_net.get(net, [])
            if len(refs) != 2 or refs.count(led) != 1:
                continue
            res = refs[0] if refs[1] == led else refs[1]
            if not is_resistor(fps.get(res), res):
                continue
            out[led] = {'res': res, 'net': net,
                        'led_out': next(iter(_nets(fp) - {net})),
                        'res_out': next(iter(_nets(fps[res]) - {net}))}
            break
    return out


def led_rows(pcb, zone_of: Dict[str, str]) -> List[dict]:
    """One `arrays[]` row per indicator bank: two or more LED channels whose
    LEDs and resistors all sit in one zone (`zone_of`: {ref: zone name}),
    whose LEDs share a footprint, and which share one RAIL -- an outer net
    common to all of them -- while each has a SIGNAL of its own. A channel's
    rail is whichever of its two outer nets more channels of its zone and
    footprint share, so a zone holding two banks on two rails (ulx3s's user
    LEDs to ground beside status LEDs to a supply) splits into both. Members
    alternate LED, resistor in the LEDs' natural order, so each resistor
    stands beside its own LED. The axis and the angle are the seeder's to
    choose: it takes the first that fits the zone around what is already
    there (a narrow strip beside a connector takes the parts end to end, a
    short one takes them across the row).
    """
    fps = pcb.footprints or {}
    chans = led_channels(pcb)
    sets: Dict[Tuple[str, str], List[str]] = {}
    for led, ch in chans.items():
        zone = zone_of.get(led)
        if zone is None or zone != zone_of.get(ch['res']):
            continue
        sets.setdefault((zone, str(fps[led].footprint_name)), []).append(led)
    rows = []
    for (zone, _fp), leds in sorted(sets.items()):
        share: Dict[int, int] = {}
        for l in leds:
            for n in (chans[l]['led_out'], chans[l]['res_out']):
                share[n] = share.get(n, 0) + 1
        banks: Dict[int, List[str]] = {}
        for l in leds:
            a, b = chans[l]['led_out'], chans[l]['res_out']
            rail = a if (share[a], -a) >= (share[b], -b) else b
            banks.setdefault(rail, []).append(l)
        for rail, bank in sorted(banks.items(), key=lambda kv: _natural(min(kv[1], key=_natural))):
            signal = [chans[l]['res_out'] if chans[l]['led_out'] == rail
                      else chans[l]['led_out'] for l in bank]
            if len(bank) < 2 or len(set(signal)) != len(signal):
                continue
            bank = sorted(bank, key=_natural)
            rows.append({'name': f"leds:{zone}:{bank[0]}",
                         'members': [m for l in bank for m in (l, chans[l]['res'])],
                         'order': 'declared', 'rotation': 'shared',
                         'pitch_mm': 'auto', 'axis': 'auto', 'allow_mixed': True,
                         'why': (f"{len(bank)} LEDs, each beside its own series "
                                 f"resistor, on one rail: an indicator bank")})
    return rows


def bank_splits(members: List[str]) -> List[List[List[str]]]:
    """A bank's members (LED, resistor, LED, resistor, ...) as 1, 2, ...
    rows: its channels in order and never apart, balanced to within one
    channel, each row of two channels or more."""
    pairs = [members[i:i + 2] for i in range(0, len(members), 2)]
    n = len(pairs)
    out = []
    for k in range(1, n // 2 + 1):
        sizes = [n // k + (1 if i < n % k else 0) for i in range(k)]
        chunks, i = [], 0
        for size in sizes:
            chunks.append([m for p in pairs[i:i + size] for m in p])
            i += size
        out.append(chunks)
    return out


def split_bank(row: dict, fits) -> List[dict]:
    """`row` (a bank from `led_rows`) as the fewest rows that fit, or [].

    `fits(rows)` -- `rows` a list of member lists -- says whether those rows,
    laid side by side, fit where the bank goes. A whole bank that fits is
    returned as it is. Otherwise its channels are split, in order and never
    apart, into 2, 3, ... rows balanced to within one channel, each of two
    channels or more (one LED is not a bank); the first split that fits is
    returned, each row named after its first LED. None fits: []."""
    for chunks in bank_splits(row['members']):
        k = len(chunks)
        if not fits(chunks):
            continue
        if k == 1:
            return [row]
        stem = row['name'].rsplit(':', 1)[0]
        return [dict(row, name=f"{stem}:{c[0]}", members=c,
                     why=(f"{len(c) // 2} LEDs, each beside its own series "
                          f"resistor, on one rail: row {j + 1} of {k} of an "
                          f"indicator bank its region holds only as {k} "
                          f"rows side by side"))
                for j, c in enumerate(chunks)]
    return []


def _natural(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', ref)]
