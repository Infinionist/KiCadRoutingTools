#!/usr/bin/env python3
"""A fake `claude` CLI for the placement fake-run gate.

Receives the EXACT argv ClaudeBackend.build_cmd produced (the test prepends
this script to it), asserts the placement-run contract (write-capable
allowlist, --add-dir, a /pcb-free-agent prompt), then plays a canned run:
stream-json init + a milestone `converge.py record` tool line, a lap board +
ledger row (left to sit long enough for the monitor's size-stable artifact
scan to render a preview; the row is what the tab's stage line shows now that
there is no staged driver), then REPORT.md, final.kicad_pcb, and a
terminal result event ending with a valid RESULT= line.
"""
import json
import os
import shutil
import sys
import time


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    if "-p" not in argv or "--add-dir" not in argv:
        sys.stderr.write("fake_claude: missing -p or --add-dir\n")
        return 2
    i = argv.index("-p")
    nxt = argv[i + 1] if i + 1 < len(argv) else ""
    if not nxt or nxt.startswith("--"):
        # The npm .cmd path: AISkillRunner strips the prompt from argv (cmd.exe
        # mangles embedded quotes) and pipes it on stdin, like the real CLI
        # accepts. Read it there.
        prompt = sys.stdin.read()
    else:
        prompt = nxt
    workdir = argv[argv.index("--add-dir") + 1]
    allowed = argv[argv.index("--allowedTools") + 1] if "--allowedTools" in argv else ""
    for tool in ("Write", "Edit", "Bash"):
        if tool not in allowed.split(","):
            sys.stderr.write(f"fake_claude: {tool} missing from allowlist: {allowed}\n")
            return 2
    if ("input.kicad_pcb" not in prompt or "--no-delegate" in prompt
            or "/pcb-free-agent " not in prompt):
        # Both tab modes run the free-agent skill on the board staged into
        # the workdir; no retired loop-driver flag.
        sys.stderr.write(f"fake_claude: unexpected prompt: {prompt[:200]}\n")
        return 2
    staged = os.path.join(workdir, "input.kicad_pcb")
    if not os.path.isfile(staged):
        sys.stderr.write(f"fake_claude: staged board missing: {staged}\n")
        return 2
    # Dump the received argv (prompt elided) so the test can assert flags
    # like --model/--effort actually reached the CLI.
    with open(os.path.join(workdir, "argv.json"), "w") as f:
        json.dump([a if a is not prompt else "<prompt>" for a in argv], f)

    emit({"type": "system", "subtype": "init", "model": "fake-model",
          "claude_code_version": "0.0-test", "cwd": workdir,
          "skills": ["pcb-free-agent"]})
    # Both keys, like real Claude Code events. The skill records each
    # milestone in the converge ledger, and the ledger row written below is
    # what the tab's stage line derives from.
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"description": "Record the milestone board",
                   "command": "python3 -X utf8 py_placer/converge.py record "
                              "--ledger ledger.jsonl --board lap1.kicad_pcb "
                              "--kind quench --lever nudge"}}]}})

    # A lap board + a ledger row appear mid-run; then sit quietly long enough
    # for the monitor (1 s ticks, artifact scan every 2nd tick, two same-size
    # scans required) to notice and render a preview frame.
    shutil.copyfile(staged, os.path.join(workdir, "lap1.kicad_pcb"))
    with open(os.path.join(workdir, "ledger.jsonl"), "a") as f:
        f.write(json.dumps({"iteration": 1, "kind": "quench",
                            "lever": "nudge"}) + "\n")
    emit({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Lap 1 recorded."}]}})
    time.sleep(8)

    with open(os.path.join(workdir, "REPORT.md"), "w") as f:
        f.write("# Fake placement report\n\nblocking 0 -> 0 (canned).\n")
    final = os.path.join(workdir, "final.kicad_pcb")
    shutil.copyfile(staged, final)

    result = json.dumps({
        "status": "complete", "board": final.replace("\\", "/"),
        "report": os.path.join(workdir, "REPORT.md").replace("\\", "/"),
        "blocking": 0, "summary": "canned run"})
    emit({"type": "result", "is_error": False,
          "result": f"Canned run complete.\nRESULT={result}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
