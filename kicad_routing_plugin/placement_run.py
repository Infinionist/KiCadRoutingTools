"""
KiCad Routing Tools - headless placement-run contracts (Placement tab).

The Placement tab drives Claude Code headless with the /pcb-free-agent skill,
in its `place` or `full` mode. Unlike the "Ask AI" analysis skills,
those runs WRITE (lap boards, a converge ledger, REPORT.md, the movie), take
minutes to hours, and must be observable from the outside while they run.

This module holds everything about such a run that is not wx: the workdir
layout, the instruction/RESULT contracts, and the read-only pollers the GUI's
monitor timer uses to answer "what is it doing right now" (ledger tail, board
artifacts, stage derivation). Intentionally wx-free so tests can import it
headless, mirroring ai_backend.py.
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time

# ai_backend.DATASHEET_SENTENCE, mirrored: this module is imported flat by
# the tests and as a package by the GUI, and a plugin-only sibling cannot be
# imported both ways (tests/test_plugin_sibling_imports.py).
# tests/test_datasheet_scratch.py holds the two copies equal.
DATASHEET_SENTENCE = (
    "Read datasheets with WebSearch and WebFetch; if you must save a "
    "downloaded file, save it only under $KRT_DATASHEET_DIR, which is deleted "
    "when this run ends."
)

# The write-capable allowlist for placement runs. Write/Edit are the reason
# this cannot ride ai_backend.CLAUDE_ALLOWED_TOOLS (read-only by convention);
# the subagent-dispatch tool stays because the skills dispatch verification
# subagents at close-out. A headless -p run cannot answer permission prompts,
# so anything the skill needs must be listed here (a refused tool errors
# visibly in the transcript).
#
# BOTH dispatch spellings (#552): Claude Code renamed the tool, and a
# permission rule matches the canonical name only. This line has said `Task`
# alone since #633; on 2.1.251 the dispatch event carries `"name":"Agent"`, so
# the close-out verification this comment claims may never have been granted.
#
# Monitor, because /pcb-free-agent's stop rules require watching long jobs
# (never unwatched for more than 20 minutes); without it a headless run can
# only poll with Bash.
#
# WebFetch, so the grouping step can read a part's datasheet rather than a
# search snippet (Andy, 2026-10-15). Downloads go to the run's own datasheet
# folder (ai_backend.datasheet_scratch), which the runner deletes at the end.
PLACEMENT_ALLOWED_TOOLS = (
    "Bash,Read,Glob,Grep,Write,Edit,WebSearch,WebFetch,Agent,Task,TodoWrite,"
    "Monitor")

# The machine-readable completion contract, appended to the instructions.
# Same last-RESULT=-line convention as ai_plan.PLAN_RESULT_SCHEMA, parsed by
# ai_backend.extract_result_line + parse_placement_result below.
PLACEMENT_RESULT_SCHEMA = (
    'RESULT=<compact single-line JSON> with this exact schema: '
    '{"status": "complete"|"residue"|"refused", '
    '"board": "<absolute path to the final board file>", '
    '"movie": "<absolute path to the movie, or null>", '
    '"report": "<absolute path to REPORT.md, or null>", '
    '"blocking": <final board_score blocking count as an integer, or null>, '
    '"summary": "<one line>"}')

# Tab mode -> (skill, the skill's own mode argument). Both tab modes run the
# one free-agent skill; the keys also name the run folders (_RUN_DIR_RE).
PLACEMENT_SKILLS = {
    "place": "pcb-free-agent",
    "place_route": "pcb-free-agent",
}
PLACEMENT_SKILL_MODES = {
    "place": "place",
    "place_route": "full",
}

# Backends that can drive a placement run today. The tab shows ALL backends
# in its dropdown (so the UI already communicates future harness support) but
# reverts any unsupported pick: the skills must WRITE, and e.g. opencode's
# pcb-analysis agent denies edits. Growing this tuple (plus per-backend
# allowlist handling in build_cmd) is the whole cost of adding a harness.
PLACEMENT_SUPPORTED_BACKENDS = ("claude",)

# The folder beside the board that holds one directory per run (#1057).
RUNS_DIRNAME = "krt_placement"
# `*` ignores the .gitignore itself too, so the whole folder stays out of the
# user's repository. Written once, never over a file the user already has.
_RUNS_GITIGNORE = (
    "# KiCad Routing Tools placement runs: scratch, never version it.\n*\n")
# Per-run record of the board the run was started on: several boards can
# share one folder, and a run's own files do not say which one it served.
RUN_MARKER = ".krt_run.json"
# How many runs of one board the folder keeps, the new run included.
KEEP_RUNS = 3
# Only directories named the way create_workdir names them are ever pruned,
# so nothing else a user keeps in the folder can be touched.
_RUN_DIR_RE = re.compile(
    r"^(\d{8}_\d{6})_(?:%s)(?:_(\d+))?$"
    % "|".join(sorted(PLACEMENT_SKILLS, key=len, reverse=True)))


def create_workdir(board_filename, mode):
    """Create and return the run's working directory.

    Lives next to the board file (krt_placement/<stamp>_<mode>/) so the movie
    and REPORT.md survive the session and are easy to find; falls back to the
    system temp dir when the board has no on-disk file yet. The folder gets a
    .gitignore and the run a RUN_MARKER naming its board, which is what lets
    prune_runs keep the folder bounded (#1057).
    """
    base = None
    if board_filename:
        d = os.path.dirname(os.path.abspath(board_filename))
        if os.path.isdir(d):
            base = d
    if base is None:
        base = tempfile.gettempdir()
    root = os.path.join(base, RUNS_DIRNAME)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    # exist_ok=False + suffix retry: a same-second restart must NOT reuse a
    # dirty workdir (its leftover final.kicad_pcb/REPORT.md would be
    # scavenged as the new run's outputs).
    for n in range(1, 100):
        suffix = "" if n == 1 else f"_{n}"
        workdir = os.path.join(root, f"{stamp}_{mode}{suffix}")
        try:
            os.makedirs(workdir, exist_ok=False)
        except FileExistsError:
            continue
        _ensure_gitignore(root)
        _write_run_marker(workdir, board_filename, mode)
        return workdir
    raise OSError(f"could not create a fresh workdir under {base}")


def _ensure_gitignore(root):
    path = os.path.join(root, ".gitignore")
    if os.path.lexists(path):
        return
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_RUNS_GITIGNORE)
    except OSError:
        pass  # a read-only project dir still gets its run


def _write_run_marker(workdir, board_filename, mode):
    # Best effort: a run without a marker is attributed to no board, and
    # prune_runs never deletes an unattributed run.
    try:
        with open(os.path.join(workdir, RUN_MARKER), "w",
                  encoding="utf-8") as f:
            json.dump({
                "board": (os.path.abspath(board_filename)
                          if board_filename else None),
                "mode": mode,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, f)
    except OSError:
        pass


def _board_key(path):
    return os.path.normcase(os.path.abspath(path)) if path else None


def _run_board(run_dir, sole_board):
    """(attributed, board key) for one run directory.

    A run from before RUN_MARKER existed names no board. It is attributed to
    the folder's board when that board is the ONLY one beside the folder --
    it cannot have served another -- and otherwise stays unattributed.
    """
    try:
        with open(os.path.join(run_dir, RUN_MARKER), encoding="utf-8") as f:
            return True, _board_key(json.load(f).get("board"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        return False, None      # unreadable marker: leave the run alone
    if sole_board is None:
        return False, None
    return True, sole_board


def _sole_board(board_dir):
    """The one .kicad_pcb in board_dir (KiCad autosaves excluded), or None."""
    try:
        boards = [n for n in os.listdir(board_dir)
                  if n.endswith(".kicad_pcb") and not n.startswith("_autosave-")
                  and os.path.isfile(os.path.join(board_dir, n))]
    except OSError:
        return None
    return _board_key(os.path.join(board_dir, boards[0])) \
        if len(boards) == 1 else None


def _is_link(path):
    isjunction = getattr(os.path, "isjunction", None)   # Python 3.12+
    return os.path.islink(path) or bool(isjunction and isjunction(path))


def _tree_bytes(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _remove_tree(path):
    """rmtree that clears read-only bits; True when the tree is gone."""
    def _retry(func, p, _exc):
        if func not in (os.unlink, os.remove, os.rmdir):
            return
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass    # judged below by whether the tree is still there
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:
        shutil.rmtree(path, onerror=_retry)
    return not os.path.lexists(path)


def prune_runs(workdir, board_filename, keep):
    """Delete this board's oldest run directories beyond `keep` (#1057).

    `workdir` is the run just created by create_workdir: it counts as one of
    the `keep` and is never deleted. keep <= 0 keeps every run. Only this
    board's runs are candidates (see _run_board); another board's runs in the
    same folder, unattributed runs, links and anything not named like a run
    are left alone.

    Returns {"root", "pruned": [(path, bytes)], "failed": [path],
    "kept": [(path, bytes)], "unattributed": int}.
    """
    root = os.path.dirname(os.path.abspath(workdir))
    result = {"root": root, "pruned": [], "failed": [], "kept": [],
              "unattributed": 0}
    if keep <= 0 or os.path.basename(root) != RUNS_DIRNAME:
        return result
    me = _board_key(board_filename)
    sole = _sole_board(os.path.dirname(root))
    this_run = os.path.normcase(os.path.abspath(workdir))
    runs = []
    try:
        names = os.listdir(root)
    except OSError:
        return result
    for name in names:
        m = _RUN_DIR_RE.match(name)
        path = os.path.join(root, name)
        if (m is None or os.path.normcase(path) == this_run
                or _is_link(path) or not os.path.isdir(path)):
            continue
        attributed, board = _run_board(path, sole)
        if not attributed:
            result["unattributed"] += 1
        elif board == me:
            runs.append(((m.group(1), int(m.group(2) or 1)), path))
    runs.sort(reverse=True)                     # newest first
    keep_others = [p for _k, p in runs[:keep - 1]]
    for path in [workdir] + keep_others:
        result["kept"].append((path, _tree_bytes(path)))
    for _key, path in runs[keep - 1:]:
        size = _tree_bytes(path)
        if _remove_tree(path):
            result["pruned"].append((path, size))
        else:
            # rmtree may have taken the marker before failing on an open
            # file; without it the next run could not retry this one.
            if not os.path.exists(os.path.join(path, RUN_MARKER)):
                _write_run_marker(path, board_filename, None)
            result["failed"].append(path)
    return result


def _fmt_bytes(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def format_prune_note(result):
    """prune_runs' result as log lines ('' when there is nothing to say)."""
    lines = []
    root = result["root"]
    if result["pruned"]:
        kept = result["kept"]
        lines.append(
            f"Placement: removed {len(result['pruned'])} older run(s) of this "
            f"board from {root} "
            f"({_fmt_bytes(sum(b for _p, b in result['pruned']))} freed); "
            f"keeping the newest {len(kept)} "
            f"({_fmt_bytes(sum(b for _p, b in kept))}).")
    if result["failed"]:
        lines.append(
            f"Placement: could not fully remove {len(result['failed'])} old "
            f"run folder(s) (a file may be open); retried at the next run: "
            + ", ".join(result["failed"]))
    if result["unattributed"]:
        lines.append(
            f"Placement: {result['unattributed']} run folder(s) in {root} "
            f"name no board, so they are never pruned; delete them by hand "
            f"if unneeded.")
    return "".join(line + "\n" for line in lines)


def stage_inputs(workdir, snapshot_path, board_filename):
    """Copy the board snapshot (and the real board's project siblings) into
    the workdir, so the whole run needs exactly one --add-dir.

    The snapshot comes from pcbnew.SaveBoard into a temp file, which strands
    the sibling .kicad_pro/.kicad_dru (the DRC floor and per-layer clearance
    rules the route steps auto-read - see CLAUDE.md #441/#498), so those are
    taken from the real board file's directory when present.
    """
    staged = os.path.join(workdir, "input.kicad_pcb")
    shutil.copyfile(snapshot_path, staged)
    if board_filename:
        stem = os.path.splitext(os.path.abspath(board_filename))[0]
        # GUARDED, like route.py's own fallback: this module runs inside
        # KiCad's plugin loader, which does not put py_router on sys.path.
        # An unconditional import here raised ModuleNotFoundError and took
        # `stage_inputs` with it -- caught by tests/test_placement_run.py.
        try:
            from copy_board import SIBLING_EXTS
        except Exception:                                  # noqa: BLE001
            SIBLING_EXTS = (".kicad_pro", ".kicad_prl", ".kicad_dru",
                            ".design-brief.json")
        for ext in SIBLING_EXTS:
            sibling = stem + ext
            if os.path.isfile(sibling):
                shutil.copyfile(sibling, os.path.join(workdir, "input" + ext))
        # mechanical.json is a DIRECTORY file, not a stem sibling:
        # reconcile.discover_mechanical reads <board dir>/mechanical.json, and
        # the run's board is <workdir>/input.kicad_pcb. Unstaged, every
        # declared mechanical fact beside the user's board was invisible to a
        # run launched from this tab, while the same skill run in place saw it.
        try:
            from placement.reconcile import MECHANICAL_NAME
        except Exception:                                  # noqa: BLE001
            MECHANICAL_NAME = "mechanical.json"
        mech = os.path.join(os.path.dirname(stem), MECHANICAL_NAME)
        if os.path.isfile(mech):
            shutil.copyfile(mech, os.path.join(workdir, MECHANICAL_NAME))
    return staged


def _fwd(path):
    """Forward-slash a path for prompt text (Windows-safe, shell-safe)."""
    return os.path.abspath(path).replace("\\", "/")


def build_placement_instructions(workdir, mode, extra=""):
    """The instruction text handed to the skill prompt (after the board arg)."""
    wd = _fwd(workdir)
    lines = [
        "Work headless and unattended; never wait for user input.",
        f"Use {wd} as the working directory for ALL artifacts (ledger, lap "
        "boards, renders, scores, REPORT.md, journal.md, the movie). Keep the "
        f"converge ledger at {wd}/ledger.jsonl.",
        f"Do not modify any file outside {wd}, and never edit "
        "input.kicad_pcb in place.",
        DATASHEET_SENTENCE,
        # #552 item 2. This run's allowlist is the write-capable one, so it is
        # the run whose CHILDREN can actually damage something -- and the
        # workdir pin above was parent-only. A subagent inherits the tools and
        # nothing else, so the pin has to be restated INTO the child, the same
        # way the skills' own <subagent_prompt> blocks are copied verbatim.
        f"If you dispatch a subagent, copy this sentence into its prompt "
        f"verbatim: it must not modify any file outside {wd} (a downloaded "
        f"datasheet may go under $KRT_DATASHEET_DIR), must never edit "
        f"input.kicad_pcb in place, and must answer with a line beginning "
        f"VERDICT= (never RESULT=, which this GUI reads as the run's own "
        f"result line).",
        f"At close-out write the final board to {wd}/final.kicad_pcb (with "
        f"sibling final.kicad_pro), the movie to {wd}/placement.mp4 (or .gif "
        f"fallback), and the report to {wd}/REPORT.md.",
        "If a gate refuses (for example the board already carries routed "
        'copper), stop and report status "refused" instead of stripping '
        "copper.",
        # The GUI's progress line is the newest ledger row, so the skill's
        # milestone rows are what the user sees while the run works.
        "Record every milestone board in that ledger as you go (the skill's "
        "`converge.py record` step): it is the only progress this GUI shows.",
    ]
    if extra and extra.strip():
        lines.append(extra.strip())
    lines.append("After the report, end your reply with exactly one line of "
                 "the form " + PLACEMENT_RESULT_SCHEMA)
    return " ".join(lines)


def build_placement_prompt(backend, workdir, staged_board, mode, extra=""):
    """The full skill prompt for a placement run (via backend.skill_prompt)."""
    return backend.skill_prompt(
        PLACEMENT_SKILLS[mode],
        f"{PLACEMENT_SKILL_MODES[mode]} {_fwd(staged_board)}",
        build_placement_instructions(workdir, mode, extra))


def _norm_existing(path):
    """Normalize a path from the RESULT JSON; return it if it exists."""
    if not path:
        return None
    p = os.path.normpath(str(path).replace("\\", "/").replace("/", os.sep))
    return p if os.path.isfile(p) else None


def parse_placement_result(value):
    """Parse the RESULT= JSON payload -> (outputs dict | None, [errors]).

    outputs: {status, board, movie, report, blocking, summary}. A missing or
    non-existing board path is fatal for complete/residue (the caller falls
    back to scan_workdir_outputs); movie/report problems are non-fatal.
    """
    errors = []
    if not value:
        return None, ["no RESULT= line in the agent's reply"]
    try:
        data = json.loads(value)
    except (json.JSONDecodeError, ValueError) as e:
        return None, [f"RESULT= payload is not valid JSON: {e}"]
    if not isinstance(data, dict):
        return None, ["RESULT= payload is not a JSON object"]
    status = data.get("status")
    if status not in ("complete", "residue", "refused"):
        return None, [f"RESULT= status {status!r} not in complete/residue/refused"]
    board = _norm_existing(data.get("board"))
    if board is None and status != "refused":
        return None, [f"RESULT= board path missing or not found: {data.get('board')!r}"]
    movie = _norm_existing(data.get("movie"))
    if data.get("movie") and movie is None:
        errors.append(f"movie path not found: {data.get('movie')!r}")
    report = _norm_existing(data.get("report"))
    if data.get("report") and report is None:
        errors.append(f"report path not found: {data.get('report')!r}")
    blocking = data.get("blocking")
    # `isinstance(True, int)` holds, so the bool test is not redundant: a
    # `false` or a `-1` used to pass validation and reach the status line as
    # "blocking False" (#1075).
    if blocking is not None and (isinstance(blocking, bool)
                                 or not isinstance(blocking, int)
                                 or blocking < 0):
        errors.append(f"blocking is not a non-negative integer: {blocking!r}")
        blocking = None
    return {
        "status": status,
        "board": board,
        "movie": movie,
        "report": report,
        "blocking": blocking,
        "summary": str(data.get("summary", "")),
    }, errors


def list_board_artifacts(workdir):
    """Top-level *.kicad_pcb files -> [(path, mtime, size)], oldest first.

    Deliberately not recursive: the converge boards/<sha> CAS store and
    attempt archives churn far too much to preview; the interesting boards
    (input, lap*, loop_round*, placed, final) land at the top level.
    """
    out = []
    try:
        names = os.listdir(workdir)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".kicad_pcb"):
            continue
        path = os.path.join(workdir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue  # vanished mid-scan
        out.append((path, st.st_mtime, st.st_size))
    out.sort(key=lambda t: t[1])
    return out


def newest_stable_board(artifacts, prev_artifacts):
    """The newest board whose size is unchanged since the previous scan.

    A board still being written grows between ticks; requiring two scans at
    the same size keeps the preview renderer off torn files. prev_artifacts
    is the previous call's list (or None on the first tick).
    """
    prev_sizes = {p: s for p, _m, s in (prev_artifacts or [])}
    for path, _mtime, size in reversed(artifacts):
        if size > 0 and prev_sizes.get(path) == size:
            return path
    return None


def read_ledger_tail(ledger_path, offset):
    """Incrementally read complete ledger.jsonl rows -> (rows, new_offset).

    Only lines terminated by a newline are consumed, so a torn last line is
    left for the next tick. Unparseable complete lines are skipped (their
    bytes are still consumed).
    """
    rows = []
    try:
        with open(ledger_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return rows, offset
    if not chunk:
        return rows, offset
    end = chunk.rfind(b"\n")
    if end < 0:
        return rows, offset  # only a torn partial line so far
    for line in chunk[:end].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, offset + end + 1


_ARTIFACT_STAGES = (
    (re.compile(r"loop_round(\d+)_route\.log$"), "routing round {0}"),
    (re.compile(r"loop_round(\d+)_routed\.kicad_pcb$"), "routing round {0}"),
    (re.compile(r"loop_round(\d+)\.kicad_pcb$"), "placing round {0}"),
    (re.compile(r"render_lap(\d+)"), "rendering lap {0}"),
    (re.compile(r"^score.*\.json$"), "scoring the board"),
    (re.compile(r"^lap(\d+)\.kicad_pcb$"), "lap {0}"),
)


def _row_label(row):
    """What a ledger row DID, in one line. `converge.row_label` is the AUTHORITY.

    The import is guarded the same way `SIBLING_EXTS` above is, and for the same
    reason: this module runs inside KiCad's plugin loader, which does not put
    py_placer on sys.path. The fallback below is a copy, and a copy is a thing
    that drifts -- `tests/test_904_lens_file_binding.py` compares the two on the
    same rows so it cannot drift silently.

    What it fixes: `ledger_row.get("lever") or "?"` ended the ladder at the
    first field, so an `--exhausted` declaration -- which has `lever: null` by
    construction, its whole content being the reason a person wrote -- rendered
    in the GUI as `lap 31: systemic/?`.
    """
    try:
        from converge import row_label
        return row_label(row)
    except Exception:                                      # noqa: BLE001
        lever = str(row.get("lever") or "").strip()
        if lever:
            return lever
        dec = row.get("exhausted")
        if isinstance(dec, dict) and str(dec.get("reason") or "").strip():
            return "declared exhausted: " + str(dec["reason"]).strip()
        stop = str(row.get("stop_condition") or "").strip()
        if stop:
            return "close-out: " + stop
        return "(no lever recorded)"


def derive_stage(transcript_tail, ledger_row, newest_artifact_name):
    """Best human answer to "what is it doing right now".

    Priority: the newest converge ledger row (the skill records a row per
    milestone) -> artifact-name heuristics -> a generic fallback.
    `transcript_tail` is kept in the signature for the caller; there is no
    staged driver whose `--stage` ids it could name any more.
    """
    if ledger_row:
        lap = ledger_row.get("iteration")
        kind = ledger_row.get("kind") or "?"
        return f"lap {lap}: {kind}/{_row_label(ledger_row)}"
    if newest_artifact_name:
        for rx, fmt in _ARTIFACT_STAGES:
            m = rx.search(newest_artifact_name)
            if m:
                return fmt.format(*m.groups())
    return "working..."


def scan_workdir_outputs(workdir):
    """Fallback outputs when the RESULT= line is missing or unusable.

    Scavenges what the run left behind: the declared final board (else the
    newest stable non-input board), REPORT.md, and the movie (mp4 preferred,
    gif fallback). Same dict shape as parse_placement_result, with
    status "scavenged" so callers can say so.
    """
    artifacts = [t for t in list_board_artifacts(workdir)
                 if os.path.basename(t[0]) != "input.kicad_pcb"]
    board = os.path.join(workdir, "final.kicad_pcb")
    if not os.path.isfile(board):
        # No newer scan to compare sizes against - the run is over, so any
        # remaining file is as stable as it will ever get.
        board = newest_stable_board(artifacts, artifacts)
    report = os.path.join(workdir, "REPORT.md")
    if not os.path.isfile(report):
        report = None
    movie = None
    for name in ("placement.mp4", "placement.gif"):
        candidate = os.path.join(workdir, name)
        if os.path.isfile(candidate):
            movie = candidate
            break
    if movie is None:
        try:
            movies = sorted(
                (os.path.join(workdir, n) for n in os.listdir(workdir)
                 if n.endswith((".mp4", ".gif"))),
                key=lambda p: os.path.getmtime(p))
        except OSError:
            movies = []
        movie = movies[-1] if movies else None
    return {
        "status": "scavenged",
        "board": board,
        "movie": movie,
        "report": report,
        "blocking": None,
        "summary": "",
    }
