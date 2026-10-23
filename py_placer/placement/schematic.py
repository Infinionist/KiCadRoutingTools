"""A board's KiCad schematic, read for placement grouping.

The board file says which sheet each footprint came from (its `(path ...)`
uuids, and on KiCad 8+ a `Sheetname`), and nothing about how the schematic is
DRAWN. The drawing is the designer's own grouping: a regulator drawn with its
caps, a crystal with its load caps, an LED with its resistor, the USB
connector with its CC resistors and ESD. This module reads it, for a model
deciding the groups of a placement. It never writes a schematic.

WHAT THE HUMAN BOARDS SAY
-------------------------
Measured on 239 corpus boards (35-220 parts, keyboards left out) whose
schematics were fetched from their source repositories, against where the
designers put the parts:

* How close two parts are DRAWN predicts whether the designer put them in
  the board's closest 10% of pairs with a median AUC of 0.76, against 0.60
  for how strongly the netlist connects them and 0.66 for sheet plus netlist;
  better on 88% of boards. Inside one sheet it still predicts at a fixed
  netlist distance (0.79 for parts sharing a net, 0.73 for parts sharing
  none), so it carries something the netlist does not.
* The drawn blocks this module cuts (`BLOCK_CUT_MM`, a median 3.6 parts
  each) sit 0.20 as wide on the human board as random sets of their sizes,
  against 0.45 for as many netlist clusters; tighter on 91% of boards.
* Sheets are the coarse groups: on hierarchical boards, grouping into 4-8
  groups by sheet then netlist is as tight as grouping by the drawing.

That is agreement with human layouts, not a routed result, and it is why the
output is evidence for the model's groups and not a grouping of its own.

WHAT IT READS
-------------
KiCad 6+ `.kicad_sch` (s-expressions): the sheet tree from the root, each
sheet instance's uuid path and name, every placed symbol (uuid, reference,
lib id, unit, its drawn box from its library pins and graphics), and the
free text and text boxes. A KiCad 5 `.sch` is reported, not read.

A footprint is matched to its symbol by the `(sheet prefix, symbol uuid)` of
its path (`groups.sheet_and_symbol`, the pair `replicate` maps by), so a
part matches on any board the schematic was synced to, whatever its
reference. The other units of a multi-unit part are the symbols of the same
file with its lib id and its reference.

Sheet files resolve against the directory of the sheet that names them,
which is how eeschema loads a hierarchy.

NOTES
-----
A short note (`NOTE_MAX_CHARS`) belongs to the nearest block within
`NOTE_REACH_MM`: "RTC Clock Crystal", "ESP32 3V3 LDO", "Buck Converter (12v
-> 5v)". 87% of the boards carry notes, and 28% of their blocks of three or
more parts have one beside them.
"""
from __future__ import annotations

import math
import os
import re
from typing import Dict, List, Optional, Tuple

import numpy as np

#: A block is cut where the AVERAGE drawn gap between two sets of parts
#: passes this (mm, between symbol boxes). 25.4 gave blocks of a median 3.6
#: parts and the tightest blocks on the human boards for their size; single
#: linkage at any cut chained unrelated blocks along a crowded sheet.
BLOCK_CUT_MM = 25.4

#: A note belongs to the nearest block whose drawn box is within this of the
#: note's estimated text box.
NOTE_REACH_MM = 12.7

#: Longer text is a comment, not a block's title.
NOTE_MAX_CHARS = 80

_TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


class SchematicError(ValueError):
    """A schematic that cannot be read, named."""


# ---------------------------------------------------------------------------
# s-expressions
# ---------------------------------------------------------------------------

class _Str(str):
    """A quoted string, so a value is never mistaken for a keyword."""


def _sexp(text: str):
    stack: List[list] = [[]]
    for m in _TOK.finditer(text):
        t = m.group(0)
        if t == '(':
            stack.append([])
        elif t == ')':
            if len(stack) < 2:
                raise SchematicError('unbalanced parentheses')
            node = stack.pop()
            stack[-1].append(node)
        elif t[0] == '"':
            stack[-1].append(_Str(t[1:-1].replace('\\"', '"')
                                  .replace('\\n', '\n').replace('\\\\', '\\')))
        else:
            stack[-1].append(t)
    if len(stack) != 1 or not stack[0]:
        raise SchematicError('unbalanced parentheses')
    return stack[0][0]


def _head(n):
    if isinstance(n, list) and n and isinstance(n[0], str) \
            and not isinstance(n[0], _Str):
        return n[0]
    return None


def _kids(n, name):
    return [c for c in n[1:] if isinstance(c, list) and _head(c) == name]


def _kid(n, name):
    for c in n[1:]:
        if isinstance(c, list) and _head(c) == name:
            return c
    return None


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _at(n) -> Tuple[float, float, float]:
    a = _kid(n, 'at')
    v = [_num(x) for x in (a[1:4] if a else ())]
    return tuple((v + [0.0, 0.0, 0.0])[:3])


def _props(n) -> Dict[str, str]:
    return {str(p[1]): str(p[2]) for p in _kids(n, 'property') if len(p) >= 3}


# ---------------------------------------------------------------------------
# one file
# ---------------------------------------------------------------------------

def _grow(box, x, y):
    if box is None:
        return [x, y, x, y]
    return [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]


def _lib_extents(libsym) -> Dict[int, list]:
    """`{unit: [x0, y0, x1, y1]}` over a library symbol's pins and graphics,
    in library coordinates (y up). Unit 0 is common to every unit."""
    out: Dict[int, list] = {}

    def walk(node, unit):
        for c in node[1:]:
            if not isinstance(c, list):
                continue
            h = _head(c)
            if h == 'symbol':
                m = re.search(r'_(\d+)_(\d+)$', str(c[1]))
                walk(c, int(m.group(1)) if m else unit)
                continue
            pts = []
            if h == 'pin':
                pts.append(_at(c)[:2])
            elif h == 'rectangle':
                for k in ('start', 'end'):
                    q = _kid(c, k)
                    if q:
                        pts.append((_num(q[1]), _num(q[2])))
            elif h in ('polyline', 'bezier'):
                q = _kid(c, 'pts')
                pts += [(_num(p[1]), _num(p[2])) for p in _kids(q, 'xy')] \
                    if q else []
            elif h == 'circle':
                ce, ra = _kid(c, 'center'), _kid(c, 'radius')
                if ce and ra:
                    r = _num(ra[1])
                    pts += [(_num(ce[1]) - r, _num(ce[2]) - r),
                            (_num(ce[1]) + r, _num(ce[2]) + r)]
            elif h == 'arc':
                for k in ('start', 'mid', 'end'):
                    q = _kid(c, k)
                    if q:
                        pts.append((_num(q[1]), _num(q[2])))
            for x, y in pts:
                out[unit] = _grow(out.get(unit), x, y)
    walk(libsym, 0)
    return out


def _place(px, py, rot, mirror) -> Tuple[float, float]:
    """A library offset (y up) on the sheet (y down): KiCad's symbol
    transform -- rotation counter-clockwise on screen, then the mirror
    (`x` flips about the X axis, `y` about the Y axis)."""
    x, y = px, -py
    for _ in range((int(round(rot)) % 360) // 90):
        x, y = y, -x
    if mirror == 'x':
        y = -y
    elif mirror == 'y':
        x = -x
    return x, y


def _text_box(t, text: str) -> list:
    """An estimated box for a free text (KiCad stores the anchor only)."""
    x, y, rot = _at(t)
    h_size = w_size = 1.27
    just = set()
    eff = _kid(t, 'effects')
    if eff:
        font = _kid(eff, 'font')
        size = _kid(font, 'size') if font else None
        if size and len(size) >= 3:
            h_size, w_size = _num(size[1]) or 1.27, _num(size[2]) or 1.27
        j = _kid(eff, 'justify')
        if j:
            just = {str(v) for v in j[1:]}
    lines = text.split('\n') or ['']
    w = max(len(s) for s in lines) * w_size * 0.85
    h = len(lines) * h_size * 1.6
    x0 = 0.0 if 'left' in just else (-w if 'right' in just else -w / 2)
    y0 = -h if 'bottom' in just else (0.0 if 'top' in just else -h / 2)
    corners = [(x0, y0), (x0 + w, y0 + h)]
    if int(round(rot)) % 180 == 90:
        corners = [(cy, -cx) for cx, cy in corners]
    xs = [x + c[0] for c in corners]
    ys = [y + c[1] for c in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def read_file(path: str) -> Dict:
    """One `.kicad_sch`: its symbols, sheets and notes."""
    with open(path, encoding='utf-8', errors='replace') as fh:
        root = _sexp(fh.read())
    if _head(root) != 'kicad_sch':
        raise SchematicError(f'{os.path.basename(path)} is not a KiCad 6+ '
                             f'schematic')
    libs = {}
    lib_node = _kid(root, 'lib_symbols')
    for s in (_kids(lib_node, 'symbol') if lib_node else ()):
        libs[str(s[1])] = s
    extents: Dict[str, Dict[int, list]] = {}
    symbols = []
    for s in _kids(root, 'symbol'):
        lib_id = _kid(s, 'lib_id')
        lib_name = _kid(s, 'lib_name')
        key = str(lib_name[1]) if lib_name else (str(lib_id[1]) if lib_id
                                                 else '')
        x, y, rot = _at(s)
        mi = _kid(s, 'mirror')
        unit_n = _kid(s, 'unit')
        unit = int(_num(unit_n[1])) if unit_n else 1
        uid = _kid(s, 'uuid')
        pr = _props(s)
        if key not in extents:
            extents[key] = _lib_extents(libs[key]) if key in libs else {}
        box = None
        for u in (0, unit):
            g = extents[key].get(u)
            if not g:
                continue
            for gx, gy in ((g[0], g[1]), (g[2], g[3]), (g[0], g[3]),
                           (g[2], g[1])):
                dx, dy = _place(gx, gy, rot, str(mi[1]) if mi else '')
                box = _grow(box, x + dx, y + dy)
        ref = pr.get('Reference', '')
        lid = str(lib_id[1]) if lib_id else ''
        power = (ref.startswith('#') or lid.lower().startswith('power:')
                 or (key in libs and _kid(libs[key], 'power') is not None))
        symbols.append({'uuid': str(uid[1]) if uid else '', 'lib_id': lid,
                        'ref': ref, 'value': pr.get('Value', ''),
                        'unit': unit, 'box': box or [x, y, x, y],
                        'power': power})
    sheets = []
    for sh in _kids(root, 'sheet'):
        pr = _props(sh)
        uid = _kid(sh, 'uuid')
        sheets.append({'uuid': str(uid[1]) if uid else '',
                       'name': pr.get('Sheetname', pr.get('Sheet name', '')),
                       'file': pr.get('Sheetfile', pr.get('Sheet file', ''))})
    notes = []
    for kind in ('text', 'text_box'):
        for t in _kids(root, kind):
            text = str(t[1]) if len(t) > 1 else ''
            if not text.strip():
                continue
            if kind == 'text_box':
                x, y, _r = _at(t)
                size = _kid(t, 'size')
                box = [x, y, x + (_num(size[1]) if size else 0),
                       y + (_num(size[2]) if size else 0)]
            else:
                box = _text_box(t, text)
            notes.append({'text': text.strip(), 'box': box})
    return {'symbols': symbols, 'sheets': sheets, 'notes': notes}


# ---------------------------------------------------------------------------
# the tree
# ---------------------------------------------------------------------------

def discover(board_path: str) -> Tuple[Optional[str], List[str]]:
    """`(root schematic or None, the paths looked at)`: `<board>.kicad_sch`
    beside the board, else the one `.kicad_pro` in its folder named so."""
    stem = os.path.splitext(os.path.abspath(board_path))[0]
    looked = [stem + '.kicad_sch']
    if os.path.isfile(looked[0]):
        return looked[0], looked
    folder = os.path.dirname(stem)
    try:
        pros = sorted(n for n in os.listdir(folder)
                      if n.endswith('.kicad_pro'))
    except OSError:
        pros = []
    if len(pros) == 1:
        cand = os.path.join(folder, pros[0][:-len('.kicad_pro')] + '.kicad_sch')
        if cand not in looked:
            looked.append(cand)
            if os.path.isfile(cand):
                return cand, looked
    return None, looked


def read_tree(root_path: str) -> Dict:
    """Every file of the hierarchy under `root_path`, and its sheet
    instances `[(uuid prefix 'a/b', file path, (sheet names...))]` in
    depth-first order. A sheet file that cannot be found or read is listed
    in `missing`, never dropped silently."""
    files: Dict[str, Dict] = {}
    missing: List[str] = []
    instances: List[Tuple[str, str, Tuple[str, ...]]] = []

    def load(path):
        if path not in files:
            files[path] = read_file(path)
        return files[path]

    def walk(path, prefix, names, depth):
        instances.append((prefix, path, names))
        if depth > 16:
            return
        for sh in load(path)['sheets']:
            rel = sh['file'].replace('\\', '/')
            if not rel:
                continue
            cands = [os.path.normpath(os.path.join(os.path.dirname(path), rel)),
                     os.path.normpath(os.path.join(os.path.dirname(root_path),
                                                   rel))]
            child = next((c for c in cands if os.path.isfile(c)), None)
            if child is None:
                missing.append(rel)
                continue
            try:
                load(child)
            except (OSError, SchematicError) as exc:
                missing.append(f'{rel} ({exc})')
                continue
            walk(child, (prefix + '/' if prefix else '') + sh['uuid'],
                 names + (sh['name'],), depth + 1)

    walk(os.path.abspath(root_path), '', (), 0)
    return {'root': os.path.abspath(root_path), 'files': files,
            'instances': instances, 'missing': sorted(set(missing))}


def tree_files(root_path: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """`([(file, path relative to the root's folder)], [files outside it])`
    -- what a copy of the hierarchy must carry to resolve as it did."""
    tree = read_tree(root_path)
    base = os.path.dirname(tree['root'])
    inside, outside = [], []
    for path in tree['files']:
        rel = os.path.relpath(path, base)
        if rel.startswith('..') or os.path.isabs(rel):
            outside.append(path)
        else:
            inside.append((path, rel))
    return inside, outside


# ---------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------

def _gap(a, b) -> float:
    dx = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _parts_gap(boxes_a, boxes_b) -> float:
    return min(_gap(a, b) for a in boxes_a for b in boxes_b)


def average_link(D: np.ndarray, cut: float) -> List[List[int]]:
    """Average-linkage clusters of a symmetric distance matrix, merged while
    the closest pair of clusters is within `cut`. Deterministic: ties go to
    the lowest index."""
    n = len(D)
    if n == 0:
        return []
    D = D.astype(float).copy()
    np.fill_diagonal(D, np.inf)
    members = {i: [i] for i in range(n)}
    while len(members) > 1:
        k = int(np.argmin(D))
        i, j = divmod(k, n)
        if not D[i, j] <= cut:
            break
        if j < i:
            i, j = j, i
        ni, nj = len(members[i]), len(members[j])
        row = (ni * D[i] + nj * D[j]) / (ni + nj)
        D[i, :] = row
        D[:, i] = row
        D[i, i] = np.inf
        D[j, :] = np.inf
        D[:, j] = np.inf
        members[i] += members.pop(j)
    return [sorted(m) for _i, m in sorted(members.items())]


# ---------------------------------------------------------------------------
# the board
# ---------------------------------------------------------------------------

def _natural(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', ref)]


def context(pcb_data, root_path: str) -> Dict:
    """The schematic, matched to `pcb_data`'s footprints.

    Returns `parts` (`{ref: {sheet, block}}`), `sheets` (each instance with
    its blocks, their notes, and its single parts) and the match counts."""
    from placement.groups import sheet_and_symbol

    tree = read_tree(root_path)
    inst = {prefix: (path, names) for prefix, path, names in tree['instances']}
    by_uuid: Dict[str, Dict[str, dict]] = {}
    for path, f in tree['files'].items():
        for s in f['symbols']:
            if s['uuid'] and not s['power']:
                by_uuid.setdefault(path, {})[s['uuid']] = s

    on_board: Dict[str, List[Tuple[str, dict]]] = {}
    not_on_schematic = []
    copper_parts = matched_copper = 0
    for ref, fp in sorted((pcb_data.footprints or {}).items(),
                          key=lambda kv: _natural(kv[0])):
        copper = any((getattr(p, 'pad_type', '') or '') != 'np_thru_hole'
                     for p in (fp.pads or ()))
        copper_parts += bool(copper)
        prefix, sym = sheet_and_symbol(fp)
        hit = inst.get(prefix)
        s = by_uuid.get(hit[0], {}).get(sym) if hit else None
        if s is None:
            if copper:
                not_on_schematic.append(ref)
            continue
        matched_copper += bool(copper)
        on_board.setdefault(prefix, []).append((ref, s))

    parts: Dict[str, Dict] = {}
    sheets = []
    nb = 0
    for prefix, path, names in tree['instances']:
        f = tree['files'][path]
        members = on_board.get(prefix, [])
        sheet_name = '/'.join(names)
        boxes = []
        for ref, s in members:
            units = [s['box']]
            if s['ref'] and not s['ref'].endswith('?'):
                units += [o['box'] for o in f['symbols']
                          if o is not s and not o['power']
                          and o['ref'] == s['ref'] and o['lib_id'] == s['lib_id']]
            boxes.append(units)
        n = len(members)
        D = np.zeros((n, n))
        for i in range(n):
            for j in range(i + 1, n):
                D[i, j] = D[j, i] = _parts_gap(boxes[i], boxes[j])
        clusters = average_link(D, BLOCK_CUT_MM)

        def bbox(idx):
            b = None
            for i in idx:
                for u in boxes[i]:
                    b = _grow(_grow(b, u[0], u[1]), u[2], u[3])
            return b
        blocks = []
        for cl in clusters:
            if len(cl) < 2:
                continue
            blocks.append({'idx': cl, 'box': bbox(cl)})
        blocks.sort(key=lambda b: (round(b['box'][1] / BLOCK_CUT_MM),
                                   b['box'][0]))
        for b in blocks:
            nb += 1
            b['id'] = f'B{nb}'
            b['refs'] = sorted((members[i][0] for i in b['idx']), key=_natural)
            b['notes'] = []
        singles = sorted((members[cl[0]][0] for cl in clusters if len(cl) == 1),
                         key=_natural)
        for b in blocks:
            for r in b['refs']:
                parts[r] = {'sheet': sheet_name, 'block': b['id']}
        for r in singles:
            parts[r] = {'sheet': sheet_name, 'block': None}

        loose = []
        for note in f['notes']:
            flat = ' '.join(note['text'].split())
            near = min(((_gap(note['box'], b['box']), k)
                        for k, b in enumerate(blocks)), default=None)
            if len(flat) <= NOTE_MAX_CHARS:
                if near is not None and near[0] <= NOTE_REACH_MM:
                    blocks[near[1]]['notes'].append(flat)
                elif members:
                    loose.append(flat)
        sheets.append({
            'sheet': sheet_name, 'file': os.path.relpath(
                path, os.path.dirname(tree['root'])).replace('\\', '/'),
            'parts': n,
            'blocks': [{'id': b['id'], 'refs': b['refs'], 'notes': b['notes']}
                       for b in blocks],
            'single': singles, 'notes': loose})
    # Both counts over parts with copper: a drilled mounting hole matched
    # to its symbol is still a part on the schematic, but `board_parts`
    # never counted it, and the ratio is read as a sync check.
    matched = matched_copper
    return {
        'root': os.path.basename(tree['root']),
        'files': len(tree['files']),
        'sheet_instances': len(tree['instances']),
        'missing_files': tree['missing'],
        'matched': matched,
        'board_parts': copper_parts,
        'not_on_schematic': not_on_schematic,
        'block_cut_mm': BLOCK_CUT_MM,
        'parts': parts,
        'sheets': [s for s in sheets if s['parts']],
    }


def section(pcb_data, board_path: str, schematic: Optional[str] = None) -> Dict:
    """`context` for a board, with how the schematic was found; an error is
    returned named, never raised, so a sheet that could not read the
    schematic says so instead of reading as a board without one."""
    if schematic:
        root, looked = os.path.abspath(schematic), [schematic]
        if not os.path.isfile(root):
            return {'found': False, 'looked_for': looked,
                    'error': f'{schematic} does not exist'}
    else:
        root, looked = discover(board_path)
    if root is None:
        legacy = [os.path.splitext(p)[0] + '.sch' for p in looked
                  if os.path.isfile(os.path.splitext(p)[0] + '.sch')]
        out = {'found': False, 'looked_for': looked}
        if legacy:
            out['error'] = (f'{os.path.basename(legacy[0])} is a KiCad 5 '
                            f'schematic, which this reader does not parse')
        return out
    try:
        out = context(pcb_data, root)
    except (OSError, SchematicError, RecursionError) as exc:
        return {'found': True, 'looked_for': looked,
                'error': f'{type(exc).__name__}: {exc}'}
    out['found'] = True
    out['looked_for'] = looked
    return out
