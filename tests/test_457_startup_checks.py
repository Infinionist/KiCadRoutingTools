"""
Tests for startup_checks raising rather than exiting (issue #457 item 3).

`startup_checks` called `sys.exit(1)` when numpy/scipy/shapely were missing or the
Rust router was absent or stale, and route.py / route_diff.py / route_planes.py /
repair_planes.py all call it at MODULE scope. A `SystemExit` raised
during pytest collection escapes as an INTERNALERROR rather than a per-file
collect error, so on a checkout with no built router the 8 test files that import
a routing module at module level took the whole ~200-test suite down with them.

The checks now raise `StartupCheckError`. The CLI behaviour is deliberately
unchanged — `exit_on_error_if_main(__name__)` prints the same message to stdout
and exits 1 when the module IS the program — but an importer gets a catchable
exception instead of a dead interpreter.

This module had ZERO test coverage before.
"""

import io
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

import startup_checks
from startup_checks import (MIN_PYTHON, StartupCheckError, check_python_dependencies,
                           check_rust_library, exit_on_error_if_main, run_all_checks)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _NoModule:
    """Import hook that makes specific top-level modules unimportable."""

    def __init__(self, *names):
        self.names = set(names)

    def find_module(self, name, path=None):     # py2-style, still consulted
        return self if name.split('.')[0] in self.names else None

    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in self.names:
            raise ImportError(f"blocked for test: {name}")
        return None


def _without(*names):
    """Context: those modules raise ImportError, and are evicted from the cache."""
    class Ctx:
        def __enter__(self):
            self.hook = _NoModule(*names)
            sys.meta_path.insert(0, self.hook)
            self.saved = {k: v for k, v in sys.modules.items()
                          if k.split('.')[0] in names}
            for k in self.saved:
                del sys.modules[k]
            return self

        def __exit__(self, *exc):
            sys.meta_path.remove(self.hook)
            sys.modules.update(self.saved)
            return False
    return Ctx()


# --- the checks raise, and say something useful ------------------------------

def test_missing_python_dep_raises_not_exits():
    with _without('scipy'):
        try:
            check_python_dependencies()
        except StartupCheckError as exc:
            msg = str(exc)
        except SystemExit:                       # the bug
            raise AssertionError(
                "check_python_dependencies still calls sys.exit; pytest "
                "collection cannot survive that")
        else:
            raise AssertionError("expected StartupCheckError")
    assert 'scipy' in msg, f"missing library not named: {msg!r}"
    assert 'pip install' in msg, f"no install instruction: {msg!r}"


def test_startup_check_error_is_a_runtime_error():
    """So `except Exception` sites keep working, and it is not a SystemExit."""
    assert issubclass(StartupCheckError, RuntimeError)
    assert not issubclass(StartupCheckError, SystemExit)


def test_missing_rust_router_raises_and_keeps_the_import_error_text():
    """The dlopen message is the ONLY place the real cause appears; a bare
    `except ImportError` used to discard it."""
    with _without('grid_router'):
        try:
            check_rust_library()
        except StartupCheckError as exc:
            msg = str(exc)
        except SystemExit:
            raise AssertionError("check_rust_library still calls sys.exit")
        else:
            raise AssertionError("expected StartupCheckError")
    assert 'Rust router module not found' in msg, msg
    assert 'blocked for test' in msg, \
        f"the underlying ImportError text was swallowed: {msg!r}"
    assert 'build_router.py' in msg, msg


def test_run_all_checks_propagates():
    with _without('shapely'):
        try:
            run_all_checks()
        except StartupCheckError:
            return
        except SystemExit:
            raise AssertionError("run_all_checks still exits")
    raise AssertionError("expected StartupCheckError")


def test_python_floor_matches_the_abi3_pin():
    """MIN_PYTHON must track rust_router/Cargo.toml's abi3 feature, or the
    message tells users the wrong version."""
    cargo = open(os.path.join(ROOT, 'rust_router', 'Cargo.toml'),
                 encoding='utf-8').read()
    tag = f"abi3-py{MIN_PYTHON[0]}{MIN_PYTHON[1]}"
    assert tag in cargo, \
        f"MIN_PYTHON={MIN_PYTHON} implies {tag}, not found in Cargo.toml"


# --- CLI behaviour is unchanged ----------------------------------------------

_PROBE = textwrap.dedent('''
    import sys
    import startup_checks


    def boom():
        raise startup_checks.StartupCheckError("SIMULATED: router missing")


    startup_checks.check_rust_library = boom
    startup_checks.exit_on_error_if_main(sys.argv[1])
    print("REACHED-END")
''')


def _run_probe(module_name):
    # #522: startup_checks lives under py_router/ now; the probe subprocess
    # needs it on ITS path (rust_router for the library the checks probe).
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        (os.path.join(ROOT, 'py_router'),
         os.path.join(ROOT, 'rust_router'), ROOT)))
    return subprocess.run([sys.executable, '-c', _PROBE, module_name],
                          capture_output=True, text=True, cwd=ROOT, env=env)


def test_as_main_prints_and_exits_one():
    """A user running `python3 py_router/route.py` must see exactly what they saw before."""
    r = _run_probe('__main__')
    assert r.returncode == 1, f"expected exit 1, got {r.returncode}"
    assert 'SIMULATED: router missing' in r.stdout, \
        f"message not printed to stdout: {r.stdout!r}"
    assert 'Traceback' not in r.stderr, \
        f"a CLI user should not see a traceback: {r.stderr!r}"
    assert 'REACHED-END' not in r.stdout


def test_as_import_raises_instead():
    """An importer gets the exception, so pytest can attribute it to a file."""
    r = _run_probe('route')
    assert r.returncode == 1
    assert 'StartupCheckError' in r.stderr, \
        f"expected the exception to propagate: {r.stderr!r}"
    assert 'REACHED-END' not in r.stdout


def test_routing_clis_use_the_guard_at_module_scope():
    """All four entry points must go through the helper, and must still do it
    ABOVE their heavy imports so a missing dep is reported before numpy or
    grid_router fails with something cryptic."""
    for name in ('route.py', 'route_diff.py', 'route_planes.py',
                 'repair_planes.py'):
        # #522: the routing CLIs live under py_router/ now.
        src = open(os.path.join(ROOT, 'py_router', name),
                   encoding='utf-8').read()
        assert 'exit_on_error_if_main(__name__)' in src, \
            f"{name} does not use the import-safe guard"
        assert 'run_all_checks()' not in src, \
            f"{name} still calls run_all_checks() directly, which exits on import"
        guard = src.index('exit_on_error_if_main(__name__)')
        for heavy in ('\nimport numpy', '\nfrom kicad_parser', '\nimport grid_router'):
            if heavy in src:
                assert src.index(heavy) > guard, \
                    f"{name}: {heavy.strip()} is imported before the startup guard"


def test_gui_catches_the_new_exception_type():
    """CLAUDE.md's CLI/GUI rule: swig_gui catches what `import route` throws.
    Catching only SystemExit would turn its dependency dialog into a traceback.
    (wx/pcbnew-only code, so this is a source assertion, not an execution.)"""
    src = open(os.path.join(ROOT, 'kicad_routing_plugin', 'swig_gui.py'),
               encoding='utf-8').read()
    assert '_STARTUP_FAILURES' in src, \
        "swig_gui does not reference the startup-failure tuple"
    assert 'except _STARTUP_FAILURES' in src, \
        "swig_gui still catches only SystemExit around `import route`"
    assert 'StartupCheckError' in src


# --- the CLI gate FINDS the probed submodule; the GUI gate LOADS it ---------

_PROBE_CHILD = textwrap.dedent("""
    import sys
    sys.path.insert(0, %r)
    import startup_checks as S
    try:
        S.check_python_dependencies()
        print('GATE-OK')
    except S.StartupCheckError as e:
        print('GATE-REFUSED', e)
    print('OPTIMIZE-LOADED', 'scipy.optimize' in sys.modules)
    print('DEEP', sorted(p.name for p in
                         S.dependency_problems(S.ROUTING_PACKAGES)))
""")

# A scipy.optimize whose spec is FOUND but whose import fails: the shape of a
# broken compiled build (a DLL that will not load), which find_spec alone
# cannot see.
_BROKEN_OPTIMIZE = textwrap.dedent("""
    import importlib.abc, importlib.util, sys
    class _Broken(importlib.abc.Loader):
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            raise ImportError('simulated broken scipy.optimize build')
    class _Finder:
        def find_spec(self, name, path=None, target=None):
            if name == 'scipy.optimize':
                return importlib.util.spec_from_loader(name, _Broken(),
                                                       is_package=True)
            return None
    sys.meta_path.insert(0, _Finder())
""")


def _gate_child(prelude=''):
    r = subprocess.run([sys.executable, '-X', 'utf8', '-c',
                        prelude + _PROBE_CHILD % os.path.join(ROOT,
                                                              'py_router')],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=ROOT)
    out = r.stdout + r.stderr
    assert 'OPTIMIZE-LOADED' in out, f"BROKEN TEST: the probe died: {out}"
    return out


def test_the_cli_gate_does_not_load_scipy_optimize():
    """The point of `load=False`: `from scipy.optimize import ...` was ~0.9 s
    of every CLI launch, and nothing at routing module scope needs it."""
    out = _gate_child()
    assert 'GATE-OK' in out, out
    assert 'OPTIMIZE-LOADED False' in out, \
        f"check_python_dependencies loaded scipy.optimize again: {out}"


def test_a_missing_scipy_is_absent_through_either_probe():
    """The user-facing refusal is unchanged: same Problem, same message."""
    with _without('scipy'):
        cli = startup_checks.dependency_problems(['scipy'], load=False)
        gui = startup_checks.dependency_problems(['scipy'])
    assert [(p.name, p.state) for p in cli] == [('scipy', 'absent')], cli
    assert [(p.name, p.state) for p in gui] == [('scipy', 'absent')], gui
    assert cli[0].describe() == gui[0].describe()


def test_a_broken_submodule_is_the_deep_probes_job():
    """The documented trade: a FOUND-but-unloadable scipy.optimize passes the
    CLI gate (routing then degrades where it imports scipy) and is still
    refused by the deep probe the GUI's first-launch dialog and the installer
    run, which is where a broken install can be repaired."""
    out = _gate_child(_BROKEN_OPTIMIZE)
    assert 'GATE-OK' in out, out
    assert "DEEP ['scipy']" in out, \
        f"the deep probe no longer catches a broken submodule: {out}"


def test_the_probed_module_is_read_from_the_one_table():
    assert startup_checks.probed_module('scipy') == 'scipy.optimize'
    assert startup_checks.probed_module('shapely') == 'shapely.geometry'
    assert startup_checks.probed_module('numpy') == 'numpy'
    assert startup_checks.probed_module('Pillow') == 'PIL'
    assert startup_checks.probed_module('_kr_unlisted') == '_kr_unlisted'


TESTS = [
    test_missing_python_dep_raises_not_exits,
    test_startup_check_error_is_a_runtime_error,
    test_missing_rust_router_raises_and_keeps_the_import_error_text,
    test_run_all_checks_propagates,
    test_python_floor_matches_the_abi3_pin,
    test_as_main_prints_and_exits_one,
    test_as_import_raises_instead,
    test_routing_clis_use_the_guard_at_module_scope,
    test_gui_catches_the_new_exception_type,
    test_the_cli_gate_does_not_load_scipy_optimize,
    test_a_missing_scipy_is_absent_through_either_probe,
    test_a_broken_submodule_is_the_deep_probes_job,
    test_the_probed_module_is_read_from_the_one_table,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
