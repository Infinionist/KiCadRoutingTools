"""Argparse flags shared by the placement CLIs (#431, widened by #702).

Defined once so the CLIs cannot drift: an `--allow-unplaced` that spells
itself differently, or a `--brief` one tool discovers and another does not,
is exactly the kind of divergence CLAUDE.md's CLI/GUI section warns about --
here between two CLIs rather than between a CLI and the GUI.
"""
from __future__ import annotations


def add_intent_arg(parser, *, summary: str) -> None:
    """`--intent`: the floorplan intent, with `summary` saying what this CLI
    reads of it (#1067: place_fanout_clearance holds the decap limits and the
    declared rotations, and nothing else), so one flag spelling, metavar and
    default serve every CLI without a help text that lies about one.
    """
    parser.add_argument(
        "--intent", metavar="JSON", default=None,
        help="Floorplan intent JSON (`check_floorplan.py --emit-intent` "
             "writes a starter). " + summary)


def load_intent_or_exit(args):
    """(intent, exit_code). `exit_code` is 2 when the intent is unreadable.

    Call it BEFORE `record_invocation`, so an exit 2 never records a manifest
    command that touched no file.
    """
    import sys
    from placement import floorplan
    if not getattr(args, 'intent', None):
        return None, 0
    try:
        return floorplan.load_intent(args.intent), 0
    except (OSError, ValueError) as exc:
        print(f"cannot load intent {args.intent}: {exc}", file=sys.stderr)
        return None, 2


def add_mechanical_arg(parser) -> None:
    """`--mechanical` / `--no-mechanical`: the RECORDED mechanical facts (#959).

    `mechanical.json` is what `stage_unaided` writes next to the staged board:
    the poses carried over from the source because they are mechanical facts
    a real new board would already know. Nothing read it until #959 -- run 29
    moved a fiducial off its declared pose and no gate objected. Discovered in
    the board's own directory, like the design brief; `--no-mechanical` is
    the OFF arm.
    """
    parser.add_argument(
        "--mechanical", metavar="JSON", default=None,
        help="mechanical.json: poses (and, in the declaration form, edges) a "
             "run did not choose. Auto-discovered in the board's directory "
             "when omitted. Reconciled against the brief and the board, "
             "compiled at GRADE time into grade-only anchor blocks (never "
             "written into a plan), and graded as mechanical_drift (#959)")
    parser.add_argument(
        "--no-mechanical", action="store_true",
        help="Do not read mechanical.json, even if one sits beside the board. "
             "The OFF arm for an auto-discovered input (#959)")


def _same(a: str, b: str) -> bool:
    """Two spellings of one path, compared the way the filesystem does
    (case-folded on Windows)."""
    import os
    return (os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


def _relocated(recorded: str, sha: str, board_path: str):
    """The recorded mechanical file under its new home, if the run dir
    moved: the same basename beside the regime manifest or beside the board,
    with the recorded sha. None otherwise -- a file with other bytes is not
    the declaration, whatever it is called."""
    import os
    from placement import provenance as PV
    from placement import reconcile
    if not sha:
        return None
    wd = PV.regime_for(board_path)
    cands = [os.path.join(wd, os.path.basename(recorded)) if wd else None,
             reconcile.discover_mechanical(board_path) or None]
    for c in cands:
        if c and os.path.isfile(c) and reconcile._sha256(c) == sha:
            return c
    return None


def load_mechanical_or_exit(args, board_path: str):
    """(mechanical, path, exit_code). `exit_code` is 2 when an explicit
    `--mechanical` names nothing, or the file found is not a mechanical
    declaration this build reads -- a file by that name that reads as "no
    mechanical facts" would be the silent absence #959 is about.

    UNDER AN UNAIDED REGIME whose manifest recorded a `mechanical.json`, that
    file is an input the run was handed, and the run cannot make it
    disappear: `--no-mechanical`, another `--mechanical`, a deleted file and
    a rewritten one (its sha no longer the recorded one) all exit 2. The
    Phase-3 verifier cleared two undispositioned contradictions each way --
    the flag, deleting the file, a lap board copied to a directory without
    it, and a rewrite -- and the recorded path is read wherever the board
    now lives, so a copied board keeps it."""
    import os
    import sys
    from placement import reconcile
    man = reconcile.regime_manifest(board_path)
    recorded = man.get('mechanical') if isinstance(man, dict) else None
    if recorded:
        asked = getattr(args, 'mechanical', None)
        why = None
        if getattr(args, 'no_mechanical', False):
            why = ("--no-mechanical: the unaided regime governing this board "
                   f"recorded {recorded} as an input at staging, and a "
                   "recorded input cannot be switched off")
        elif asked and _same(asked, recorded) is False:
            why = (f"--mechanical {asked}: the unaided regime recorded "
                   f"{recorded} at staging; another file is not it")
        elif not os.path.isfile(recorded):
            # A run dir that was MOVED keeps its bytes: the file beside the
            # regime manifest, or beside the board, with the recorded sha is
            # the same declaration (round-2 verifier: an archived run exited
            # 2 with the file sitting right there).
            found = _relocated(recorded, man.get('mechanical_sha256'),
                               board_path)
            if found:
                recorded = found
            else:
                why = (f"the mechanical declaration the unaided regime "
                       f"recorded at staging, {recorded}, is gone -- "
                       f"restore it")
        if why:
            print(f"cannot use the mechanical declaration: {why}",
                  file=sys.stderr)
            return None, recorded, 2
        try:
            mech = reconcile.load_mechanical(recorded)
        except reconcile.MechanicalError as exc:
            print(f"{exc}", file=sys.stderr)
            return None, recorded, 2
        msha = man.get('mechanical_sha256')
        if msha and mech['sha256'] != msha:
            print(f"cannot use the mechanical declaration: {recorded} "
                  f"changed after staging (sha {mech['sha256'][:12]}, "
                  f"recorded {msha[:12]}). It is an input the run was "
                  f"handed, not the run's to rewrite -- restore it",
                  file=sys.stderr)
            return None, recorded, 2
        return mech, recorded, 0
    if getattr(args, 'no_mechanical', False):
        return None, '', 0
    path = getattr(args, 'mechanical', None)
    if path:
        if not os.path.isfile(path):
            print(f"cannot read mechanical declaration {path}: no such file",
                  file=sys.stderr)
            return None, path, 2
    else:
        path = reconcile.discover_mechanical(board_path)
        if not path:
            return None, '', 0
    try:
        return reconcile.load_mechanical(path), path, 0
    except reconcile.MechanicalError as exc:
        print(f"{exc}", file=sys.stderr)
        return None, path, 2


def add_brief_arg(parser) -> None:
    """`--brief` / `--no-brief`: the DECLARED design intent (#711).

    Defined here for the same reason `--intent` is: one spelling, one
    description of what it buys. The sibling is auto-discovered, following the
    `.kicad_dru` precedent -- the artifact is the source of truth and a board
    that carries one should not need a flag to be believed.

    `--no-brief` exists because an auto-discovered input that changes placement
    needs an OFF arm: an A/B with no way to turn the variable off is not an
    A/B, and a board whose sibling brief you deliberately want ignored has no
    other way to say so.
    """
    parser.add_argument(
        "--brief", metavar="JSON", default=None,
        help="Design brief JSON: what the board file cannot know -- which "
             "connectors are user-facing, which edge each belongs on and "
             "where along it, what the enclosure forbids. Auto-discovered as "
             "the sibling <board>.design-brief.json when this is omitted; "
             "pass a path to override. A DECLARED claim outranks the edge "
             "this toolchain would otherwise infer from the part's current "
             "pose (#711)")
    parser.add_argument(
        "--no-brief", action="store_true",
        help="Do not read the sibling design brief, even if one exists. The "
             "OFF arm: without it there is no way to measure what declaring "
             "changed")


def load_brief_or_exit(args, board_path: str):
    """(brief, path, exit_code). `exit_code` is 2 when a brief is unreadable.

    Called BEFORE `record_invocation`, as `load_intent_or_exit` is, so an exit
    2 never records a manifest command that touched no file.

    An EXPLICIT `--brief` that does not exist is an error; a MISSING sibling
    is not. Those are different acts: naming a file that is not there is a
    mistake worth stopping for, and not having written one is the ordinary
    case this whole channel is designed around.
    """
    import os
    import sys
    from placement import design_brief
    if getattr(args, 'no_brief', False):
        return None, '', 0
    path = getattr(args, 'brief', None)
    if path:
        if not os.path.isfile(path):
            print(f"cannot read design brief {path}: no such file",
                  file=sys.stderr)
            return None, path, 2
    else:
        path = design_brief.discover_brief(board_path)
        if not path:
            return None, '', 0
    try:
        return design_brief.load_brief(path), path, 0
    except (OSError, ValueError) as exc:
        print(f"{exc}", file=sys.stderr)
        return None, path, 2


def add_board_state_args(parser) -> None:
    """`--allow-unplaced` / `--allow-routed` overrides for the two gates."""
    parser.add_argument("--allow-unplaced", action="store_true",
                        help="Run even when the board does not look placed "
                             "(parts stacked at one coordinate). Off by default: "
                             "this toolchain REFINES a placement, so on a pile "
                             "every candidate pose is illegal and the run prints "
                             "'0 parts moved' plus a legality block that looks "
                             "like a result")
    parser.add_argument("--allow-routed", action="store_true",
                        help="Run even when the board already carries copper. "
                             "Off by default: placement moves FOOTPRINTS and "
                             "not tracks, so every segment would be left behind "
                             "detached from its pad")


