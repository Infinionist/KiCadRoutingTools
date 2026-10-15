#!/usr/bin/env python3
"""#1127: the licence census -- what an OFF run admits and refuses that the
licence (L) and exact (FULL) gates would decide differently.

Pre-registered in tests/1127_stack_ab_prereg.json (pinned by
tests/test_1127_prereg.py), which this script reads its boards from.

The stack conjunct of `LegalityContext.pads_ok` is seed-relative, and in BOX
mode `seed_baseline` records a box-only near-touch at the seeds as a stack.
That LICENSES a later candidate whose pads really stack with that neighbour:
OFF admits it. #1127 is the other direction: the box refuses near-touches the
exact check calls clean. The census measures both on the OFF trajectory,
without changing it:

* L-HOLE -- a `pads_ok` call OFF admits and L refuses. L's stack conjunct is
  OFF's, OR (box-only licence at the seeds AND an exact stack now);
* C-FLIP -- a call OFF refuses and FULL admits. FULL is the switch,
  `STACK_EXACT_CONFIRM`: its stack conjunct is exact(cur) and not
  exact(base), and its PAD conjunct (`pad`, `pad_overlap`) is check_drc's
  pad-pad answer on the outlines, against a baseline measured the same way.
  Also counted at ANY `pair_shortfall` level, for the consumers that read a
  pair without a baseline, per conjunct: a box stack the outlines clear
  (`c_flip_any`), and a box pad shortfall or overlap they clear
  (`c_flip_any_pad`).

FULL confirms BOTH conjuncts because it has to be the switch. The
trial-cell rule says a cell with no C-flip gives identical outputs in every
arm, and the A/B's FULL arm sets `STACK_EXACT_CONFIRM`, which confirms the
pad conjunct too since it was extended. A FULL that confirmed the stack
alone would call a cell identical where the pad conjunct alone changes a
decision. `Shim(pad_exact=False)` is the stack-only FULL the prereg
measured (#1144's toggle); control 1 runs it to keep the prereg's pinned
numbers.

The SHIM makes this possible on an OFF run:
- It wraps `PartPads.__init__` to take the footprint snapshot whatever
  `STACK_EXACT_CONFIRM` says. Under OFF no snapshot is taken, so
  `_stack_confirmed` and `_pad_confirmed` would return the box answer and
  every count would read 0.
- It re-evaluates every `pads_ok` call per neighbour in the three modes.
- It computes exact answers by calling the real `pair_shortfall` with the
  context's `stack_exact` and `pad_exact` set for that one call; the exact
  seed baselines go in the shim's own cache, never in `_baselines`.

FIVE CONTROLS run before any count is read. If one fails, the census is
broken: exit 2.
1. Positive: esp_prog's C4/Y1 grid (121 poses at 315 degrees, #1064) gives
   35 stack C-flips at the pair level, at least one of which survives to
   the pads_ok verdict. With the stack alone exact (the prereg's FULL), 6
   survive, because 29 of the 35 are also refused by the box-currency PAD
   conjunct (Y1's other-net pads); that is the prereg's amended wording,
   still pinned. With the pad conjunct exact too, the pads_ok-level count is
   pinned as well (`GRID_FULL_PADS_OK`), so a drift in the instrument shows.
2. Positive: a licence witness (C4 seeded at a box-only pose beside Y1, then
   asked for an exact-stack pose) gives one L-hole.
3. Self-check: the shim's OFF verdict equals the real return on every call
   of every run.
4. Purity: esp_prog's seed is placement-identical with and without the shim.
5. FULL is the switch: on the grid, the shim's FULL verdict equals the
   `pads_ok` return of a context built with `STACK_EXACT_CONFIRM` on, at
   every pose.

Then, per cell (board x engine; corpus seed and quench, pile seed, and
StickHub as a diagnostic):
- `pads_ok` calls, OFF refusals, L-holes (calls and distinct pairs), and
  C-flips (`pads_ok` level and any level, the any level per conjunct), each
  with its callers. The any level is split into calls inside a `pads_ok`
  (already decided in all three modes) and outside one;
- every real pad stack in the OFF output (`pad_intersection_pairs`), classed
  at the input board as one of: box-only licence, genuine (exact) licence,
  over-cap pair, pile part, or unexplained (the prereg's term).

A cell counts ONLY inside the engine call (`Shim(engine_only=True)`: the
`test_placement_ab._module_flags` scope). The A/B harness's grade runs after
it in box mode in every arm, so nothing it meets is a flip an arm could
change. The first full run, at f90801c1, counted the grader too, and that
alone made orangecrab a seed trial board.

What the census does NOT observe:
- `relocate.exact_refusal`, which compares `.stack` against `seed_baseline`
  the same way `pads_ok` does. It is reached only through `place_route_loop
  --relocate-block`.
- `place_pose`/`rank_poses`, `reseat`, `portfolio` and `perturb`. Each
  reaches `pads_ok` through `candidate_valid`, but none is in these cells.
- Other intents, other quench parameters and RNG seeds, and chained steps
  whose seed is an earlier step's output.

On the committed corpus the L-hole zero is STRUCTURAL: no committed input
carries a box-only licence, and a pile has none by `_degenerate_refs`.
StickHub has 35 such licences, which the cells do not visit (Phase-3
verifier).

The decision the pre-registration asks for is printed last:
- L1, L0-demonstrated or L0-undemonstrated;
- the family-B trial cells per engine, as distinct committed boards;
- STOP A when L is undemonstrated and no engine has 3 trial boards.

Not collected by run_all (no `test_` prefix). About an hour or more.

    python3 -X utf8 tests/measure_1127_licence_census.py [--workdir DIR]
        [--json-out PATH] [--board NAME ...] [--no-piles] [--no-stickhub]
        [--controls-only]

Exit 0 = taken, 2 = a control failed (not taken), 3 = partial (a --board
filter or --controls-only).
"""
import argparse
import collections
import contextlib
import io
import json
import os
import sys
import tempfile
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, TESTS_DIR)
import test_placement_ab as AB                           # noqa: E402
from test_1094_rotated_courtyards import stickhub        # noqa: E402

PREREG = os.path.join(TESTS_DIR, '1127_stack_ab_prereg.json')
CORPUS_ENGINES = ('seed', 'quench')
#: control 1, FULL arm: the grid's pads_ok-level C-flips with both conjuncts
#: exact (the switch), measured when the switch took the pad conjunct.
#: Pinned like the stack-only 6, as a drift detector.
GRID_FULL_PADS_OK = 42


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **k)


class Shim:
    """Observe an OFF run: every `pads_ok` call in three modes, and every box
    stack `pair_shortfall` returns, at no cost to the run's decisions.

    `engine_only` (the cells) counts ONLY inside `test_placement_ab.
    _module_flags`, the scope `_run_seed` and `_run` put around the engine
    call and nothing else. Their GRADE runs after it, in box mode in every
    arm, so a flip the grader meets is not one any arm could change -- the
    first run counted it, and that alone made orangecrab a seed trial board
    (its pile cell's 2 any-level flips were all the grader's; Phase-3
    verifier). The controls, which call `pads_ok` directly, count always."""

    def __init__(self, engine_only=False, pad_exact=True):
        from placement import legality as L
        self.L = L
        self.engine_only = engine_only
        # FULL = the switch: both conjuncts exact. False is the prereg's
        # stack-only FULL (#1144's toggle), which control 1 still measures.
        self.pad_exact = pad_exact
        # control 5: a list receives every counted call's FULL verdict
        self.full_log = None
        self.counting = not engine_only
        self._saved = {}
        self.reset()

    def reset(self):
        self.calls = 0
        self.off_refuse = 0
        self.mismatch = 0
        self.l_hole = 0
        self.c_flip = 0
        self.c_flip_any = 0
        # The any-level count split by where the pair call came from: inside
        # a `pads_ok` call (whose verdict is already counted in all three
        # modes, so these change nothing by themselves) or outside one (the
        # consumers that read `.stack` with no baseline).
        self.c_flip_any_in_pads_ok = 0
        self.c_flip_any_outside = 0
        # ...and the PAD conjunct's: a box pad shortfall or overlap that the
        # outlines clear (FULL with `pad_exact` only), split the same way.
        self.c_flip_any_pad = 0
        self.c_flip_any_pad_in_pads_ok = 0
        self.c_flip_any_pad_outside = 0
        self.l_hole_pairs = set()
        self.c_flip_refs = set()
        self.by_caller = collections.Counter()
        self._inside = False
        self._in_pads_ok = False

    # -- exact answers, outside the context's own caches --------------------
    def _exact(self, ctx, a, b, pose_a=None, pose_b=None):
        prev = ctx.stack_exact, ctx.pad_exact
        ctx.stack_exact, ctx.pad_exact = True, self.pad_exact
        try:
            return self._pair(ctx, a, b, pose_a=pose_a, pose_b=pose_b)
        finally:
            ctx.stack_exact, ctx.pad_exact = prev

    def _exact_base(self, ctx, a, b):
        L = self.L
        if a in ctx._degenerate_refs or b in ctx._degenerate_refs:
            return L.ZERO_SHORTFALL
        cache = ctx.__dict__.setdefault('_shim_xbase', {})
        a, b = (a, b) if a <= b else (b, a)
        key = (a, b, self.pad_exact)
        if key not in cache:
            cache[key] = self._exact(ctx, a, b, pose_a=ctx.seed_of(a),
                                     pose_b=ctx.seed_of(b))
        return cache[key]

    def _modes(self, ctx, ref, pose, neighbors, exclude):
        """(off_ok, l_ok, full_ok, l_hole_nb, c_flip_nb) for one call."""
        L = self.L
        EPS = L.EPS
        if ref not in ctx.parts:
            return True, True, True, None, None
        if not ctx.keepout_ok(ref, *pose):
            return False, False, False, None, None
        off_ok = l_ok = full_ok = True
        l_nb = c_nb = None
        for nb in neighbors:
            if nb == ref or (exclude is not None and nb in exclude):
                continue
            cur = self._pair(ctx, ref, nb, pose_a=pose)
            if cur is L.ZERO_SHORTFALL:
                continue
            base = ctx.seed_baseline(ref, nb)
            common = (cur.pad > base.pad + EPS
                      or (cur.pad_overlap and not base.pad_overlap)
                      or cur.hole > base.hole + EPS)
            off_stack = cur.stack and not base.stack
            cur_x = self._exact(ctx, ref, nb, pose_a=pose)
            base_x = self._exact_base(ctx, ref, nb)
            box_only_licence = base.stack and not base_x.stack
            l_stack = off_stack or (box_only_licence and cur_x.stack)
            full_stack = cur_x.stack and not base_x.stack
            # FULL's other conjuncts in its own currency: with `pad_exact`
            # the pad pair is graded on the outlines on both sides
            full_common = (cur_x.pad > base_x.pad + EPS
                           or (cur_x.pad_overlap and not base_x.pad_overlap)
                           or cur_x.hole > base_x.hole + EPS)
            o_ref = common or off_stack
            l_ref = common or l_stack
            f_ref = full_common or full_stack
            if o_ref:
                off_ok = False
            if l_ref:
                l_ok = False
                if not o_ref and l_nb is None:
                    l_nb = nb
            if f_ref:
                full_ok = False
        if not off_ok and full_ok:
            # the refusing neighbours all refuse on the box stack alone
            c_nb = 'any'
        return off_ok, l_ok, full_ok, l_nb, c_nb

    def __enter__(self):
        L = self.L
        shim = self
        self._saved = {
            'pp_init': L.PartPads.__init__,
            'pads_ok': L.LegalityContext.pads_ok,
            'pair': L.LegalityContext.pair_shortfall,
        }
        self._pair = self._saved['pair']
        orig_init = self._saved['pp_init']
        orig_pads_ok = self._saved['pads_ok']
        orig_pair = self._saved['pair']

        def pp_init(pp, fp, *a, **k):
            orig_init(pp, fp, *a, **k)
            if getattr(pp, 'fp_snapshot', None) is None:
                pp.fp_snapshot = L._fp_snapshot(fp)

        def pair_shortfall(ctx, a, b, pose_a=None, pose_b=None):
            sf = orig_pair(ctx, a, b, pose_a=pose_a, pose_b=pose_b)
            box_pad = shim.pad_exact and (sf.pad > L.EPS or sf.pad_overlap)
            if ((sf.stack or box_pad) and not ctx.stack_exact
                    and not ctx.pad_exact and not shim._inside
                    and shim.counting):
                shim._inside = True
                try:
                    x = shim._exact(ctx, a, b, pose_a=pose_a, pose_b=pose_b)
                finally:
                    shim._inside = False
                caller = sys._getframe(1).f_code.co_name
                if sf.stack and not x.stack:
                    shim.c_flip_any += 1
                    if shim._in_pads_ok:
                        shim.c_flip_any_in_pads_ok += 1
                    else:
                        shim.c_flip_any_outside += 1
                    shim.by_caller['any:' + caller] += 1
                if box_pad and not (x.pad > L.EPS or x.pad_overlap):
                    shim.c_flip_any_pad += 1
                    if shim._in_pads_ok:
                        shim.c_flip_any_pad_in_pads_ok += 1
                    else:
                        shim.c_flip_any_pad_outside += 1
                    shim.by_caller['any_pad:' + caller] += 1
            return sf

        def pads_ok(ctx, ref, x, y, rot, neighbors, exclude=None, why=None):
            neighbors = list(neighbors)
            prev_in = shim._in_pads_ok
            shim._in_pads_ok = True
            try:
                real = orig_pads_ok(ctx, ref, x, y, rot, neighbors,
                                    exclude=exclude, why=why)
            finally:
                shim._in_pads_ok = prev_in
            if not shim.counting:
                return real
            if ctx.stack_exact or ctx.pad_exact:
                raise AssertionError('the census observes an OFF run; this '
                                     'context was built exact')
            shim._inside = True
            try:
                o, l_, f, l_nb, c_nb = shim._modes(ctx, ref, (x, y, rot),
                                                   neighbors, exclude)
            finally:
                shim._inside = False
            shim.calls += 1
            caller = sys._getframe(1).f_code.co_name
            if o != real:
                shim.mismatch += 1
            if not o:
                shim.off_refuse += 1
            if o and not l_:
                shim.l_hole += 1
                shim.l_hole_pairs.add(tuple(sorted((ref, l_nb))))
                shim.by_caller['l_hole:' + caller] += 1
            if not o and f:
                shim.c_flip += 1
                shim.c_flip_refs.add(ref)
                shim.by_caller['c_flip:' + caller] += 1
            if shim.full_log is not None:
                shim.full_log.append(f)
            return real

        L.PartPads.__init__ = pp_init
        L.LegalityContext.pair_shortfall = pair_shortfall
        L.LegalityContext.pads_ok = pads_ok
        if self.engine_only:
            mf = AB._module_flags
            self._saved['mf_enter'] = mf.__enter__
            self._saved['mf_exit'] = mf.__exit__
            enter0, exit0 = mf.__enter__, mf.__exit__

            def mf_enter(this):
                r = enter0(this)
                shim.counting = True
                return r

            def mf_exit(this, *exc):
                shim.counting = False
                return exit0(this, *exc)
            mf.__enter__ = mf_enter
            mf.__exit__ = mf_exit
        return self

    def __exit__(self, *exc):
        L = self.L
        L.PartPads.__init__ = self._saved['pp_init']
        L.LegalityContext.pads_ok = self._saved['pads_ok']
        L.LegalityContext.pair_shortfall = self._saved['pair']
        if 'mf_enter' in self._saved:
            AB._module_flags.__enter__ = self._saved.pop('mf_enter')
            AB._module_flags.__exit__ = self._saved.pop('mf_exit')
        return False

    def summary(self):
        return {'calls': self.calls, 'off_refuse': self.off_refuse,
                'mismatch': self.mismatch, 'l_hole': self.l_hole,
                'l_hole_pairs': sorted(self.l_hole_pairs),
                'c_flip': self.c_flip, 'c_flip_any': self.c_flip_any,
                'c_flip_any_in_pads_ok': self.c_flip_any_in_pads_ok,
                'c_flip_any_outside': self.c_flip_any_outside,
                'c_flip_any_pad': self.c_flip_any_pad,
                'c_flip_any_pad_in_pads_ok': self.c_flip_any_pad_in_pads_ok,
                'c_flip_any_pad_outside': self.c_flip_any_pad_outside,
                'c_flip_refs': sorted(self.c_flip_refs),
                'by_caller': dict(self.by_caller)}


class NotAPile(Exception):
    """`_pile_inputs` refused the staged board. Kept apart from the shim's
    own AssertionError, which must never read as "not a pile"."""


# -- the output-stack attribution ------------------------------------------
def _windowed_over_cap(ctx, parts, a, b) -> bool:
    """Does the pair reach `pair_shortfall`'s extent branch at its current
    poses? Since #1213 the cap compares the WINDOWED pad-pair product."""
    from placement import legality as L
    pa, pb = parts[a], parts[b]
    xa, ya, ra = ctx.pose_of(a)
    xb, yb, rb = ctx.pose_of(b)
    ea = pa.extent(xa, ya, ra)
    if ea is None or pb.extent(xb, yb, rb) is None:
        return False
    reach = max(ctx.clearance, pa.hole_reach, pb.hole_reach)
    wa, wb = L._pad_windows(pa.pad_rects(xa, ya, ra), ea,
                            pb.pad_rects(xb, yb, rb), reach)
    return len(wa) * len(wb) > L.PAIR_TEST_CAP


def attribute(board_in, board_out, clearance=0.2):
    """Every real pad stack in `board_out`, classed at `board_in`'s poses."""
    from kicad_parser import parse_kicad_pcb
    from placement import legality as L
    pin = _quiet(parse_kicad_pcb, board_in)
    pout = _quiet(parse_kicad_pcb, board_out)
    stacks = _quiet(L.pad_intersection_pairs, pout, clearance)
    exact_in = {frozenset((q.a, q.b))
                for q in _quiet(L.pad_intersection_pairs, pin, clearance)}
    fps = pin.footprints
    parts = _quiet(L.build_part_pads, fps, clearance, tolerant=True)

    def pose(r):
        f = fps[r]
        return (f.x, f.y, (f.rotation or 0.0) % 360.0)
    ctx = L.LegalityContext(parts, None, clearance, pose, pose)
    out = collections.Counter()
    rows = []
    for q in stacks:
        a, b = q.a, q.b
        if a not in parts or b not in parts:
            cls = 'unmodelled'
        elif a in ctx._degenerate_refs or b in ctx._degenerate_refs:
            cls = 'pile_part'
        elif _windowed_over_cap(ctx, parts, a, b):
            # #1213: the cap compares the WINDOWED product.
            cls = 'over_cap'
        elif frozenset((a, b)) in exact_in:
            cls = 'genuine_licence'
        elif ctx.pair_shortfall(a, b).stack:
            cls = 'box_only_licence'
        else:
            # the prereg's 'unexplained': no stack of either kind at the
            # input poses, so not a licence the stack gate gave
            cls = 'unexplained'
        out[cls] += 1
        rows.append((a, b, cls))
    return dict(out), rows


# -- the four controls -----------------------------------------------------
def _esp_state(board):
    import routing_defaults as defaults
    from kicad_parser import parse_kicad_pcb
    from list_nets import board_floor_knobs
    from placement.quench import QuenchState
    clr, edge, _k = board_floor_knobs(board, clearance=None,
                                      board_edge_clearance=None,
                                      clearance_default=defaults.CLEARANCE,
                                      edge_default=0.55)
    return _quiet(QuenchState, _quiet(parse_kicad_pcb, board), board,
                  clearance=clr, board_edge_clearance=edge,
                  crossing_penalty=10.0, halo_base=0.5, halo_coef=0.25,
                  halo_weight=2.0, edge_halo=2.0, edge_weight=2.0,
                  grid_step=defaults.GRID_STEP, length_weight=1.0)


def _grid():
    return [(round(135.86 + i * 0.04, 4), round(103.42 + j * 0.04, 4))
            for j in range(11) for i in range(11)]


def _grid_census(esp, pad_exact):
    """Control 1 in one FULL mode: the grid's box-only stacks, and its
    C-flips at the pair and pads_ok levels, with the FULL verdict of every
    pads_ok call (control 5 compares those with a real exact context)."""
    with Shim(pad_exact=pad_exact) as sh:
        st = _esp_state(esp)
        ctx = st.legality_ctx
        box_only, both = [], []
        for x, y in _grid():
            b = sh._pair(ctx, 'C4', 'Y1', pose_a=(x, y, 315.0)).stack
            e = sh._exact(ctx, 'C4', 'Y1', pose_a=(x, y, 315.0)).stack
            if b and not e:
                box_only.append((x, y))
            elif b and e:
                both.append((x, y))
        sh.reset()
        sh.full_log = []
        for x, y in _grid():
            ctx.pads_ok('C4', x, y, 315.0, ['Y1'])
        return box_only, both, sh.full_log, {
            'box_only': len(box_only), 'both': len(both),
            'c_flip_pair_level': sh.c_flip_any,
            'c_flip_pad_pair_level': sh.c_flip_any_pad,
            'c_flip_pads_ok': sh.c_flip, 'mismatch': sh.mismatch}


def _real_exact_verdicts(esp, pad_exact):
    """Control 5: the grid's pads_ok returns from a context built with
    `STACK_EXACT_CONFIRM` on (its `pad_exact` set before any query, so the
    seed baselines are measured in that mode), outside any shim."""
    from placement import legality as L
    saved = L.STACK_EXACT_CONFIRM
    L.STACK_EXACT_CONFIRM = True
    try:
        st = _esp_state(esp)
    finally:
        L.STACK_EXACT_CONFIRM = saved
    ctx = st.legality_ctx
    ctx.pad_exact = pad_exact
    return [ctx.pads_ok('C4', x, y, 315.0, ['Y1']) for x, y in _grid()]


def controls(work):
    from placement.writer import write_placed_output
    esp = os.path.join(AB.BOARDS, 'esp_prog.kicad_pcb')
    out = {}
    # 1. the C4/Y1 grid: 35 box-only poses must read as C-flips -- with the
    #    stack alone exact (the prereg's FULL) and with both (the switch).
    box_only, both, stack_log, out['grid'] = _grid_census(esp, False)
    _b, _e, full_log, out['grid_full'] = _grid_census(esp, True)
    # The prereg's control 1 (as amended): the pair-level count is #1064's 35,
    # and at least one survives to the pads_ok verdict -- 29 of the 35 are
    # also refused there by the box-currency PAD conjunct (Y1's other-net
    # pads) when only the stack is exact.
    g, gf = out['grid'], out['grid_full']
    ok1 = (g['box_only'] == 35
           and g['c_flip_pair_level'] == 35
           and g['c_flip_pads_ok'] >= 1
           # the prereg says >= 1; the measured value is pinned too, so a
           # drift in the instrument shows (Phase-3 verifier)
           and g['c_flip_pads_ok'] == 6
           # ...and with the pad conjunct exact too: the same stacks, and
           # the pads_ok-level count pinned the same way
           and gf['box_only'] == 35 and gf['c_flip_pair_level'] == 35
           and gf['c_flip_pads_ok'] == GRID_FULL_PADS_OK
           and gf['mismatch'] == 0)
    # 5. FULL is the switch: the shim's FULL verdicts equal a real exact
    #    context's, in both modes.
    out['full_is_switch'] = {
        'stack_only': stack_log == _real_exact_verdicts(esp, False),
        'both': full_log == _real_exact_verdicts(esp, True),
        'calls': len(full_log)}
    ok5 = (out['full_is_switch']['stack_only']
           and out['full_is_switch']['both'] and len(full_log) == 121)
    # 2. the licence witness: seed C4 at a box-only pose, ask for an exact
    #    stack -- OFF admits it (licensed), L refuses it.
    ok2 = False
    if box_only and both:
        (sx, sy), (tx, ty) = box_only[0], both[-1]
        seeded = os.path.join(work, 'witness.kicad_pcb')
        _quiet(write_placed_output, esp, seeded,
               [{'reference': 'C4', 'new_x': sx, 'new_y': sy,
                 'new_rotation': 315.0}])
        with Shim() as sh:
            st = _esp_state(seeded)
            sh.reset()
            real = st.legality_ctx.pads_ok('C4', tx, ty, 315.0, ['Y1'])
            out['witness'] = {'seed': (sx, sy), 'target': (tx, ty),
                              'off_admits': bool(real), 'l_hole': sh.l_hole,
                              'mismatch': sh.mismatch}
        ok2 = bool(real) and out['witness']['l_hole'] == 1
    else:
        out['witness'] = {'error': 'no box-only or no exact-stack pose on '
                          'the grid', 'box_only': len(box_only),
                          'both': len(both)}
    # 4. purity: esp_prog's seed with and without the shim.
    intent = _quiet(AB._intent_for, esp, [], work)
    a = os.path.join(work, 'pure_off.kicad_pcb')
    b = os.path.join(work, 'pure_shim.kicad_pcb')
    _quiet(AB._run_seed, esp, a, intent, {}, ignore_nets=['GND'])
    with Shim():
        _quiet(AB._run_seed, esp, b, intent, {}, ignore_nets=['GND'])
    out['purity'] = _poses(a) == _poses(b)
    ok = (ok1 and ok2 and out['purity'] and out['grid']['mismatch'] == 0
          and ok5)
    return ok, out


def _poses(path):
    from kicad_parser import parse_kicad_pcb
    pcb = _quiet(parse_kicad_pcb, path)
    return {r: (round(f.x, 6), round(f.y, 6), round((f.rotation or 0) % 360, 6))
            for r, f in pcb.footprints.items()}


# -- the cells ---------------------------------------------------------------
def cell(board, engine, work, pile=False):
    name = os.path.splitext(os.path.basename(board))[0]
    d = os.path.join(work, f"{name}_{engine}{'_pile' if pile else ''}")
    os.makedirs(d, exist_ok=True)
    if pile:
        try:
            src, intent, _doc, refs = _quiet(AB._pile_inputs, board, d,
                                             require_decaps=False)
        except AssertionError as exc:
            raise NotAPile(str(exc)) from exc
        kw = {'seed_refs': refs}
    else:
        src, intent, kw = board, _quiet(AB._intent_for, board, [], d), {}
    out = os.path.join(d, 'off.kicad_pcb')
    t0 = time.time()
    with Shim(engine_only=True) as sh:
        if engine == 'seed':
            g = _quiet(AB._run_seed, src, out, intent, kw, ignore_nets=['GND'])
        else:
            g = _quiet(AB._run, src, out, intent,
                       dict(AB.QUENCH_BASE, ignore_nets=['GND']))
    att, rows = attribute(src, out)
    rec = dict(sh.summary(), seconds=round(time.time() - t0, 1),
               body_blocking=g.get('body_blocking'), attribution=att,
               stacks=rows)
    print(f"{name}{' (pile)' if pile else ''} [{engine}] calls {rec['calls']}"
          f", OFF refusals {rec['off_refuse']}, L-holes {rec['l_hole']} "
          f"({len(rec['l_hole_pairs'])} pair(s) {rec['l_hole_pairs'][:4]}), "
          f"C-flips {rec['c_flip']} (any level {rec['c_flip_any']}: "
          f"{rec['c_flip_any_in_pads_ok']} inside pads_ok, "
          f"{rec['c_flip_any_outside']} outside; pad conjunct "
          f"{rec['c_flip_any_pad']}: {rec['c_flip_any_pad_in_pads_ok']} "
          f"inside, {rec['c_flip_any_pad_outside']} outside), "
          f"mismatch {rec['mismatch']}; OFF body_blocking "
          f"{rec['body_blocking']} = {att} ({rec['seconds']}s)", flush=True)
    return rec


def decide(cells, doc):
    committed = {os.path.splitext(b)[0] for b in doc['boards']}

    def board_of(key):
        return key.split('|')[0]
    l_corpus = [k for k, r in cells.items()
                if board_of(k) in committed and r['l_hole'] > 0]
    l_sh = [k for k, r in cells.items()
            if board_of(k) not in committed and r['l_hole'] > 0]
    if l_corpus:
        lv = 'L1'
    elif l_sh:
        lv = 'L0-demonstrated'
    else:
        lv = 'L0-undemonstrated'
    trial = {}
    for eng in ('seed', 'quench'):
        trial[eng] = sorted({board_of(k) for k, r in cells.items()
                             if k.split('|')[1] == eng
                             and board_of(k) in committed
                             and (r['c_flip'] > 0 or r['c_flip_any'] > 0
                                  or r.get('c_flip_any_pad', 0) > 0)})
    stop_a = lv == 'L0-undemonstrated' and all(len(v) < 3
                                               for v in trial.values())
    return {'licence': lv, 'l_cells_corpus': l_corpus,
            'l_cells_diagnostic': l_sh, 'family_B_trial_boards': trial,
            'stop_A': stop_a}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--workdir', default=None)
    ap.add_argument('--json-out', default=None)
    ap.add_argument('--board', action='append', default=None)
    ap.add_argument('--no-piles', action='store_true')
    ap.add_argument('--no-stickhub', action='store_true')
    ap.add_argument('--controls-only', action='store_true')
    a = ap.parse_args(argv)
    with open(PREREG, encoding='utf-8') as fh:
        doc = json.load(fh)
    work = a.workdir or tempfile.mkdtemp(prefix='m1127lic_')
    os.makedirs(work, exist_ok=True)
    ok, ctl = controls(work)
    print(f"controls: {json.dumps(ctl, default=str)}", flush=True)
    if not ok:
        print("CONTROL FAILED: the census is broken, no count is read "
              "(exit 2)")
        return 2
    print("controls: all five hold", flush=True)
    if a.controls_only:
        return 3
    boards = [os.path.join(AB.BOARDS, b) for b in doc['boards']]
    if a.board:
        boards = [b for b in boards if os.path.basename(b) in a.board
                  or os.path.splitext(os.path.basename(b))[0] in a.board]
    cells = {}
    for b in boards:
        n = os.path.splitext(os.path.basename(b))[0]
        for eng in CORPUS_ENGINES:
            cells[f'{n}|{eng}'] = cell(b, eng, work)
        if not a.no_piles:
            try:
                cells[f'{n}|seed|pile'] = cell(b, 'seed', work, pile=True)
            except NotAPile as exc:
                print(f"{n} (pile): not a pile ({str(exc)[:160]})")
    if not a.no_stickhub:
        sh = stickhub()
        if sh:
            for eng in CORPUS_ENGINES:
                cells[f'StickHub|{eng}'] = cell(sh, eng, work)
        else:
            print("StickHub demo not found (KICAD_STICKHUB_DEMO or the "
                  "install paths): diagnostic omitted")
    mism = {k: r['mismatch'] for k, r in cells.items() if r['mismatch']}
    if mism:
        print(f"SHIM MISMATCH {mism}: not taken (exit 2)")
        return 2
    dec = decide(cells, doc)
    print(f"\ndecision: {json.dumps(dec)}")
    if a.json_out:
        with open(a.json_out, 'w', encoding='utf-8') as fh:
            json.dump({'controls': ctl, 'cells': cells, 'decision': dec}, fh,
                      indent=1, default=str)
    return 3 if a.board else 0


if __name__ == '__main__':
    sys.exit(main())
