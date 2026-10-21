"""Config hardening — api_keys.json gets G1's cure.

The config file holds the Gemini key, every Phase E credential and
every setting the app has. It used to be written in place by a dozen
setters, and load_api_keys() swallowed any parse error into {} — one
torn write silently reset the entire configuration, with no backup,
no incident and no trace. These suites pin the new contract:

  * write_config() is the ONE write funnel (temp file + os.replace);
    a crash mid-write leaves the previous valid config;
  * every write first snapshots the outgoing file into a rotation of
    5 backups under config/backups/ (a file that does not parse is
    never snapshotted);
  * a present-but-unreadable config recovers from the newest readable
    backup, records config_recovered, and leaves a note the startup
    briefing speaks once; total loss preserves the wreck as
    api_keys.json.corrupt-<ts>, records config_lost and heals the
    file;
  * a missing file is a fresh install — no incident, no note;
  * no runtime module outside memory/config_manager.py writes the
    keys file directly (source pin — the funnel cannot be bypassed
    by a future writer added carelessly).
"""
import json
import re
from pathlib import Path

import pytest

from core import incidents
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    inc = tmp_path / "incidents.jsonl"
    monkeypatch.setattr(incidents, "INCIDENTS_PATH", inc)
    monkeypatch.setattr(cm, "_recovery_note", None)
    yield path


def _kinds(path):
    if not path.exists():
        return []
    return [json.loads(ln)["kind"]
            for ln in path.read_text(encoding="utf-8").splitlines() if ln]


# ── Round-trips through the public setters ──────────────────────────────

def test_public_setters_round_trip(cfg):
    cm.save_api_keys("gemini-key-1234567890")
    cm.save_assistant_config("ARIA", "Alek")
    cm.save_voice("Kore")
    cm.save_gmail_credentials("alek@example.com", "app-password")
    cm.save_ical_url("https://calendar.example/secret.ics")
    cm.save_github_credentials("ghp_token", "ABerger94")
    cm.save_github_armed(True)
    cm.save_providers(mode="pipeline",
                      creds={"openai_compat": {"api_key": "sk-x"}})
    cm.save_plugin_config("printer", {"host": "10.0.0.9"})
    cm.save_plugin_enabled("printer", False)

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["gemini_api_key"] == "gemini-key-1234567890"
    assert cm.get_assistant_name() == "ARIA"
    assert cm.get_user_name() == "Alek"
    assert cm.get_voice() == "Kore"
    assert cm.get_gmail_user() == "alek@example.com"
    assert cm.get_gmail_app_password() == "app-password"
    assert cm.get_ical_url().endswith("secret.ics")
    assert cm.get_github_token() == "ghp_token"
    assert cm.get_github_username() == "ABerger94"
    assert cm.get_github_armed() is True
    assert cm.get_provider_mode() == "pipeline"
    assert cm.get_provider_config("openai_compat")["api_key"] == "sk-x"
    assert cm.get_plugin_setting("printer", "host") == "10.0.0.9"
    assert cm.get_plugin_enabled("printer") is False


def test_insert_placeholder_still_means_unset(cfg):
    # Phase E semantics, unchanged: ARIA's keys file shipped with the
    # "INSERT" placeholder, and a config still carrying it is an
    # unconfigured config, not a wrong credential.
    cm._patch_config(gmail_user="INSERT", github_token="INSERT")
    assert cm.get_gmail_user() == ""
    assert cm.get_github_token() == ""


def test_setters_preserve_each_others_keys(cfg):
    # The read-modify-write property the funnel must not break: each
    # setter touches its own keys and no others (the class of bug
    # save_providers' docstring was written about).
    cm.save_api_keys("gemini-key-1234567890")
    cm.save_voice("Puck")
    cm.save_brief_enabled(False)
    cm.save_input_device("USB Mic")
    data = cm.load_api_keys()
    assert data["gemini_api_key"] == "gemini-key-1234567890"
    assert data["voice_name"] == "Puck"
    assert data["morning_brief_enabled"] is False
    assert data["input_device"] == "USB Mic"


# ── Atomic writes ───────────────────────────────────────────────────────

def test_failed_replace_leaves_previous_config(cfg, monkeypatch):
    cm.save_api_keys("key-A")
    cm.save_api_keys("key-B")
    before = cfg.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated crash at replace")

    monkeypatch.setattr(cm.os, "replace", boom)
    with pytest.raises(OSError):
        cm.save_api_keys("key-C")
    assert cfg.read_text(encoding="utf-8") == before
    assert cm.load_api_keys()["gemini_api_key"] == "key-B"
    assert not cfg.with_name("api_keys.json.tmp").exists()


# ── Backups ─────────────────────────────────────────────────────────────

def test_backups_rotate_and_cap_at_five(cfg):
    for i in range(1, 8):
        cm.save_api_keys(f"key-{i}")
    backups = sorted((cfg.parent / "backups").glob("*.json"))
    assert len(backups) == cm.BACKUP_KEEP == 5
    for slot in backups:
        json.loads(slot.read_text(encoding="utf-8"))  # every slot parses
    newest = json.loads(backups[0].read_text(encoding="utf-8"))
    assert newest["gemini_api_key"] == "key-6"   # state before the last write
    oldest = json.loads(backups[-1].read_text(encoding="utf-8"))
    assert oldest["gemini_api_key"] == "key-2"


def test_corrupt_config_is_never_snapshotted(cfg):
    cm.save_gmail_credentials("alek@example.com", "pw-1")
    cm.save_gmail_credentials("alek@example.com", "pw-2")
    slot = cfg.parent / "backups" / "api_keys.1.json"
    good_snapshot = slot.read_text(encoding="utf-8")
    cfg.write_text("{torn", encoding="utf-8")
    cm.save_voice("Kore")   # this write recovers first, then saves
    assert slot.read_text(encoding="utf-8") == good_snapshot
    for backup in (cfg.parent / "backups").glob("*.json"):
        assert "{torn" not in backup.read_text(encoding="utf-8")


# ── Load validation + recovery ──────────────────────────────────────────

def test_missing_file_is_a_fresh_install(cfg):
    assert cm.load_api_keys() == {}
    assert cm.get_gemini_key() is None
    assert _kinds(cfg.parent / "incidents.jsonl") == []
    assert cm.pop_recovery_note() is None


def test_corrupt_live_recovers_from_backup(cfg):
    cm.save_gmail_credentials("alek@example.com", "pw-1")
    cm.save_voice("Kore")
    cm.save_github_armed(True)   # the newest state — never backed up
    cfg.write_text("{torn", encoding="utf-8")

    data = cm.load_api_keys()
    # Restored to the newest backup: the state before the last write.
    assert data["gmail_user"] == "alek@example.com"
    assert data["voice_name"] == "Kore"
    assert "github_armed" not in data
    # The live file is healed to the restored config.
    assert json.loads(cfg.read_text(encoding="utf-8")) == data
    assert _kinds(cfg.parent / "incidents.jsonl") == ["config_recovered"]
    note = cm.pop_recovery_note()
    assert note is not None and "settings" in note
    assert cm.pop_recovery_note() is None     # spoken once


def test_total_loss_preserves_wreck_and_heals(cfg):
    cm.save_api_keys("key-A")
    cm.save_api_keys("key-B")
    cfg.write_text("{torn beyond reading", encoding="utf-8")
    for slot in (cfg.parent / "backups").glob("*.json"):
        slot.write_text("{also torn", encoding="utf-8")

    assert cm.load_api_keys() == {}
    assert _kinds(cfg.parent / "incidents.jsonl") == ["config_lost"]
    wrecks = list(cfg.parent.glob("api_keys.json.corrupt-*"))
    assert len(wrecks) == 1
    assert wrecks[0].read_text(encoding="utf-8") == "{torn beyond reading"
    # Healed: a valid empty config now lives at the path, and a later
    # load does not re-record the loss.
    assert json.loads(cfg.read_text(encoding="utf-8")) == {}
    assert cm.load_api_keys() == {}
    assert _kinds(cfg.parent / "incidents.jsonl") == ["config_lost"]
    note = cm.pop_recovery_note()
    assert note is not None and "reset" in note


def test_recovery_note_reaches_the_briefing(cfg):
    cm.save_api_keys("key-A")
    cm.save_api_keys("key-B")
    cfg.write_text("{torn", encoding="utf-8")
    cm.load_api_keys()
    from actions import morning_briefing
    suffix = morning_briefing._recovery_note_suffix()
    assert "settings" in suffix
    assert morning_briefing._recovery_note_suffix() == ""


# ── The funnel pin ──────────────────────────────────────────────────────

_WRITE_PATTERNS = [
    re.compile(r"(CONFIG_FILE|API_FILE|_CONFIG_PATH|API_CONFIG_PATH)"
               r"\.write_text"),
    re.compile(r"open\([^)]*api_keys\.json[^)]*[\"']w[\"']"),
]


def _runtime_sources():
    files = [ROOT / "main.py", ROOT / "ui.py"]
    for pkg in ("actions", "core", "dashboard", "memory",
                "providers", "plugins", "tools"):
        files += sorted((ROOT / pkg).rglob("*.py"))
    # providers/_tests.py is the providers self-test HARNESS: it
    # deliberately writes a fixture config to exercise provider
    # resolution, the way a user would by hand. It is not a runtime
    # write path and is excluded by name.
    return [f for f in files
            if f.name != "_tests.py"
            and f != ROOT / "memory" / "config_manager.py"]


def test_no_direct_config_writes_outside_the_funnel():
    offenders = []
    for f in _runtime_sources():
        src = f.read_text(encoding="utf-8")
        for pat in _WRITE_PATTERNS:
            if pat.search(src):
                offenders.append(f"{f.relative_to(ROOT)}: {pat.pattern}")
    assert offenders == []


def test_funnel_shape_and_routed_writers():
    cm_src = (ROOT / "memory" / "config_manager.py").read_text(
        encoding="utf-8")
    assert "CONFIG_FILE.write_text" not in cm_src
    assert "os.replace(tmp, CONFIG_FILE)" in cm_src
    # The two out-of-file writers route through the funnel now.
    assert "write_config" in (ROOT / "ui.py").read_text(encoding="utf-8")
    sp_src = (ROOT / "actions" / "screen_processor.py").read_text(
        encoding="utf-8")
    assert "write_config" in sp_src
