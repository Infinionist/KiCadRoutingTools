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

Coverage is the route entries named in `stamp()`'s callers. A path that reaches
the A* without them would not see the region; KiCad DRC of the exported rules
stays the authority, as it is for every other constraint.
"""
from __future__ import annotations

import contextlib
import fnmatch
import json
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

#: Keys a keep-out entry may carry. Unknown keys are refused so a typo in the
#: handoff file cannot read as an honoured constraint.
_KEYS = {'name', 'polygon', 'layers', 'tracks', 'vias', 'appliesToNets', 'exemptNets'}


def load_net_keepouts(path: str) -> List[dict]:
    """Read and validate a `--net-keepouts` JSON file. Raises ValueError naming
    the offending entry and what a valid one looks like."""
    with open(path, 'r', encoding='utf-8') as f:
        doc = json.load(f)
    if not isinstance(doc, list):
        raise ValueError(f"--net-keepouts {path}: expected a JSON array of keep-outs, "
                         f"got {type(doc).__name__}")
    return [_validate(entry, i, path) for i, entry in enumerate(doc)]


def _validate(entry, i: int, path: str) -> dict:
    """One keep-out entry, checked and normalised to the extract_keepouts dict
    shape plus its net filter."""
    where = f"--net-keepouts {path} entry {i}"
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: expected an object, got {type(entry).__name__}")
    unknown = sorted(set(entry) - _KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                         f"allowed: {', '.join(sorted(_KEYS))}")
    poly = entry.get('polygon')
    if (not isinstance(poly, list) or len(poly) < 3
            or not all(isinstance(p, list) and len(p) == 2 for p in poly)):
        raise ValueError(f"{where}: polygon must be >= 3 [x, y] vertices in mm")
    layers = entry.get('layers')
    if not isinstance(layers, list) or not layers or not all(isinstance(l, str) for l in layers):
        raise ValueError(f"{where}: layers must be a non-empty list of copper layer names")
    applies = entry.get('appliesToNets') or []
    exempt = entry.get('exemptNets') or []
    for key, val in (('appliesToNets', applies), ('exemptNets', exempt)):
        if not isinstance(val, list) or not all(isinstance(v, str) and v for v in val):
            raise ValueError(f"{where}: {key} must be a list of net-name globs")
    tracks = bool(entry.get('tracks', True))
    vias = bool(entry.get('vias', True))
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
    each binds. No keep-outs leaves pcb_data untouched."""
    if not keepouts:
        return
    pcb_data._net_keepouts = list(keepouts)
    pcb_data._net_keepout_cells = {}
    names = {nid: n.name for nid, n in pcb_data.nets.items() if nid and n.name}
    print(f"Net-filtered keep-outs: {len(keepouts)} region(s)")
    for ko in keepouts:
        exempt = sorted(n for n in names.values() if not binds(ko, n))
        shown = ', '.join(exempt[:8]) + (', ...' if len(exempt) > 8 else '')
        print(f"  {ko['name']}: {len(names) - len(exempt)} net(s) kept out, "
              f"{len(exempt)} exempt ({shown or 'none'})")


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
    run's geometry. Cached per (binding set, geometry) on pcb_data."""
    key = (idx, config.grid_step, config.track_width, config.clearance,
           config.via_size, config.via_drill, tuple(config.layers))
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
