"""Phase R2 — diagnostics self-heal (core/selfheal.py + the tool).

What is being pinned shut:

  * a corrupt config or memory store with a readable backup is
    healed through its loader's own recovery, and the heal is
    verified by re-parsing the file — the restored content is the
    backup's content, not a claim;
  * total loss (no readable backup) follows the loader's designed
    path — the wreck kept aside, the store reset — and the report
    says so, not "restored";
  * a store diagnostics already recovered is reported RECOVERED,
    never re-claimed as the heal's work, and its loader is not
    run again;
  * rejected tool files are REPORT ONLY: named, and nothing on
    disk changes — no delete, no move, no rewrite;
  * failures with no designed remedy (audio, engine, …) are
    reported, not touched;
  * a healthy system heals to a no-op: no entries, no loader
    calls, no incidents;
  * every heal action lands in the incident log.
"""
import json

import pytest

import core.selfheal as sh
from core import incidents
from memory import config_manager as cm
from memory import memory_manager as mm


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    cfg = tmp_path / "api_keys.json"
    mem = tmp_path / "long_term.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mm, "MEMORY_PATH", mem)
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    monkeypatch.setattr(cm, "_recovery_note", None)
    monkeypatch.setattr(mm, "_recovery_note", None)
    return {"cfg": cfg, "mem": mem, "inc": tmp_path / "incidents.jsonl"}


def _kinds(path):
    if not path.exists():
        return []
    return [json.loads(ln)["kind"]
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln]


def _fail(name, detail="broken"):
    return {"name": name, "status": "fail", "detail": detail}


def _plant_config_backup(cfg_path, content):
    slot = cm._backup_slots()[0]
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(json.dumps(content), encoding="utf-8")


def _plant_memory_backup(mem_path, content):
    slot = mm._backup_slots()[0]
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(json.dumps(content), encoding="utf-8")


# ── the two healable stores ─────────────────────────────────────────────

def test_corrupt_config_healed_from_backup(stores):
    stores["cfg"].write_text("{not json", encoding="utf-8")
    _plant_config_backup(stores["cfg"], {"gemini_api_key": "k" * 20})
    entries = sh.heal([_fail("Configuration")])
    assert entries[0]["action"] == sh.HEALED
    restored = cm._parse_config(stores["cfg"].read_text(encoding="utf-8"))
    assert restored == {"gemini_api_key": "k" * 20}
    kinds = _kinds(stores["inc"])
    assert "config_recovered" in kinds        # the loader's record
    assert "self_heal_restored" in kinds      # the heal's record


def test_corrupt_memory_healed_from_backup(stores):
    stores["mem"].write_text("### torn ###", encoding="utf-8")
    _plant_memory_backup(stores["mem"],
                         {"identity": {"name": {"value": "Alek"}}})
    entries = sh.heal([_fail("Memory store")])
    assert entries[0]["action"] == sh.HEALED
    restored = mm._parse_store(stores["mem"].read_text(encoding="utf-8"))
    assert restored["identity"]["name"]["value"] == "Alek"
    kinds = _kinds(stores["inc"])
    assert "memory_recovered" in kinds
    assert "self_heal_restored" in kinds


def test_total_loss_is_reported_as_reset_not_restored(stores):
    stores["cfg"].write_text("{not json", encoding="utf-8")
    entries = sh.heal([_fail("Configuration")])
    assert entries[0]["action"] == sh.HEALED
    # the loader's designed path: wreck kept aside, empty config
    assert cm._parse_config(
        stores["cfg"].read_text(encoding="utf-8")) == {}
    asides = list(stores["cfg"].parent.glob("api_keys.json.corrupt-*"))
    assert len(asides) == 1
    assert "damaged" in entries[0]["detail"]   # the loader's own note


def test_already_recovered_store_is_not_reclaimed(stores, monkeypatch):
    stores["cfg"].write_text(json.dumps({"gemini_api_key": "k" * 20}),
                              encoding="utf-8")
    def _boom():
        raise AssertionError("loader must not run on a readable file")
    monkeypatch.setattr(cm, "load_api_keys", _boom)
    entries = sh.heal([_fail("Configuration")])
    assert entries[0]["action"] == sh.RECOVERED
    assert _kinds(stores["inc"]) == []


# ── report-only classes ─────────────────────────────────────────────────

def test_rejected_files_are_named_and_untouched(stores, monkeypatch):
    sentinel = stores["cfg"].parent / "broken_thing.py"
    sentinel.write_text("# a tool file that does not load\n",
                        encoding="utf-8")
    before = sentinel.read_bytes()
    monkeypatch.setattr(sh, "_rejected_files",
                        lambda: ["broken_thing.py"])
    healthy_cfg = json.dumps({"gemini_api_key": "k" * 20})
    stores["cfg"].write_text(healthy_cfg, encoding="utf-8")
    entries = sh.heal([_fail("Command registry",
                             "tool file(s) failed to load")])
    assert entries[0]["action"] == sh.REPORT_ONLY
    assert "broken_thing.py" in entries[0]["detail"]
    assert sentinel.read_bytes() == before          # nothing moved
    assert stores["cfg"].read_text(encoding="utf-8") == healthy_cfg
    assert _kinds(stores["inc"]) == []              # no heal, no record


def test_unremedied_failures_are_reported_not_touched(stores):
    entries = sh.heal([_fail("Volume control", "no volume read back"),
                       _fail("Engine", "no Gemini key")])
    assert [e["action"] for e in entries] == [sh.REPORT_ONLY] * 2
    assert "no volume read back" in entries[0]["detail"]


# ── the healthy no-op ───────────────────────────────────────────────────

def test_healthy_system_heals_to_nothing(stores, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("no loader may run on a healthy system")
    monkeypatch.setattr(cm, "load_api_keys", _boom)
    monkeypatch.setattr(mm, "load_memory", _boom)
    ok = [{"name": n, "status": "pass", "detail": ""}
          for n in ("Configuration", "Memory store", "Engine")]
    assert sh.heal(ok) == []
    assert sh.spoken_summary([]) == "Self-heal: nothing to fix."
    assert _kinds(stores["inc"]) == []


def test_spoken_summary_names_heals_and_leftovers():
    entries = [
        {"check": "Configuration", "action": sh.HEALED, "detail": ""},
        {"check": "Command registry", "action": sh.REPORT_ONLY,
         "detail": ""},
    ]
    text = sh.spoken_summary(entries)
    assert "restored Configuration" in text
    assert "Command registry" in text and "human" in text
