"""
actions/run_shell.py — run a terminal command, behind the gate, always.

WHY THIS EXISTS
    Until now she had no shell at all. "git pull origin main" fell into
    code_helper.py's file runner — a tool that executes script FILES
    through an interpreter map keyed by extension — and died with
    "No interpreter for .", because a shell command is not a file and
    no other tool could take it either. The subprocess calls elsewhere
    in the codebase live inside specific features (open this app,
    update that game); none is a command line the assistant can be
    handed. This tool is that command line.

THE GATE — AND THE ONE RULE THAT MAKES THIS TOOL DIFFERENT
    A shell command is arbitrary code with the user's full
    permissions, so every invocation parks behind core/confirm.py's
    on-screen banner, showing the EXACT command and working directory
    that will run. Nothing executes until a human presses CONFIRM.

    Deliberate rule: this tool NEVER rides confirm.trusted_scope().
    Every other gated action lets request() run its callable at once
    inside a trusted routine replay — the trust was the user's answer,
    given in advance, for an inspectable step list. A shell step's
    step list is not inspectable in that sense: the command string is
    the whole program, and "the routine was trusted" is not an answer
    to "should THIS command run now". So this tool checks the scope
    itself and refuses inside one: a shell command never executes
    from a routine replay, trusted or not. It must be asked for again
    as a live call and confirmed in the moment. (request() cannot
    park inside a scope — parking there is not an option the gate
    offers — so refusal is how the rule is enforced, and the refusal
    is a failure outcome, which halts the replay on that step rather
    than letting it walk past a step that did not run.)

    Delegated tasks never see the tool at all: it sits in main.py's
    _DELEGATED_EXCLUDE beside the self-edit tools, so no background
    task can even ask.

EXECUTION
    subprocess.run(command, shell=True) with output captured as
    UTF-8 (undecodable bytes replaced, never fatal). The result
    reports the exit code, then stdout, then stderr, labelled; the
    combined output is truncated to its LAST 6000 characters — the
    tail is where a failing command explains itself — with a plain
    "[output truncated]" note when it was cut. A timeout stops the
    command (subprocess.run kills the child itself) and is reported
    as a timeout, not dressed up as output. There is no stdin: an
    interactive command cannot be answered, so it runs until it
    finishes or the timeout stops it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core import confirm, outcome

# The result keeps the tail of the combined output: when a command
# floods, the last lines are the error, the summary, the verdict —
# the head is the part that scrolled away in a real terminal too.
OUTPUT_LIMIT = 6000

TIMEOUT_DEFAULT = 60
TIMEOUT_MIN = 5
TIMEOUT_MAX = 300


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _execute(command: str, cwd: str, timeout: int) -> str:
    """Run the command and render the result. Separate from the
    handler so tests can exercise execution (timeout, exit code,
    truncation, cwd) without the confirmation layer in the way."""
    try:
        result = subprocess.run(
            command, shell=True, cwd=cwd,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return outcome.failed(
            "run_shell_command",
            f"the command timed out after {timeout}s and was stopped",
            command=command)
    except Exception as e:
        return outcome.failed(
            "run_shell_command",
            f"the command could not be started: {e}",
            command=command)

    sections = []
    if result.stdout:
        sections.append(f"--- stdout ---\n{result.stdout.rstrip()}")
    if result.stderr:
        sections.append(f"--- stderr ---\n{result.stderr.rstrip()}")
    body = "\n".join(sections) if sections else "(no output)"
    if len(body) > OUTPUT_LIMIT:
        body = "[output truncated]\n" + body[-OUTPUT_LIMIT:]
    return f"Exit code: {result.returncode}\n{body}"


def run_shell_command(parameters: dict = None, player=None,
                      session_memory=None) -> str:
    params = parameters or {}
    command = (params.get("command") or "").strip()
    if not command:
        return outcome.failed("run_shell_command",
                              "no command was given")

    # Validate the working directory BEFORE anything is parked: a
    # bad cwd is a plain error, never a confirmation banner.
    raw_cwd = (params.get("cwd") or "").strip()
    if raw_cwd:
        cwd_path = Path(raw_cwd).expanduser()
        if not cwd_path.is_dir():
            return outcome.failed(
                "run_shell_command",
                f"the working directory does not exist: {raw_cwd}",
                command=command)
        cwd = str(cwd_path)
    else:
        cwd = str(get_base_dir())

    try:
        timeout = int(float(params.get("timeout_seconds")
                            or TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        timeout = TIMEOUT_DEFAULT
    timeout = max(TIMEOUT_MIN, min(TIMEOUT_MAX, timeout))

    if confirm.pending_title():
        return outcome.ask(
            "run_shell_command",
            "there is already a confirmation waiting on screen — the "
            "user has to answer that one first",
            command=command)

    if confirm.in_trusted_scope():
        # See the module docstring: request() would run this at once
        # in here, and a shell command is the one thing that must
        # never run on a pre-answer. Refuse, as a failure, so a
        # routine replay halts on this step instead of passing it.
        return outcome.failed(
            "run_shell_command",
            "shell commands never run inside a routine replay — every "
            "command needs a fresh on-screen confirmation as a live "
            "call. Nothing was run",
            command=command)

    return confirm.request(
        key="run_shell_command",
        title="Run shell command",
        detail=(f"Command: {command}\n"
                f"Working directory: {cwd}\n"
                f"Timeout: {timeout}s\n"
                "This runs in the system shell with the signed-in "
                "user's full permissions, exactly as if it were typed "
                "into a terminal. It runs only if you press CONFIRM."),
        run=lambda: _execute(command, cwd, timeout),
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "run_shell_command",
    "description": (
        "Run a terminal command on the user's computer in the system "
        "shell — things like 'git pull origin main', 'pip install "
        "<package>', 'dir', 'mkdir', or a build command. EVERY command "
        "needs the user's on-screen approval first: the exact command "
        "and working directory are shown on the HUD and nothing runs "
        "until the user presses CONFIRM, so ask for one command at a "
        "time and wait for the outcome before chaining more. You get "
        "back the exit code plus stdout and stderr (the tail of long "
        "output). Commands run with no stdin and a timeout (default "
        "60s, max 300s), so interactive prompts, editors, servers and "
        "anything that waits for input are a bad fit — they will hang "
        "until the timeout stops them. 'cwd' sets the working "
        "directory (default: the app folder); a directory that does "
        "not exist is refused before anything is asked."
    ),
    "brief": "Run a terminal command ('command', 'cwd', 'timeout_seconds') — asks on screen first, every time.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "description": "The exact command line to run in the system shell",
            },
            "cwd": {
                "type": "STRING",
                "description": "Working directory for the command (optional; default the app folder)",
            },
            "timeout_seconds": {
                "type": "NUMBER",
                "description": "Seconds before the command is stopped (optional; default 60, clamped 5-300)",
            },
        },
        "required": ["command"],
    },
    "handler": run_shell_command,
}
