"""Crystal claims: each passive crystal at its IC's oscillator pins, and each
of its load caps at the crystal, written as `proximity[]` rows.

A crystal's loop is the one placement fact its netlist cannot show: a 3 mm
loop and a 30 mm one have the same connectivity. `emc_roles` already finds
each passive crystal and the IC its two load nets reach, by pin functions and
nets, never by guessing a pin number. This module writes the claims those
roles imply:

* the crystal: each of its two signal pads within `leg_mm` of the IC pad on
  the same net;
* each load cap, meaning a two-pad cap with one pad on a load net and the
  other on ground: its load-net pad within `cap_mm` of the crystal pad on that
  net. A cap whose other pad is not ground is not a load cap, so it gets no
  claim.

The rows are ordinary `proximity[]` claims, so `check_floorplan` grades them
and `place_simple`'s claim term pulls each subject to its partner: the
crystal to its IC, and each cap to the crystal.

The limits are derived defaults, not declarations. They are read off the
human-placed stress corpus by `tests/stress/placement_survey.py`
(`leg_gap_mm`, `cap_gap_mm`: the farthest leg and the farthest load cap of
each crystal, pad edge to pad edge, as the claim grades them). Each row says
so in its `context`.
"""
from __future__ import annotations

import contextlib
import io
import re
from typing import Dict, List, Tuple

#: The farthest crystal leg, pad edge to the IC pad on its net, at the human
#: corpus's 75th percentile, so three human placements in four meet it
#: (`placement_survey.py report`, CRYSTALS: 258 crystals on 437 boards,
#: median 5.37 mm, p75 8.11 mm). It guards against a crystal drifting away;
#: the anneal's claim term pulls the crystal as near its pins as it can.
LEG_MM = 8.1
#: The farthest load cap, pad edge to the crystal pad on its net, likewise
#: (174 crystals with a load cap: median 0.81 mm, p75 2.22 mm).
CAP_MM = 2.2

_CAP_REF = re.compile(r'^C\d')


def _ground(name: str) -> bool:
    from net_queries import is_ground_net_name
    return bool(name) and is_ground_net_name(name)


def crystal_claims(pcb, leg_mm: float = LEG_MM, cap_mm: float = CAP_MM
                   ) -> Tuple[List[Dict], List[str]]:
    """`(rows, notes)`: one `proximity[]` row per passive crystal and per load
    cap, and a line per crystal saying what was found. Reads no pose."""
    from .emc_roles import emc_roles
    with contextlib.redirect_stdout(io.StringIO()):
        roles = emc_roles(pcb).get('roles') or []
    net_name = {nid: (n.name or '') for nid, n in pcb.nets.items()}
    rows: List[Dict] = []
    notes: List[str] = []
    for rec in roles:
        if rec.get('role') != 'crystal' or not rec.get('proximity'):
            continue
        x, ic = rec['part'], rec['ic']
        pads = rec['proximity'][0]['row']['pads']
        rows.append({
            'ref': x, 'near': ic, 'max_mm': leg_mm, 'basis': 'pad_edge',
            'pads': {x: list(pads[x]), ic: list(pads[ic])},
            'context': {'source': 'placement.crystals', 'role': 'crystal',
                        'limit': 'derived_default', 'basis': (
                            'the farthest crystal leg on the human corpus, '
                            '75th percentile (placement_survey.py)')}})
        xfp = pcb.footprints[x]
        load = {p.net_id: str(p.pad_number) for p in xfp.pads
                if str(p.pad_number) in set(pads[x]) and p.net_id}
        caps = []
        for c in sorted(pcb.footprints):
            if c == x or not _CAP_REF.match(c):
                continue
            cu = [p for p in pcb.footprints[c].pads if p.net_id]
            if len(cu) != 2:
                continue
            on = [p for p in cu if p.net_id in load]
            off = [p for p in cu if p.net_id not in load]
            if len(on) != 1 or len(off) != 1 or not _ground(
                    net_name.get(off[0].net_id, '')):
                continue
            caps.append(c)
            rows.append({
                'ref': c, 'near': x, 'max_mm': cap_mm, 'basis': 'pad_edge',
                'pads': {c: [str(on[0].pad_number)],
                         x: [load[on[0].net_id]]},
                'context': {'source': 'placement.crystals',
                            'role': 'crystal load cap',
                            'limit': 'derived_default', 'basis': (
                                'the farthest load cap on the human corpus, '
                                '75th percentile (placement_survey.py)')}})
        notes.append(f"crystal {x} at {ic} pads {', '.join(pads[ic])}"
                     + (f"; load caps {', '.join(caps)}" if caps
                        else "; no load cap (a cap from a load net to ground)"))
    return rows, notes
