"""Placement blocks: which footprints should move as one rigid body (#459).

A local search fixes what it can reach — individual parts within a few mm.
The failures that sink a board are block-scale: a magnetics block 80mm from its
endpoints, unroutable because of the floorplan rather than the routing. Before
anything can *move* a block, something has to know what a block IS, and nothing
in this repo did.

Four sources, in precedence order, first match wins per footprint. Each is
reported with its source so a user can see what was inferred before trusting it.

    kicad      KiCad (group ...) blocks -- the designer's own statement that
               these parts belong together, so it outranks everything inferred.
    sheet      Schematic sheet path. A sheet IS the functional block a floorplan
               thinks in, and it is declarative rather than guessed.
    netprefix  Net-name prefix (AUDIO_*, GPDI_*), gated on spatial coherence.
    decap      Two-pad capacitors tethered to the IC they decouple.

CORPUS EVIDENCE (measured across kicad_files/, not assumed):

  kicad      0 of 27 boards carry a single (group ...) block. Exact when present,
             but no in-repo board exercises it end to end.
  sheet      12 of 22 boards with (path ...) have more than one sheet. ulx3s: 11
             sheets sized 83/34/23/20/20/12 footprints. glasgow_revC: 4, nested.
             This is the workhorse.
  decap      Median distance from a 2-pad cap to the nearest IC bbox is 0.0-2.6mm
             across 8 boards, and the cap shares a net with that IC in 93-100% of
             cases (e.g. glasgow 91/92, orangecrab 75/75, ulx3s 57/70). The
             net-sharing check is what makes this safe: it is what separates "a
             cap that decouples this IC" from "a cap that happens to sit nearby",
             and on ulx3s it correctly rejects 13 of 70.
  netprefix  The WEAKEST source, and it is worth saying so. Raw prefixes are
             dominated by KiCad's auto-generated Net-(U1-Pad) names -- 21 to 92
             refs per board under a bogus "NET" prefix, spread over 16-75mm.
             With auto-generated and power names excluded and a 20mm coherence
             gate, what remains is small but genuine: ulx3s 9 blocks / 63 refs
             (AUDIO, GPDI, JTAG, LED, FPGA), orangecrab 13/80, glasgow 3/20,
             rp2350 1 block of 3, tigard 0. Enable it expecting little.

All output is sorted. Anything whose order can reach the optimizer must be
deterministic, or placement output stops being reproducible across processes
(#457).
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Set, Tuple

# --- decap tethering -------------------------------------------------------
# 5mm from the IC's bounding box. The corpus median is 0.0-2.6mm and <5mm
# captures essentially every real decoupling cap; kit-dev (a large, loosely
# packed board) is the only one needing the full 5mm for 41 of its 53.
DECAP_RADIUS_MM = 5.0
DECAP_MIN_IC_PADS = 4          # what chip_boundary already calls "not a passive"

# --- netprefix -------------------------------------------------------------
# KiCad's auto-generated names carry no functional meaning: Net-(U1-Pad3),
# unconnected-(U2-Pad7). Bucketing on them produces one enormous "NET" pseudo
# block per board (up to 92 refs spread over 75mm), which is exactly the kind of
# wrong group that is worse than no group at all -- it would rigidly drag half
# the board.
_AUTO_NET = re.compile(r'^(net-\(|unconnected-\(|net\d)', re.I)
# Power and ground reach everywhere by design, so they say nothing about blocks.
# The pattern MOVED to `net_queries.is_power_net_name` (#705): the pin rule's
# rail-net fallback asks the same question, and a second copy of a predicate
# this tree already spells nine ways is exactly what it keeps paying for.
# Imported in the function body, matching this module's own idiom for a
# `py_router` sibling (see `decap_tethers`) -- a module-level import would need
# `py_router` on `sys.path` before `placement` is imported, which several test
# files do not arrange.
#
# It does EXTEND that requirement to `_from_netprefix`, which had no py_router
# dependency before: with only `py_placer` on the path it now raises
# ModuleNotFoundError where it used to work. Every in-tree caller is safe --
# `kicad_parser` also lives in `py_router`, so holding a real `PCBData` already
# implies it, and `swig_gui` inserts `py_router` first -- but the constraint is
# now wider than the sentence above, and saying so is cheaper than a reader
# rediscovering it.
_PREFIX = re.compile(r'^([A-Za-z][A-Za-z0-9]+)[_\-]')
NETPREFIX_MIN_REFS = 3
# Max distance from the cluster centroid to any member. A prefix scattered wider
# than this is a naming convention, not a physical block.
NETPREFIX_MAX_SPREAD_MM = 20.0

MIN_GROUP_SIZE = 2             # a block of one is not a block

SOURCES = ('kicad', 'sheet', 'netprefix', 'decap')
AUTO_SOURCES = ('kicad', 'sheet')   # the declarative ones


class GroupError(ValueError):
    """An unknown --group-by source."""


def parse_sources(spec: Optional[str]) -> Tuple[str, ...]:
    """'none' | 'auto' | comma list -> the ordered source tuple."""
    if not spec or spec.strip().lower() in ('', 'none'):
        return ()
    out: List[str] = []
    for tok in spec.split(','):
        tok = tok.strip().lower()
        if not tok:
            continue
        if tok == 'auto':
            out.extend(s for s in AUTO_SOURCES if s not in out)
            continue
        if tok not in SOURCES:
            raise GroupError(
                f"unknown group source {tok!r}; choose from "
                f"{', '.join(SOURCES)}, or 'auto' ({'+'.join(AUTO_SOURCES)}), "
                f"or 'none'")
        if tok not in out:
            out.append(tok)
    return tuple(out)


def _pads(fp) -> list:
    """`fp`'s pads without its aperture-only ones (#1143): a paste or mask
    window is not a pin, so it moves neither a centroid, a row test nor a
    chip's bounds (tigard J1's 8 paste windows put C25 2.43 mm from it where
    its copper is 2.93 mm away)."""
    from kicad_parser import non_aperture_pads
    return non_aperture_pads(fp)


def _pads_view(pcb_data):
    """`pcb_data` as `chip_boundary.build_chip_list` reads it -- each
    footprint's `.pads` only -- with the aperture-only pads dropped. The
    router's own callers of `build_chip_list` are untouched."""
    from types import SimpleNamespace
    return SimpleNamespace(footprints={
        r: SimpleNamespace(pads=_pads(f))
        for r, f in (pcb_data.footprints or {}).items()})


def _centroid(fp) -> Tuple[float, float]:
    pts = [(p.global_x, p.global_y) for p in _pads(fp)]
    if not pts:
        return (fp.x, fp.y)
    return (sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts))


def sheet_and_symbol(fp) -> Tuple[str, str]:
    """`(sheet prefix, symbol uuid)` of a footprint's (path ...).

    The prefix is every uuid but the last (`_sheet_of`); the LAST is the
    schematic symbol. Two instances of one hierarchical sheet carry the SAME
    symbol uuid under different prefixes -- multichannel_mixer's R4, R15, R26
    and R37 all end in one uuid -- which is the correspondence KiCad's own
    "Repeat Layout" uses and `replicate` maps by (#1211). A footprint with no
    path yields ('', '')."""
    parts = [q for q in (fp.sheet_path or '').split('/') if q]
    return '/'.join(parts[:-1]), (parts[-1] if parts else '')


def _sheet_of(fp) -> str:
    """The sheet prefix of a footprint's (path ...): everything but the LAST
    uuid, which is the symbol itself. A bare single-uuid path is top level and
    yields '' -- those must not all collapse into one bogus block."""
    return sheet_and_symbol(fp)[0]


def _from_kicad(pcb_data, movable) -> Dict[str, List[str]]:
    return {f"kicad:{name}": [r for r in refs if r in movable]
            for name, refs in (pcb_data.groups or {}).items()}


def _from_sheet(pcb_data, movable) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for ref in movable:
        fp = pcb_data.footprints.get(ref)
        if fp is None:
            continue
        sheet = _sheet_of(fp)
        if not sheet:          # top level: not a block
            continue
        out.setdefault(f"sheet:{sheet}", []).append(ref)
    return out


def _from_netprefix(pcb_data, movable) -> Dict[str, List[str]]:
    from net_queries import is_power_net_name
    buckets: Dict[str, Set[str]] = {}
    for net_id, net in (pcb_data.nets or {}).items():
        if net_id <= 0:
            continue
        leaf = (net.name or '').split('/')[-1]
        if not leaf or _AUTO_NET.match(leaf) or is_power_net_name(leaf):
            continue
        m = _PREFIX.match(leaf)
        if not m:
            continue
        for pad in net.pads:
            if pad.component_ref in movable:
                buckets.setdefault(m.group(1).upper(), set()).add(pad.component_ref)

    out: Dict[str, List[str]] = {}
    for prefix, refs in buckets.items():
        if len(refs) < NETPREFIX_MIN_REFS:
            continue
        pts = [_centroid(pcb_data.footprints[r]) for r in refs
               if r in pcb_data.footprints]
        if len(pts) < NETPREFIX_MIN_REFS:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        # Spatial coherence: a prefix whose parts are scattered across the board
        # is a naming convention, not something that can move as one body.
        if max(math.hypot(p[0] - cx, p[1] - cy) for p in pts) > NETPREFIX_MAX_SPREAD_MM:
            continue
        out[f"net:{prefix}"] = sorted(refs)
    return out


def _net_name(pcb_data, net_id: int) -> str:
    """A net's name, or '' -- pcb_data.nets is keyed by id and may not have it."""
    n = pcb_data.nets.get(net_id)
    return getattr(n, 'name', '') or ''


def _pads_are_collinear(fp, eps: float = 1e-6) -> bool:
    """True when every pad of `fp` sits on one line -- a 1xN header, a pin
    strip, a castellated edge row.

    `build_chip_list` qualifies a footprint as a "chip" on PAD COUNT ALONE, so a
    1x10 castellated breakout row (10 pads) is an IC as far as it is concerned.
    That row spans a whole board edge and carries a rail, so it is nearer to
    half the decoupling caps than their real IC is and it passes the shared-net
    test -- it captures them. Measured on one board it captured three, and the grader
    then reported "C12 is 3.30mm from CN2, the IC it decouples".

    The false positive is only noise. The FALSE NEGATIVE is the reason this
    filter exists: a cap 2mm from the edge row and 8mm from the part it actually
    decouples grades CLEAN, because the tether measured to the row. That is a
    HARD spec limit (100nF within 3mm of every VDD pin) silently passing.

    Collinearity is the discriminator that needs no new parser field: at >= 4
    pads a real IC always has two-dimensional pad extent (SOIC, SOT-223, QFN,
    a 4-pad crystal), and a single row never does. `attr exclude_from_bom` would
    be the more direct signal, but the parser does not carry footprint attrs and
    adding one means touching both parse paths.
    """
    pads = _pads(fp) if fp is not None else ()
    if not pads:
        return False
    xs = {round(p.global_x, 4) for p in pads}
    ys = {round(p.global_y, 4) for p in pads}
    return len(xs) <= 1 or len(ys) <= 1


def _copper_pads(fp) -> int:
    """How many of a footprint's pads carry copper.

    `chip_boundary.build_chip_list` gates on RAW pad count, and a KiCad 0201
    footprint carries two solder-paste apertures beside its two copper pads --
    so a two-terminal passive reaches four and is called a chip. The
    collinearity guard does not save it: at a non-orthogonal angle its two
    copper pads share neither an x nor a y, so the row test says "not a row".
    Measured, exactly two parts corpus-wide reach the chip list this way, both
    on rp2350: `C28` (Capacitor_SMD:C_0201_0603Metric at -45 degrees) and `R9`
    (the resistor of the same body at the same angle). `orangecrab_ext_pll C1`
    is the same four-pad shape and escapes only because it sits at 90 degrees,
    where the x coordinates collapse and the row test catches it -- so the old
    behaviour was rotation-dependent, which is not a property anyone chose.

    Found by review, and it was not theoretical: the pin rule graded C28 as an
    IC needing decoupling and reported `C28 pin 1 (/RP2354A/1V1) is 6.11mm from
    C30`, i.e. a capacitor flagged for being far from another capacitor.
    """
    if fp is None or not fp.pads:
        return 0
    return sum(1 for p in fp.pads
               if any(str(l).endswith('.Cu') for l in (p.layers or ())))


def _ic_list(pcb_data):
    """The ChipBoundary objects of the parts that are ICs by SHAPE: >= 4
    copper pads, not a row. `_chip_list` adds the supply owners to them."""
    from chip_boundary import build_chip_list
    return [c for c in build_chip_list(_pads_view(pcb_data),
                                       min_pads=DECAP_MIN_IC_PADS)
            if not _pads_are_collinear(pcb_data.footprints.get(c.reference))
            and _copper_pads(pcb_data.footprints.get(c.reference))
            >= DECAP_MIN_IC_PADS]


#: A supply owner that is not an IC by shape has at least this many copper
#: pads: a regulator has an input, an output and a return (#1249). A
#: two-pad part typed `power_out` (a battery holder in some libraries) is a
#: source, not something a cap is placed to decouple.
SUPPLY_OWNER_MIN_PADS = 3

#: The roles `infer_role` may give a part the three-pad rule admits: an IC,
#: a regulator, or nothing the table knows. Any other role -- a transistor,
#: a feed-through capacitor, a three-pin header -- is a part whose two capped
#: nets do not make it the thing the caps decouple.
_THREE_PAD_ROLES = ('integrated circuit', 'regulator', 'unknown')


def _capped_nets(pcb_data) -> Set[int]:
    """Every net a decoupling cap (`is_decoupling_cap`) carries."""
    out: Set[int] = set()
    for ref, fp in (pcb_data.footprints or {}).items():
        if is_decoupling_cap(fp, ref):
            out.update(p.net_id for p in fp.pads if p.net_id > 0)
    return out


def supply_owner_evidence(pcb_data, ref: str,
                          capped: Optional[Set[int]] = None
                          ) -> Optional[str]:
    """Why `ref` owns decoupling caps although it is not an IC by shape, or
    None (#1249).

    `DECAP_MIN_IC_PADS` is four, so a three-pad regulator (SOT-89, TO-92)
    was never a chip: its input and output caps were elected to whatever
    other part carried the rail, and the grade called that part "the IC it
    decouples" (esp_prog C3 -> U1 at 2.125 mm, its own VOUT pin 1.12 mm
    away; complex_hierarchy C102 -> U102 at 35.08 mm). A part with at least
    `SUPPLY_OWNER_MIN_PADS` copper pads and no `part_class` (not a
    connector, switch, hole or marker) is an owner when ANY of:

    * a pad is typed `power_out` -- the symbol says it sources a rail
      (complex_hierarchy U101, a 78L05);
    * `roles.infer_role` calls it a `regulator` -- the one table
      `board_context` prints from (esp_prog U2, an ME6210 in SOT-89, which
      carries no pintype at all);
    * it has exactly three copper pads on three nets, one of them ground,
      and each of the other two carries a decoupling cap, and its role is
      an IC, a regulator or unknown (`_THREE_PAD_ROLES`). esp_prog U2 and
      complex_hierarchy U101 both meet it.

    No single signal covers both of those boards, which is why there are
    three. THE KNOWN CASE THE THIRD ADMITS THAT IS NOT A REGULATOR: kit-dev
    LV101, a DS1818 reset supervisor in TO-92 whose VCC (`+3.3V`) and RST
    nets each carry a cap. Admitting it changes 0 of that board's 53
    elections (every cap on those nets is nearer another chip). The shape
    says the part OWNS caps, not which of its pins are supply pins, so
    `floorplan.supply_pins` reads its pins by role only for the first two
    signals (`regulator_owners`); LV101 keeps the ordinary pin ladder, which
    takes its +3.3V pin and not its RST.
    """
    fp = (pcb_data.footprints or {}).get(ref)
    if fp is None:
        return None
    from .roles import _copper_pads as copper, infer_role, power_out_pads
    pads = copper(fp)
    if len(pads) < SUPPLY_OWNER_MIN_PADS:
        return None
    from .part_class import classify_part
    cls = classify_part(fp, ref).name
    if cls is not None:
        return None
    po = power_out_pads(fp)
    if po:
        return f"pad {po[0].pad_number} is typed power_out"
    role = infer_role(fp, ref, cls)
    if role['role'] == 'regulator':
        return 'role regulator (' + '; '.join(role['evidence']) + ')'
    if len(pads) != 3 or role['role'] not in _THREE_PAD_ROLES:
        return None
    nets = [p.net_id for p in pads]
    if min(nets) <= 0 or len(set(nets)) != 3:
        return None
    from net_queries import is_ground_net_name
    gnd = [n for n in nets if is_ground_net_name(_net_name(pcb_data, n))]
    if len(gnd) != 1:
        return None
    if capped is None:
        capped = _capped_nets(pcb_data)
    if not all(n in capped for n in nets if n not in gnd):
        return None
    return THREE_PAD_EVIDENCE


#: `supply_owner_evidence`'s answer for an owner the three-pad shape alone
#: admits -- the one kind `regulator_owners` leaves out.
THREE_PAD_EVIDENCE = ('3 copper pads: one ground net and two nets that each '
                      'carry a decoupling cap')


def supply_owners(pcb_data, ics=None) -> Dict[str, str]:
    """`{ref: evidence}` -- the cap owners that are not ICs by shape
    (`supply_owner_evidence`), ref-sorted. `ics` is the set to skip
    (default: `_ic_list`'s)."""
    if ics is None:
        ics = {c.reference for c in _ic_list(pcb_data)}
    capped = None
    out: Dict[str, str] = {}
    for ref, fp in sorted((pcb_data.footprints or {}).items()):
        # The pad count first: it is the cheap test, and it rejects every
        # two-pad passive before the classifier is asked.
        if ref in ics or _copper_pads(fp) < SUPPLY_OWNER_MIN_PADS:
            continue
        if capped is None:
            capped = _capped_nets(pcb_data)
        ev = supply_owner_evidence(pcb_data, ref, capped)
        if ev is not None:
            out[ref] = ev
    return out


def regulator_owners(pcb_data, ics=None) -> Dict[str, str]:
    """The supply owners a pin or the role table calls a regulator -- a
    `power_out` pad or the `regulator` role -- with their evidence; the
    three-pad shape alone does not say which pins are supply pins
    (`supply_owner_evidence`'s LV101). `floorplan.supply_pins` reads these
    by role."""
    return {r: ev for r, ev in supply_owners(pcb_data, ics=ics).items()
            if ev != THREE_PAD_EVIDENCE}


def _chip_list(pcb_data):
    """The ChipBoundary objects this module calls a cap's OWNER -- an IC by
    shape (`_ic_list`) or a supply owner (`supply_owners`, #1249). ONE
    answer, one place: `chip_refs`, `_elect_tethers`, `rail_chips` and
    through them the grade, the tether gate and `floorplan.supply_pins`
    all read it. In the footprints'
    own order, so the election's first-strict-minimum tie-break is the
    one it always was."""
    ics = _ic_list(pcb_data)
    have = {c.reference for c in ics}
    owners = supply_owners(pcb_data, ics=have)
    if not owners:
        return ics
    from chip_boundary import build_chip_list
    keep = have | set(owners)
    return [c for c in build_chip_list(_pads_view(pcb_data), min_pads=1)
            if c.reference in keep]


def chip_refs(pcb_data) -> Set[str]:
    """The refs this module calls a cap's owner: an IC (>= 4 COPPER pads,
    and not a row) or a supply owner (`supply_owners`, #1249).

    Public since #792, because a second reader was answering the same
    question with `owner[0] == 'U'` -- a second answer to the problem
    `_pads_are_collinear`
    was written for, and the one without the measurement behind it. One
    question, one answer, and this is the one that carries its scar.
    """
    return {c.reference for c in _chip_list(pcb_data)}


def is_decoupling_cap(fp, ref: str) -> bool:
    """The syntactic half of "is this a decap": a `C*` bridging exactly two nets.

    TWO DISTINCT NETS, not two net-bearing PADS, and case-INSENSITIVE. Both
    differences were spelled three ways across this tree before #792 (here,
    in a placement scope, and in `emit_intent`'s census) and all three produce
    identical sets on every tracked board -- so the choice is argued, not
    measured:

    * a 2-pad part with both pads on ONE net is a zero-ohm link, not a decap
      (pad-count says yes, distinct-nets says no);
    * a 3-pad cap with a tied thermal pad IS a decap (pad-count says no).

    Both differences favour distinct-nets. `ref[:1]` rather than `ref[0]` also
    survives an empty reference instead of raising.
    """
    if fp is None or ref[:1].upper() != 'C':
        return False
    return len({p.net_id for p in fp.pads if p.net_id > 0}) == 2


def _elect_tethers(pcb_data, movable=None):
    """[(cap, ic|None, distance|None)] in sorted cap order -- the ONE election.

    **The radius never reaches here, and that is the entire point.** It used to
    live at the end of `decap_tethers` as a prune on the winner, which meant
    that asking the same question at two radii ran the election twice and made
    "the wider answer contains the narrower one" a per-board coincidence that a
    test had to re-verify. `decap_census`'s own comment asserted the opposite --
    that the unbounded pass "can pick a DIFFERENT chip, so this is a second
    measurement rather than a superset" -- and that was never true of this code:
    `radius` appeared only after the argmin, and `chips` and `ic_nets` are built
    before any cap is considered. Measured over the tracked corpus, 0 of 357
    tethers re-elect. With one election it is a property of the code shape
    instead of a fact about a corpus.

    `ic is None` means NO chip carries this cap's rail -- a bulk or filter cap
    upstream of an LC network, not a decoupler. Ten of ulx3s's caps are that,
    and they are why "a decap by its rail" and "graded against an IC" are two
    different questions rather than two spellings of one (#792).
    """
    # THE SAME chip list `chip_refs` publishes, so an owner test and the
    # grader's tether election cannot answer "what is an
    # IC" differently -- which is the whole of #792.
    chips = _chip_list(pcb_data)
    if movable is None:
        movable = set(pcb_data.footprints)
    ic_nets = {}
    for c in chips:
        fp = pcb_data.footprints.get(c.reference)
        if fp is not None:
            ic_nets[c.reference] = {p.net_id for p in fp.pads if p.net_id > 0}

    out: List[Tuple[str, Optional[str], Optional[float]]] = []
    for ref in sorted(movable):
        fp = pcb_data.footprints.get(ref)
        if not is_decoupling_cap(fp, ref):
            continue
        power = _cap_power_nets(pcb_data, fp)
        # Nearest chip THAT CARRIES THE RAIL, not nearest-then-reject.
        # Rejecting after the fact dropped the cap entirely whenever some
        # unrelated part happened to be closer, so a genuinely distant decap
        # went ungraded instead of flagged.
        best, best_d = nearest_chip(
            _centroid(fp),
            ((c.reference, c.bounds) for c in chips
             if c.reference != ref
             and power & ic_nets.get(c.reference, set())))
        # A second `nets & ic_nets[best]` recheck used to stand here, commented
        # "near, but not electrically its cap". It was UNREACHABLE: `power` is a
        # subset of `nets`, and `best` is only ever assigned inside the branch
        # that already required `power & ic_nets[c]` to be non-empty. Deleted
        # rather than kept, because a guard that cannot execute is worse than an
        # absent one -- it reads like protection, and its comment described an
        # ordering (proximity first, net-sharing second) that is the opposite of
        # what the loop above does.
        out.append((ref, best, best_d))
    return out


def _cap_power_nets(pcb_data, fp) -> Set[int]:
    """The nets a cap is tethered ON. Match on the POWER net, not on ground. A
    decoupling cap bridges a rail and GND, and GND is shared with nearly every
    part on the board -- so matching on "shares a net" lets the nearest 4-pad
    part win on ground alone. Measured, that tethered a rail decap to the
    CRYSTAL and the flash's own C2 to the USB connector, and the reported
    distance was then to the wrong part in both directions."""
    from net_queries import is_ground_net_name
    nets = {p.net_id for p in fp.pads if p.net_id > 0}
    return {n for n in nets
            if not is_ground_net_name(_net_name(pcb_data, n))} or nets


def nearest_chip(pt, candidates) -> Tuple[Optional[str], Optional[float]]:
    """`(ref, distance)` -- the election's argmin over `(ref, bounds)`
    candidates in the order given (the first strict minimum wins), or
    `(None, None)`. `_elect_tethers` calls it over the chips carrying a cap's
    rail; the #1043 quench gate calls it over the same chips at LIVE poses,
    so the pair it holds is the pair the grade will elect."""
    best, best_d = None, None
    for ref, bounds in candidates:
        d = _point_to_bounds(pt, bounds)
        if best_d is None or d < best_d:
            best, best_d = ref, d
    return best, best_d


def rail_chips(pcb_data, cap: str) -> List[str]:
    """The chips `_elect_tethers` would consider for `cap`, in its order:
    every chip of `_chip_list` (bar the cap) carrying the cap's power rail.
    Pose-invariant for a quench (membership reads pad counts, nets, and the
    collinearity of a chip's pads, which a 90-degree move keeps; a supply
    owner's evidence -- pintypes, footprint name, nets -- reads no pose)."""
    fp = pcb_data.footprints.get(cap)
    if fp is None:
        return []
    power = _cap_power_nets(pcb_data, fp)
    out = []
    for c in _chip_list(pcb_data):
        if c.reference == cap:
            continue
        cfp = pcb_data.footprints.get(c.reference)
        if cfp is not None and power & {p.net_id for p in cfp.pads
                                        if p.net_id > 0}:
            out.append(c.reference)
    return out


def _point_to_bounds(pt, bounds) -> float:
    """The election's distance: a point to a chip's pad bbox (0 inside)."""
    cx, cy = pt
    x0, y0, x1, y1 = bounds
    return math.hypot(max(x0 - cx, cx - x1, 0.0), max(y0 - cy, cy - y1, 0.0))


def chip_bounds_of(fp):
    """The bounds `_chip_list` gives this ONE footprint at the pose it holds,
    or None when it has no pads. Through `build_chip_list` itself, so a
    caller measuring a footprint the election has not seen (#1043: the
    quench, at a pose it is only considering) reads the election's margin."""
    from types import SimpleNamespace
    from chip_boundary import build_chip_list
    got = build_chip_list(SimpleNamespace(footprints={
        '_': SimpleNamespace(pads=_pads(fp))}), min_pads=1)
    return got[0].bounds if got else None


def elect_live(cap_fp, candidates) -> Tuple[Optional[str], Optional[float]]:
    """`_elect_tethers`' election for ONE cap with the footprints at whatever
    poses they hold: the cap's pad centroid against `(ref, bounds)` of the
    chips on its rail (`rail_chips`' order, bounds from `chip_bounds_of`),
    nearest wins. The #1043 quench gate calls this per candidate pose, so
    it holds the pair the GRADE will elect -- a cap that walks toward a
    different chip on its rail is measured against that chip."""
    return nearest_chip(_centroid(cap_fp), candidates)


def decap_tethers(pcb_data, movable=None,
                  radius: float = DECAP_RADIUS_MM
                  ) -> Dict[str, List[Tuple[str, float]]]:
    """{IC ref: [(cap ref, bbox distance mm)]} -- the decoupling tethers.

    Nearest IC by bounding-box distance, but ONLY when the cap shares a net with
    it. Proximity alone is not enough -- on ulx3s 13 of 70 caps sit near an IC
    they have no electrical relationship with, and dragging those along would be
    a wrong group.

    Public, and returning the DISTANCE, because two consumers need the same
    tether and must not each decide what "near its IC" means: `_from_decap`
    wants the membership, and the floorplan grader's `decap_distance` rule
    (#549) wants the number. Grading the quench's own decap rule against a
    second implementation of it would measure the reimplementation, not the
    board.

    `movable` restricts which caps are considered (None = every footprint), as
    in `derive_groups`. Iteration is over a SORTED ref list: `movable` reaches
    us as a set, and cap order within an IC's list would otherwise vary with
    PYTHONHASHSEED (#457).

    Since #794 this is a thin filter over `_elect_tethers`. **Signature and
    result are unchanged, deliberately**: THREE callers depend on both --
    `_from_decap`, `reseat.clusters_from_tethers` and `board_brief` -- and
    `_from_decap` is what `--group-by decap` resolves through on BOTH the CLI
    and the GUI, so a change here is a change to grouping on two front ends.
    (`rule_decap_distance` and `decap_census` used to be a fourth and fifth;
    they now reach the election through `decap_populations`. An earlier draft
    of this line said six, which was the count before that refactor.)
    """
    out: Dict[str, List[Tuple[str, float]]] = {}
    for cap, ic, d in _elect_tethers(pcb_data, movable):
        if ic is not None and d <= radius:
            out.setdefault(ic, []).append((cap, d))
    return out


def decap_populations(pcb_data, movable=None,
                      radius: float = DECAP_RADIUS_MM):
    """(near, beyond, orphans) -- the in-scope caps, PARTITIONED, from one pass.

    * `near`    {ic: [(cap, d)]} -- byte-identical to `decap_tethers(radius)`;
                the population `rule_decap_distance` grades.
    * `beyond`  [(cap, ic, d)] cap-sorted -- elected, then pruned by the radius.
                Invisible to the grader before #794; `rule_decap_ungraded`
                reports them.
    * `orphans` [cap] -- in scope, but NO chip carries their rail. There is no
                IC for them to be far from, so the grader is right to ignore
                them; the SEEDER is not, which is #792.

    The three sets partition the in-scope population, and `decap_census` emits
    an `unaccounted` key whose only correct value is 0 so that stays a measured
    fact rather than a claim.
    """
    near: Dict[str, List[Tuple[str, float]]] = {}
    beyond: List[Tuple[str, str, float]] = []
    orphans: List[str] = []
    for cap, ic, d in _elect_tethers(pcb_data, movable):
        if ic is None:
            orphans.append(cap)
        elif d <= radius:
            near.setdefault(ic, []).append((cap, d))
        else:
            beyond.append((cap, ic, d))
    return near, beyond, orphans


def _from_decap(pcb_data, movable) -> Dict[str, List[str]]:
    """Two-pad caps tethered to the IC they decouple -- membership only.

    The tether itself (and its distance) is `decap_tethers`; this is the
    grouping view of it.
    """
    out: Dict[str, List[str]] = {}
    for ic, caps in decap_tethers(pcb_data, movable).items():
        refs = [cap for cap, _d in caps]
        # The IC itself anchors the tether, so it belongs to the block.
        if ic in movable:
            refs.append(ic)
        out[f"decap:{ic}"] = refs
    return out


_BUILDERS = {'kicad': _from_kicad, 'sheet': _from_sheet,
             'netprefix': _from_netprefix, 'decap': _from_decap}


def derive_groups(pcb_data, sources, movable=None) -> Dict[str, List[str]]:
    """{group name: sorted [reference]} for the requested sources.

    `movable` restricts membership to refs the caller is willing to move (locked
    parts, or the loop's target set); None means every footprint.

    Precedence is the order of `sources`, and a ref lands in at most ONE group:
    an explicit KiCad group must not be torn apart by a later sheet or prefix
    rule. Groups smaller than MIN_GROUP_SIZE after that are dropped.
    """
    if not sources:
        return {}
    if movable is None:
        movable = set(pcb_data.footprints)
    else:
        movable = set(movable)

    claimed: Set[str] = set()
    out: Dict[str, List[str]] = {}
    for src in sources:
        builder = _BUILDERS.get(src)
        if builder is None:
            raise GroupError(f"unknown group source {src!r}")
        for name, refs in sorted(builder(pcb_data, movable).items()):
            members = sorted({r for r in refs if r not in claimed})
            if len(members) < MIN_GROUP_SIZE:
                continue
            out[name] = members
            claimed.update(members)
    return out


def short_name(name: str) -> str:
    """Readable form of a group key. Sheet keys are uuid paths -- KiCad's
    Sheetname property is absent from every corpus board, so the uuid is all
    there is; show its TAIL rather than 36 meaningless characters.

    Public because it is what the user SEES: `--list-groups` prints this form,
    so `route.py --group` has to accept it back, and both need the same rule.
    """
    src, _, rest = name.partition(':')
    if src != 'sheet':
        return name
    return 'sheet:' + '/'.join(p[-8:] for p in rest.split('/') if p)


def describe(groups: Dict[str, List[str]], limit: int = 8) -> str:
    """One-line-per-block summary, for the run banner."""
    if not groups:
        return "  placement groups: none"
    lines = [f"  placement groups: {len(groups)} block(s), "
             f"{sum(len(v) for v in groups.values())} part(s)"]
    for name, refs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]:
        shown = ', '.join(refs[:6]) + (', ...' if len(refs) > 6 else '')
        lines.append(f"    {short_name(name)}  ({len(refs)}): {shown}")
    if len(groups) > limit:
        lines.append(f"    ... and {len(groups) - limit} more")
    return '\n'.join(lines)
