"""Phase A2 — failure records (core/incidents.py + the outcome hook).

The properties that matter are the module's two rules: a record is durable
and classified, and recording can NEVER take the assistant down — it lives
inside error paths, so its own failures are swallowed whole. The outcome
tests pin the third property: model-facing strings are unchanged by the
hook existing.
"""
import json

import pytest

from core import incidents, outcome


@pytest.fixture
def inc_path(tmp_path, monkeypatch):
    path = tmp_path / "incidents.jsonl"
    monkeypatch.setattr(incidents, "INCIDENTS_PATH", path)
    return path


def _records(path):
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_appends_classified_jsonl(inc_path):
    incidents.record("tool:open_app", "tool_failed", "status=failed ...")
    recs = _records(inc_path)
    assert len(recs) == 1
    assert recs[0]["source"] == "tool:open_app"
    assert recs[0]["kind"] == "tool_failed"
    assert recs[0]["message"] == "status=failed ..."
    assert recs[0]["ts"]  # a timestamp is always written


def test_record_outcome_mapping(inc_path):
    incidents.record_outcome("failed", "open_app", "status=failed ...")
    incidents.record_outcome("unverified", "close_app", "status=unverified ...")
    # Not failures: done is the ordinary day, ask is a question, started is
    # a promise — none of them belong in an incident log.
    for status in ("done", "ask", "started"):
        incidents.record_outcome(status, "anything", "status=... ...")
    kinds = [r["kind"] for r in _records(inc_path)]
    assert kinds == ["tool_failed", "tool_unverified"]


def test_outcome_constructors_record_and_render_unchanged(inc_path):
    out = outcome.failed("open_app", "no running application matches that name")
    assert out.startswith("status=failed action=open_app")
    assert "This did NOT happen" in out

    out = outcome.unverified("close_app", "it is still running")
    assert out.startswith("status=unverified action=close_app")
    assert "NOT confirmed" in out

    kinds = [r["kind"] for r in _records(inc_path)]
    assert kinds == ["tool_failed", "tool_unverified"]

    # A success writes nothing.
    outcome.done("open_app", verified=True)
    assert len(_records(inc_path)) == 2


def test_record_exception_classifies_by_type(inc_path):
    try:
        raise ValueError("boom")
    except ValueError as e:
        incidents.record_exception("tool:send_message", e)
    recs = _records(inc_path)
    assert recs[0]["kind"] == "ValueError"
    assert "boom" in recs[0]["message"]


def test_rotation_caps_the_file(inc_path, monkeypatch):
    monkeypatch.setattr(incidents, "MAX_BYTES", 400)
    monkeypatch.setattr(incidents, "KEEP_LINES", 5)
    for i in range(30):
        incidents.record(f"tool:t{i}", "tool_failed", "x" * 60)
    lines = [l for l in inc_path.read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) <= incidents.KEEP_LINES + 1
    # And what survives a rotation is the NEWEST records.
    assert json.loads(lines[-1])["source"] == "tool:t29"


def test_record_never_raises_when_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / "a-file-not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(incidents, "INCIDENTS_PATH", blocker / "incidents.jsonl")
    incidents.record("tool:x", "tool_failed", "cannot be written")  # no raise
    incidents.record_outcome("failed", "x", "status=failed ...")    # no raise
    assert incidents.recent() == [] or isinstance(incidents.recent(), list)


def test_broken_recorder_never_breaks_an_outcome(inc_path, monkeypatch):
    def _explode(*a, **k):
        raise RuntimeError("disk is on fire")

    monkeypatch.setattr(incidents, "record_outcome", _explode)
    out = outcome.failed("open_app", "still returns its string")
    assert out.startswith("status=failed action=open_app")
