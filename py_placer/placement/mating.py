"""Which way a connector OPENS: its mating face, read off its drawn body (#1155).

Before this module the toolchain could say which edge a connector's courtyard
was NEAREST, and nothing else. A connector seated backwards, its shell lying
over the board and its mating face pointing inward, is still nearest the edge
it was declared on whenever the shell's back grazes that edge -- so
`rule_edge_connector`, board_context's "Mating face", check_assembly and
board_score all passed sonde_xilinx's reversed J2 (#1155).

The fact that decides it is where the BODY sits relative to the PAD ROW. A
horizontal or edge-mount connector solders at the back and extends forward to
the opening: a DSUB edge-mount's shell runs 14 mm past its pads, a USB-C
receptacle's 7 mm, a right-angle header's plastic 9 mm. So the mating face
is the direction of the body's centre from the pad bbox's centre, in the
footprint's own frame, mapped through the pose to a board edge.

It abstains -- `face is None`, with a reason -- whenever that reading is not
a fact:

* no pads (nothing for a body to be offset FROM);
* a board-copper plug (`floorplan.is_board_copper_plug`): the BOARD is the
  mating body, and its courtyard points inward by construction (StickHub J1);
* a `Vertical` footprint name: a vertical mount mates perpendicular to the
  board, so it has no in-plane face, however its plastic is drawn (a JST-XH
  vertical courtyard sits 0.5 mm off its pad row);
* no source offset by at least `FACE_MIN_OFFSET_MM` and `FACE_DOMINANCE`
  times its cross-axis offset (a symmetric part: a box header, a terminal);
* sources that disagree (courtyard says north, fab says south: glasgow SW1);
* a pose within 30 degrees of a diagonal.

Two sources vote: the courtyard (when that rung answered) and the drawn body
(`drawn_local`, fab else silk -- AFTER #1201's guard, so a pin-1 dot casts no
vote). Every decisive vote must agree. The KiCad library's "PCB edge"
Dwgs.User line would be a third, but only 4 tracked footprints draw it (and
all four agree); if it is ever read it may only force an abstention on a
contradiction, never decide on its own.

The board frame uses the sign `connector_geometry.rect` and
`legality.rotate_local_bounds` use, with NO extra mirror for a B-side part:
the board file stores a B-side footprint's local coordinates already mirrored,
measured on sonde_u J2, orangecrab J4 and rp2350 J3.

Measured over the tracked corpus and the fa10 human boards
(`tests/measure_1155_mating_census.py`, fixture
`tests/fixtures/1155_mating_census.json`): every decisive receptacle faces
the edge it is seated on.
"""
import math
from typing import Dict, NamedTuple, Optional, Tuple

#: A body must sit at least this far (mm) off its pad row's centre to name a
#: face. The census's gap: 0.52 (the most offset part that has no face --
#: splitflap's JST-XH vertical headers, before the name rule) to 0.59 (the
#: least offset receptacle with one -- orangecrab J3's courtyard).
FACE_MIN_OFFSET_MM = 0.55
#: ...and by this factor more on the face axis than across it. The lowest
#: ratio among faced receptacles is 2.02 (coldfire J201's courtyard).
FACE_DOMINANCE = 1.5
#: A pose more than this far off the nearest cardinal angle names no edge.
CARDINAL_TOL_DEG = 30.0

FACES = ('+x', '-x', '+y', '-y')
_UNIT = {'+x': (1.0, 0.0), '-x': (-1.0, 0.0),
         '+y': (0.0, 1.0), '-y': (0.0, -1.0)}


class LocalFace(NamedTuple):
    """A footprint's mating face in its own UNROTATED frame."""
    face: Optional[str]          # '+x' | '-x' | '+y' | '-y', or None
    offset_mm: float             # the decisive offset (largest vote), 0 if none
    basis: str                   # 'courtyard+fab', 'silk', ...; '' if abstained
    reason: str                  # why it abstained; '' when it did not
    votes: Tuple = ()            # ((source, face_or_None, ox, oy), ...)


def _centre(b):
    return (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0


def _vote(body, pads):
    """`(face_or_None, ox, oy)` for one body bbox against the pad bbox."""
    bx, by = _centre(body)
    px, py = _centre(pads)
    ox, oy = bx - px, by - py
    major, minor = max(abs(ox), abs(oy)), min(abs(ox), abs(oy))
    if major < FACE_MIN_OFFSET_MM or major < FACE_DOMINANCE * minor:
        return None, ox, oy
    if abs(ox) >= abs(oy):
        return ('+x' if ox > 0 else '-x'), ox, oy
    return ('+y' if oy > 0 else '-y'), ox, oy


def is_vertical_name(fp) -> bool:
    """True when the footprint's ITEM name says it is a vertical mount.

    The item name, not the `lib:item` string: the text parser keeps the
    library prefix and the pcbnew path does not (`part_class` has the same
    rule for the same reason)."""
    name = str(getattr(fp, 'footprint_name', '') or '')
    item = name.rsplit(':', 1)[-1].lower()
    return 'vertical' in item and 'horizontal' not in item


def mating_face_local(fp, geom) -> LocalFace:
    """The mating face of one footprint, from its `body.BodyGeometry`."""
    if geom is None or getattr(geom, 'pads_local', None) is None:
        return LocalFace(None, 0.0, '', 'no pads to be offset from')
    if is_vertical_name(fp):
        return LocalFace(None, 0.0, '',
                         'a vertical mount (footprint name): it mates '
                         'perpendicular to the board')
    from .floorplan import is_board_copper_plug
    if is_board_copper_plug(fp):
        return LocalFace(None, 0.0, '',
                         'a board-copper plug: the board is the mating body')
    from .body import SOURCE_COURTYARD, SOURCE_FAB, SOURCE_SILK
    pads = geom.pads_local
    srcs = []
    if geom.source == SOURCE_COURTYARD and geom.body_local is not None:
        srcs.append(('courtyard', geom.body_local))
    if geom.drawn_source in (SOURCE_FAB, SOURCE_SILK) \
            and geom.drawn_local is not None:
        srcs.append((geom.drawn_source, geom.drawn_local))
    votes = tuple((name,) + _vote(b, pads) for name, b in srcs)
    decisive = [v for v in votes if v[1] is not None]
    if not decisive:
        return LocalFace(None, 0.0, '',
                         'symmetric about its pads' if votes
                         else 'no drawn body', votes)
    faces = sorted({v[1] for v in decisive})
    if len(faces) > 1:
        return LocalFace(None, 0.0, '', 'sources disagree: ' + ', '.join(
            '%s %s' % (v[0], v[1]) for v in decisive), votes)
    offset = max(max(abs(v[2]), abs(v[3])) for v in decisive)
    return LocalFace(faces[0], round(offset, 4),
                     '+'.join(v[0] for v in decisive), '', votes)


def facing_edge(face: Optional[str], rotation: float) -> Optional[str]:
    """The board edge (`west|north|east|south`) a local face points at.

    Board y grows southward. The transform is `connector_geometry.rect`'s:
    local (u, v) -> (cos r * u + sin r * v, -sin r * u + cos r * v)."""
    if face not in _UNIT:
        return None
    ux, uy = _UNIT[face]
    r = math.radians(rotation or 0.0)
    c, s = math.cos(r), math.sin(r)
    vx, vy = c * ux + s * uy, -s * ux + c * uy
    if max(abs(vx), abs(vy)) < math.cos(math.radians(CARDINAL_TOL_DEG)):
        return None
    if abs(vx) >= abs(vy):
        return 'east' if vx > 0 else 'west'
    return 'south' if vy > 0 else 'north'


def facing_rotations(face: Optional[str], edge: str) -> Tuple[float, ...]:
    """The quarter turns at which `face` points at `edge`, ascending."""
    return tuple(float(r) for r in (0, 90, 180, 270)
                 if facing_edge(face, r) == edge)


def board_faces(pcb_data, pcb_file: Optional[str] = None,
                bodies: Optional[Dict] = None) -> Dict[str, LocalFace]:
    """`{ref: LocalFace}` for every footprint `body.board_bodies` answers."""
    if bodies is None:
        from .body import board_bodies
        bodies = board_bodies(pcb_data, pcb_file)
    fps = getattr(pcb_data, 'footprints', None) or {}
    return {ref: mating_face_local(fps[ref], geom)
            for ref, geom in sorted(bodies.items()) if ref in fps}
