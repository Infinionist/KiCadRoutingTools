"""
Rigid block translation in the quench (issue #459).

The per-part nudge cannot express "these parts need to travel together": an IC
and its decoupling caps fight each other one at a time, because moving either
alone worsens the pair. A block move shifts the whole body by one offset.

The design constraint that makes this safe to add is the PER-MEMBER SEED CAP —
every member must still land within `max_displacement` of its own seed. That is
what keeps three existing guarantees true rather than merely probably-true:

  * `build_neighbor_lists`' exactness argument is stated per part ("a movable
    part's live position stays within travel_budget of its seed"), so the pruned
    neighbour lists stay EXACT, not lossy.
  * `BoardOutlineGate.edges_near` caches its reachable-edge list per ref on first
    call, sized by that budget — a block allowed to travel further would outrun
    the cache and silently skip the exact ring test, walking parts into cutouts.
  * `test_quench_swap_cap`'s no-stranding invariant keeps holding unmodified.

Lifting that cap is what makes #459's 80mm relocation a separate piece of work.
"""

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import parse_kicad_pcb
from placement.groups import derive_groups, parse_sources
from placement.quench import QuenchState, quench

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KF = os.path.join(ROOT, 'kicad_files')
KN = dict(clearance=0.25, board_edge_clearance=0.55, crossing_penalty=10.0,
          halo_base=0.3, halo_coef=0.15, halo_weight=1.0,
          edge_halo=1.0, edge_weight=1.0, grid_step=0.05)


def _board(n):
    return os.path.join(KF, n + '.kicad_pcb')


def _poses(placements):
    return {p['reference']: (round(p['new_x'], 6), round(p['new_y'], 6),
                             round(p['new_rotation'], 6)) for p in placements}


_GROUPED = {}


def _grouped(name):
    """`name`'s decap blocks quenched at step 0.5 for two passes:
    `(placements, metrics_out)`, cached per board. ONE quench serves the two
    cases that read it (the overlap case reads the placements, the objective
    case the metrics) -- they used to run it twice with identical arguments.
    `metrics_out` is filled after the pass loop and changes no move, and the
    quench is deterministic, so sharing it changes neither case's input."""
    if name not in _GROUPED:
        f = _board(name)
        blocks = derive_groups(parse_kicad_pcb(f), ('decap',))
        metrics = {}
        placements = quench(parse_kicad_pcb(f), pcb_file=f,
                            max_displacement=2.0, step=0.5, max_passes=2,
                            groups=blocks, metrics_out=metrics)
        _GROUPED[name] = (placements, metrics)
    return _GROUPED[name]


# --- inertness: the load-bearing claim ---------------------------------------

def test_no_groups_is_byte_identical():
    """With grouping off — the default — the group phase never runs and output
    must match the ungrouped engine exactly. This is what lets every existing
    bit-identity suite stay unmodified."""
    # `groups=None` IS the default, so quenching once without the argument
    # and once with `groups=None` was the same call made twice. The default
    # is asserted instead, which keeps the claim (the default behaves as
    # None) and still fails the day the default changes.
    import inspect
    default = inspect.signature(quench).parameters['groups'].default
    assert default is None, f"quench's groups default is now {default!r}"
    for b in ('interf_u_unrouted', 'splitflap_driver'):
        f = _board(b)
        a = quench(parse_kicad_pcb(f), pcb_file=f, max_displacement=2.0,
                   step=1.0, max_passes=2)
        c = quench(parse_kicad_pcb(f), pcb_file=f, max_displacement=2.0,
                   step=1.0, max_passes=2, groups={})
        assert _poses(a) == _poses(c), f"{b}: groups={{}} changed output"
    print("  PASS: groups off -> identical placements")


def test_a_single_member_block_is_dropped():
    """A block of one is not a block, and must not add a phase."""
    f = _board('splitflap_driver')
    a = quench(parse_kicad_pcb(f), pcb_file=f, max_displacement=2.0, step=1.0,
               max_passes=1)
    b = quench(parse_kicad_pcb(f), pcb_file=f, max_displacement=2.0, step=1.0,
               max_passes=1, groups={'solo': ['C1']})
    assert _poses(a) == _poses(b)


# --- the move ----------------------------------------------------------------

def test_block_translates_rigidly():
    """Relative geometry inside the block must be bit-identical afterwards —
    that is what 'rigid' means, and a per-member grid snap would shear it."""
    f = _board('splitflap_driver')
    pcb = parse_kicad_pcb(f)
    blocks = derive_groups(pcb, ('decap',))
    assert blocks, "fixture: expected decap blocks on splitflap"
    before = {r: (pcb.footprints[r].x, pcb.footprints[r].y)
              for refs in blocks.values() for r in refs}

    placements = quench(parse_kicad_pcb(f), pcb_file=f, max_displacement=2.0,
                        step=0.5, max_passes=1, allow_swaps=False,
                        allow_rotations=False, groups=blocks)
    after = _poses(placements)

    moved_blocks = 0
    for name, refs in blocks.items():
        deltas = set()
        for r in refs:
            if r not in after:
                continue
            deltas.add((round(after[r][0] - before[r][0], 6),
                        round(after[r][1] - before[r][1], 6)))
        # Every member that moved as part of the block shares ONE delta. The
        # per-part nudge may then polish individuals, so only require that a
        # single shared delta exists when the whole block moved together.
        if len(refs) > 1 and len(deltas) == 1 and deltas != {(0.0, 0.0)}:
            moved_blocks += 1
    assert moved_blocks >= 1, \
        f"no block translated rigidly (deltas per block: {moved_blocks})"
    print(f"  PASS: {moved_blocks} block(s) translated as one body")


def test_every_member_stays_within_its_own_seed_cap():
    """The invariant the whole design rests on."""
    f = _board('splitflap_driver')
    pcb = parse_kicad_pcb(f)
    seeds = {r: (fp.x, fp.y) for r, fp in pcb.footprints.items()}
    blocks = derive_groups(pcb, ('decap',))
    cap, grid = 2.0, 0.05
    placements = quench(parse_kicad_pcb(f), pcb_file=f, max_displacement=cap,
                        step=0.5, grid_step=grid, max_passes=3, groups=blocks)
    for p in placements:
        sx, sy = seeds[p['reference']]
        d = math.hypot(p['new_x'] - sx, p['new_y'] - sy)
        assert d <= cap + grid + 1e-6, \
            f"{p['reference']} stranded {d:.3f}mm from its seed (cap {cap})"
    print(f"  PASS: {len(placements)} parts, none beyond {cap}mm + grid")


def test_block_move_does_not_create_an_overlap():
    """Intra-group pairs are excluded from validity (their geometry is invariant
    under a translate), but FOREIGN collisions must still veto a candidate."""
    f = _board('splitflap_driver')
    placements, _m = _grouped('splitflap_driver')
    out = parse_kicad_pcb(f)
    for p in placements:
        fp = out.footprints[p['reference']]
        fp.x, fp.y, fp.rotation = p['new_x'], p['new_y'], p['new_rotation']
    st = QuenchState(out, f, **KN)
    m = st.legality_metrics()
    base = QuenchState(parse_kicad_pcb(f), f, **KN).legality_metrics()
    assert m['overlap_area'] <= base['overlap_area'] + 1e-6, \
        (f"block moves increased courtyard overlap "
         f"{base['overlap_area']:.3f} -> {m['overlap_area']:.3f}mm2")
    print(f"  PASS: overlap {base['overlap_area']:.3f} -> {m['overlap_area']:.3f}mm2")


def test_group_move_valid_excludes_intra_group_pairs():
    """Without the exclusion a block vetoes its own every candidate, because
    members routinely sit at sub-clearance courtyard gaps already."""
    f = _board('splitflap_driver')
    pcb = parse_kicad_pcb(f)
    blocks = derive_groups(pcb, ('decap',))
    st = QuenchState(pcb, f, **KN)
    st.build_neighbor_lists(2.05)
    name, refs = sorted(blocks.items())[0]
    # A zero move is always valid; the point is that it is not vetoed by members.
    assert st.group_move_valid(refs, 0.0, 0.0), \
        f"{name} vetoed its own current pose -- intra-group pairs leaked in"


# Grouped-vs-plain, measured on three boards after the decap tethers were
# corrected to follow the RAIL rather than ground (`groups._from_decap`):
#
#   splitflap_driver   plain  4693.8   grouped  4710.0   +0.345%
#   ulx3s              plain 40868.2   grouped 40655.6   -0.520%
#   watchy             plain  3223.9   grouped  3223.9    0.000%
#
# 3x the worst observed regression, so a real degradation trips it and a
# tie-break does not.
GROUPED_REGRESSION_BAND = 0.01


def test_block_moves_do_not_degrade_the_objective():
    """Grouping must not MATERIALLY worsen the final cost.

    This deliberately does NOT assert `grouped <= plain`. That stricter form was
    here first and it is a non-sequitur: "a block move is only accepted on
    strict improvement" is true, and it constrains each move -- but grouping
    changes WHICH candidates are explored and in what order, so the two runs are
    two different greedy trajectories and can settle in different local minima.
    Nothing makes the grouped one's minimum the lower of the two.

    It held until the decap tethers were corrected to match on the cap's RAIL
    instead of on ground. On splitflap_driver two caps then retethered to the IC
    that actually carries their rail -- C10 to U6 (+12V) and C11 to U7 (+3V3),
    both at *exactly* the same 6.84 mm as the ICs they used to follow, which do
    not carry those rails at all. Strictly better blocks, a different
    trajectory, and a 0.345% worse landing on that one small symmetric board.
    Restoring the strict form would mean preferring a wrong tether for a 16-unit
    cost artifact, so the band is the honest gate and the mechanism-level
    invariant below is the real one.
    """
    # splitflap_driver only: it is the small board, and it is also the WORST of
    # the three measured above, so it is the one that would catch a real
    # degradation. ulx3s takes minutes and belongs nowhere near --fast.
    for name in ('splitflap_driver',):
        f = _board(name)
        plain = {}
        quench(parse_kicad_pcb(f), pcb_file=f, max_displacement=2.0, step=0.5,
               max_passes=2, metrics_out=plain)
        _placements, grouped = _grouped(name)
        p, g = plain['after']['total'], grouped['after']['total']

        # The invariant that actually holds, and the one worth pinning: every
        # accepted move -- nudge, swap or block -- passes a strict-improvement
        # test, so a run's cost is monotonically non-increasing. A block move
        # that worsened its own run would break this and nothing else would.
        assert g <= grouped['before']['total'] + 1e-6, \
            f"{name}: the grouped run INCREASED its own cost: " \
            f"{grouped['before']['total']:.1f} -> {g:.1f}"

        assert g <= p * (1.0 + GROUPED_REGRESSION_BAND), \
            (f"{name}: grouping materially worse: {p:.1f} -> {g:.1f} "
             f"({100 * (g - p) / p:+.3f}%, band "
             f"{100 * GROUPED_REGRESSION_BAND:.1f}%)")
        print(f"  PASS: {name} total {p:.1f} (plain) -> {g:.1f} (grouped), "
              f"{100 * (g - p) / p:+.3f}%")


def test_locked_parts_are_never_in_a_moving_block():
    f = _board('splitflap_driver')
    pcb = parse_kicad_pcb(f)
    blocks = derive_groups(pcb, ('decap',))
    # Lock every block member; the block phase must then have nothing to move.
    locked = sorted({r for v in blocks.values() for r in v})
    placements = quench(parse_kicad_pcb(f), pcb_file=f, max_displacement=2.0,
                        step=0.5, max_passes=1, groups=blocks,
                        lock_refs=locked)
    moved = {p['reference'] for p in placements}
    assert not (moved & set(locked)), f"locked parts moved: {moved & set(locked)}"


TESTS = [
    test_no_groups_is_byte_identical,
    test_a_single_member_block_is_dropped,
    test_block_translates_rigidly,
    test_every_member_stays_within_its_own_seed_cap,
    test_block_move_does_not_create_an_overlap,
    test_group_move_valid_excludes_intra_group_pairs,
    test_block_moves_do_not_degrade_the_objective,
    test_locked_parts_are_never_in_a_moving_block,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
