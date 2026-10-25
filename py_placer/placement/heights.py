"""How tall each part is, and what fits under it (#1443).

A part's 3D model says how high its body stands above its own face and, cell
by cell, how high its LOWEST point is -- the room under it. A shield can's lid
stands 2 mm over the parts inside it, a module on headers 7-10 mm over the
board, an M.2 card over the parts under it; a QFP's body sits on its leads.

Two rules read it, for two parts on one face whose heights are both KNOWN:

  * a part may stand inside a cover's occupancy (the box the seat and the
    grade give it -- its courtyard, else its fab outline or pads) where the
    cover's model is over it at least the part's height plus `MARGIN_MM`
    higher, and never under `ROOM_FLOOR_MM`, and nowhere inside a courtyard
    the cover DRAWS (KiCad grades that
    one, and the human boards never put a part in one);
  * a part may NOT stand under a cover's raised body outside that occupancy
    (a module's overhang, a card) where the two models meet, cell by cell
    (`OVERHANG_MARGIN_MM`); a part with no model has no shape to judge
    there and is judged on its occupancy alone.

A part whose height is unknown -- no model kicad-cli resolves, and no height
in its IPC-7351 name -- is judged as before, on its occupancy alone: nothing
is risked on a guess. So is every pair where neither part has room under it.
A part something reaches from above -- a connector its plug mates with, a
switch, a test point, a fiducial, a mounting hole (`part_class` gives it a
class, or its designator is one of those) -- never goes under a cover,
whatever its height, and admits nothing inside its own occupancy.

Only the surfaces of a model that face the board (or stand on it) say how
much room there is under it: a model drawn without a bottom face would
otherwise read as hollow under its top.

Measured on the human corpus (#1443): of 2819 parts that 157 boards stand
inside another part's outline, KiCad flags 87 -- the covers draw no
courtyard where parts go under them. The tightest gap measured between a
part's top and a cover's underside was 0.52 mm (a 2.0 mm shield lid);
`MARGIN_MM` is 0.5. Graded on the 56 human boards with a cover the installed
library models, the overhang rule refuses only parts whose models
interpenetrate (two connectors drawn over each other, either-or footprints).

The heights come from one `kicad-cli pcb export glb` of the board, cached by
the footprints' models (not their poses) in `~/.cache/krt_heights`, so the
seeds of one placement export once. Without kicad-cli every height is
unknown and nothing changes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import subprocess
import tempfile
from typing import Dict, Optional, Tuple

#: the room a part needs between its top and a cover's underside to be
#: ADMITTED under it, mm
MARGIN_MM = 0.5
#: ...and to be REFUSED under a raised body past the cover's occupancy, the
#: two bodies must meet: the human boards tuck a part's edge under a raised
#: body with 0.06-0.47 mm to spare (a ferrite under a tantalum's moulded
#: edge, a resistor at a module's), and never through one
OVERHANG_MARGIN_MM = 0.0
#: a body at least this high over the board is raised: a part under it has to
#: fit under it, wherever its occupancy is
LIFT_MIN_MM = 0.5
#: ...and the lowest underside that ADMITS a part. Every underside under this
#: the corpus census found was a model's offset or a body's standoff, not
#: room: an SMD module soldered flat whose model floats 1.0 mm (about its own
#: board's thickness), a radio module's board at 0.8 mm, tantalum and DIP
#: standoffs, a QFP's lead shoulders. The lowest real cover measured was a
#: 2.0 mm shield lid.
ROOM_FLOOR_MM = 1.5
#: the raster cell, mm
CELL_MM = 0.25
#: the room a part must have under it, or the raised overhang past its own
#: occupancy, to be a cover at all: less is a body's curve or step, mm^2
ROOM_MIN_MM2 = 1.0
#: the shortest part the room under a cover is measured for, mm (an 0201 is
#: 0.3 mm tall)
_SHORTEST_MM = 0.2
#: geometry lower than this above its face is the part's pins or seat, not body
_ABOVE_MM = 0.05
#: a surface whose normal points this much away from the board faces up: it
#: is a top, and says nothing about the room under it -- a model drawn
#: without a bottom face would otherwise read hollow (cos 78 degrees)
_UP_FACING = 0.2
#: kicad-cli's export, a hang guard (a 500-part board takes under a minute)
EXPORT_TIMEOUT_S = 600
#: bump to invalidate every cached measurement
_CACHE_VERSION = 4

#: IPC-7351 land-pattern names end their body dimensions with the height in
#: 1/100 mm: CAPC1005X55N, QFN50P400X400X80-25N, SOIC127P600X175-8N,
#: BGA324C80P18X18_1500X1500X150
_IPC = re.compile(r'^(?:CAP|RES|IND|DIO|LED|FUS|OSC|XTAL|SO|SOT|SOD|SOIC|SOP|SSOP|TSOP|'
                  r'MSOP|QFN|DFN|SON|QFP|LQFP|TQFP|BGA|LGA|TO|SC|PLCC|CSP|TS|HTS|VS|HS|PQFP|MQFP|'
                  r'VQFN|WQFN|UQFN|XQFN|WSON|VSON|USON)[A-Z]*\d')
_IPC_H = re.compile(r'X(\d{2,4})(?=[A-Z_\-]|$)')


def ipc_height(name: str) -> Optional[float]:
    """The body height an IPC-7351 name carries, mm, or None."""
    stem = os.path.splitext(os.path.basename((name or '').replace('\\', '/')))[0]
    stem = stem.split(':')[-1].upper()
    if not _IPC.match(stem):
        return None
    hs = _IPC_H.findall(stem)
    if not hs:
        return None
    h = int(hs[-1]) / 100.0
    return h if 0.05 <= h <= 30.0 else None


class Cover:
    """One part's room-under raster, in its footprint's LOCAL frame (the frame
    its courtyard and pads are stored in; board = pose + R(-rot) local).

    `lo[i, j]` is the lowest point of the model over cell (i, j), mm above the
    part's face, `inf` where the model is not over it; `court[i, j]` marks a
    cell nothing may stand in: one the drawn courtyard reaches, or the part's
    own pad copper -- its pins stand there, and a model need not draw them (a
    module's model is often its board alone, floating over its headers).
    `solid`: nothing may stand inside the part's occupancy, whatever the
    model says -- a part something reaches from above (a connector's model
    is the likeliest to stand off its footprint); its raised body past the
    occupancy still binds."""

    __slots__ = ('ref', 'x0', 'y0', 'lo', 'court', 'lift_box', 'solid')

    def __init__(self, ref, x0, y0, lo, court=None, solid=False):
        import numpy as np
        self.ref = ref
        self.solid = solid
        self.x0, self.y0 = x0, y0
        self.lo = lo
        self.court = court if court is not None else np.zeros(lo.shape, bool)
        raised = np.isfinite(lo) & (lo >= LIFT_MIN_MM)
        if raised.any():
            ii, jj = np.nonzero(raised)
            self.lift_box = (x0 + ii.min() * CELL_MM, y0 + jj.min() * CELL_MM,
                             x0 + (ii.max() + 1) * CELL_MM, y0 + (jj.max() + 1) * CELL_MM)
        else:
            self.lift_box = None

    def _window(self, box):
        """The cell index ranges over local `box`, clipped, and whether the box
        reaches past the raster."""
        nx, ny = self.lo.shape
        i0 = math.floor((box[0] - self.x0) / CELL_MM)
        i1 = math.ceil((box[2] - self.x0) / CELL_MM)
        j0 = math.floor((box[1] - self.y0) / CELL_MM)
        j1 = math.ceil((box[3] - self.y0) / CELL_MM)
        past = i0 < 0 or j0 < 0 or i1 > nx or j1 > ny
        return max(0, i0), min(nx, i1), max(0, j0), min(ny, j1), past

    def blocked(self, occ, box, top, grow=0.0, inside=True, body=None) -> float:
        """How much of a part the cover blocks, mm^2; 0.0 when it fits:
        `inside_area` (when `inside`) plus `over_area`."""
        a = self.inside_area(occ, box, top, grow) if inside else None
        return (a or 0.0) + self.over_area(body)

    def inside_area(self, occ, box, top, grow=0.0) -> Optional[float]:
        """Inside the cover's occupancy `occ`, the part's occupancy `box`
        (local to this cover), judged as a part `top` mm tall (`inf` for one
        that may never be covered): None when the part keeps `grow` (the
        seat's clearance, measured as `legality.rect_gap` measures it) from
        `occ` -- nothing to judge -- else 0.0 when the model is over all of it,
        high enough (`MARGIN_MM`, `ROOM_FLOOR_MM`), with no drawn courtyard
        or pad there, else the area it reaches into, mm^2."""
        from placement.legality import rect_gap
        g = _grown(box, grow)
        ix = (max(g[0], occ[0]), max(g[1], occ[1]),
              min(g[2], occ[2]), min(g[3], occ[3]))
        if not (ix[2] > ix[0] and ix[3] > ix[1] and rect_gap(box, occ) < grow + 1e-9):
            return None
        i0, i1, j0, j1, past = self._window(ix)
        ok = not past and i1 > i0 and j1 > j0 and not self.solid
        if ok:
            w = self.lo[i0:i1, j0:j1]
            # a cell the model is not over (inf) is unknown, not room
            ok = bool((w < math.inf).all() and (w >= max(top + MARGIN_MM, ROOM_FLOOR_MM)).all()
                      and not self.court[i0:i1, j0:j1].any())
        return 0.0 if ok else (ix[2] - ix[0]) * (ix[3] - ix[1])

    def over_area(self, body) -> float:
        """Anywhere, the cover's RAISED body against the part's own: `body` =
        (points, heights), the part's model cell by cell in this cover's frame;
        mm^2 of the cells where the cover's underside is below the part's top
        (`OVERHANG_MARGIN_MM`). 0.0 for a part with no model (None): it has no
        shape to judge there, its courtyard box being no body."""
        if self.lift_box is None or body is None:
            return 0.0
        import numpy as np
        pts, hv = body
        if not len(pts):
            return 0.0
        nx, ny = self.lo.shape
        ii = np.floor((pts[:, 0] - self.x0) / CELL_MM).astype(int)
        jj = np.floor((pts[:, 1] - self.y0) / CELL_MM).astype(int)
        ok = (ii >= 0) & (ii < nx) & (jj >= 0) & (jj < ny)
        if not ok.any():
            return 0.0
        w = self.lo[ii[ok], jj[ok]]
        return int(((w >= LIFT_MIN_MM) & (w < hv[ok] + OVERHANG_MARGIN_MM)).sum()) * CELL_MM * CELL_MM


def to_local(box, pose):
    """Board-frame `box` as a box in the local frame of a part at `pose`
    (x, y, rot): the inverse of `legality.rotate_local_bounds`."""
    x, y, rot = pose
    r = math.radians(rot % 360)
    c, s = math.cos(r), math.sin(r)
    if abs(s) < 1e-12 or abs(c) < 1e-12:
        c, s = round(c), round(s)
    xs, ys = [], []
    for px, py in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        dx, dy = px - x, py - y
        xs.append(dx * c - dy * s)
        ys.append(dx * s + dy * c)
    return min(xs), min(ys), max(xs), max(ys)


def to_board(box, pose):
    """Local `box` of a part at `pose` as a board-frame box (its bbox)."""
    from placement.legality import rotate_local_bounds
    x, y, rot = pose
    b = rotate_local_bounds(*box, rot)
    return x + b[0], y + b[1], x + b[2], y + b[3]


class Heights:
    """The board's heights: `top[ref]` (mm, how it was known) and `covers[ref]`
    for the parts with room under them. Empty when nothing was measured."""

    def __init__(self, top=None, covers=None, note='', reached=(), bodies=None):
        self.top: Dict[str, Tuple[float, str]] = dict(top or {})
        #: each modelled part's body, local frame: (cell centres (n, 2), the
        #: model's top over each (n,), their bounding box)
        self.bodies = {r: _with_extent(v) for r, v in (bodies or {}).items()}
        self.covers: Dict[str, Cover] = dict(covers or {})
        self.note = note
        #: the parts reached from above, which nothing may cover
        self.reached = frozenset(reached)

    def __bool__(self):
        return bool(self.covers)

    def height(self, ref) -> Optional[float]:
        t = self.top.get(ref)
        return t[0] if t else None

    def under_height(self, ref) -> Optional[float]:
        """How tall `ref` is when something would cover it: its height, or
        `inf` for a part reached from above; None when unknown."""
        return math.inf if ref in self.reached else self.height(ref)

    def verdict(self, a, pose_a, occ_a, side_a, b, pose_b, occ_b, side_b,
                grow=0.0, inside=True):
        """What the heights say about two parts at these poses, or None when
        they cannot judge the pair (another face, no cover, a height unknown):
        `(cleared, over, inside)` -- `cleared`, one stands inside the other's
        occupancy with room to spare, so the occupancy test does not apply;
        `over`, mm^2 of the two bodies meeting under a raised body; `inside`,
        mm^2 the occupancy test would price when not cleared. `occ_*` are the
        parts' LOCAL occupancy boxes; `grow` the seat's clearance; `inside`
        False judges the raised bodies alone."""
        if side_a != side_b:
            return None
        ca, cb = self.covers.get(a), self.covers.get(b)
        if ca is None and cb is None:
            return None
        ta, tb = self.under_height(a), self.under_height(b)
        judged = cleared = False
        over = 0.0
        ins = None
        for cov, x, px, ox, y, py, oy, ty in ((ca, a, pose_a, occ_a, b, pose_b, occ_b, tb),
                                              (cb, b, pose_b, occ_b, a, pose_a, occ_a, ta)):
            if cov is None or ty is None:
                continue
            judged = True
            box = to_local(to_board(oy, py), px)
            if inside and not (box[0] >= ox[2] + grow or ox[0] >= box[2] + grow
                               or box[1] >= ox[3] + grow or ox[1] >= box[3] + grow):
                v = cov.inside_area(ox, box, ty, grow)
                if v is not None:
                    cleared = cleared or v == 0.0
                    ins = v if ins is None else min(ins, v)
            lb = cov.lift_box
            bb = self.bodies.get(y)
            if lb is not None and bb is not None:
                ext = to_local(to_board(bb[2], py), px)
                if ext[0] < lb[2] and lb[0] < ext[2] and ext[1] < lb[3] and lb[1] < ext[3]:
                    over = max(over, cov.over_area(self._body_in(y, py, px)))
        if not judged:
            return None
        return cleared, over, ins or 0.0

    def pair(self, a, pose_a, occ_a, side_a, b, pose_b, occ_b, side_b,
             grow=0.0, inside=True) -> Optional[float]:
        """The overlap the two parts' bodies make, mm^2 -- 0.0 when one fits
        under the other or they do not meet -- or None when the heights
        cannot judge the pair (`verdict`)."""
        v = self.verdict(a, pose_a, occ_a, side_a, b, pose_b, occ_b, side_b, grow, inside)
        if v is None:
            return None
        cleared, over, ins = v
        return over + (0.0 if cleared else ins)

    def _body_in(self, ref, pose, frame):
        """`ref`'s body at `pose`, cell by cell, in the local frame of a part
        at `frame`, or None when it has no model."""
        b = self.bodies.get(ref)
        if b is None:
            return None
        import numpy as np
        pts, hv = b[0], b[1]
        r1 = math.radians(-(pose[2] % 360))
        r2 = math.radians(frame[2] % 360)
        c1, s1, c2, s2 = math.cos(r1), math.sin(r1), math.cos(r2), math.sin(r2)
        bx = pose[0] + pts[:, 0] * c1 - pts[:, 1] * s1 - frame[0]
        by = pose[1] + pts[:, 0] * s1 + pts[:, 1] * c1 - frame[1]
        return np.stack([bx * c2 - by * s2, bx * s2 + by * c2], 1), hv

    def reach(self, ref, pose, occ):
        """The board box a cover's rule can reach at `pose`: its occupancy and
        its raised body. None for a part with no room under it."""
        c = self.covers.get(ref)
        if c is None:
            return None
        b = to_board(occ, pose)
        if c.lift_box is None:
            return b
        lb = to_board(c.lift_box, pose)
        return (min(b[0], lb[0]), min(b[1], lb[1]), max(b[2], lb[2]), max(b[3], lb[3]))


def _with_extent(body):
    """A body as (cell centres, heights, their bounding box)."""
    if len(body) >= 3:
        return body
    pts, hv = body
    return (pts, hv, (float(pts[:, 0].min()) - CELL_MM, float(pts[:, 1].min()) - CELL_MM,
                      float(pts[:, 0].max()) + CELL_MM, float(pts[:, 1].max()) + CELL_MM))


def _grown(box, g):
    return (box[0] - g, box[1] - g, box[2] + g, box[3] + g) if g else box


EMPTY = Heights(note='no heights')
_MEMO: Dict[str, Heights] = {}


def for_board(pcb_data, pcb_file: Optional[str] = None) -> Heights:
    """The board's `Heights`, measured once per process and cached on disk.
    Never raises: a board it cannot measure has no heights, and every pair
    keeps its occupancy test."""
    try:
        return _for_board(pcb_data, pcb_file)
    except Exception as ex:                                  # noqa: BLE001
        return Heights(note=f'no heights: the measurement failed ({type(ex).__name__}: '
                            f'{str(ex)[:120]})')


def _for_board(pcb_data, pcb_file):
    path = pcb_file or getattr(pcb_data, 'source_path', None)
    if not path or not os.path.isfile(path):
        return EMPTY
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return EMPTY
    from kicad_parser import iter_footprint_blocks
    from model_export import model_dirs, resolve_cli, stage_models
    names = {}
    for _s, _e, fp, _raw, k in iter_footprint_blocks(text):
        names[k] = [m.group(1) for m in re.finditer(r'\(model\s+"([^"]+)"', fp)]
    cli, _why = resolve_cli()
    if not cli:
        return Heights(top=_ipc_tops(pcb_data, {}, names),
                       note='no kicad-cli: heights from IPC names only')
    board_dir = os.path.dirname(os.path.abspath(path))
    staged, _counts, _missing, _f = stage_models(text, model_dirs(cli, board_dir), board_dir)
    key = _key(staged, cli, iter_footprint_blocks)
    if key in _MEMO:
        return _MEMO[key]
    cache = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
                         'krt_heights', key + '.json')
    raw = None
    if os.path.isfile(cache):
        try:
            with open(cache, encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, ValueError):
            raw = None
    failed = 'the 3D export failed'
    if raw is None:
        raw = _measure(staged, cli, pcb_data)
        if isinstance(raw, str):
            failed, raw = raw, None
        if raw is not None:
            try:
                os.makedirs(os.path.dirname(cache), exist_ok=True)
                tmp = cache + f'.{os.getpid()}'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(raw, f)
                os.replace(tmp, cache)
            except OSError:
                pass
    h = _load(raw, pcb_data, path, names) if raw is not None else Heights(
        top=_ipc_tops(pcb_data, {}, names),
        note=f'{failed}: heights from IPC names only')
    _MEMO[key] = h
    return h


def _key(staged, cli, iter_blocks):
    """What the measurement depends on: each footprint's layer, models (the
    blocks, and the files' sizes and times) and pads, and kicad-cli -- never
    its pose, so every seed of a placement reads one measurement."""
    h = hashlib.sha1(f'v{_CACHE_VERSION}\0{cli}'.encode())
    for _s, _e, fp, _raw, key in iter_blocks(staged):
        m = re.search(r'\(layer\s+"([^"]+)"', fp)
        h.update(f'{key}\0{m.group(1) if m else ""}\0'.encode())
        for tok in ('(model ', '(pad '):
            i = fp.find(tok)
            while i >= 0:
                blk = _sexpr(fp, i)
                h.update(re.sub(r'\s+', ' ', blk).encode())
                if tok == '(model ':
                    f = re.match(r'\(model\s+"([^"]+)"', blk)
                    try:
                        st = os.stat(f.group(1))
                        h.update(f'{st.st_size}:{int(st.st_mtime)}'.encode())
                    except (OSError, AttributeError):
                        h.update(b'?')
                i = fp.find(tok, i + len(blk))
    return h.hexdigest()[:20]


def _sexpr(text, i):
    """The balanced s-expression starting at `text[i]` ('('), strings skipped."""
    depth, j, n = 0, i, len(text)
    while j < n:
        c = text[j]
        if c == '"':
            j += 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == '\\' else 1
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    return text[i:]


def _ipc_tops(pcb_data, have, names):
    """{ref: (mm, 'ipc_name')} for the parts `have` leaves out whose model
    file or footprint carries an IPC-7351 name."""
    out = {}
    for ref, fp in (pcb_data.footprints or {}).items():
        if ref in have:
            continue
        h = None
        for name in list(names.get(ref) or ()) + [fp.footprint_name or '']:
            h = ipc_height(name)
            if h:
                break
        if h:
            out[ref] = (h, 'ipc_name')
    return out


def _measure(staged, cli, pcb_data):
    """Export the staged board's parts and measure them: {'top': {ref: mm},
    'covers': {ref: [x0, y0, nx, ny, hex of uint16 centi-mm]}}, None when the
    export failed, or the reason when it timed out (never cached, so the next
    run measures again)."""
    import numpy as np
    td = tempfile.mkdtemp(prefix='krt_heights_')
    try:
        brd = os.path.join(td, 'b.kicad_pcb')
        with open(brd, 'w', encoding='utf-8') as f:
            f.write(staged)
        glb = os.path.join(td, 'b.glb')
        try:
            r = subprocess.run([cli, 'pcb', 'export', 'glb', '--no-dnp', '--subst-models',
                                '--no-board-body', '-f', '-o', glb, brd],
                               capture_output=True, timeout=EXPORT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return f'the 3D export timed out after {EXPORT_TIMEOUT_S} s'
        except OSError:
            return None
        if r.returncode != 0 or not os.path.isfile(glb):
            return None
        with open(glb, 'rb') as f:
            data = f.read()
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    from kicad_parser import footprint_at_match, iter_footprint_blocks
    poses = {}
    for _s, _e, fp, _raw, k in iter_footprint_blocks(staged):
        m = footprint_at_match(fp)
        if m:
            poses[k] = (float(m.group(1)), float(m.group(2)),
                        float(m.group(3)) if m.group(3) else 0.0)
    tris, norms, roots = _glb_parts(data, set(pcb_data.footprints or {}) & set(poses))
    if not roots:
        return {'top': {}, 'covers': {}}
    fps = pcb_data.footprints
    refs = sorted(roots)
    T = np.array([roots[r][:3, 3] for r in refs])
    P = np.array([poses[r][:2] for r in refs])
    best = None
    for up in range(3):
        pl = [k for k in range(3) if k != up]
        for a, b in ((pl[0], pl[1]), (pl[1], pl[0])):
            for sx in (1, -1):
                for sy in (1, -1):
                    for u in (1000.0, 1.0):
                        gx, gy = sx * T[:, a] * u, sy * T[:, b] * u
                        ox, oy = np.median(P[:, 0] - gx), np.median(P[:, 1] - gy)
                        err = float(np.median(np.abs(P[:, 0] - gx - ox) + np.abs(P[:, 1] - gy - oy)))
                        if best is None or err < best[0]:
                            best = (err, up, a, b, sx, sy, u, ox, oy)
    err, up, ax, ay, sx, sy, u, ox, oy = best
    if err > 0.01:
        return None
    from placement.legality import footprint_side
    side = {r: footprint_side(fps[r]) for r in refs}
    fz = [roots[r][up, 3] for r in refs if side[r] == 'F']
    bz = [roots[r][up, 3] for r in refs if side[r] == 'B']
    if not fz and not bz:
        return None
    # which way is out of the front face: a front part's body is on that side
    outward = 1.0
    for r in refs:
        if side[r] == 'F' and r in tris:
            zs = tris[r][:, :, up]
            outward = 1.0 if np.median(zs) >= roots[r][up, 3] else -1.0
            break
    else:
        for r in refs:
            if side[r] == 'B' and r in tris:
                zs = tris[r][:, :, up]
                outward = -1.0 if np.median(zs) <= roots[r][up, 3] else 1.0
                break
    top_face = float(np.median(fz)) if fz else None
    bot_face = float(np.median(bz)) if bz else None
    out_top, out_cov, out_body = {}, {}, {}
    for r in refs:
        t = tris.get(r)
        if t is None:
            continue
        fp = fps[r]
        face = top_face if side[r] == 'F' else bot_face
        sgn = outward if side[r] == 'F' else -outward
        bx = sx * t[:, :, ax] * u + ox
        by = sy * t[:, :, ay] * u + oy
        h = sgn * (t[:, :, up] - face) * u
        px, py, prot = poses[r]
        rot = math.radians(prot % 360)
        c, s = math.cos(rot), math.sin(rot)
        dx, dy = bx - px, by - py
        q = np.stack([dx * c - dy * s, dx * s + dy * c, h], 2)
        nh = sgn * norms[r][:, up]           # each triangle's normal, away from the face
        keep = q[:, :, 2].max(1) > _ABOVE_MM
        q, nh = q[keep], nh[keep]
        if not len(q):
            continue
        if not _sits_on(q, fp):
            continue          # a model that is not over its pads says nothing
        out_top[r] = round(float(q[:, :, 2].max()), 3)
        out_body[r] = _raster(q, high=True)
        # the room under a body is read off the surfaces that face the board
        # or stand on it, never off a top
        cov = _raster(q[nh <= _UP_FACING])
        if cov is not None:
            out_cov[r] = cov
    return {'top': out_top, 'covers': out_cov, 'bodies': out_body}


def _sits_on(q, fp):
    """Does the model's plan cover the footprint's pad centre (local frame)?"""
    pads = [p for p in (fp.pads or ())]
    if not pads:
        return True
    cx = sum(p.local_x for p in pads) / len(pads)
    cy = sum(p.local_y for p in pads) / len(pads)
    tol = 0.5
    return (q[:, :, 0].min() - tol <= cx <= q[:, :, 0].max() + tol
            and q[:, :, 1].min() - tol <= cy <= q[:, :, 1].max() + tol)


def _raster(q, high=False):
    """A cover's raster [x0, y0, nx, ny, hex] of its lowest point per cell, or
    None when no cell of the model is raised `LIFT_MIN_MM` -- a body that
    sits on the board. `high`: every part's raster of its HIGHEST point per
    cell instead, never None."""
    import numpy as np
    if not len(q):
        return None
    q = _subdivide(q)
    x0 = math.floor(q[:, :, 0].min() / CELL_MM) * CELL_MM
    y0 = math.floor(q[:, :, 1].min() / CELL_MM) * CELL_MM
    nx = int(math.ceil((q[:, :, 0].max() - x0) / CELL_MM)) + 1
    ny = int(math.ceil((q[:, :, 1].max() - y0) / CELL_MM)) + 1
    lo = np.full((nx, ny), np.inf)
    i0 = np.floor((q[:, :, 0].min(1) - x0) / CELL_MM).astype(int)
    i1 = np.floor((q[:, :, 0].max(1) - x0) / CELL_MM).astype(int)
    j0 = np.floor((q[:, :, 1].min(1) - y0) / CELL_MM).astype(int)
    j1 = np.floor((q[:, :, 1].max(1) - y0) / CELL_MM).astype(int)
    if high:
        hi = np.full((nx, ny), -np.inf)
        hm = q[:, :, 2].max(1)
        for di in (0, 1):
            for dj in (0, 1):
                np.maximum.at(hi, (np.minimum(i0 + di, i1), np.minimum(j0 + dj, j1)), hm)
        cm = np.where(np.isfinite(hi), np.clip(np.round(hi * 100), 0, 65534), 65535).astype('<u2')
        return [round(x0, 4), round(y0, 4), nx, ny, cm.tobytes().hex()]
    hm = np.maximum(0.0, q[:, :, 2].min(1))
    # a subdivided triangle spans at most two cells each way
    for di in (0, 1):
        for dj in (0, 1):
            np.minimum.at(lo, (np.minimum(i0 + di, i1), np.minimum(j0 + dj, j1)), hm)
    fin = np.isfinite(lo)
    if not (fin & (lo >= LIFT_MIN_MM)).any():
        return None
    cm = np.where(fin, np.minimum(np.round(lo * 100), 65534), 65535).astype('<u2')
    return [round(x0, 4), round(y0, 4), nx, ny, cm.tobytes().hex()]


def _subdivide(t):
    """Split (n, 3, 3) triangles (x, y, h) until no plan edge is longer than a
    cell, so a triangle's own bbox stands for the cells it covers."""
    import numpy as np
    out = []
    while len(t):
        e = np.max([np.hypot(*(t[:, i, :2] - t[:, (i + 1) % 3, :2]).T) for i in range(3)], 0)
        small = e <= CELL_MM
        out.append(t[small])
        t = t[~small]
        if not len(t):
            break
        a, b, c = t[:, 0], t[:, 1], t[:, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        t = np.concatenate([np.stack(k, 1) for k in ((a, ab, ca), (ab, b, bc), (ca, bc, c),
                                                     (ab, bc, ca))])
    return np.concatenate(out)


def _glb_parts(data, refs):
    """({ref: (n, 3, 3) world triangles}, {ref: (n, 3) their unit normals,
    world frame}, {ref: world matrix of its node}). A triangle's normal is
    its vertices' mean where the mesh gives them, else its winding's."""
    import numpy as np
    if len(data) < 20 or struct.unpack('<I', data[:4])[0] != 0x46546C67:
        return {}, {}, {}
    jl = struct.unpack('<I', data[12:16])[0]
    g = json.loads(data[20:20 + jl])
    off = 20 + jl
    bin_ = b''
    if off + 8 <= len(data):
        bl = struct.unpack('<I', data[off:off + 4])[0]
        bin_ = data[off + 8:off + 8 + bl]
    ct = {5121: np.uint8, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
    nc = {'SCALAR': 1, 'VEC3': 3}

    def acc(i):
        a = g['accessors'][i]
        bv = g['bufferViews'][a['bufferView']]
        dt = np.dtype(ct[a['componentType']]).newbyteorder('<')
        n = nc[a['type']]
        start = bv.get('byteOffset', 0) + a.get('byteOffset', 0)
        stride = bv.get('byteStride', 0)
        if stride and stride != dt.itemsize * n:
            arr = np.ndarray((a['count'], n), dt, bin_, start, (stride, dt.itemsize))
        else:
            arr = np.frombuffer(bin_, dt, a['count'] * n, start).reshape(a['count'], n)
        return np.array(arr, dtype=np.float64 if dt.kind == 'f' else np.int64)

    def local(n):
        if 'matrix' in n:
            return np.array(n['matrix'], float).reshape(4, 4).T
        x, y, z, w = n.get('rotation', [0, 0, 0, 1])
        s = n.get('scale', [1, 1, 1])
        t = n.get('translation', [0, 0, 0])
        r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        m = np.eye(4)
        m[:3, :3] = r * np.array(s)
        m[:3, 3] = t
        return m

    tris, norms, roots = {}, {}, {}
    stack = [(i, np.eye(4), None) for i in g['scenes'][g.get('scene', 0)]['nodes']]
    while stack:
        i, m, own = stack.pop()
        node = g['nodes'][i]
        w = m @ local(node)
        name = node.get('name')
        # kicad-cli names a part's node by its bare reference; a duplicate
        # reference's later blocks cannot be told apart, so only the first
        if own is None and name in refs and name not in roots:
            own = name
            roots[name] = w
        if 'mesh' in node and own:
            for prim in g['meshes'][node['mesh']]['primitives']:
                if prim.get('mode', 4) != 4:
                    continue
                v = acc(prim['attributes']['POSITION'])
                v = (w[:3, :3] @ v.T).T + w[:3, 3]
                idx = (acc(prim['indices']).reshape(-1) if 'indices' in prim
                       else np.arange(len(v)))
                idx = idx[:len(idx) // 3 * 3].reshape(-1, 3)
                t = v[idx]
                if 'NORMAL' in prim['attributes']:
                    nv = acc(prim['attributes']['NORMAL'])
                    nv = (np.linalg.inv(w[:3, :3]).T @ nv.T).T
                    n = nv[idx].mean(1)
                else:
                    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
                    if np.linalg.det(w[:3, :3]) < 0:
                        n = -n
                n = n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
                tris.setdefault(own, []).append(t)
                norms.setdefault(own, []).append(n)
        for ch in node.get('children', []):
            stack.append((ch, w, own))
    return ({r: np.concatenate(v) for r, v in tris.items()},
            {r: np.concatenate(v) for r, v in norms.items()}, roots)


def _load(raw, pcb_data, pcb_file, names) -> Heights:
    """`Heights` from a measurement, with each cover's drawn courtyard marked
    and the IPC names filling the heights the models left unknown."""
    import numpy as np
    top = {r: (float(h), 'model') for r, h in (raw.get('top') or {}).items()
           if r in (pcb_data.footprints or {})}
    top.update(_ipc_tops(pcb_data, top, names))
    covers = {}
    reached = _reached(pcb_data)
    shapes, occ = {}, {}
    if raw.get('covers'):
        try:
            from placement.body import board_bodies
            for r, g in board_bodies(pcb_data, pcb_file).items():
                shapes[r] = g.court_shape_local
                occ[r] = g.occupancy_local
        except Exception:                                    # noqa: BLE001
            shapes = {}
        shapes = {r: _no_go(pcb_data.footprints[r], shapes.get(r))
                  for r in raw['covers'] if r in (pcb_data.footprints or {})}
    for r, (x0, y0, nx, ny, hx) in (raw.get('covers') or {}).items():
        if r not in top:
            continue
        cm = np.frombuffer(bytes.fromhex(hx), '<u2').reshape(nx, ny)
        lo = np.where(cm == 65535, np.inf, cm / 100.0)
        court = _mask(shapes.get(r), x0, y0, nx, ny)
        solid = r in reached
        if _room(lo, court, x0, y0, occ.get(r), solid) >= ROOM_MIN_MM2:
            covers[r] = Cover(r, x0, y0, lo, court, solid)
    n_model = sum(1 for v in top.values() if v[1] == 'model')
    return Heights(top, covers, note=f'{n_model} part heights from models, '
                                     f'{len(top) - n_model} from IPC names, '
                                     f'{len(covers)} part(s) with room under them',
                   reached=reached, bodies=_bodies(raw.get('bodies') or {}, top))


def _bodies(raw, top):
    """{ref: (cell centres (n, 2), the top over each (n,))}, local frame."""
    import numpy as np
    out = {}
    for r, (x0, y0, nx, ny, hx) in raw.items():
        if r not in top:
            continue
        cm = np.frombuffer(bytes.fromhex(hx), '<u2').reshape(nx, ny)
        ii, jj = np.nonzero(cm != 65535)
        if not len(ii):
            continue
        pts = np.stack([x0 + (ii + 0.5) * CELL_MM, y0 + (jj + 0.5) * CELL_MM], 1)
        out[r] = (pts, cm[ii, jj] / 100.0,
                  (float(pts[:, 0].min()) - CELL_MM, float(pts[:, 1].min()) - CELL_MM,
                   float(pts[:, 0].max()) + CELL_MM, float(pts[:, 1].max()) + CELL_MM))
    return out


def _reached(pcb_data):
    """The parts something reaches from above: those `part_class` classes,
    and those whose designator is a connector's, a switch's, a test point's
    or a mounting part's (`part_class`'s prefix tables) -- a connector drawn
    with a library's own name is still a connector, and a crystal called X1
    kept out from under a cover costs nothing."""
    from placement.part_class import (ACTUATOR_PREFIX, CONN_PREFIX_LOW, CONN_PREFIX_MED,
                                      _designator, _prefix, classify_part)
    prefixes = set(CONN_PREFIX_MED) | set(CONN_PREFIX_LOW) | set(ACTUATOR_PREFIX)
    out = set()
    for ref, fp in (pcb_data.footprints or {}).items():
        try:
            if (classify_part(fp, ref).name is not None or _prefix(ref) in prefixes
                    or _designator(ref) in prefixes):
                out.add(ref)
        except Exception:                                    # noqa: BLE001
            out.add(ref)
    return out


def _room(lo, court, x0, y0, occ, solid=False):
    """The larger of a raster's room for the shortest part (none in a `solid`
    part) and its raised overhang past the part's own occupancy box, mm^2."""
    import numpy as np
    free = np.isfinite(lo) & ~court
    room = 0 if solid else (free & (lo >= max(MARGIN_MM + _SHORTEST_MM, ROOM_FLOOR_MM))).sum()
    over = 0
    if occ is not None:
        nx, ny = lo.shape
        cx = x0 + (np.arange(nx) + 0.5) * CELL_MM
        cy = y0 + (np.arange(ny) + 0.5) * CELL_MM
        out = ~(((cx >= occ[0]) & (cx <= occ[2]))[:, None] & ((cy >= occ[1]) & (cy <= occ[3]))[None, :])
        over = (free & (lo >= LIFT_MIN_MM) & out).sum()
    return max(room, over) * CELL_MM * CELL_MM


def _no_go(fp, court):
    """A cover's no-go shape, local frame: its drawn courtyard united with its
    pads' copper."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from check_pads import custom_pad_copper, pad_outline_polygon
    from placement.legality import _pad_carries_copper
    parts = [court] if court is not None else []
    pose = (fp.x, fp.y, fp.rotation or 0.0)
    r = math.radians(pose[2] % 360)
    c, s = math.cos(r), math.sin(r)
    for pad in (fp.pads or ()):
        if not _pad_carries_copper(pad):
            continue
        try:
            pp = custom_pad_copper(pad)
            if pp is None:
                pts = pad_outline_polygon(pad)
                pp = Polygon(pts) if len(pts) >= 3 else None
        except Exception:                                    # noqa: BLE001
            pp = None
        if pp is None or not pp.is_valid:
            continue
        loc = [((x - pose[0]) * c - (y - pose[1]) * s, (x - pose[0]) * s + (y - pose[1]) * c)
               for x, y in pp.exterior.coords]
        parts.append(Polygon(loc))
    return unary_union(parts) if parts else None


def _mask(shape, x0, y0, nx, ny):
    """The cells a drawn courtyard reaches (any overlap), local frame."""
    import numpy as np
    m = np.zeros((nx, ny), bool)
    if shape is None:
        return m
    try:
        from shapely.geometry import box
        from shapely.prepared import prep
        b = shape.bounds
        ps = prep(shape)
        for i in range(max(0, math.floor((b[0] - x0) / CELL_MM)),
                       min(nx, math.ceil((b[2] - x0) / CELL_MM))):
            for j in range(max(0, math.floor((b[1] - y0) / CELL_MM)),
                           min(ny, math.ceil((b[3] - y0) / CELL_MM))):
                cx, cy = x0 + i * CELL_MM, y0 + j * CELL_MM
                if ps.intersects(box(cx, cy, cx + CELL_MM, cy + CELL_MM)):
                    m[i, j] = True
    except Exception:                                        # noqa: BLE001
        m[:] = True
    return m
