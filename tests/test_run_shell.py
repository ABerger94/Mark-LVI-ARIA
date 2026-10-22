"""run_shell_command (Alek's ask, 2026-10-22) — the shell she never had.

Before this tool, "git pull origin main" fell into code_helper's file
runner and died with "No interpreter for .": no tool could run a
command at all. What is pinned here:

  * The tool is discovered with the right declaration.
  * NOTHING runs without a human CONFIRM: the call parks, the banner
    shows the exact command and working directory, and only
    confirm.resolve(True) executes — a cancel executes nothing.
  * Execution itself (through _execute, the confirm layer's callable):
    exit code and labelled stdout/stderr come back, the working
    directory is honoured, a timeout stops the command, and long
    output is truncated to its tail with a plain note.
  * Validation precedes the gate: an empty command and a missing
    working directory are plain errors, never banners.
  * The trusted-scope rule: inside confirm.trusted_scope() the tool
    REFUSES — status=failed, nothing run, nothing parked. request()
    itself cannot park inside a scope (it runs its callable there by
    design), so refusal is the enforcement; a shell command only ever
    executes as a live call a human confirmed in the moment. The
    failure verb also halts a routine replay on that step instead of
    letting it walk past a step that did not run.
  * Delegated tasks are never offered the tool (main.py's
    _DELEGATED_EXCLUDE).

Commands under test run this same Python interpreter on a helper
script, so the suite is identical on Windows and POSIX shells.
"""
import sys
import time
from pathlib import Path

import pytest

import actions.run_shell as rs
from core import confirm, incidents

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def ui_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    shown, logs = [], []
    confirm.bind(show=lambda t, d: shown.append((t, d)),
                 hide=lambda: None,
                 log=lambda m: logs.append(m))
    yield shown, logs
    if confirm.pending_title():
        confirm.resolve(False)
    confirm.bind(None, None, None)


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _script(tmp_path, body, name="helper.py"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


def _writer(tmp_path, sentinel):
    return _script(tmp_path,
                   f"from pathlib import Path\n"
                   f"Path({str(sentinel)!r}).write_text('ran')\n")


# ── declaration + registry ─────────────────────────────────────────────

def test_declaration_shape():
    tool = rs.TOOL
    assert tool["name"] == "run_shell_command"
    assert tool["handler"] is rs.run_shell_command
    props = tool["parameters"]["properties"]
    assert set(props) == {"command", "cwd", "timeout_seconds"}
    assert tool["parameters"]["required"] == ["command"]
    # The description must teach the gate, not just the mechanics.
    assert "CONFIRM" in tool["description"]


def test_registry_discovers_run_shell_command():
    import main
    from core.action_loader import discover_actions

    reserved = {t["name"] for t in main.TOOL_DECLARATIONS}
    actions = discover_actions(ROOT / "actions", reserved_names=reserved,
                               logger=lambda msg: None)
    assert actions.has("run_shell_command")


def test_delegated_tasks_never_see_the_shell():
    import main
    assert "run_shell_command" in main.JarvisLive._DELEGATED_EXCLUDE


# ── the gate ───────────────────────────────────────────────────────────

def test_nothing_runs_without_confirmation(tmp_path, ui_bound):
    shown, _logs = ui_bound
    sentinel = tmp_path / "sentinel.txt"
    command = _writer(tmp_path, sentinel)
    result = rs.run_shell_command({"command": command,
                                   "cwd": str(tmp_path)})
    assert "[CONFIRMATION_PENDING]" in result
    assert not sentinel.exists()                    # parked, not run
    assert confirm.pending_title() == "Run shell command"
    # The banner carries the EXACT command and directory that would run.
    title, detail = shown[-1]
    assert title == "Run shell command"
    assert command in detail
    assert str(tmp_path) in detail
    confirm.resolve(False)


def test_confirmed_command_runs_and_reports(tmp_path, ui_bound):
    _shown, logs = ui_bound
    sentinel = tmp_path / "sentinel.txt"
    body = (f"from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('ran')\n"
            f"print('hello from the shell')\n")
    command = _script(tmp_path, body)
    rs.run_shell_command({"command": command, "cwd": str(tmp_path)})
    confirm.resolve(True)
    assert _wait_for(lambda: sentinel.exists())
    # The callable's result is delivered through confirm's log seam:
    # exit code and stdout, as _execute rendered them.
    assert _wait_for(lambda: any("Exit code: 0" in line for line in logs))
    assert any("hello from the shell" in line for line in logs)


def test_cancelled_command_never_runs(tmp_path, ui_bound):
    sentinel = tmp_path / "sentinel.txt"
    command = _writer(tmp_path, sentinel)
    rs.run_shell_command({"command": command, "cwd": str(tmp_path)})
    confirm.resolve(False)
    time.sleep(0.3)
    assert not sentinel.exists()
    assert confirm.pending_title() == ""


def test_second_call_refused_while_one_is_parked(tmp_path, ui_bound):
    sentinel = tmp_path / "sentinel.txt"
    command = _writer(tmp_path, sentinel)
    rs.run_shell_command({"command": command, "cwd": str(tmp_path)})
    result = rs.run_shell_command({"command": command,
                                   "cwd": str(tmp_path)})
    assert result.startswith("status=ask"), result
    assert "already a confirmation waiting" in result
    assert not sentinel.exists()
    confirm.resolve(False)


def test_timeout_is_clamped_in_the_banner(tmp_path, ui_bound):
    shown, _logs = ui_bound
    command = _writer(tmp_path, tmp_path / "s.txt")
    rs.run_shell_command({"command": command, "cwd": str(tmp_path),
                          "timeout_seconds": 1})
    assert "Timeout: 5s" in shown[-1][1]            # floor
    confirm.resolve(False)
    rs.run_shell_command({"command": command, "cwd": str(tmp_path),
                          "timeout_seconds": 99999})
    assert "Timeout: 300s" in shown[-1][1]          # ceiling
    confirm.resolve(False)
    rs.run_shell_command({"command": command, "cwd": str(tmp_path)})
    assert "Timeout: 60s" in shown[-1][1]           # default
    confirm.resolve(False)


# ── validation, before any banner ──────────────────────────────────────

def test_empty_command_is_a_plain_error():
    for params in ({}, {"command": ""}, {"command": "   "}):
        result = rs.run_shell_command(params)
        assert result.startswith("status=failed"), result
    assert confirm.pending_title() == ""


def test_missing_cwd_is_a_plain_error(tmp_path):
    result = rs.run_shell_command(
        {"command": "echo hi", "cwd": str(tmp_path / "no-such-dir")})
    assert result.startswith("status=failed"), result
    assert "does not exist" in result
    assert confirm.pending_title() == ""            # never parked


# ── the trusted-scope rule ─────────────────────────────────────────────

def test_trusted_scope_refuses_and_runs_nothing(tmp_path, ui_bound):
    sentinel = tmp_path / "sentinel.txt"
    command = _writer(tmp_path, sentinel)
    with confirm.trusted_scope():
        result = rs.run_shell_command({"command": command,
                                       "cwd": str(tmp_path)})
    assert result.startswith("status=failed"), result
    assert "never run inside a routine replay" in result
    assert not sentinel.exists()                    # did NOT run ungated
    assert confirm.pending_title() == ""            # and nothing parked


# ── execution, direct through _execute ─────────────────────────────────

def test_execute_reports_exit_code_and_labelled_streams(tmp_path):
    body = ("import sys\n"
            "print('hello out')\n"
            "print('oops err', file=sys.stderr)\n"
            "sys.exit(3)\n")
    result = rs._execute(_script(tmp_path, body), str(tmp_path), 30)
    assert result.startswith("Exit code: 3"), result
    assert "--- stdout ---\nhello out" in result
    assert "--- stderr ---\noops err" in result


def test_execute_honours_cwd(tmp_path):
    sub = tmp_path / "workdir"
    sub.mkdir()
    body = "import os\nprint(os.getcwd())\n"
    result = rs._execute(_script(tmp_path, body), str(sub), 30)
    assert str(sub.resolve()) in result


def test_execute_timeout_stops_the_command(tmp_path):
    body = "import time\ntime.sleep(30)\n"
    started = time.time()
    result = rs._execute(_script(tmp_path, body), str(tmp_path), 1)
    assert result.startswith("status=failed"), result
    assert "timed out after 1s" in result
    assert time.time() - started < 15              # stopped, not waited out


def test_execute_truncates_to_the_tail(tmp_path):
    body = "print('y' * 9000 + 'TAILMARKER')\n"
    result = rs._execute(_script(tmp_path, body), str(tmp_path), 30)
    assert "[output truncated]" in result
    assert "TAILMARKER" in result                   # the tail survives
    assert len(result) < rs.OUTPUT_LIMIT + 200
