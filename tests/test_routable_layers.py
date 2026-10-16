#!/usr/bin/env python3
"""Relocation, stub switching and layer swaps never target a FORBIDDEN layer.

route.py / route_diff.py / route_planes.py append the copper layers the caller
did not request to config.layers at FORBIDDEN cost (-1): they block, vias may
span them, no track may be placed on them. A 6-layer board routed with
--layers F.Cu In2.Cu B.Cu had USB terminals relocated and stubs swapped onto
In1.Cu / In4.Cu, its GND reference planes.

Candidate lists draw from GridRouteConfig.routable_layers(). Each check
below drives one site:

  1. routable_layers(): forbidden layers dropped; a short / absent
     layer_costs leaves the uncosted layers routable (as get_layer_costs).
  2. rank_fallback_layers (setback-failure launch layer).
  3. diff-pair multipoint terminal relocation candidates.
  4. diff-pair multipoint terminal stub switch.
  7. multi-point common-layer collapse, and the per-cluster fallback.

    python3 tests/test_routable_layers.py
"""
import contextlib
import io
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_router"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from kicad_parser import BoardInfo, parse_kicad_pcb  # noqa: E402
from routing_config import GridRouteConfig  # noqa: E402
from synth import make_net, make_pad, make_pcb, make_seg  # noqa: E402

FORBIDDEN = {"In1.Cu", "In2.Cu"}
failures = []


def check(label, ok, detail=""):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + str(detail)) if detail else ''}")
    if not ok:
        failures.append(label)


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


class _OpenMap:
    """An obstacle map with nothing blocked on any layer."""
    def is_blocked(self, gx, gy, layer):
        return False


class _WalledMap:
    """F.Cu (index 0) fully walled, B.Cu (index 1) a third blocked, the
    forbidden inner layers wide open -- the most open layers ARE the
    forbidden ones, so a candidate list that keeps them ranks them first."""
    def is_blocked(self, gx, gy, layer):
        if layer == 0:
            return True
        if layer == 1:
            return (gx + gy) % 3 == 0
        return False


def _cfg4(**kw):
    """The full-stack layout of a `--layers F.Cu B.Cu` run on a 4-layer board."""
    base = dict(layers=["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"],
                layer_costs=[1.0, 1.0, -1, -1], grid_step=0.1, track_width=0.2,
                clearance=0.15, via_size=0.45, via_drill=0.2,
                hole_to_hole_clearance=0.25)
    base.update(kw)
    return GridRouteConfig(**base)


def _diff_board():
    """A P/N terminal: F.Cu pads 1 mm apart, each with a 1 mm F.Cu escape."""
    pp = make_pad(1, 10.0, 10.0, ref="J1", num="1", net_name="/D_P", size_x=0.6, size_y=0.6)
    nn = make_pad(2, 11.0, 10.0, ref="J1", num="2", net_name="/D_N", size_x=0.6, size_y=0.6)
    segs = [make_seg(10.0, 10.0, 10.0, 11.0, net_id=1),
            make_seg(11.0, 10.0, 11.0, 11.0, net_id=2)]
    bi = BoardInfo(layers={0: "F.Cu", 1: "In1.Cu", 2: "In2.Cu", 31: "B.Cu"},
                   copper_layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                   board_bounds=(0.0, 0.0, 30.0, 30.0))
    pcb = make_pcb(nets={1: make_net(1, "/D_P"), 2: make_net(2, "/D_N")},
                   segments=segs, pads_by_net={1: [pp], 2: [nn]}, board_info=bi)
    return pcb, (pp, nn)


def routable_basics():
    print("1. routable_layers()")
    cfg = GridRouteConfig(layers=["F.Cu", "In2.Cu", "B.Cu", "In1.Cu", "In3.Cu", "In4.Cu"],
                          layer_costs=[1.0, 1.0, 1.0, -1, -1, -1])
    check("forbidden layers dropped, order kept",
          cfg.routable_layers() == ["F.Cu", "In2.Cu", "B.Cu"], cfg.routable_layers())
    check("no layer_costs: every layer routable",
          GridRouteConfig(layers=["F.Cu", "B.Cu"]).routable_layers() == ["F.Cu", "B.Cu"])
    short = GridRouteConfig(layers=["F.Cu", "In1.Cu", "B.Cu"], layer_costs=[-1])
    check("a short layer_costs leaves the uncosted layers routable",
          short.routable_layers() == ["In1.Cu", "B.Cu"], short.routable_layers())
    none = GridRouteConfig(layers=["F.Cu", "B.Cu"])
    none.layer_costs = None
    check("layer_costs None: every layer routable",
          none.routable_layers() == ["F.Cu", "B.Cu"], none.routable_layers())
    return cfg


def fallback(cfg6):
    print("2. rank_fallback_layers")
    from layer_swap_fallback import rank_fallback_layers
    ranked = rank_fallback_layers(cfg6, SimpleNamespace(zones=[]), _OpenMap(),
                                  10.0, 10.0, "F.Cu")
    check("only routable launch layers offered", sorted(ranked) == ["B.Cu", "In2.Cu"], ranked)


def diff_multipoint():
    import diff_pair_multipoint as dpm
    print("3. diff-pair multipoint relocation candidates")
    pcb, term = _diff_board()
    cfg = _cfg4()
    state = SimpleNamespace(config=cfg, pcb_data=pcb,
                            diff_pair_base_obstacles=_WalledMap())
    ranked = dpm._candidate_relocation_layers(state, [term], _WalledMap())
    check("relocation offers B.Cu, never a forbidden layer",
          ranked == ["B.Cu"], ranked)

    print("4. diff-pair multipoint terminal stub switch")
    switched, out = quiet(dpm._switch_blocked_terminal_stubs, state, None, [term])
    layers = {s.layer for s in pcb.segments}
    check("the walled stubs are switched", len(switched) == 1, out.strip())
    check("onto B.Cu, not the more open forbidden layers",
          layers == {"B.Cu"}, sorted(layers))


def _text_board(body, nets):
    text = ('(kicad_pcb\n\t(version 20221018)\n'
            '\t(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) '
            '(31 "B.Cu" signal) (44 "Edge.Cuts" user))\n\t(net 0 "")\n'
            + ''.join(f'\t(net {i} "{n}")\n' for i, n in enumerate(nets, 1))
            + '\t(gr_rect (start 0 0) (end 30 12) (layer "Edge.Cuts") (width 0.1))\n'
            + body + ')\n')
    fd, path = tempfile.mkstemp(suffix=".kicad_pcb", prefix="t_routable_")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return parse_kicad_pcb(path)
    finally:
        os.remove(path)


def _fp(ref, x, y, layer, net=1, name="/SIG"):
    return (f'\t(footprint "L:{ref}" (layer "F.Cu") (at {x} {y})\n'
            f'\t\t(property "Reference" "{ref}")\n'
            f'\t\t(pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "{layer}") '
            f'(net {net} "{name}")))\n')


def single_ended():
    from layer_swap_optimization import apply_single_ended_layer_swaps
    cfg = _cfg4(layer_costs=[1.0, 3.0, -1, -1], clearance=0.1, via_size=0.4)

    print("7. multi-point common-layer collapse")
    # U1 pinned to In1.Cu (forbidden), U2 through-hole, U3 an F.Cu pad whose
    # via-in-pad escapes to a dangling B.Cu stub. With every layer routable
    # the stub collapses onto In1.Cu (the only layer serving every end).
    body = (_fp("U1", 3, 3, "In1.Cu")
            + '\t(footprint "L:U2" (layer "F.Cu") (at 27 3)\n\t\t(property "Reference" "U2")\n'
              '\t\t(pad "1" thru_hole circle (at 0 0) (size 0.8 0.8) (drill 0.4) '
              '(layers "*.Cu") (net 1 "/SIG")))\n'
            + _fp("U3", 18, 3, "F.Cu")
            + '\t(via (at 18 3) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "v3"))\n'
            + '\t(segment (start 18 3) (end 22 3) (width 0.2) (layer "B.Cu") (net 1) (uuid "s3"))\n')
    control = _text_board(body, ["/SIG"])
    _, _ = quiet(apply_single_ended_layer_swaps, control,
                 _cfg4(layer_costs=[1.0, 1.0, 1.0, 1.0], clearance=0.1, via_size=0.4),
                 [("/SIG", 1)], can_swap_to_top_layer=True,
                 all_segment_modifications=[], all_swap_vias=[],
                 all_swap_segments=[], verbose=True)
    moved = [s.layer for s in control.segments]
    if moved != ["In1.Cu"]:
        check("BROKEN FIXTURE: the all-routable control collapses onto In1.Cu",
              False, moved)
        return
    pcb = _text_board(body, ["/SIG"])
    _, out = quiet(apply_single_ended_layer_swaps, pcb, cfg, [("/SIG", 1)],
                   can_swap_to_top_layer=True, all_segment_modifications=[],
                   all_swap_vias=[], all_swap_segments=[], verbose=True)
    layers = [s.layer for s in pcb.segments]
    check("the stub is not collapsed onto the forbidden In1.Cu",
          not (set(layers) & FORBIDDEN), f"{layers}; {out.strip()}")

    print("7b. multi-point per-cluster assignment")
    # U1 pinned to In1.Cu, U2 to In2.Cu: no common layer, so the greedy
    # cover picks {In1, In2} and moves the B.Cu stub onto In1.Cu -- which
    # this run forbids.
    body = (_fp("U1", 3, 3, "In1.Cu") + _fp("U2", 27, 3, "In2.Cu")
            + _fp("U3", 18, 3, "F.Cu")
            + '\t(via (at 18 3) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "v3"))\n'
            + '\t(segment (start 18 3) (end 22 3) (width 0.2) (layer "B.Cu") (net 1) (uuid "s3"))\n')
    pcb = _text_board(body, ["/SIG"])
    _, out = quiet(apply_single_ended_layer_swaps, pcb,
                   _cfg4(layer_costs=[1.0, 1.0, -1, 1.0], clearance=0.1, via_size=0.4),
                   [("/SIG", 1)], can_swap_to_top_layer=True,
                   all_segment_modifications=[], all_swap_vias=[],
                   all_swap_segments=[], verbose=True)
    layers = [s.layer for s in pcb.segments]
    check("the stub is not assigned to the forbidden In1.Cu",
          "In1.Cu" not in layers, f"{layers}; {out.strip()}")


def main():
    cfg6 = routable_basics()
    fallback(cfg6)
    diff_multipoint()
    single_ended()
    print("FAILED: " + ", ".join(failures) if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
