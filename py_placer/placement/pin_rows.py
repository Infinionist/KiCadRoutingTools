"""Can a track cross a through-hole pin row?  (#1196)

On a two-layer board of DIPs, PGAs and headers the routing capacity is not
the escape of each part's own pins -- a through-hole pin is on every copper
layer -- but the gaps between ADJACENT pins of a row, which every other net
has to cross. The lane ledgers (`escape`, `routability.face_lane_ledger`)
model a part's own escape and nothing else, so a board whose rows no track
can cross read `Faces in DEFICIT: 0` while its route made hundreds of
reroutes.

The answer is measured on the ROUTING GRID, in the router's own obstacle map
(`obstacle_map.build_base_obstacle_map`), because the analytic test is wrong
by the grid's quantisation. interf_u's DIP rows (1.6 mm pads at 2.54 mm, a
0.94 mm gap): a 0.4 mm track at clearance 0.254 needs 0.4 + 2 x 0.254 =
0.908 mm, which fits the gap only within +-0.016 mm of its midpoint, and the
0.1 mm grid stamp never lands there -- 0 of 115 gaps cross, where the
arithmetic says all 115 do.

A gap is OPEN when one grid column between the two pins (one row of cells
for a row running along y) is free on some copper layer across the band the
two pads' own keep-out can reach. That is a straight crossing; a convex pad
blocks least on the row line, so a path that crosses the row anywhere must
cross the line in a free cell, and the column through that cell is free
across the band. Only pairs at the row's own pitch are counted, as the
issue's probe did: a missing pin leaves a gap no width question applies to.

The map is built with every pad as an obstacle and the board's existing
copper stamped too -- on a routed board a gap a track already uses reads
closed, which is what is left for the next net.
"""
from __future__ import annotations

import contextlib
import io
import math
from typing import Dict, List, Optional, Sequence

#: A through-hole part needs this many plated pins to have rows worth
#: measuring (the issue's probe used the same floor).
TPART_MIN_PINS = 6
#: ...and a row this many pins; a two-pin "row" across a header is not one.
ROW_MIN_PINS = 3
#: Two pins are adjacent at the row's pitch when their spacing is within this
#: of the row's smallest spacing.
PITCH_TOL_MM = 0.02
#: The narrower track widths a ladder tries, widest first. Round numbers so
#: the answer reads as a net-class choice ("0.3 mm opens 48/115").
LADDER_MM = (0.35, 0.3, 0.25, 0.2, 0.15, 0.127, 0.1)


def _plated(pad) -> bool:
    from kicad_parser import pad_is_plated_through
    return pad_is_plated_through(pad)


def is_through_hole_part(fp) -> bool:
    """Plated through-hole pins outnumber SMD pads -- the part's own pins,
    not a thermal-via field under an exposed pad (#1196). The same majority
    test `escape.fine_pitch_parts` excludes through-hole parts by."""
    from kicad_parser import non_aperture_pads
    pads = non_aperture_pads(fp)
    tht = sum(1 for p in pads if _plated(p))
    smd = sum(1 for p in pads if (getattr(p, 'drill', 0) or 0) <= 0)
    return tht > smd


def through_pins(fp) -> List[Dict]:
    """`fp`'s plated through-hole PINS: each pad number's overlapping plated
    copper merged into one box `{number, x, y, w, h, clearance}`.

    A pin is not always one pad. A Teensy footprint draws every pin as a
    1.6 mm circle plus a 2.07 mm rect 1.035 mm apart, and a mounting hole
    with stitching vias numbers all nine pads `1` on one 7 mm pad; read pad
    by pad, both are "rows" whose gaps lie inside one pin's own copper.
    """
    pads = [p for p in (fp.pads or ()) if _plated(p)]
    boxes = [(p.global_x - p.size_x / 2.0, p.global_y - p.size_y / 2.0,
              p.global_x + p.size_x / 2.0, p.global_y + p.size_y / 2.0)
             for p in pads]
    parent = list(range(len(pads)))

    def _find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(pads)):
        for j in range(i + 1, len(pads)):
            if pads[i].pad_number != pads[j].pad_number:
                continue
            a, b = boxes[i], boxes[j]
            if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]:
                parent[_find(i)] = _find(j)
    groups: Dict[int, List[int]] = {}
    for i in range(len(pads)):
        groups.setdefault(_find(i), []).append(i)
    pins = []
    for members in groups.values():
        x0 = min(boxes[i][0] for i in members)
        y0 = min(boxes[i][1] for i in members)
        x1 = max(boxes[i][2] for i in members)
        y1 = max(boxes[i][3] for i in members)
        pins.append({'number': pads[members[0]].pad_number,
                     'x': (x0 + x1) / 2.0, 'y': (y0 + y1) / 2.0,
                     'w': x1 - x0, 'h': y1 - y0,
                     'clearance': max((getattr(pads[i], 'local_clearance', 0.0)
                                       or 0.0) for i in members)})
    return pins


def pin_gaps(fp, ref: str) -> List[Dict]:
    """The adjacent-pin gaps of `fp`'s straight through-hole rows.

    Rows are read in board coordinates, so a part at a non-90-degree angle
    has none and is reported with no gaps rather than guessed at.
    """
    pins = through_pins(fp)
    if len(pins) < TPART_MIN_PINS:
        return []
    out: List[Dict] = []
    for axis in ('x', 'y'):
        rows: Dict[float, list] = {}
        for p in pins:
            line = p['y'] if axis == 'x' else p['x']
            rows.setdefault(round(line, 2), []).append(p)
        for _key, row in sorted(rows.items()):
            if len(row) < ROW_MIN_PINS:
                continue
            # the row's own coordinate, not the 0.01 mm bucket it was found by
            line = sum(p['y'] if axis == 'x' else p['x'] for p in row) / len(row)
            key = 'x' if axis == 'x' else 'y'
            row.sort(key=lambda p: p[key])
            steps = [b[key] - a[key] for a, b in zip(row, row[1:])]
            pitch = min(steps)
            if pitch <= 1e-6:
                continue
            for (a, b), step in zip(zip(row, row[1:]), steps):
                if abs(step - pitch) > PITCH_TOL_MM:
                    continue
                across = max((q['h'] if axis == 'x' else q['w']) / 2.0
                             for q in (a, b))
                out.append({'ref': ref, 'axis': axis, 'line': line,
                            'lo': a[key], 'hi': b[key],
                            'pitch_mm': round(pitch, 4),
                            'across_mm': across,
                            'pad_clearance': max(a['clearance'],
                                                 b['clearance']),
                            'pads': (a['number'], b['number'])})
    return out


def _gap_open(om, gap, n_layers, step, half_track, clearance) -> bool:
    """Is one grid line between the two pins free across their keep-out
    band, on some layer?

    The band is exactly the reach of the two pins' own keep-out (half the
    pin across the row, half the track, the clearance or a larger pad
    override). Past it the pins block nothing, so a track that has crossed
    may turn; a band one grid step wider charged ulx3s J1's outer row for
    the board-edge keep-out beyond it, at every width.
    """
    band = (gap['across_mm'] + half_track
            + max(clearance, gap.get('pad_clearance') or 0.0))
    t_lo = math.ceil((gap['line'] - band) / step - 1e-9)
    t_hi = math.floor((gap['line'] + band) / step + 1e-9)
    g_lo = math.floor(gap['lo'] / step)
    g_hi = math.ceil(gap['hi'] / step)
    horizontal_row = gap['axis'] == 'x'
    for layer in range(n_layers):
        for g in range(g_lo, g_hi + 1):
            for t in range(t_lo, t_hi + 1):
                gx, gy = (g, t) if horizontal_row else (t, g)
                if om.is_blocked(gx, gy, layer):
                    break
            else:
                return True
    return False


def _open_count(pcb_data, gaps, *, track_width, clearance, grid_step,
                layers) -> List[bool]:
    from obstacle_map import build_base_obstacle_map
    from routing_config import GridRouteConfig
    cfg = GridRouteConfig(track_width=track_width, clearance=clearance,
                          grid_step=grid_step, layers=list(layers))
    with contextlib.redirect_stdout(io.StringIO()):
        om = build_base_obstacle_map(pcb_data, cfg, [])
    return [_gap_open(om, g, len(layers), grid_step, track_width / 2.0,
                      clearance) for g in gaps]


def tht_row_crossings(pcb_data, *, track_width: float, clearance: float,
                      grid_step: float, layers: Optional[Sequence[str]] = None,
                      min_track_width: float = 0.0,
                      refs: Optional[Sequence[str]] = None) -> Dict:
    """How many through-hole pin gaps a `track_width` track crosses, and the
    width that opens the rest.

    Returns `{'gaps', 'open', 'ladder': [{track_width, open}], 'opens_all_at',
    'parts': [{ref, rows, gaps, open, pitch_mm, ladder}], basis...}`. The
    ladder starts at `track_width` and walks `LADDER_MM` down to
    `min_track_width` (the fab floor), stopping once every gap is open; one
    obstacle map per width. `gaps == 0` means the board has no through-hole
    pin row, not that every row is open.
    """
    if layers is None:
        layers = list(getattr(pcb_data.board_info, 'copper_layers', None)
                      or ['F.Cu', 'B.Cu'])
    targets = (list(refs) if refs is not None else
               [r for r, fp in sorted((pcb_data.footprints or {}).items())
                if is_through_hole_part(fp)])
    per_ref: Dict[str, List[Dict]] = {}
    for ref in targets:
        fp = pcb_data.footprints.get(ref)
        if fp is None:
            continue
        g = pin_gaps(fp, ref)
        if g:
            per_ref[ref] = g
    flat = [g for ref in sorted(per_ref) for g in per_ref[ref]]
    out: Dict = {'basis': 'obstacle_map.build_base_obstacle_map, every pad '
                          'an obstacle, existing copper included',
                 'track_width': track_width, 'clearance': clearance,
                 'grid_step': grid_step, 'layers': list(layers),
                 'min_track_width': min_track_width,
                 'gaps': len(flat), 'open': 0, 'ladder': [],
                 'opens_all_at': None, 'parts': []}
    if not flat:
        return out
    widths = [track_width] + [w for w in LADDER_MM
                              if w < track_width - 1e-9
                              and w >= min_track_width - 1e-9]
    by_width = []
    for w in widths:
        flags = _open_count(pcb_data, flat, track_width=w,
                            clearance=clearance, grid_step=grid_step,
                            layers=layers)
        by_width.append((w, flags))
        if all(flags):
            break
    out['open'] = sum(by_width[0][1])
    out['ladder'] = [{'track_width': w, 'open': sum(f)} for w, f in by_width]
    if all(by_width[-1][1]):
        out['opens_all_at'] = by_width[-1][0]
    parts = []
    i = 0
    for ref in sorted(per_ref):
        n = len(per_ref[ref])
        rows = {(g['axis'], g['line']) for g in per_ref[ref]}
        lad = [{'track_width': w, 'open': sum(f[i:i + n])}
               for w, f in by_width]
        parts.append({'ref': ref, 'rows': len(rows), 'gaps': n,
                      'open': lad[0]['open'],
                      'pitch_mm': min(g['pitch_mm'] for g in per_ref[ref]),
                      'ladder': lad})
        i += n
    parts.sort(key=lambda p: (p['open'] / p['gaps'], p['ref']))
    out['parts'] = parts
    return out


def ladder_phrase(rep: Dict, *, what: str = 'through-hole pin gaps') -> str:
    """`0.4 mm crosses 0/115 through-hole pin gaps; 0.3 mm opens 48/115, ...`
    -- the sentence #1196 asks for, from a `tht_row_crossings` report (or one
    of its `parts` rows, given the report's own track width)."""
    lad = rep.get('ladder') or []
    n = rep.get('gaps') or 0
    if not lad or not n:
        return f"no {what}"
    head = (f"{lad[0]['track_width']:g} mm crosses {lad[0]['open']}/{n} "
            f"{what}")
    rest = []
    for r in lad[1:]:
        if rest and rest[-1][1] == n:
            break                       # this row was already all open
        rest.append((r['track_width'], r['open']))
    if lad[0]['open'] == n:
        rest = []
    return '; '.join([head] + [f"{w:g} mm opens {k}/{n}" for w, k in rest])
