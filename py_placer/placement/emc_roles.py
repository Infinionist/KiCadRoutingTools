"""EMC roles (#1254): which parts form a converter's hot loop, a crystal's load
loop or an ESD path, and how far apart their pads are. REPORT ONLY.

`proximity[]` (#902) can grade "Cin within 1 mm of VIN/PGND", "L within 1 mm
of SW", "the crystal within 2 mm of XIN/XOUT", "the ESD array within 1 mm of
the connector" -- the placement facts that decide EMC on a converter, a crystal
or a USB port -- and nothing proposed those rows. This names the parts and
measures each leg; it writes no intent key. #902's "never emitted" stands: a
row whose limit is copied from the current gap would bless the current pose, so
every row's `max_mm` is left null for the author.

`board_context --json` publishes it as `emc_roles`. It lives here, in
py_placer, beside the role table it reads (`roles.py`), so a placement-side
consumer can read it without importing py_tools.

Two pitfalls #1254 measured are designed out:

* LEGS, never a loop AREA. Flipping ulx3s C5 LOWERS its pad-centre C5+ -> VIN
  -> GND -> C5- polygon from 4.620 to 0.852 mm2 while both legs get worse: the
  flipped polygon crosses itself and its lobes cancel.
* a crystal leg is never classified by `net_queries.is_power_net_name`: its
  `\\d` alternative reads watchy's `/32K_P` and `/40M_N` as rails. Only ground
  is read off a net name (`is_ground_net_name`, which has no such
  alternative); every other role is read off PIN FUNCTIONS, and a part whose
  pins carry none is `unknown` -- a pin number is never guessed at (ulx3s's
  three TLV62569 bucks).
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .roles import _copper_pads, _norm, infer_role, pin_function_name

#: A switch-node pin function token: SW, SW1, LX, LX2, VSW (LT1373), and the
#: LX of an on-chip regulator (`VREG_LX`, tokenised).
_SW_FN = re.compile(r'^V?(SW|LX)\d?$')
#: An input-supply pin function token: VIN, VIN1, PVIN (`VREG_VIN` too).
_VIN_FN = re.compile(r'^P?VIN\d?$')
_PGND_FN = re.compile(r'^PGND\d*$')
_GND_FN = re.compile(r'^[ADP]?GND\d*$')
#: An ESD array's protected-line pins: I/O1, IO2, CH3.
_IO_FN = re.compile(r'^(I/?O|CH)\d+$')
#: What else an ESD array's pins may be called: its rail clamp, its return,
#: no-connect. A part with any OTHER named pin (an ADC's CLK / DIN / CS
#: beside its CH0..CH7) is not an ESD array.
_ESD_OTHER_FN = re.compile(r'^(V?BUS|VCC|VDD|VP|[ADP]?GND\d*|N/?C)$')
#: An active oscillator's clock output.
_OSC_OUT_FN = re.compile(r'^(OUT|OUTPUT|CLK\w*)$')

#: The measurement every leg carries, said once.
LEG_BASIS = ('pad edge to pad edge: placement.legality.pad_rect + rect_gap, '
             'signed (negative = the pad boxes overlap) -- what '
             'rule_proximity measures for a declared row: each subject pad '
             'against the nearest partner pad on its own net')
NOTE = ('REPORT ONLY: no intent key is written. Each row is in the shape '
        '`proximity[]` reads (`ref`, `near`, `pads`) with `max_mm` left null: '
        'the limit is the author\'s, from the datasheet, and copying the '
        'measured gap into it would bless the current pose (#902). Legs are '
        'reported, never a loop area, which a self-crossing pad polygon can '
        'shrink while every leg grows (#1254).')


def _nkey(x) -> Tuple:
    """Natural sort key for a pad number or a reference."""
    return tuple((0, int(t), '') if t.isdigit() else (1, 0, t)
                 for t in re.split(r'(\d+)', str(x)) if t)


def _fn_tokens(pad) -> List[str]:
    """A pad's pin function split into tokens: `VREG_LX` -> VREG, LX."""
    return [t for t in re.split(r'[^A-Z0-9/+]+', pin_function_name(pad)) if t]


def _fn_is(pad, rx) -> bool:
    return any(rx.match(t) for t in _fn_tokens(pad))


def _pad_side(pad) -> str:
    """`F`, `B` or `both` -- the copper face(s) the pad lands on."""
    lay = [str(l) for l in (getattr(pad, 'layers', None) or ())]
    if any(l.startswith('*') for l in lay) or ('F.Cu' in lay
                                                and 'B.Cu' in lay):
        return 'both'
    return 'B' if 'B.Cu' in lay else 'F'


class _Board:
    """What every detector reads, computed once per board."""

    def __init__(self, pcb_data):
        from .part_class import classify_part
        from net_queries import is_ground_net_name
        self.pcb = pcb_data
        self.fps = pcb_data.footprints or {}
        self.copper = {r: _copper_pads(fp) for r, fp in self.fps.items()}
        self.net_parts: Dict[int, set] = {}
        for r, pads in self.copper.items():
            for p in pads:
                if p.net_id > 0:
                    self.net_parts.setdefault(p.net_id, set()).add(r)
        self.cls = {r: classify_part(fp, r).name
                    for r, fp in self.fps.items()}
        self.role = {r: infer_role(fp, r, self.cls[r])
                     for r, fp in self.fps.items()}
        self._is_ground = is_ground_net_name

    def net(self, nid) -> str:
        n = (self.pcb.nets or {}).get(nid)
        return (getattr(n, 'name', '') or '') if n is not None else ''

    def ground(self, nid) -> bool:
        return nid > 0 and self._is_ground(self.net(nid))

    def value(self, ref) -> str:
        return getattr(self.fps[ref], 'value', '') or ''

    def big(self, ref) -> bool:
        """A part a loop is formed AT: three or more copper pads and no
        part_class (not a connector, switch, hole or marker)."""
        return (len(self.copper.get(ref, ())) >= 3
                and self.cls.get(ref) is None)

    def connector(self, ref) -> bool:
        return (self.cls.get(ref) in ('edge_receptacle', 'connector_affinity')
                or self.role[ref]['role'] in ('connector', 'usb receptacle',
                                              'pin header'))

    def inductor(self, ref) -> bool:
        name = getattr(self.fps[ref], 'footprint_name', '') or ''
        return (len(self.copper[ref]) == 2
                and (self.role[ref]['role'] == 'inductor'
                     or 'inductor' in _norm(name)))

    def decoupled(self, nid) -> bool:
        """Whether a decoupling cap bridges this net and a ground net."""
        from .groups import is_decoupling_cap
        for r in self.net_parts.get(nid, ()):
            if is_decoupling_cap(self.fps[r], r) and any(
                    self.ground(p.net_id) for p in self.copper[r]):
                return True
        return False

    def has_functions(self, ref) -> bool:
        return any(pin_function_name(p) for p in self.copper[ref])

    def end(self, ref, pad) -> Dict[str, object]:
        return {'ref': ref, 'pad': pad.pad_number, 'net': self.net(pad.net_id),
                'pin_function': pin_function_name(pad) or None,
                'side': _pad_side(pad)}

    def leg(self, what, a_ref, a_pad, b_ref, b_pads) -> Dict[str, object]:
        """`a_pad` to the nearest of `b_pads` (the caller passes the partner
        pads on `a_pad`'s own net, as rule_proximity narrows them)."""
        from .legality import pad_rect, rect_gap
        best = None
        for q in sorted(b_pads, key=lambda q: _nkey(q.pad_number)):
            g = rect_gap(pad_rect(a_pad), pad_rect(q))
            if best is None or g < best[0]:
                best = (g, q)
        return {'leg': what, 'from': self.end(a_ref, a_pad),
                'to': self.end(b_ref, best[1]), 'gap_mm': round(best[0], 3)}


def _row(ref, near, ref_pads, near_pads, legs) -> Dict[str, object]:
    """A ready `proximity[]` row with its measured legs beside it."""
    return {'row': {'ref': ref, 'near': near,
                    'pads': {ref: sorted({str(p.pad_number)
                                          for p in ref_pads}, key=_nkey),
                             near: sorted({str(p.pad_number)
                                           for p in near_pads}, key=_nkey)},
                    'max_mm': None},
            'legs': legs,
            'worst_leg_mm': max(l['gap_mm'] for l in legs)}


def _input_cap(b: _Board, ic, vin):
    """`(cap, VIN leg, its ground pad)` for the decoupling cap that bridges
    a VIN net of `ic` and a ground net with the SHORTEST VIN leg -- the cap
    that closes the input loop -- or None."""
    from .groups import is_decoupling_cap
    vin_nets = {q.net_id for q in vin}
    best = None
    for c in sorted(b.fps, key=_nkey):
        if not is_decoupling_cap(b.fps[c], c):
            continue
        vp = [p for p in b.copper[c] if p.net_id in vin_nets]
        gp = [p for p in b.copper[c] if b.ground(p.net_id)]
        if not vp or not gp:
            continue
        leg = b.leg('Cin+ -> VIN', c, vp[0], ic,
                    [q for q in vin if q.net_id == vp[0].net_id])
        if best is None or leg['gap_mm'] < best[1]['gap_mm']:
            best = (c, leg, vp[0], gp[0])
    return best


def _converters(b: _Board, out: List[Dict]) -> None:
    """Switching converters by PIN FUNCTION: a part with a switch-node pin
    (SW / LX) on a net an inductor carries, its input-supply pin (VIN) and
    its return (PGND, else GND, else a pad on a ground net). Topology from
    where the inductor's other end goes. A part an inductor shares a net
    with -- the only part on that net a loop forms at -- whose pads carry no
    pin function at all is `unknown`."""
    cands: Dict[str, List] = {}
    for L in sorted(b.fps, key=_nkey):
        if not b.inductor(L):
            continue
        for lp in b.copper[L]:
            n = lp.net_id
            if n <= 0 or b.ground(n):
                continue
            bigs = sorted((r for r in b.net_parts.get(n, ())
                           if r != L and b.big(r)), key=_nkey)
            sw_ics = [r for r in bigs
                      if any(q.net_id == n and _fn_is(q, _SW_FN)
                             for q in b.copper[r])]
            for r in sw_ics:
                cands.setdefault(r, []).append((L, lp))
            # The pin-less arm: one part on the net, and the net is not
            # decoupled -- a switch node never carries a cap to ground,
            # while an LC filter's output does (ulx3s U8, an ADC whose
            # /analog/ADC3V3 comes through an inductor, is not a candidate).
            if (not sw_ics and len(bigs) == 1
                    and not b.has_functions(bigs[0])
                    and not b.decoupled(n)):
                cands.setdefault(bigs[0], []).append((L, lp))
    for ic in sorted(cands, key=_nkey):
        items = cands[ic]
        if not b.has_functions(ic):
            nets = sorted({b.net(lp.net_id) for _L, lp in items})
            out.append({
                'role': 'unknown', 'candidate': 'switching converter',
                'part': ic, 'value': b.value(ic),
                'evidence': [f"{L} (an inductor) shares {b.net(lp.net_id)} "
                             f"with {ic}" for L, lp in items],
                'why': (f"no pad of {ic} carries a pin function, so which pin "
                        f"is SW, VIN or GND is not read -- pin numbers are "
                        f"never guessed. Read them off the datasheet and "
                        f"declare the rows by hand ({', '.join(nets)})"),
                'proximity': []})
            continue
        pads = b.copper[ic]
        vin = [q for q in pads if _fn_is(q, _VIN_FN)]
        gnd = ([q for q in pads if _fn_is(q, _PGND_FN)]
               or [q for q in pads if _fn_is(q, _GND_FN)]
               or [q for q in pads if b.ground(q.net_id)])
        vin_nets = {q.net_id for q in vin}
        for L, lp in items:
            sw = [q for q in pads if q.net_id == lp.net_id
                  and _fn_is(q, _SW_FN)]
            far = next((p for p in b.copper[L] if p is not lp), None)
            if far is not None and far.net_id in vin_nets:
                topo, how = 'boost', f"{L} runs from VIN to SW"
            elif far is not None and b.ground(far.net_id):
                topo, how = 'inverting', f"{L} runs from SW to ground"
            else:
                topo, how = 'buck', (f"{L} runs from SW to "
                                     f"{b.net(far.net_id) if far else '?'}")
            ev = ([f"{ic} pad {q.pad_number} is {pin_function_name(q)} on "
                   f"{b.net(q.net_id)}" for q in sw]
                  + [f"{ic} pad {q.pad_number} is {pin_function_name(q)}"
                     for q in vin]
                  + [f"{ic} pad {q.pad_number} is "
                     f"{pin_function_name(q) or 'on ' + b.net(q.net_id)} "
                     f"(the return)" for q in gnd[:2]]
                  + [f"{L} is an inductor on the SW net ({how})"])
            rec = {'role': 'switching converter', 'topology': topo,
                   'part': ic, 'value': b.value(ic), 'inductor': L,
                   'input_cap': None,
                   'pins': {'sw': [q.pad_number for q in sw],
                            'vin': [q.pad_number for q in vin],
                            'gnd': [q.pad_number for q in gnd]},
                   'evidence': ev, 'notes': [], 'proximity': []}
            got = _input_cap(b, ic, vin) if vin else None
            if not vin:
                rec['notes'].append(f"no pin of {ic} is named VIN, so its "
                                    f"input loop is not measured")
            elif got is None:
                rec['notes'].append(f"no decoupling cap bridges {ic}'s VIN "
                                    f"net and ground")
            else:
                c, vleg, vpad, gpad = got
                rec['input_cap'] = c
                vin_same = [q for q in vin if q.net_id == vpad.net_id]
                gnd_same = [q for q in gnd if q.net_id == gpad.net_id]
                legs, subj = [vleg], [vpad]
                if gnd_same:
                    legs.append(b.leg('Cin- -> GND', c, gpad, ic, gnd_same))
                    subj.append(gpad)
                else:
                    rec['notes'].append(
                        f"{c}'s ground pad is on {b.net(gpad.net_id)} and no "
                        f"return pin of {ic} is, so its ground leg is not "
                        f"measured")
                rec['proximity'].append(
                    _row(c, ic, subj, vin_same + gnd_same, legs))
            if sw:
                rec['proximity'].append(_row(
                    L, ic, [lp], sw, [b.leg('L -> SW', L, lp, ic, sw)]))
            out.append(rec)


def _crystals(b: _Board, out: List[Dict]) -> None:
    """A crystal (`roles.infer_role` says so) and the IC its two load nets
    reach. A PASSIVE crystal has exactly two signal pads -- pads on a
    non-ground net that reaches another part -- on two distinct nets.
    Anything else in the crystal role (an active oscillator: a supply pin,
    a tie, an enable) is read only through a pad typed `output` or named
    OUT / CLK, and is `unknown` without one."""
    for x in sorted(b.fps, key=_nkey):
        if b.role[x]['role'] != 'crystal':
            continue
        sig = [p for p in b.copper[x] if p.net_id > 0
               and not b.ground(p.net_id)
               and b.net_parts.get(p.net_id, set()) - {x}]
        ev = [f"{x} is in the role table's crystal row: "
              + '; '.join(b.role[x]['evidence'])]
        if len(sig) == 2 and len({p.net_id for p in sig}) == 2:
            votes: Dict[str, int] = {}
            for p in sig:
                for r in b.net_parts.get(p.net_id, ()):
                    if r != x and b.big(r):
                        votes[r] = votes.get(r, 0) + 1
            if not votes:
                out.append({'role': 'unknown', 'candidate': 'crystal',
                            'part': x, 'value': b.value(x), 'evidence': ev,
                            'why': ('no part a loop forms at (three or more '
                                    'copper pads, no part class) is on '
                                    'either load net'),
                            'proximity': []})
                continue
            ic = sorted(votes, key=lambda r: (-votes[r], _nkey(r)))[0]
            legs, subj, part, unmeasured = [], [], [], []
            for p in sorted(sig, key=lambda p: _nkey(p.pad_number)):
                qs = [q for q in b.copper[ic] if q.net_id == p.net_id]
                if not qs:
                    via = sorted(b.net_parts.get(p.net_id, set()) - {x},
                                 key=_nkey)
                    unmeasured.append({
                        'pad': p.pad_number, 'net': b.net(p.net_id),
                        'why': (f"no pin of {ic} is on this net; it reaches "
                                f"{', '.join(via)}")})
                    continue
                legs.append(b.leg('crystal -> load pin', x, p, ic, qs))
                subj.append(p)
                part += qs
            ev += [f"{ic} pad {l['to']['pad']} is "
                   f"{l['to']['pin_function'] or 'on ' + l['to']['net']}"
                   for l in legs]
            out.append({'role': 'crystal', 'part': x, 'value': b.value(x),
                        'ic': ic, 'evidence': ev,
                        'unmeasured_legs': unmeasured,
                        'proximity': ([_row(x, ic, subj, part, legs)]
                                      if legs else [])})
            continue
        outs = [p for p in b.copper[x]
                if 'output' in str(getattr(p, 'pintype', '') or '').split('+')
                or _fn_is(p, _OSC_OUT_FN)]
        if not outs:
            out.append({
                'role': 'unknown', 'candidate': 'crystal or oscillator',
                'part': x, 'value': b.value(x),
                'evidence': ev + [
                    f"{len(sig)} signal pad(s) on "
                    f"{len({p.net_id for p in sig})} non-ground net(s) -- "
                    f"not the two load nets of a passive crystal"],
                'why': ('no pad is typed output or named OUT / CLK, so which '
                        'pad is the clock is not read -- pin numbers are '
                        'never guessed'),
                'proximity': []})
            continue
        for p in sorted(outs, key=lambda p: _nkey(p.pad_number)):
            loads = sorted((r for r in b.net_parts.get(p.net_id, ())
                            if r != x and b.big(r)), key=_nkey)
            if not loads:
                continue
            ic = loads[0]
            qs = [q for q in b.copper[ic] if q.net_id == p.net_id]
            leg = b.leg('oscillator output -> clock pin', x, p, ic, qs)
            out.append({'role': 'oscillator', 'part': x, 'value': b.value(x),
                        'ic': ic,
                        'evidence': ev + [
                            f"{x} pad {p.pad_number} is its output "
                            f"({pin_function_name(p) or p.pintype})"],
                        'proximity': [_row(x, ic, [p], qs, [leg])]})


def _esd_arrays(b: _Board, out: List[Dict]) -> None:
    """An ESD array by PIN FUNCTION: protected-line pins (I/O1, CH2), every
    other named pin a rail clamp, a return or a no-connect, a ground pin,
    and a protected line shared with a connector. One record per connector
    it protects, a leg from each of its pins on that connector's nets."""
    for e in sorted(b.fps, key=_nkey):
        if not b.big(e):
            continue
        pads = b.copper[e]
        io = [p for p in pads if _fn_is(p, _IO_FN)]
        if not io:
            continue
        names = {pin_function_name(p) for p in pads} - {''}
        if any(not (_IO_FN.match(n) or _ESD_OTHER_FN.match(n))
               for n in names):
            continue
        if not any(_fn_is(p, _GND_FN) or b.ground(p.net_id) for p in pads):
            continue
        by_conn: Dict[str, List] = {}
        for p in io:
            for r in b.net_parts.get(p.net_id, ()):
                if r != e and b.connector(r):
                    by_conn.setdefault(r, []).append(p)
        conn_nets = {p.net_id for ps in by_conn.values() for p in ps}
        ic_side = sorted({r for p in io if p.net_id not in conn_nets
                          for r in b.net_parts.get(p.net_id, ())
                          if r != e and b.big(r)}, key=_nkey)
        for j in sorted(by_conn, key=_nkey):
            legs, subj, part = [], [], []
            for p in sorted(by_conn[j], key=lambda p: _nkey(p.pad_number)):
                qs = [q for q in b.copper[j] if q.net_id == p.net_id]
                legs.append(b.leg('ESD line -> connector', e, p, j, qs))
                subj.append(p)
                part += qs
            out.append({
                'role': 'esd array', 'part': e, 'value': b.value(e),
                'connector': j, 'ic': ic_side,
                'evidence': [f"{e} pins are " + ', '.join(sorted(names)),
                             f"{j} is a connector carrying "
                             + ', '.join(sorted({b.net(p.net_id)
                                                 for p in by_conn[j]}))],
                'proximity': [_row(e, j, subj, part, legs)]})


def emc_roles(pcb_data) -> Dict[str, object]:
    """`{roles, counts, basis, note}` -- the converters, crystals,
    oscillators and ESD arrays a board's pins and roles name, each relation
    with its legs measured and a ready `proximity[]` row (#1254). `unknown`
    records a part that looks like one (an inductor shares its net, the role
    table calls it a crystal) whose pins cannot say which pin is which."""
    b = _Board(pcb_data)
    roles: List[Dict] = []
    _converters(b, roles)
    _crystals(b, roles)
    _esd_arrays(b, roles)
    counts: Dict[str, int] = {}
    for r in roles:
        counts[r['role']] = counts.get(r['role'], 0) + 1
    return {'roles': roles, 'counts': counts, 'basis': LEG_BASIS,
            'note': NOTE}
