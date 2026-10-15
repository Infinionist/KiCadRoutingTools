#!/usr/bin/env python3
"""Would a tool's OWN argparse accept this recorded command line?

A recorded manifest keeps every command the agent ran, including the ones the
tool REJECTED at argument parsing -- hexberry_fpga's `qfn_fanout.py ...
--dry-run` (qfn_fanout has no --dry-run: argparse error, exit 2, no board).
The manifest records no exit status, and the pruner keeps a command by the
boards it names, so a rejected command that names an output a later step
reads is KEPT, and manifest_to_plan used to convert it into a plan step that
runs, and succeeds, in the GUI -- where the CLI ran nothing at all.

`rejection(argv)` answers with the tool's real parser, not a copy of its flag
list: the parser is captured in a CHILD process (it is built inside each
tool's `main()` or `__main__` block, whose side effects -- the redo-manifest
recorder, cli_banner's exit echo, the provenance registry -- must not run in
the caller), at its first `ArgumentParser.parse_args` call, which is then
handed each command line in turn. One child per tool, kept for the life of
the calling process, so converting the whole corpus costs one parser build
per tool rather than one per command.

The parser is TODAY'S. A command recorded under an older commit that used a
flag removed since then reads as rejected here; that is what a replay of it
would do today too, and `rejection` says which flag in its reason.
"""
import atexit
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The tools manifest_to_plan converts, by basename -> path under the repo.
TOOL_PATHS = {
    'route.py': 'py_router/route.py',
    'route_diff.py': 'py_router/route_diff.py',
    'route_planes.py': 'py_router/route_planes.py',
    'repair_planes.py': 'py_router/repair_planes.py',
    'route_disconnected_planes.py': 'py_router/repair_planes.py',
    'bga_fanout.py': 'py_router/bga_fanout.py',
    'qfn_fanout.py': 'py_router/qfn_fanout.py',
    'place_fanout_clearance.py': 'py_placer/place_fanout_clearance.py',
}

_CHILD = r'''
import argparse, contextlib, io, json, os, runpy, sys
tool = sys.argv[1]
real = argparse.ArgumentParser.parse_args
class _Got(BaseException):
    pass
got = {}
def capture(self, args=None, namespace=None):
    got['p'] = self
    raise _Got()
argparse.ArgumentParser.parse_args = capture
sys.argv = [tool]
sys.path.insert(0, os.path.dirname(os.path.abspath(tool)))
out = sys.stdout
try:
    with contextlib.redirect_stdout(io.StringIO()):
        runpy.run_path(tool, run_name='__main__')
except _Got:
    pass
except BaseException as e:
    pass
argparse.ArgumentParser.parse_args = real
p = got.get('p')
out.write('##READY %s\n' % ('ok' if p is not None else 'none'))
out.flush()
if p is None:
    os._exit(0)
for line in sys.stdin:
    args = json.loads(line)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            p.parse_args(args)
        reply = {'ok': True}
    except SystemExit as e:
        msg = [l for l in err.getvalue().splitlines() if l.strip()]
        reply = {'ok': False, 'code': e.code,
                 'why': msg[-1] if msg else 'exited %r while parsing' % (e.code,)}
    except Exception as e:
        reply = {'ok': False, 'code': None,
                 'why': '%s: %s' % (type(e).__name__, e)}
    out.write('##R ' + json.dumps(reply) + '\n')
    out.flush()
os._exit(0)
'''

_children = {}


def _child(rel):
    """The live checker for one tool, started on first use; None when its
    parser could not be captured (the caller then accepts the command)."""
    if rel in _children:
        return _children[rel]
    env = dict(os.environ)
    env.pop('REDO_MANIFEST', None)    # the recorder must not log the probe
    proc = subprocess.Popen(
        [sys.executable, '-c', _CHILD, os.path.join(REPO, rel)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, cwd=REPO, env=env)
    ready = None
    for line in proc.stdout:
        if line.startswith('##READY'):
            ready = line.split()[1]
            break
    if ready != 'ok':
        proc.kill()
        proc = None
    _children[rel] = proc
    return proc


def _close():
    for proc in _children.values():
        if proc is not None:
            try:
                proc.stdin.close()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


atexit.register(_close)


def tool_args(argv):
    """(tool basename, the arguments after it) for a recorded command whose
    tool this module can check, else None."""
    for k, a in enumerate(argv):
        base = os.path.basename(a)
        if base in TOOL_PATHS:
            return base, list(argv[k + 1:])
    return None


def _flag_list_rejection(tool, args):
    """The fallback when the tool's parser cannot be built here (a missing
    dependency in this interpreter): its long flags as krt_capabilities reads
    them from the source -- held exact against the real parsers by
    tests/test_798_registrar_flags.py -- with argparse's own rule that an
    unambiguous prefix of a long option is that option. It sees an unknown
    flag (the --dry-run class), not a bad value or a missing argument."""
    try:
        sys.path.insert(0, REPO)
        import krt_capabilities as caps
        flags = set(caps.script_flags(caps._tool_path(caps.ROOT, tool)))
    except Exception:
        return None
    if len(flags) < 5:        # read nothing: nothing to refuse on
        return None
    for a in args:
        if not a.startswith('--') or a == '--':
            continue
        name = a.split('=', 1)[0]
        if name in flags or sum(f.startswith(name) for f in flags) == 1:
            continue
        return (f"{tool} rejects its arguments: unrecognized argument {name} "
                f"(its parser could not be built here; checked against the "
                f"tool's flag list)")
    return None


def rejection(argv):
    """Why the recorded command's tool would refuse its arguments at parse
    time (argparse's own message), or None when it would accept them, or when
    the tool is not one this module checks. Where the parser cannot be built,
    the tool's flag list stands in (`_flag_list_rejection`)."""
    found = tool_args(argv)
    if found is None:
        return None
    tool, args = found
    proc = _child(TOOL_PATHS[tool])
    if proc is None:
        return _flag_list_rejection(tool, args)
    proc.stdin.write(json.dumps(args) + '\n')
    proc.stdin.flush()
    for line in proc.stdout:
        if line.startswith('##R '):
            reply = json.loads(line[4:])
            if reply['ok']:
                return None
            if reply.get('code') == 0:
                return f"{tool} prints and exits at parse time ({reply['why']})"
            return f"{tool} rejects its arguments: {reply['why']}"
    return None


if __name__ == '__main__':
    print(rejection(sys.argv[1:]) or 'accepted')
