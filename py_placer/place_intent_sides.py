#!/usr/bin/env python3
"""Mirror unrouted footprints onto the sides declared by a floorplan intent.

``place_seed`` deliberately has no flip move: its legality search assumes the
footprint geometry it was given.  This preparatory actor makes that assumption
explicit.  It reads every face the intent declares -- ``blocks[].side``,
``edge_connectors[].side`` and ``fixed_poses[].side``, through
``floorplan.declared_side_claims``, the one reading ``plan_check`` prices the
plan on -- and mirrors only the footprints whose current side differs, at
their pose, through the #714 writer flip.

Refused with nothing written, and a ``JSON_SUMMARY`` naming the refusal:

* exit 2 -- one ref declared on both faces, or a KiCad-LOCKED part declared on
  the face it is not on (a lock is a decision; no placement tool lifts one);
* exit 3 -- the board carries copper, which a flip would strand.
"""

#: #937 registry: which door(s) show this tool, and whether it changes the
#: board. Read by krt_registry.py by AST, never imported.
KRT_TOOL = {'scope': ['placement'], 'kind': 'actor'}

import _path  # noqa: F401  (py_placer -> py_router/py_tools on sys.path)
import argparse
import json
import sys

#: A contradiction in the intent, or a lock the declaration would lift.
REFUSED_EXIT = 2


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


def _refuse(reason, message, code, **extra):
    """Print the refusal and a JSON_SUMMARY, so a caller scraping the summary
    can tell a refusal from a kill. Nothing has been written."""
    print(f"place_intent_sides: {message}", file=sys.stderr)
    print('JSON_SUMMARY: ' + json.dumps(
        dict({'refused': reason, 'exit_code': code, 'written': False},
             **extra), sort_keys=True))
    return code


def main():
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan
    from placement.groups import GroupError, parse_sources
    from placement.parser import extract_locked_refs
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

    # Self-record like the other board writers (place_seed, place_pose), so a
    # replayed redo manifest flips the parts instead of seeding them unflipped.
    try:
        from redo_record import record_invocation
        record_invocation()
    except Exception:                                       # noqa: BLE001
        pass

    pcb = parse_kicad_pcb(args.input_file)
    state = assess_placement(pcb, args.input_file)
    if state.has_copper:
        return _refuse(
            'has_copper',
            f"this board carries {state.copper_carried}; flipping footprints "
            f"would strand routed copper. Apply sides before routing.",
            UNPLACED_EXIT)

    targets, declaration_sources, conflicts, problems = intent_side_targets(
        intent, pcb, sources)
    if conflicts:
        return _refuse(
            'contradictory_sides',
            "contradictory side declarations: " + '; '.join(
                f"{ref}: " + ', '.join(f"{side} from {src}"
                                       for side, src in rows)
                for ref, rows in sorted(conflicts.items()))
            + ". Fix the intent; nothing was written.",
            REFUSED_EXIT,
            conflicts={ref: [list(r) for r in rows]
                       for ref, rows in sorted(conflicts.items())})
    moves = side_moves(pcb, targets)
    # A KiCad lock is a decision someone recorded in the file, and no
    # placement tool lifts one: place_pose refuses to move a locked part, and
    # stage 0 refuses a fixed pose the lock contradicts. A locked part already
    # on its declared face is not moved, so it is not refused.
    locked = sorted({m['reference'] for m in moves}
                    & extract_locked_refs(args.input_file))
    if locked:
        return _refuse(
            'locked',
            f"{', '.join(locked)} {'is' if len(locked) == 1 else 'are'} "
            f"locked in the board (KiCad `(locked yes)`) on the face the "
            f"intent does not declare, and a lock is a decision, not a "
            f"preference. Unlock deliberately (place_pose.py IN OUT unlock "
            f"{' '.join(locked)}) or fix the declaration; nothing was "
            f"written.",
            REFUSED_EXIT, locked=locked)
    try:
        write_placed_output(args.input_file, args.output_file, moves)
    except (ValueError, SideFlipUnsupported) as exc:
        return _refuse('flip_unsupported', str(exc), REFUSED_EXIT)
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
