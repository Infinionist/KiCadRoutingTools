"""Net-filtered copper keep-outs: a region some nets may cross and others may not.

A KiCad rule area has no net dimension, so a design that says "only GND and the
four RF nets may run tracks or vias through this corridor" exports it as an
allow-everything rule area plus a custom DRC rule

    (rule "..." (constraint disallow track)
                (condition "A.insideArea('corridor') && !(A.NetName == 'GND' || ...)"))

`zone (keepout ...)` parsing (`extract_keepouts`) sees only the permissive rule
area, and `design_rules` reports an `insideArea()` condition as UNSUPPORTED, so
the router laid the filtered nets straight through the region and KiCad DRC
reported `items_not_allowed` afterwards.

`--net-keepouts FILE` carries the same regions with their net filter as JSON:

    [{"name": "RF corridor", "polygon": [[x, y], ...], "layers": ["F.Cu"],
      "tracks": true, "vias": true,
      "appliesToNets": ["*"], "exemptNets": ["GND", "RF_*"]}]

`tracks` / `vias` say which items the region forbids (true = not allowed).
`appliesToNets` (fnmatch globs, absent/empty = every net) selects the nets the
region binds; `exemptNets` removes nets from that set. A net the region binds is
FILTERED.

The region is stamped into the obstacle map only while a filtered net routes:
`stamp()` wraps the single-ended and diff-pair route entries, adds the cells
(the same rasterisation and clearance band `add_rule_area_keepout_obstacles`
uses for a native keep-out) and removes exactly those cells on exit, so refcounts
balance and an exempt net routes through the region as if it were not there.
Existing copper is never touched: copper already inside the region is an ordinary
obstacle, and nothing rips it.

Coverage is the route entries that stamp: route_net_with_obstacles,
route_multipoint_main / _taps (and with them net_rescue, stub-swap and terminal
escalation, which call them -- a rescue window is a shallow copy that carries
the regions), and on the diff-pair side the pair route (its centreline map at
`centerline_config`, its connectors' base map plain), the hybrid coupled middle
and its legs (`stamping`). Copper laid by anything else -- the plane finalize's
taps and region joins, which have their own A*, and post-route passes such as
meanders -- does not see a region, so the run ends with `audit()` of the board
it ships: bound-net copper found inside a region is printed and reported as
JSON_SUMMARY / --json-out `net_keepouts`, never shipped silently. KiCad DRC of
the equivalent rule stays the authority.
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
import math
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

#: Keys a keep-out entry may carry. Unknown keys are refused so a typo in the
#: handoff file cannot read as an honoured constraint.
_KEYS = {'name', 'polygon', 'layers', 'tracks', 'vias', 'appliesToNets', 'exemptNets'}

#: Layer tokens add_rule_area_keepout_obstacles expands (#369 A5).
_COMPOSITE_LAYERS = {'*.Cu', 'F&B.Cu', 'F&B'}


def load_net_keepouts(path: str) -> List[dict]:
    """Read and validate a `--net-keepouts` JSON file. Raises ValueError naming
    the file, the offending entry and what a valid one looks like -- an
    unreadable or non-JSON file included, so a caller catches one type."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            doc = json.load(f)
    except OSError as e:
        raise ValueError(f"{path}: cannot read ({e.strerror or e})") from e
    except ValueError as e:                      # json.JSONDecodeError
        raise ValueError(f"{path}: not valid JSON ({e})") from e
    if not isinstance(doc, list):
        raise ValueError(f"{path}: expected a JSON array of keep-outs, "
                         f"got {type(doc).__name__}")
    return [_validate(entry, i, path) for i, entry in enumerate(doc)]


def _is_mm(v) -> bool:
    """A finite JSON number (a bool is not one)."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _validate(entry, i: int, path: str) -> dict:
    """One keep-out entry, checked and normalised to the extract_keepouts dict
    shape plus its net filter."""
    where = f"{path} entry {i}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {type(entry).__name__}")
    unknown = sorted(set(entry) - _KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                         f"allowed: {', '.join(sorted(_KEYS))}")
    poly = entry.get('polygon')
    if (not isinstance(poly, list) or len(poly) < 3
            or not all(isinstance(p, list) and len(p) == 2 and all(map(_is_mm, p))
                       for p in poly)):
        raise ValueError(f"{where}: polygon must be >= 3 [x, y] vertices in mm")
    layers = entry.get('layers')
    if not isinstance(layers, list) or not layers or not all(isinstance(l, str) for l in layers):
        raise ValueError(f"{where}: layers must be a non-empty list of copper layer names")
    applies = entry.get('appliesToNets') or []
    exempt = entry.get('exemptNets') or []
    for key, val in (('appliesToNets', applies), ('exemptNets', exempt)):
        if not isinstance(val, list) or not all(isinstance(v, str) and v for v in val):
            raise ValueError(f"{where}: {key} must be a list of net-name globs")
    # true/false only: bool("false") is True, so a quoted value would read as
    # the opposite of what it says.
    for key in ('tracks', 'vias'):
        if key in entry and not isinstance(entry[key], bool):
            raise ValueError(f"{where}: {key} must be true or false, "
                             f"got {entry[key]!r}")
    tracks = entry.get('tracks', True)
    vias = entry.get('vias', True)
    return {
        'name': str(entry.get('name') or f'net_keepout_{i}'),
        'polygon': [(float(x), float(y)) for x, y in poly],
        'holes': [],
        'layers': set(layers),
        'tracks_allowed': not tracks,
        'vias_allowed': not vias,
        'applies': list(applies),
        'exempt': list(exempt),
    }


def binds(ko: dict, net_name: str) -> bool:
    """True when keep-out `ko` forbids its items to net `net_name`."""
    applies = ko.get('applies') or []
    if applies and not any(fnmatch.fnmatchcase(net_name, g) for g in applies):
        return False
    return not any(fnmatch.fnmatchcase(net_name, g) for g in ko.get('exempt') or [])


def attach(pcb_data, keepouts: Optional[List[dict]]) -> None:
    """Record the run's net-filtered keep-outs on `pcb_data` and say which nets
    each binds. No keep-outs CLEARS any a previous run attached: the GUI hands
    every run the same long-lived PCBData, so a field emptied between two runs
    must not leave the first run's regions stamping the second."""
    if not keepouts:
        pcb_data.__dict__.pop('_net_keepouts', None)
        pcb_data.__dict__.pop('_net_keepout_cells', None)
        return
    pcb_data._net_keepouts = list(keepouts)
    pcb_data._net_keepout_cells = {}
    names = {nid: n.name for nid, n in pcb_data.nets.items() if nid and n.name}
    copper = set(getattr(pcb_data.board_info, 'copper_layers', None) or ())
    print(f"Net-filtered keep-outs: {len(keepouts)} region(s)")
    for ko in keepouts:
        exempt = sorted(n for n in names.values() if not binds(ko, n))
        shown = ', '.join(exempt[:8]) + (', ...' if len(exempt) > 8 else '')
        print(f"  {ko['name']}: {len(names) - len(exempt)} net(s) kept out, "
              f"{len(exempt)} exempt ({shown or 'none'})")
        # A misspelt layer resolves to nothing in the keep-out rasteriser,
        # which then blocks no track cell at all: say so rather than run as
        # if the region were honoured on it.
        unknown = sorted(l for l in ko['layers']
                         if l not in copper and l not in _COMPOSITE_LAYERS)
        if copper and unknown:
            print(f"  WARNING: {ko['name']}: layer(s) {', '.join(unknown)} are not "
                  f"copper layers of this board; the region blocks no track there")


class _Recorder:
    """Captures what add_rule_area_keepout_obstacles would stamp, so the keep-out
    rasterisation stays single-sourced."""

    def __init__(self):
        self.cells: List[np.ndarray] = []
        self.vias: List[np.ndarray] = []

    def add_blocked_cells_batch(self, arr):
        """Record (N, 3) track cells."""
        self.cells.append(np.asarray(arr, dtype=np.int32))

    def add_blocked_vias_batch(self, arr):
        """Record (N, 2) via cells."""
        self.vias.append(np.asarray(arr, dtype=np.int32))


def _binding(pcb_data, net_ids: Iterable[int]) -> Tuple[int, ...]:
    """Indices of the keep-outs that bind any of `net_ids`."""
    kos = getattr(pcb_data, '_net_keepouts', None) or []
    names = [getattr(pcb_data.nets.get(nid), 'name', '') for nid in set(net_ids) if nid]
    return tuple(i for i, ko in enumerate(kos)
                 if any(n and binds(ko, n) for n in names))


def _stamper(pcb_data, idx: Tuple[int, ...]):
    """`stamp(map, config)` that rasterises keep-outs `idx` into `map` with
    the native keep-out pass, so the rasterisation stays single-sourced."""
    from obstacle_map import add_rule_area_keepout_obstacles
    kos = pcb_data._net_keepouts
    shim = SimpleNamespace(board_info=SimpleNamespace(
        keepouts=[kos[i] for i in idx],
        board_bounds=getattr(pcb_data.board_info, 'board_bounds', None)))
    return lambda sink, cfg: add_rule_area_keepout_obstacles(sink, shim, cfg)


def _cells_for(pcb_data, idx: Tuple[int, ...], config) -> Tuple[np.ndarray, np.ndarray]:
    """(track cells (N, 3), via cells (M, 2)) keep-outs `idx` stamp at the
    run's geometry. Cached per (binding set, geometry, clip) on pcb_data.

    The clip (board_bounds) is part of the key because the rasteriser clips
    to it and a rescue window (plane_pad_tap.make_local_window) is a SHALLOW
    copy that shares this cache dict with the whole board: keyed on geometry
    alone, a window's clipped cells would be handed to a whole-board route
    at the same geometry, which would then see only part of the region."""
    key = (idx, config.grid_step, config.track_width, config.clearance,
           config.via_size, config.via_drill, tuple(config.layers),
           getattr(pcb_data.board_info, 'board_bounds', None))
    cache: Dict = pcb_data.__dict__.setdefault('_net_keepout_cells', {})
    if key not in cache:
        rec = _Recorder()
        _stamper(pcb_data, idx)(rec, config)
        cells = (np.unique(np.concatenate(rec.cells), axis=0) if rec.cells
                 else np.empty((0, 3), dtype=np.int32))
        vias = (np.unique(np.concatenate(rec.vias), axis=0) if rec.vias
                else np.empty((0, 2), dtype=np.int32))
        cache[key] = (cells, vias)
    return cache[key]


@contextlib.contextmanager
def stamp(obstacles, pcb_data, net_ids: Iterable[int], config):
    """Block the keep-outs binding any of `net_ids` in `obstacles` for the
    duration of the block, then remove exactly what was added.

    Via cells reach every via map the list twins (add/remove_vias_list) do:
    rung 0 at the run's via, the #568 small rung at the run's via (a superset
    for the smaller fab via), and each #530 per-net rung at its OWN via
    (#1226 `_stamp_rungs_at_own_via`, whose remove twin re-derives the same
    rows), so a search at any rung sees the region."""
    if obstacles is None or not getattr(pcb_data, '_net_keepouts', None):
        yield
        return
    idx = _binding(pcb_data, net_ids)
    if not idx:
        yield
        return
    cells, vias = _cells_for(pcb_data, idx, config)
    from obstacle_map import _rung_small_armed, _stamp_rungs_at_own_via
    small = _rung_small_armed() and hasattr(obstacles, 'add_blocked_vias_small_batch')
    rungs = len(vias) and hasattr(obstacles, 'rung_count')
    if len(cells):
        obstacles.add_blocked_cells_batch(cells)
    if len(vias):
        obstacles.add_blocked_vias_batch(vias)
        if small:
            obstacles.add_blocked_vias_small_batch(vias)
    if rungs:
        _stamp_rungs_at_own_via(obstacles, config, _stamper(pcb_data, idx))
    try:
        yield
    finally:
        if len(cells):
            obstacles.remove_blocked_cells_batch(cells)
        if len(vias):
            obstacles.remove_blocked_vias_batch(vias)
            if small:
                obstacles.remove_blocked_vias_small_batch(vias)
        if rungs:
            _stamp_rungs_at_own_via(obstacles, config, _stamper(pcb_data, idx),
                                    remove=True)


def centerline_config(config):
    """The geometry a diff-pair CENTRELINE map is stamped at. The members run
    (track_width + diff_pair_gap) / 2 either side of the centreline, so the
    band a region keeps the centreline out of grows by that offset -- the
    extra clearance route_diff builds every other obstacle of that map with.
    Stamped at the plain run geometry, a member could come up to
    (track_width + diff_pair_gap) / 2 - clearance INTO the region
    (lvds_converter_dualclk /CLK+: its copper over the edge by 0.5 um)."""
    from dataclasses import replace
    extra = (config.track_width + config.diff_pair_gap) / 2
    return replace(config, clearance=config.clearance + extra)


def stamping(net_ids_of, maps_of, centerline: bool = False):
    """Decorator for a route function that reaches the A* on its own maps:
    for the duration of each call, stamp the keep-outs binding
    ``net_ids_of(args)`` into every distinct map ``maps_of(args)`` names
    (``args`` = the call's bound arguments by parameter name; the function
    takes ``pcb_data`` and ``config``), at the centreline geometry
    (`centerline_config`) when the function lays a diff pair's centreline.
    Inert, beyond one getattr, on a board with no net-filtered keep-out.

    The body is called through ``__wrapped__`` at call time, so a test can
    swap in a probe and see what the A* would see."""
    def deco(fn):
        import functools
        import inspect
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def run(*args, **kwargs):
            pcb_data = args[0] if args else kwargs.get('pcb_data')
            if not getattr(pcb_data, '_net_keepouts', None):
                return run.__wrapped__(*args, **kwargs)
            a = sig.bind(*args, **kwargs).arguments
            nets, seen = tuple(net_ids_of(a)), []
            cfg = centerline_config(a['config']) if centerline else a['config']
            with contextlib.ExitStack() as stack:
                for m in maps_of(a):
                    if m is not None and not any(m is s for s in seen):
                        seen.append(m)
                        stack.enter_context(stamp(m, pcb_data, nets, cfg))
                return run.__wrapped__(*args, **kwargs)
        return run
    return deco


def _on_layer(ko_layers, layer: str) -> bool:
    """Does a region on `ko_layers` exist on copper layer `layer`? The
    composite tokens are the ones the keep-out rasteriser expands."""
    return (layer in ko_layers or '*.Cu' in ko_layers
            or (layer in ('F.Cu', 'B.Cu') and bool({'F&B.Cu', 'F&B'} & set(ko_layers))))


def _via_layers(via, copper: List[str]) -> List[str]:
    """The copper layers a via's barrel spans, in board order (a through via:
    all of them)."""
    ends = [l for l in (getattr(via, 'layers', None) or ()) if l in copper]
    if len(ends) < 2:
        return list(copper)
    i, j = sorted((copper.index(ends[0]), copper.index(ends[-1])))
    return copper[i:j + 1]


def audit(pcb_data, segments, vias,
          keepouts: Optional[List[dict]] = None) -> Optional[List[dict]]:
    """The copper of `segments` / `vias` that a net-filtered keep-out forbids
    and that INTERSECTS its region -- what KiCad's DRC reports as
    items_not_allowed for the equivalent insideArea() rule. One row per
    (region, net): {'region', 'net', 'tracks', 'vias'}, sorted; [] when the
    copper is clean, None when the run has no net-filtered keep-out.

    It reads only geometry, so it sees copper from every path, including the
    ones stamp() does not reach (the plane finalize's taps and joins, post-route
    meanders) and copper the input already had inside a region.

    `pcb_data` supplies the net names and copper layers of the copper given,
    and the keep-outs unless `keepouts` names them (a re-parsed output board
    carries none of its own)."""
    from geometry_utils import segment_to_segment_distance
    from obstacle_map import point_in_polygon, point_to_polygon_edge_distance
    kos = keepouts if keepouts is not None else getattr(pcb_data, '_net_keepouts', None)
    if not kos:
        return None
    names = {nid: n.name for nid, n in pcb_data.nets.items() if nid and n.name}
    copper = list(getattr(pcb_data.board_info, 'copper_layers', None) or [])
    eps = 1e-6
    rows: Dict[Tuple[str, str], List[int]] = {}
    for ko in kos:
        ring = ko['polygon']
        edges = list(zip(ring, ring[1:] + ring[:1]))
        x0 = min(p[0] for p in ring)
        y0 = min(p[1] for p in ring)
        x1 = max(p[0] for p in ring)
        y1 = max(p[1] for p in ring)

        def hit(xa, ya, xb, yb, r):
            if (max(xa, xb) < x0 - r or min(xa, xb) > x1 + r
                    or max(ya, yb) < y0 - r or min(ya, yb) > y1 + r):
                return False
            if point_in_polygon(xa, ya, ring) or point_in_polygon(xb, yb, ring):
                return True
            return any(segment_to_segment_distance(xa, ya, xb, yb, a[0], a[1], b[0], b[1])
                       < r - eps for a, b in edges)

        def row(nid):
            name = names.get(nid)
            if not name or not binds(ko, name):
                return None
            return rows.setdefault((ko['name'], name), [0, 0])

        if not ko.get('tracks_allowed', True):
            for s in segments:
                if getattr(s, 'graphic', False) or not _on_layer(ko['layers'], s.layer):
                    continue
                r = row(s.net_id)
                if r is not None and hit(s.start_x, s.start_y, s.end_x, s.end_y,
                                         s.width / 2):
                    r[0] += 1
        if not ko.get('vias_allowed', True):
            for v in vias:
                if not any(_on_layer(ko['layers'], l) for l in _via_layers(v, copper)):
                    continue
                r = row(v.net_id)
                if r is not None and (
                        point_in_polygon(v.x, v.y, ring)
                        or point_to_polygon_edge_distance(v.x, v.y, ring) < v.size / 2 - eps):
                    r[1] += 1
    return [{'region': k[0], 'net': k[1], 'tracks': t, 'vias': n}
            for k, (t, n) in sorted(rows.items()) if t or n]


def format_audit(found: List[dict], where: str) -> str:
    """One line for the console: what `audit` found on `where`."""
    if not found:
        return f"Net-filtered keep-outs: no bound-net copper inside a region on {where}"
    items = sum(r['tracks'] + r['vias'] for r in found)
    parts = '; '.join(f"{r['region']}: {r['net']} "
                      f"({r['tracks']} track(s), {r['vias']} via(s))" for r in found)
    return (f"WARNING: net-filtered keep-outs: {items} bound-net item(s) inside a "
            f"region on {where} (KiCad reports them as items_not_allowed): {parts}")
