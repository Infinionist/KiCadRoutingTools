#!/usr/bin/env python3
"""A plugin module imports its plugin-only siblings RELATIVELY (#726 follow-up).

KiCad loads the plugin through the repo-root `__init__.py`, which puts the
repo root on sys.path; `action_plugin.py` (and each tab module) then adds
py_router/, py_placer/ and py_tools/. Nothing adds kicad_routing_plugin/
itself. So a module that exists ONLY there -- `gui_utils`, `placement_run`,
... -- cannot be imported by bare name in KiCad: `from gui_utils import
live_footprints_by_key` raised ModuleNotFoundError there, which took the
Placement tab's Apply down before it moved a part (and, inside a broad
`except`, turned the fanout tab's cap moves into "Cap optimization skipped").

The GUI gates could not see it: every one of them puts kicad_routing_plugin/
on sys.path, so the bare name resolved in the test and nowhere else.

Two halves, both measured every run:
  1. the PREMISE -- on the loader's sys.path a plugin-only module does not
     resolve by bare name (if that ever changes, this file says so instead of
     enforcing a rule nobody needs);
  2. the RULE -- no `import X` / `from X import` (level 0) of a plugin-only
     module anywhere in kicad_routing_plugin/, inside a try or not: a broad
     `except` around one is how the fanout case shipped silently.

    python3 tests/test_plugin_sibling_imports.py
"""
import ast
import importlib.machinery
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, 'kicad_routing_plugin')
#: What the real loader puts on sys.path (root __init__.py + action_plugin.py).
LOADER_PATH = [REPO] + [os.path.join(REPO, d)
                        for d in ('py_router', 'py_placer', 'py_tools')]


def plugin_only_modules():
    """Module names that exist in kicad_routing_plugin/ and on NO loader path."""
    own = {f[:-3] for f in os.listdir(PLUGIN)
           if f.endswith('.py') and f != '__init__.py'}
    return {m for m in own
            if importlib.machinery.PathFinder.find_spec(m, LOADER_PATH) is None}


def bare_imports(only):
    """[(file, line, module)] for every level-0 import of a name in `only`."""
    hits = []
    for fn in sorted(os.listdir(PLUGIN)):
        if not fn.endswith('.py'):
            continue
        path = os.path.join(PLUGIN, fn)
        with open(path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split('.')[0]]
            elif isinstance(node, ast.Import):
                names = [a.name.split('.')[0] for a in node.names]
            else:
                continue
            hits += [(fn, node.lineno, n) for n in names if n in only]
    return hits


def main():
    failures = []
    only = plugin_only_modules()
    # The premise, and a scan that looked at something: an empty set would
    # make the rule below pass on any tree.
    if 'gui_utils' not in only:
        failures.append("PREMISE: gui_utils resolves on the loader's sys.path "
                        f"{LOADER_PATH} -- the rule this file enforces is moot; "
                        "retire it rather than keep it")
    if len(only) < 10:
        failures.append(f"scan found only {len(only)} plugin-only module(s); "
                        "it is not looking where it thinks it is")
    hits = bare_imports(only)
    for fn, line, mod in hits:
        failures.append(f"kicad_routing_plugin/{fn}:{line}: bare import of "
                        f"plugin-only `{mod}` -- ModuleNotFoundError in KiCad; "
                        f"use `from .{mod} import ...`")
    print(f"  {len(only)} plugin-only module(s); {len(hits)} bare import(s)")
    for f in failures:
        print(f"  FAIL: {f}")
    if failures:
        return 1
    print("  PASS: every plugin-only sibling is imported relatively")
    return 0


if __name__ == '__main__':
    sys.exit(main())
