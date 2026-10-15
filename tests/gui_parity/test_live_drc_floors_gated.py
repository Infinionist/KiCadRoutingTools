#!/usr/bin/env python3
"""#693: every DRC-floor writer a GUI run reaches must be gated on 'fix DRC
settings'.

The GUI had TWO independent DRC writers, and only one of them was gated:

  1. apply_targets_to_board   -- netclasses + severities.  GATED.
  2. update_live_drc_floors   -- the live board's design-settings FLOORS
     (m_MinClearance, m_TrackMinWidth, m_ViasMinSize, m_MinThroughDrill,
     m_ViasMinAnnularWidth, m_HoleToHoleMin, + the Default netclass).  NOT.

So unchecking "Fix DRC settings after routing" suppressed (1) and left (2)
rewriting Board Setup anyway. The reporter watched Minimum annular width go
0.15 -> 0.05 with the box unchecked (#693). It was ungated at ALL FIVE call
sites -- every routing tab plus the plan executor -- so this is a drift guard,
not a spot fix: the CLI gates the twin (fix_project_for_output) on
--no-fix-drc-settings, and a sixth call site must not silently reintroduce the
bug.

The plan executor's end-of-plan writeback also writes the Default class's
diff-pair floors and the project file (fix_project_for_output); those were
ungated until the box became a VETO a plan cannot override. So every writer
below is held to the same rule, wherever it is called -- plus the executor's
half of the veto: the user's value goes back in a `finally`, so no way a plan
ends (completion, Stop, a raising step) leaves the box where the steps put it.

Pure AST, no wx and no pcbnew -- runs anywhere in about a second.
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = os.path.join(ROOT, "kicad_routing_plugin")
# Called by name (update_live_drc_floors(...)), or reached as a bound method
# (the Default netclass's SetDiffPairWidth / SetDiffPairGap).
WRITERS = ("update_live_drc_floors", "apply_targets_to_board",
           "apply_fix_to_board",
           "fix_project_for_output", "SetDiffPairWidth", "SetDiffPairGap")
# A gate is any enclosing `if` whose test mentions the toggle. ai_plan reads the
# live checkbox into a local first (it owns the real dialog), so accept that too.
GATE_TOKENS = ("fix_drc", "_fixdrc693")


def _writer(node):
    if isinstance(node, ast.Call):
        return getattr(node.func, "id", None) if getattr(
            node.func, "id", None) in WRITERS else None
    if isinstance(node, ast.Attribute) and node.attr in WRITERS:
        return node.attr
    return None


def check_writers():
    fails, checked = [], {w: 0 for w in WRITERS}
    for name in sorted(os.listdir(PLUGIN)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(PLUGIN, name)
        src = open(path, encoding="utf-8").read()
        if not any(w in src for w in WRITERS):
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            writer = _writer(node)
            if writer is None:
                continue
            checked[writer] += 1
            line = node.lineno
            tests = [ast.get_source_segment(src, n.test) or ""
                     for n in ast.walk(tree)
                     if isinstance(n, ast.If)
                     and n.lineno <= line <= (n.end_lineno or n.lineno)]
            if not any(tok in t for t in tests for tok in GATE_TOKENS):
                fails.append(
                    f"{name}:{line} reaches {writer} WITHOUT a "
                    f"'fix DRC settings' gate -- an unchecked box would still "
                    f"rewrite the board's DRC floors (#693)")
    return fails, checked


def check_executor_restores():
    """PlanExecutor._finish must put the user's value back in a `finally`."""
    src = open(os.path.join(PLUGIN, "ai_plan.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fin = next((n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_finish"),
               None)
    if fin is None:
        return ["ai_plan.py has no _finish -- the restore cannot be checked"]
    for n in ast.walk(fin):
        if isinstance(n, ast.Try) and any(
                getattr(getattr(c, "func", None), "id", None)
                == "restore_fix_drc_preference"
                for b in n.finalbody for c in ast.walk(b)):
            return []
    return ["PlanExecutor._finish does not restore the user's 'Fix DRC "
            "settings' value in a `finally`: a plan that stops or raises "
            "would leave the box where the last step put it, and that is "
            "what settings_persistence saves on close"]


def main():
    fails, checked = check_writers()
    missing = [w for w, n in checked.items()
               if n == 0 and w != "fix_project_for_output"
               and not w.startswith("SetDiffPair")]
    if missing:
        print(f"FAIL: found no {', '.join(missing)} call sites at all -- "
              f"renamed? Update this gate rather than deleting it.")
        return 1
    fails += check_executor_restores()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1
    print("PASS: all %d DRC-floor writer site(s) (%s) are gated on the 'Fix "
          "DRC settings after routing' toggle (#693), and the plan executor "
          "restores the user's value on every exit"
          % (sum(checked.values()),
             ', '.join(f"{w} x{n}" for w, n in checked.items() if n)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
