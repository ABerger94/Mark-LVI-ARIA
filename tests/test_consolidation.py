"""Memory consolidation (core/consolidation.py) — the nightly editor.

The store, the journal and the session summaries used to accumulate
without ever being reconciled. The pass that reconciles them holds
powers worth pinning down hard, because a bad night quietly rewrites
what the assistant believes:

  * changeset-only — the model proposes, the module disposes; a
    reply that is not schema-clean JSON is rejected WHOLE;
  * retire moves a fact to retired.json, never deletes it;
  * a conflicting revise to identity/relationships is not applied —
    it becomes a question for Alek in the morning briefing;
  * watermarks advance only on success, and only over material that
    was actually sent (an oversized backlog carries, never drops);
  * no new material means no model call; an unbound model seam
    means no writes at all;
  * the briefing takes the run's one-line summary (consumed by
    being read) plus at most one queued question per day;
  * due logic: the nightly slot, the >20h startup catch-up, and a
    twice-failed window parked instead of retried forever.

The model is stubbed throughout — bind() is the seam, exactly as
main.py binds it — and every store/journal/state path lives in a
tmp dir.
"""
import json
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from actions import morning_briefing
from core import consolidation, incidents, journal
from memory import config_manager, embeddings, memory_manager


NOW = datetime(2026, 10, 25, 3, 0, 0)      # a Sunday, on the slot


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_manager, "MEMORY_PATH",
                        tmp_path / "long_term.json")
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    monkeypatch.setattr(memory_manager, "_recovery_note", None)
    # The semantic leg is another suite's subject (test_memory_
    # hardening's fixture): never a network, never a worker thread.
    monkeypatch.setattr(embeddings, "_ensure_worker", lambda: None)
    embeddings.set_embedder(lambda texts, task_type: None)
    with embeddings._cv:
        embeddings._pending.clear()
    jdir = tmp_path / "journal"
    jdir.mkdir()
    monkeypatch.setattr(journal, "JOURNAL_DIR", jdir)
    cfg: dict = {}
    monkeypatch.setattr(config_manager, "load_api_keys", lambda: cfg)
    monkeypatch.setattr(consolidation, "STATE_PATH",
                        tmp_path / "consolidation.json")
    monkeypatch.setattr(consolidation, "RETIRED_PATH",
                        tmp_path / "retired.json")
    monkeypatch.setattr(consolidation, "CARD_PATH",
                        tmp_path / "resume_card.md")
    monkeypatch.setattr(consolidation, "LOG_PATH",
                        tmp_path / "consolidation_log.md")
    consolidation.bind(None)
    yield SimpleNamespace(tmp=tmp_path, cfg=cfg)
    consolidation.bind(None)
    embeddings.set_embedder(None)


class Stub:
    """The bound model seam: records calls, returns (or raises)
    the canned reply."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _cs(**sections) -> str:
    base = {"promote": [], "revise": [], "retire": [],
            "contradictions": [], "threads": []}
    base.update(sections)
    return json.dumps(base)


def _seed(facts=(), sessions=None):
    mem = memory_manager.load_memory()
    for cat, key, val in facts:
        mem.setdefault(cat, {})[key] = {"value": val,
                                        "updated": "2026-10-01"}
    if sessions is not None:
        mem["sessions"] = sessions
    memory_manager.save_memory(mem)
    return mem


def _journal_day(day: str, body: str):
    journal.write_entry(body, now=datetime.fromisoformat(
        day + "T21:15:00"))


def _state(env) -> dict:
    return json.loads((env.tmp / "consolidation.json").read_text(
        encoding="utf-8"))


def _write(facts=(), day="2026-10-24",
           body="Alek mentioned something new today."):
    _seed(facts=facts)
    _journal_day(day, body)


# ── Changeset validation ─────────────────────────────────────────────────────

def test_validate_rejects_non_json(env):
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset("I tidied everything up!")


def test_validate_rejects_unknown_section(env):
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset('{"rewrite_all": []}')


def test_validate_rejects_category_off_whitelist(env):
    # "playlist" is writable through remember() — it is NOT one of
    # the six fact categories the editor may touch.
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset(_cs(promote=[
            {"category": "playlist", "key": "mix", "value": "x",
             "reason": "r"}]))


def test_validate_rejects_overlong_value(env):
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset(_cs(promote=[
            {"category": "notes", "key": "big", "value": "x" * 381,
             "reason": "r"}]))


def test_validate_rejects_write_cap(env):
    writes = [{"category": "notes", "key": f"k{i}", "value": "v",
               "reason": "r"} for i in range(26)]
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset(_cs(promote=writes))


def test_validate_rejects_retire_cap(env):
    retires = [{"category": "notes", "key": f"k{i}", "reason": "r",
                "superseded_by": ""} for i in range(11)]
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset(_cs(retire=retires))


def test_validate_rejects_duplicate_and_conflicting_slots(env):
    item = {"category": "notes", "key": "tea", "value": "coffee",
            "reason": "r"}
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset(_cs(promote=[item, item]))
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset(_cs(
            promote=[item],
            retire=[{"category": "notes", "key": "tea",
                     "reason": "r", "superseded_by": ""}]))


def test_validate_rejects_agreeing_contradiction(env):
    with pytest.raises(consolidation._Invalid):
        consolidation.validate_changeset(_cs(contradictions=[
            {"category": "notes", "key": "tea", "existing": "green",
             "proposed": "green", "question": "?"}]))


def test_validate_normalises_keys_and_clamps_threads(env):
    out = consolidation.validate_changeset(_cs(
        promote=[{"category": "preferences", "key": " Morning Drink! ",
                  "value": "coffee", "reason": "r"}],
        threads=["t" * 200] + [f"thread {i}" for i in range(7)]))
    assert out["writes"][0]["key"] == "morning_drink"
    assert len(out["threads"]) == 5
    assert len(out["threads"][0]) == 120


# ── A full night: promote / revise / retire ──────────────────────────────────

def test_run_applies_changeset(env):
    _write(facts=[("notes", "old_note", "likes tea"),
                  ("projects", "raft_build", "building a raft")])
    stub = Stub(_cs(
        promote=[{"category": "preferences", "key": "morning drink",
                  "value": "coffee", "reason": "stated in journal"}],
        revise=[{"category": "notes", "key": "old_note",
                 "value": "likes coffee now", "reason": "updated"}],
        retire=[{"category": "projects", "key": "raft_build",
                 "reason": "finished and sold", "superseded_by": ""}],
    ))
    consolidation.bind(stub)
    logs = consolidation.run_if_due(NOW)
    assert logs and "consolidation ran" in logs[0]
    mem = memory_manager.load_memory()
    assert mem["preferences"]["morning_drink"]["value"] == "coffee"
    assert mem["preferences"]["morning_drink"]["updated"] == "2026-10-25"
    assert mem["notes"]["old_note"]["value"] == "likes coffee now"
    assert "raft_build" not in mem["projects"]
    retired = json.loads((env.tmp / "retired.json").read_text(
        encoding="utf-8"))
    assert len(retired) == 1
    assert retired[0]["key"] == "raft_build"
    assert retired[0]["value"] == "building a raft"
    assert retired[0]["reason"] == "finished and sold"
    state = _state(env)
    assert state["journal_through"] == "2026-10-24"
    assert state["last_status"] == "ok"
    # The same slot does not run twice.
    assert consolidation.run_if_due(NOW) == []
    assert len(stub.calls) == 1


def test_reapplying_a_changeset_converges(env):
    _write(facts=[("projects", "raft_build", "building a raft")])
    reply = _cs(
        promote=[{"category": "preferences", "key": "drink",
                  "value": "coffee", "reason": "r"}],
        retire=[{"category": "projects", "key": "raft_build",
                 "reason": "done", "superseded_by": ""}])
    stub = Stub(reply)
    consolidation.bind(stub)
    consolidation.run_if_due(NOW)
    # Rewind the watermarks and run the same reply over the same
    # window tomorrow: nothing may double-apply.
    state = _state(env)
    state["last_success_at"] = None
    state["last_status"] = None
    state["journal_through"] = None
    (env.tmp / "consolidation.json").write_text(
        json.dumps(state), encoding="utf-8")
    consolidation.run_if_due(NOW + timedelta(days=1))
    retired = json.loads((env.tmp / "retired.json").read_text(
        encoding="utf-8"))
    assert len(retired) == 1
    mem = memory_manager.load_memory()
    assert mem["preferences"]["drink"]["value"] == "coffee"


# ── Protected categories: conflicts become questions ─────────────────────────

def _identity_run(env):
    _write(facts=[("identity", "home_town", "Aberdeen")],
           body="He mentioned growing up in Perryville.")
    stub = Stub(_cs(revise=[
        {"category": "identity", "key": "home_town",
         "value": "Perryville", "reason": "journal"}]))
    consolidation.bind(stub)
    consolidation.run_if_due(NOW)
    return stub


def test_identity_conflict_is_queued_not_applied(env):
    _identity_run(env)
    mem = memory_manager.load_memory()
    assert mem["identity"]["home_town"]["value"] == "Aberdeen"
    note = consolidation.briefing_note(datetime(2026, 10, 25, 7, 0))
    assert "tidied my memory" in note
    assert "Quick memory check:" in note
    assert "Perryville" in note and "Aberdeen" in note
    # One question per day — a second briefing stays quiet on it.
    again = consolidation.briefing_note(datetime(2026, 10, 25, 9, 0))
    assert "Quick memory check" not in again
    # Still unanswered tomorrow: it may be asked again.
    tomorrow = consolidation.briefing_note(datetime(2026, 10, 26, 7, 0))
    assert "Quick memory check:" in tomorrow


def test_question_dropped_once_the_store_agrees(env):
    _identity_run(env)
    consolidation.briefing_note(datetime(2026, 10, 25, 7, 0))
    # Alek answers by voice; the answer lands through remember().
    memory_manager.remember("home_town", "Perryville", "identity")
    note = consolidation.briefing_note(datetime(2026, 10, 26, 7, 0))
    assert "Quick memory check" not in note
    assert _state(env)["contradictions"] == []


# ── Refusals: unbound, junk, model down ──────────────────────────────────────

def test_unbound_model_writes_nothing(env):
    _write(facts=[("notes", "a", "1")])
    consolidation.bind(None)
    logs = consolidation.run_if_due(NOW)
    assert any("skipped" in line for line in logs)
    mem = memory_manager.load_memory()
    assert mem["notes"]["a"]["value"] == "1"
    assert not (env.tmp / "retired.json").exists()
    assert not (env.tmp / "resume_card.md").exists()
    assert _state(env)["last_status"] == "skipped"


def test_junk_reply_writes_nothing_and_holds_the_watermark(env):
    _write(facts=[("notes", "a", "1")])
    stub = Stub("Sure thing — I cleaned it all up, trust me.")
    consolidation.bind(stub)
    logs = consolidation.run_if_due(NOW)
    assert any("failed" in line for line in logs)
    mem = memory_manager.load_memory()
    assert list(mem["notes"]) == ["a"]
    state = _state(env)
    assert state["last_status"] == "failed"
    assert state["journal_through"] is None
    assert state["last_success_at"] is None
    # A junk reply retries at the NEXT slot, not an hour later.
    assert consolidation.run_if_due(NOW + timedelta(hours=1)) == []
    assert len(stub.calls) == 1


def test_twice_failed_window_is_parked(env):
    _write()
    stub = Stub("not json, again")
    consolidation.bind(stub)
    consolidation.run_if_due(NOW)
    consolidation.run_if_due(NOW + timedelta(days=1))
    assert len(stub.calls) == 2
    # Same material, day three: parked — no third call.
    assert consolidation.run_if_due(NOW + timedelta(days=2)) == []
    assert len(stub.calls) == 2
    note = consolidation.briefing_note(datetime(2026, 10, 28, 7, 0))
    assert "parked" in note


def test_model_error_is_a_skip_with_a_retry_cooldown(env):
    _write()
    stub = Stub(RuntimeError("every rung is resting"))
    consolidation.bind(stub)
    logs = consolidation.run_if_due(NOW)
    assert any("skipped" in line for line in logs)
    assert _state(env)["last_status"] == "skipped"
    assert consolidation.run_if_due(NOW + timedelta(minutes=30)) == []
    assert len(stub.calls) == 1
    consolidation.run_if_due(NOW + timedelta(hours=2))
    assert len(stub.calls) == 2


# ── Material & watermark rules ───────────────────────────────────────────────

def test_no_material_means_no_model_call(env):
    _seed()
    stub = Stub(_cs())
    consolidation.bind(stub)
    logs = consolidation.run_if_due(NOW)
    assert stub.calls == []
    assert any("nothing new" in line for line in logs)
    assert _state(env)["last_status"] == "ok"


def test_session_summaries_are_sent_once(env):
    _seed(sessions=[{"date": "2026-10-24",
                     "summary": "Talked about the car insurance."}])
    stub = Stub(_cs(promote=[
        {"category": "notes", "key": "car_insurance",
         "value": "lapsed in September", "reason": "session"}]))
    consolidation.bind(stub)
    consolidation.run_if_due(NOW)
    mem = memory_manager.load_memory()
    assert mem["notes"]["car_insurance"]["value"] == \
        "lapsed in September"
    # Tomorrow the summary is known material: no second call.
    logs = consolidation.run_if_due(NOW + timedelta(days=1))
    assert len(stub.calls) == 1
    assert any("nothing new" in line for line in logs)


def test_watermark_advances_only_over_sent_days(env, monkeypatch):
    _seed()
    _journal_day("2026-10-23", "short note")
    _journal_day("2026-10-24", "a second day with more to say in it")
    stub = Stub(_cs())
    consolidation.bind(stub)
    # Budget fits the store header + day one, not day two.
    monkeypatch.setattr(consolidation, "INPUT_CAP", 170)
    consolidation.run_if_due(NOW)
    assert _state(env)["journal_through"] == "2026-10-23"
    monkeypatch.setattr(consolidation, "INPUT_CAP", 14_000)
    consolidation.run_if_due(NOW + timedelta(days=1))
    assert _state(env)["journal_through"] == "2026-10-24"
    assert len(stub.calls) == 2
    # The carried day really was in the second call's material.
    assert "2026-10-24" in stub.calls[1][1]


# ── Due logic ────────────────────────────────────────────────────────────────

def test_startup_catchup_after_a_missed_night(env):
    _write()
    state = consolidation._empty_state()
    state["last_run_at"] = "2026-10-24T04:00:00"
    state["last_success_at"] = "2026-10-24T04:00:00"
    state["last_status"] = "ok"
    consolidation._save_state(state)
    stub = Stub(_cs())
    consolidation.bind(stub)
    before_slot = datetime(2026, 10, 25, 2, 0)   # slot: Oct 24 03:00
    # 22h since success but not a startup pass: not due.
    assert consolidation.run_if_due(before_slot) == []
    # Startup catch-up: >20h old success is due immediately.
    logs = consolidation.run_if_due(before_slot, startup=True)
    assert logs and len(stub.calls) == 1


def test_disabled_in_config(env):
    env.cfg["consolidation_enabled"] = False
    _write()
    stub = Stub(_cs())
    consolidation.bind(stub)
    assert consolidation.run_if_due(NOW) == []
    assert stub.calls == []


# ── Resume card & briefing surface ───────────────────────────────────────────

def test_resume_card_written_fresh_then_stale(env):
    _write(facts=[("projects", "raft_build", "building a raft")])
    stub = Stub(_cs(threads=["Finish the raft paperwork"]))
    consolidation.bind(stub)
    consolidation.run_if_due(NOW)
    card = consolidation.resume_card_text(NOW)
    assert card is not None
    assert "Finish the raft paperwork" in card
    assert "raft build" in card
    # 49 hours old: too stale to speak from.
    old = (NOW - timedelta(hours=49)).timestamp()
    os.utime(env.tmp / "resume_card.md", (old, old))
    assert consolidation.resume_card_text(NOW) is None


def test_resume_card_never_exceeds_its_cap(env):
    facts = [("projects", f"project_{i}", "x" * 380) for i in range(6)]
    _write(facts=facts)
    stub = Stub(_cs(threads=["t" * 200 for _ in range(5)]))
    consolidation.bind(stub)
    consolidation.run_if_due(NOW)
    text = (env.tmp / "resume_card.md").read_text(encoding="utf-8")
    assert len(text) <= 1200


def test_briefing_suffix_carries_the_note_once(env):
    _write(facts=[("notes", "a", "1")])
    stub = Stub(_cs(promote=[
        {"category": "notes", "key": "b", "value": "2",
         "reason": "r"}]))
    consolidation.bind(stub)
    consolidation.run_if_due(NOW)
    suffix = morning_briefing._consolidation_note_suffix()
    assert "tidied my memory" in suffix
    assert "1 new fact" in suffix
    # Consumed by being read — the next delivery does not repeat it.
    assert morning_briefing._consolidation_note_suffix() == ""
