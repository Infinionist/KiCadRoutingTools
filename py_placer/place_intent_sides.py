#!/usr/bin/env python3
"""Mirror unrouted footprints onto the sides declared by a floorplan intent.

``place_seed`` deliberately has no flip move: its legality search assumes the
footprint geometry it was given.  This preparatory actor makes that assumption
explicit.  It reads every face the intent declares -- ``blocks[].side``,
``edge_connectors[].side`` and ``fixed_poses[].side``, through
``floorplan.declared_side_claims``, the one reading ``plan_check`` prices the
plan on -- and mirrors only the footprints whose current side differs, at
their pose.  A ref with contradictory declarations is refused before anything
is written.
"""

#: #937 registry: which door(s) show this tool, and whether it changes the
#: board. Read by krt_registry.py by AST, never imported.
KRT_TOOL = {'scope': ['placement'], 'kind': 'actor'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)
import argparse
import json
import sys


def intent_side_targets(intent, pcb, group_sources):
    """``(targets, sources, conflicts, problems)`` for the real footprints.

    ``targets`` is ``{ref: side}`` for every unambiguous claim, ``sources``
    the declarations behind each, and ``conflicts`` ``{ref: [(side, source)]}``
    for a ref two declarations put on different faces -- refused by the
    caller instead of choosing whichever claim happened to be read last.
    Refs the board lacks remain the grader's concern.
    """
    from placement import floorplan

    blocks, problems = floorplan.resolve_blocks(intent, pcb, group_sources)
    claims = {ref: rows for ref, rows
              in floorplan.declared_side_claims(intent, blocks).items()
              if ref in pcb.footprints}
    targets, conflicts = floorplan.declared_sides(claims)
    sources = {ref: [src for _side, src in rows]
               for ref, rows in claims.items() if ref in targets}
    return targets, sources, conflicts, problems


def side_moves(pcb, targets):
    """Writer moves that preserve pose and change only the requested side."""
    from placement.legality import footprint_side

    moves = []
    for ref, side in sorted(targets.items()):
        fp = pcb.footprints[ref]
        if footprint_side(fp) == side:
            continue
        moves.append({
            'reference': ref,
            'new_x': fp.x,
            'new_y': fp.y,
            'new_rotation': fp.rotation or 0.0,
            'new_side': side,
        })
    return moves


def main():
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan
    from placement.groups import GroupError, parse_sources
    from placement.placement_state import UNPLACED_EXIT, assess_placement
    from placement.portfolio import copy_siblings
    from placement.writer import SideFlipUnsupported, write_placed_output

    parser = argparse.ArgumentParser(
        description="Apply declared floorplan sides before placement seeding.")
    parser.add_argument('input_file', help='Unrouted KiCad PCB')
    parser.add_argument('output_file', help='Side-normalized KiCad PCB')
    parser.add_argument('--intent', required=True,
                        help='Floorplan intent JSON carrying side declarations')
    parser.add_argument('--group-by', default='auto',
                        help='Block sources (default: auto = kicad,sheet)')
    args = parser.parse_args()

    try:
        sources = parse_sources(args.group_by)
        intent = floorplan.load_intent(args.intent)
    except (GroupError, OSError, ValueError) as exc:
        parser.error(str(exc))

    pcb = parse_kicad_pcb(args.input_file)
    state = assess_placement(pcb, args.input_file)
    if state.has_copper:
        print(f"place_intent_sides: this board carries {state.segments} "
              f"segment(s) and {state.vias} via(s); flipping footprints would "
              "strand routed copper. Apply sides before routing.",
              file=sys.stderr)
        return UNPLACED_EXIT

    targets, declaration_sources, conflicts, problems = intent_side_targets(
        intent, pcb, sources)
    if conflicts:
        print("place_intent_sides: contradictory side declarations: "
              + '; '.join(f"{ref}: " + ', '.join(f"{side} from {src}"
                                                 for side, src in rows)
                          for ref, rows in sorted(conflicts.items()))
              + ". Fix the intent; nothing was written.", file=sys.stderr)
        return 2
    try:
        moves = side_moves(pcb, targets)
        write_placed_output(args.input_file, args.output_file, moves)
    except (ValueError, SideFlipUnsupported) as exc:
        print(f"place_intent_sides: {exc}", file=sys.stderr)
        return 2
    copy_siblings(args.input_file, args.output_file)

    summary = {
        'declared': len(targets),
        'flipped': len(moves),
        'flipped_refs': [move['reference'] for move in moves],
        'output': args.output_file,
        'unresolved_blocks': [p.to_dict() for p in problems],
        'sources': declaration_sources,
    }
    print(f"Applied {len(targets)} declared side(s); mirrored {len(moves)} "
          f"footprint(s); wrote {args.output_file}")
    print('JSON_SUMMARY: ' + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == '__main__':
    from placement.provenance import declare_lever

    with declare_lever('place_intent_sides.py', sys.argv):
        import cli_banner

        cli_banner.install()
        sys.exit(main())
