import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from threading import RLock

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

BACKUP_KEEP = 5
_lock       = RLock()

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

# ── The store: one write funnel, rolling backups, load recovery ────────────
#
# This file holds the Gemini key, every Phase E credential (the Gmail
# app password, the secret calendar URL, the GitHub token) and every
# setting the app has. It used to be written IN PLACE by a dozen
# setters, and load_api_keys() swallowed any parse error into {} — so
# one torn write (a crash, a full disk, a kill mid-save) silently
# reset the ENTIRE configuration: no backup, no incident, no trace,
# and the next setter cheerfully wrote the empty wreckage over what
# was left of the evidence.
#
# Phase G1 cured exactly this disease in the memory store; this is
# the same cure, in the same shape, applied here:
#
#   * write_config() is the ONE way the config reaches disk —
#     serialised to a temp file in the same directory and
#     os.replace()d over the live file, so a failed write leaves the
#     previous valid config in place, never a torn half-file.
#   * Before each replace, the outgoing file is snapshotted into
#     config/backups/ — the last BACKUP_KEEP copies THAT PARSE. A
#     backup of a corrupt file is worse than no backup: it would
#     occupy a slot the recovery walk trusts.
#   * load_api_keys() distinguishes a missing file (a fresh install:
#     {}, no incident) from a present-but-unreadable one (recover
#     from the backups, record an incident, leave a note for the
#     startup briefing). The silent swallow is gone.

def _parse_config(text: str) -> dict | None:
    """Parse config text; the dict, or None when the text is not a
    usable config (bad JSON, or JSON that isn't an object)."""
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _backup_slots() -> list[Path]:
    """Backup file paths, newest first (slot 1 is the most recent)."""
    d = CONFIG_FILE.parent / "backups"
    return [d / f"api_keys.{i}.json" for i in range(1, BACKUP_KEEP + 1)]


def _snapshot_backup() -> None:
    """Copy the CURRENT live config into the backup rotation — but
    only if it still parses (see the banner above). Never raises; a
    failed backup costs a snapshot, not a save."""
    try:
        if not CONFIG_FILE.exists():
            return
        if _parse_config(CONFIG_FILE.read_text(encoding="utf-8")) is None:
            return
        slots = _backup_slots()
        slots[0].parent.mkdir(parents=True, exist_ok=True)
        for i in range(len(slots) - 1, 0, -1):
            if slots[i - 1].exists():
                os.replace(slots[i - 1], slots[i])
        shutil.copyfile(CONFIG_FILE, slots[0])
        # Off-machine mirror (core/backup_mirror.py): the fresh
        # rotation, copied to the user's mirror folder if one is
        # set. Never raises, never fails this snapshot — a dead
        # mirror costs a copy, not a backup.
        try:
            from core import backup_mirror
            backup_mirror.mirror_backups(slots)
        except Exception:
            pass
    except Exception as e:
        print(f"[Config] ⚠️ Backup failed: {e}")


def write_config(data: dict, *, indent: int = 4) -> None:
    """The one way the config reaches disk. Every setter in this file
    — and the out-of-file writers that used to open-code their own
    write_text (ui.py's identity/setup saves,
    actions/screen_processor.py's key cache) — goes through here, so
    the atomicity and backup properties cannot be bypassed by a new
    setting added carelessly."""
    text = json.dumps(data, indent=indent)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _snapshot_backup()
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise


# Set when a load had to recover (or give up), cleared when the
# startup briefing speaks it once — see pop_recovery_note().
_recovery_note: str | None = None


def pop_recovery_note() -> str | None:
    """The one-line note from the most recent load-time recovery, or
    None when recent loads were clean. Consumed by reading: a
    recovery is announced once, in the startup briefing, not repeated
    on every load — the incident record is the durable copy."""
    global _recovery_note
    note, _recovery_note = _recovery_note, None
    return note


def save_api_keys(gemini_api_key: str) -> None:
    data = load_api_keys()
    data["gemini_api_key"] = gemini_api_key.strip()
    write_config(data, indent=2)


def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        # A missing file is a fresh install, not damage: no config
        # has ever been written here. (Backups are only consulted when
        # a config EXISTS but cannot be read — see
        # _recover_from_backups.)
        return {}
    with _lock:
        data = None
        try:
            data = _parse_config(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Config] ⚠️ Load error: {e}")
        if data is not None:
            return data
        return _recover_from_backups()


def _recover_from_backups() -> dict:
    """The live config exists but cannot be read. Walk the backups
    newest → oldest; the first that parses becomes the config again.

    This replaces the old behaviour — print one line, return {} —
    under which a single torn write silently cost the user every key
    and connection they had ever configured, with no record anywhere
    that it had happened. Recovery (and total loss) are both
    recorded as incidents, and both leave a note for the startup
    briefing: the user hears about it from the assistant, once, in
    plain words — they do not find out later, when an action starts
    claiming Gmail was never connected."""
    global _recovery_note
    from core import incidents
    slots = [s for s in _backup_slots() if s.exists()]
    for depth, slot in enumerate(slots, start=1):
        try:
            data = _parse_config(slot.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data is None:
            continue
        try:
            write_config(data)
        except Exception as e:
            print(f"[Config] ⚠️ Restore write failed: {e}")
        msg = (f"api_keys.json unreadable; restored from backup "
               f"{depth} of {len(slots)} ({slot.name})")
        print(f"[Config] 🛟 Recovered: {msg}")
        incidents.record("config", "config_recovered", msg)
        _recovery_note = (
            "A note about my settings: my configuration file was "
            "damaged, so I restored it from a backup — a change made "
            "very recently may have reverted.")
        return data
    print("[Config] 🛟 No readable backup — starting with an empty configuration")
    incidents.record("config", "config_lost",
                     "api_keys.json unreadable and no backup parsed; "
                     "configuration started empty")
    _recovery_note = (
        "A note about my settings: my configuration file was damaged "
        "and no backup could be read, so my settings and saved "
        "connections have reset — you may need to connect things "
        "again.")
    # Heal the file: move the unreadable config aside (kept, not
    # deleted — it is evidence, and a human may yet read something
    # out of it) and write a valid empty config, so every later load
    # does not re-walk the backups and re-record the loss.
    try:
        aside = CONFIG_FILE.with_name(
            f"{CONFIG_FILE.name}.corrupt-{datetime.now():%Y%m%d%H%M%S}")
        os.replace(CONFIG_FILE, aside)
        write_config({})
    except Exception as e:
        print(f"[Config] ⚠️ Could not reset the damaged config: {e}")
    return {}

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'ARIA' if not set."""
    return load_api_keys().get("assistant_name", "ARIA") or "ARIA"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    data = load_api_keys()
    data["assistant_name"] = assistant_name.strip() or "ARIA"
    data["user_name"] = user_name.strip()
    write_config(data)


# ── Assistant voice ──────────────────────────────────────────────────────────
# Gemini Live prebuilt voices. Names are proper nouns — identical in every
# language, so this list is safe to show verbatim in any locale.
AVAILABLE_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede"]
DEFAULT_VOICE    = "Charon"


def get_voice() -> str:
    """Return the configured Live voice, falling back to the default if unset
    or if the stored value is not a voice we recognise."""
    v = load_api_keys().get("voice_name", DEFAULT_VOICE) or DEFAULT_VOICE
    return v if v in AVAILABLE_VOICES else DEFAULT_VOICE


def save_voice(voice_name: str) -> None:
    """Persist the chosen Live voice. Unknown names collapse to the default so a
    bad value can never reach the API and break the session."""
    data = load_api_keys()
    v = (voice_name or "").strip()
    data["voice_name"] = v if v in AVAILABLE_VOICES else DEFAULT_VOICE
    write_config(data)


def get_wake_word_enabled() -> bool:
    """Whether local wake-word gating is on (assistant sleeps until 'Hey Jarvis')."""
    return load_api_keys().get("wake_word_enabled", False)


def save_wake_word_enabled(enabled: bool) -> None:
    data = load_api_keys()
    data["wake_word_enabled"] = bool(enabled)
    write_config(data)


def get_push_to_talk_enabled() -> bool:
    """Hold-a-key-to-speak. When on, the mic is closed unless the chord is held."""
    return load_api_keys().get("push_to_talk_enabled", False)


def save_push_to_talk_enabled(enabled: bool) -> None:
    _save_flag("push_to_talk_enabled", enabled)


HUD_STYLES = ("face", "core", "wave")


def get_hud_style() -> str:
    """Which centrepiece the HUD draws: the animated head, the reactor core,
    or the waveform core.

    Taste, not capability — both render in the same software painter and cost
    about the same. Defaults to the head because that is what MARK LIV shipped
    with; anyone who preferred the older look can switch back in ⚙ and the
    choice survives a restart.
    """
    v = str(load_api_keys().get("hud_style", "face")).strip().lower()
    return v if v in HUD_STYLES else "face"


def save_hud_style(style: str) -> None:
    s = str(style or "").strip().lower()
    _save_flag("hud_style", s if s in HUD_STYLES else "face")


PET_FORMS = ("bot", "orb")


def get_pet_form() -> str:
    """Which shape the desktop pet takes: "bot" — the little robot
    character with the OLED face — or "orb", her waveform core shrunk
    to pet size.

    The character is the default because that is the one Alek picked
    (2026-10-22); the orb stays one right-click away, and the choice
    survives restarts like every other display preference here.
    """
    v = str(load_api_keys().get("pet_form", "bot")).strip().lower()
    return v if v in PET_FORMS else "bot"


def save_pet_form(form: str) -> None:
    f = str(form or "").strip().lower()
    _save_flag("pet_form", f if f in PET_FORMS else "bot")


def get_pet_pos():
    """Where the pet window was last parked, as [x, y] global pixels —
    or None when it has never been moved, in which case the pet window
    picks its own first spot (bottom-right of the primary screen)."""
    v = load_api_keys().get("pet_pos")
    if isinstance(v, (list, tuple)) and len(v) == 2:
        try:
            return [int(v[0]), int(v[1])]
        except (TypeError, ValueError):
            return None
    return None


def save_pet_pos(pos) -> None:
    """Persist the pet's parked position. Junk input is ignored rather
    than stored — a corrupt position must never strand the pet
    off-screen on the next launch."""
    try:
        xy = [int(pos[0]), int(pos[1])]
    except Exception:
        return
    _save_flag("pet_pos", xy)


def get_pet_mode_enabled() -> bool:
    """Whether ARIA was left in pet mode when she last shut down.

    Pet mode is sticky (Alek, 2026-10-22): entering it sets this flag,
    leaving it clears it, and startup reads it back — so AUTO-START
    at login brings her up in whichever shape she was last left in.
    """
    return bool(load_api_keys().get("pet_mode", False))


def save_pet_mode_enabled(enabled: bool) -> None:
    _save_flag("pet_mode", bool(enabled))


# ── Live-session tuning ──────────────────────────────────────────────────────
# Everything here is optional and has a working default, so an untouched
# config behaves exactly like a configured one. Each value is also a way out:
# if a future model dislikes one of these, set it back and nothing else changes.

def get_thinking_enabled() -> bool:
    """Whether the Live model may spend tokens thinking before it answers.

    Off by default. A voice assistant is judged on how fast it starts talking,
    and the reasoning that actually needs deliberation in this app is delegated
    to the planning tools, which run on a separate non-Live model.
    """
    return bool(load_api_keys().get("thinking_enabled", False))


def save_thinking_enabled(enabled: bool) -> None:
    _save_flag("thinking_enabled", enabled)


def get_turn_tuning() -> dict:
    """How eagerly the server decides you have stopped speaking.

    OFF by default, and that default was earned. Cutting turns shorter looks
    like a free speed win and is not: proactive audio has to judge whether an
    utterance was even addressed to the assistant, and a turn clipped early
    gives it less to judge, so it stays quiet — and the reply to your first
    sentence only arrives once your second one has given it enough context.
    That reads as the assistant being a turn behind, which is far worse than
    the fraction of a second the tuning saves.

    Turn it on with "turn_tuning": {"enabled": true} if your own microphone and
    speaking pace suit it. `silence_ms` is the one that is felt: the pause the
    server sits through before accepting your turn is over.
    """
    cfg = load_api_keys().get("turn_tuning")
    cfg = cfg if isinstance(cfg, dict) else {}

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(cfg.get(key, default))))
        except (TypeError, ValueError):
            return default

    return {
        "enabled":    bool(cfg.get("enabled", False)),
        "silence_ms": _int("silence_ms", 550, 200, 3000),
        "prefix_ms":  _int("prefix_ms", 150, 0, 1000),
        # "high" = quicker to decide speech has ended.
        "end_sensitivity":   str(cfg.get("end_sensitivity", "high")).lower(),
        "start_sensitivity": str(cfg.get("start_sensitivity", "default")).lower(),
    }


def save_turn_tuning(values: dict) -> None:
    data = load_api_keys()
    cur = data.get("turn_tuning")
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    data["turn_tuning"] = cur
    write_config(data)


def get_proactive_audio_enabled() -> bool:
    """Whether the model gets to decide an utterance was not aimed at it and
    stay quiet.

    On by default — it is what stops the assistant answering the room. But it
    is also the first thing to switch off if replies ever seem to arrive a turn
    late: what looks like lag is usually the model having judged your previous
    sentence as not addressed to it, and only changing its mind once the next
    one arrives.
    """
    return bool(load_api_keys().get("proactive_audio", True))


def save_proactive_audio_enabled(enabled: bool) -> None:
    _save_flag("proactive_audio", enabled)


MEDIA_RESOLUTIONS = ("default", "low", "medium", "high")


def get_media_resolution() -> str:
    """How finely the model tokenises the screenshots and camera frames it is
    sent. 'medium' keeps on-screen text readable at a fraction of the tokens a
    full-resolution frame costs; 'low' is cheaper still but starts losing small
    text, which is most of what screen captures are for."""
    v = str(load_api_keys().get("media_resolution", "medium")).strip().lower()
    return v if v in MEDIA_RESOLUTIONS else "medium"


def save_media_resolution(value: str) -> None:
    v = str(value or "").strip().lower()
    _save_flag("media_resolution", v if v in MEDIA_RESOLUTIONS else "medium")


def _save_flag(key: str, value) -> None:
    """Read-modify-write one key without disturbing the rest of the config."""
    data = load_api_keys()
    data[key] = bool(value) if isinstance(value, bool) else value
    write_config(data)


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    data = load_api_keys()
    data["morning_brief_enabled"] = enabled
    write_config(data)


# ── Engine failover ─────────────────────────────────────────────────────────
# When Gemini Live becomes unrecoverable by its own ladder (every rung
# resting, or the session failing faster than reconnects can absorb),
# the conversation fails over to the configured pipeline engine for the
# rest of the run — see JarvisLive._maybe_failover in main.py. On unless
# explicitly switched off: anything but an explicit false means on (the
# dashboard_https idiom). The flag alone never fails anything over — a
# configured, complete pipeline provider is also required.

def get_engine_auto_failover() -> bool:
    return bool(load_api_keys().get("engine_auto_failover", True))


def save_engine_auto_failover(enabled: bool) -> None:
    data = load_api_keys()
    data["engine_auto_failover"] = bool(enabled)
    write_config(data)


# ── Proactive alerts ───────────────────────────────────────────────────
# Whether watch hits she discovers on her own — a price crossing its
# target, a screen watch's answer changing — are SPOKEN when they
# happen (Phase G4). On unless explicitly switched off: anything but
# an explicit false means on (the engine_auto_failover idiom). The
# flag gates speech only: the passes still run, the hits are still
# recorded and logged — she just doesn't say them out loud. A briefing
# Alek explicitly scheduled is never gated by this; he asked for it
# by name.

def get_proactive_alerts() -> bool:
    return bool(load_api_keys().get("proactive_alerts", True))


def save_proactive_alerts(enabled: bool) -> None:
    data = load_api_keys()
    data["proactive_alerts"] = bool(enabled)
    write_config(data)


# ── Presence (v1) ───────────────────────────────────────────────
# The presence layer's settings (core/presence.py): whether it
# runs at all, how long without keyboard/mouse input counts as
# away, how long an absence must be before a return earns a
# spoken greeting, and the quiet hours a late-night return never
# greets inside. Defaults mirror the spec: on, 10 min idle,
# greet after 30 min away, quiet 23:00–07:00. The on/off key
# also has a SETUP-drawer toggle (v1.1); the numeric knobs
# stay config-file only.

def get_presence_settings() -> dict:
    data = load_api_keys()

    def _int(key, default):
        try:
            return int(data.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "presence_enabled": bool(data.get("presence_enabled", True)),
        "presence_idle_seconds": _int("presence_idle_seconds", 600),
        "presence_greet_after_seconds": _int(
            "presence_greet_after_seconds", 1800),
        "presence_quiet_start": _int("presence_quiet_start", 23),
        "presence_quiet_end": _int("presence_quiet_end", 7),
    }



def save_presence_enabled(enabled: bool) -> None:
    data = load_api_keys()
    data["presence_enabled"] = bool(enabled)
    write_config(data)


# ── Main-process watchdog ──────────────────────────────────────────────
# Whether the watchdog supervises the two loops (core/watchdog.py):
# a loop silent past the stale threshold is restarted, at most three
# times an hour, with an incident as the record. On unless explicitly
# switched off (the engine_auto_failover idiom) — the flag exists so
# a human debugging a stall can watch it happen without being
# restarted out from under the evidence.


# ── Phone notifications (Web Push) ─────────────────────────────────────
# Whether notifications leave the machine at all (core/notify.py): the
# live dashboard toast, the Web Push fan-out and the missed queue are
# all sinks of the one notify() call, and this flag silences all of
# them together. On unless explicitly switched off (the
# engine_auto_failover idiom). Delivery still needs a phone that has
# subscribed from the dashboard — the flag alone notifies nobody.
#
# The VAPID keypair is the push identity: the public half is handed to
# the phone's browser as the applicationServerKey, the private half
# signs every push. Generated once, on first use, by core/notify.py
# and stored here like every other credential — one config to back
# up, one already in .gitignore. Never regenerated once set: a new
# keypair would orphan every existing subscription.

def get_notify_enabled() -> bool:
    return bool(load_api_keys().get("notify_enabled", True))


def save_notify_enabled(enabled: bool) -> None:
    data = load_api_keys()
    data["notify_enabled"] = bool(enabled)
    write_config(data)


def get_vapid_keys() -> tuple[str, str]:
    """(public, private) VAPID keys as stored, ('', '') when push has
    never been set up. The public key is the base64url uncompressed
    EC point the browser expects; the private key is a PEM string."""
    data = load_api_keys()
    pub = data.get("vapid_public_key")
    priv = data.get("vapid_private_key")
    return (pub if isinstance(pub, str) else "",
            priv if isinstance(priv, str) else "")


def save_vapid_keys(public_key: str, private_key: str) -> None:
    _patch_config(vapid_public_key=public_key,
                  vapid_private_key=private_key)


def get_watchdog_enabled() -> bool:
    return bool(load_api_keys().get("watchdog_enabled", True))


def save_watchdog_enabled(enabled: bool) -> None:
    data = load_api_keys()
    data["watchdog_enabled"] = bool(enabled)
    write_config(data)

# ── Self-updates ─────────────────────────────────────────────────────
# Whether the updater sidecar (updater.py, spawned detached at
# startup) may pull new code from origin and restart her on it:
# announce-first when she is awake, a clean-tree gate, a
# smoke-import before any restart, and a rollback to the last
# known-good commit when the new code doesn't boot healthy. On
# unless explicitly switched off (the watchdog_enabled idiom) — the
# sidecar re-reads this flag every poll, so the switch takes effect
# without restarting anything.

def get_auto_update_enabled() -> bool:
    return bool(load_api_keys().get("auto_update_enabled", True))


def save_auto_update_enabled(enabled: bool) -> None:
    data = load_api_keys()
    data["auto_update_enabled"] = bool(enabled)
    write_config(data)


# ── Backup mirror ──────────────────────────────────────────────────────
# Where the rolling backups are ALSO copied (core/backup_mirror.py):
# a folder on another fate-line than this machine's disk — a Drive
# or OneDrive synced folder, a NAS mount, a second drive. '' (the
# default) means no mirror: the rotations live only beside the
# originals, as they did before the mirror existed. The value is a
# plain path; nothing validates it at save time, because the folder
# may be an unmounted drive's — the mirror copy itself reports
# failure (once, as an incident) when the folder cannot be used.

# ── MCP servers ────────────────────────────────────────────────────────
# Model Context Protocol servers the user has configured
# (core/mcp_client.py): {name: spec} where a spec is either
# {"command": str, "args": [str, ...]} for a stdio server — a
# child process of this app — or {"url": str} for a streamable-
# HTTP server. Specs are data until mcp_connect launches one;
# storing a spec never runs anything (the launch is its own,
# confirm-gated act — see actions/mcp_connect.py).

def get_mcp_servers() -> dict:
    """All configured MCP servers: {name: spec}. Specs that are
    not dicts are dropped on read — a hand-edited wreck in this
    one key must not take the whole map down with it."""
    raw = load_api_keys().get("mcp_servers")
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save_mcp_server(name: str, spec: dict) -> None:
    data = load_api_keys()
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = spec
    data["mcp_servers"] = servers
    write_config(data)


def remove_mcp_server(name: str) -> bool:
    data = load_api_keys()
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    data["mcp_servers"] = servers
    write_config(data)
    return True


def get_backup_mirror_dir() -> str:
    v = load_api_keys().get("backup_mirror_dir", "")
    return v.strip() if isinstance(v, str) else ""


def save_backup_mirror_dir(path: str) -> None:
    data = load_api_keys()
    data["backup_mirror_dir"] = (path or "").strip()
    write_config(data)


# ── Audio devices ────────────────────────────────────────────────────────────
# Stored as device NAMES, not sounddevice indices. Indices shift every time a
# USB device is plugged in or removed, so a saved index silently starts pointing
# at a different microphone. The empty string means "system default", which is
# both the factory setting and what an unresolvable saved device falls back to —
# so unplugging a headset degrades to the built-in speakers instead of crashing.

def _patch_config(**fields) -> None:
    """Read-modify-write one or more keys in api_keys.json.

    Every setter in this file open-coded this. Collapsing it here means a new
    setting is one line, and there is one place where a corrupt config file is
    handled instead of nine."""
    data = load_api_keys()
    data.update(fields)
    write_config(data)


def get_input_device() -> str:
    """Microphone device name, or '' for the system default."""
    return (load_api_keys().get("input_device", "") or "").strip()


def save_input_device(name: str) -> None:
    _patch_config(input_device=(name or "").strip())


def get_output_device() -> str:
    """Speaker device name, or '' for the system default."""
    return (load_api_keys().get("output_device", "") or "").strip()


def save_output_device(name: str) -> None:
    _patch_config(output_device=(name or "").strip())


def get_plugin_enabled(plugin_name: str) -> bool:
    """Plugins are enabled by default the moment they're discovered (opt-out model)."""
    return load_api_keys().get("plugins_enabled", {}).get(plugin_name, True)


# ── Per-plugin settings ("tokens" / connection details) ───────────────────────
# Generic store so a plugin can declare its own config fields (PLUGIN_SETTINGS)
# and the settings UI renders + persists them WITHOUT any core edit — keeping the
# drop-in model intact. Values live under plugin_config[<namespace>][<key>].
# A namespace defaults to the plugin name, but a suite of plugins (e.g. the
# several printer plugins) can share ONE namespace.
def get_plugin_config(namespace: str) -> dict:
    """All stored values for a namespace (empty dict if none set yet)."""
    cfg = load_api_keys().get("plugin_config")
    val = cfg.get(namespace) if isinstance(cfg, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_plugin_setting(namespace: str, key: str, default=None):
    """A single value from a namespace, or `default` if unset."""
    return get_plugin_config(namespace).get(key, default)


def save_plugin_config(namespace: str, values: dict) -> None:
    """Merge `values` into a namespace's stored config (read-modify-write, like
    every other helper here). Only the provided keys are touched."""
    data = load_api_keys()
    pc = data.get("plugin_config")
    if not isinstance(pc, dict):
        pc = {}
    cur = pc.get(namespace)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values)
    pc[namespace] = cur
    data["plugin_config"] = pc
    write_config(data)


def save_plugin_enabled(plugin_name: str, enabled: bool) -> None:
    data = load_api_keys()
    plugins_cfg = data.get("plugins_enabled")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    plugins_cfg[plugin_name] = enabled
    data["plugins_enabled"] = plugins_cfg
    write_config(data)

# ── Providers: local STT/LLM/TTS and non-Gemini APIs ─────────────────────────
# Stored here rather than in a file of their own, for the same reason plugin
# settings are: one config to back up, one already in .gitignore, one place a
# user knows to look.
#
# The important property is what happens when nobody has configured anything.
# `get_provider_mode()` returns "gemini" on a config that has never heard of
# providers, and every caller checks that BEFORE importing the providers
# package — so an ordinary install never executes a line of it. This is the
# whole basis of the claim that the folder costs nothing when unused.
PROVIDER_MODE_GEMINI = "gemini"      # the conversation runs on the Live API
PROVIDER_MODE_PIPELINE = "pipeline"  # STT -> LLM -> TTS, assembled from providers


def get_providers() -> dict:
    """The whole providers block, or an empty dict."""
    val = load_api_keys().get("providers")
    return dict(val) if isinstance(val, dict) else {}


def get_provider_mode() -> str:
    """Which engine holds the conversation. Defaults to Gemini Live."""
    mode = str(get_providers().get("mode", "") or "").strip().lower()
    return mode if mode in (PROVIDER_MODE_GEMINI, PROVIDER_MODE_PIPELINE) \
        else PROVIDER_MODE_GEMINI


def get_provider_config(provider_id: str) -> dict:
    """Stored settings for one provider — address, model, key, limits."""
    creds = get_providers().get("creds")
    val = creds.get(provider_id) if isinstance(creds, dict) else None
    return dict(val) if isinstance(val, dict) else {}


def get_pipeline() -> dict:
    """Which provider fills each slot of the pipeline engine."""
    val = get_providers().get("pipeline")
    return dict(val) if isinstance(val, dict) else {}


def get_sidecar_provider() -> str:
    """The provider id used for one-shot calls when Gemini cannot answer them,
    or '' for none.

    Separate from the conversation on purpose: these are different jobs with
    different failure modes. Gemini's Live quota and its text quota are separate
    pools, and on the free tier it is the text pool that runs out first — which
    kills document reading, screen reasoning and code generation while the
    conversation carries on. Being able to point only those at another model,
    without changing the voice at all, is the cheapest useful thing this whole
    folder does.
    """
    return str(get_providers().get("sidecar", "") or "").strip()


def _patch_providers(**fields) -> None:
    p = get_providers()
    p.update(fields)
    _patch_config(providers=p)


def save_provider_mode(mode: str) -> None:
    mode = (mode or "").strip().lower()
    _patch_providers(mode=mode if mode in (PROVIDER_MODE_GEMINI,
                                           PROVIDER_MODE_PIPELINE)
                     else PROVIDER_MODE_GEMINI)


def save_provider_config(provider_id: str, values: dict) -> None:
    """Merge settings for one provider. Only the given keys are touched, so the
    panel can save one field without reading and rewriting the rest."""
    p = get_providers()
    creds = p.get("creds")
    if not isinstance(creds, dict):
        creds = {}
    cur = creds.get(provider_id)
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(values or {})
    creds[provider_id] = cur
    p["creds"] = creds
    _patch_config(providers=p)


def save_pipeline(stt: str = None, llm: str = None, tts: str = None) -> None:
    """Set one or more pipeline slots. `None` leaves a slot alone; '' clears it."""
    p = get_providers()
    pipe = p.get("pipeline")
    pipe = dict(pipe) if isinstance(pipe, dict) else {}
    for slot, value in (("stt", stt), ("llm", llm), ("tts", tts)):
        if value is not None:
            pipe[slot] = str(value).strip()
    p["pipeline"] = pipe
    _patch_config(providers=p)


def save_sidecar_provider(provider_id: str) -> None:
    _patch_providers(sidecar=(provider_id or "").strip())


def get_sidecar_first() -> bool:
    """Whether the side-call provider is tried BEFORE Gemini rather than after.

    Off by default: measured in core/gemini.py, the Gemini rungs answer a short
    request in well under a second, and a local model on ordinary hardware does
    not. On means something specific — 'I would rather this stayed on my
    machine, and I accept it being slower' — so it is a choice someone makes,
    never a default they inherit.
    """
    return bool(get_providers().get("sidecar_first", False))


def save_sidecar_first(enabled: bool) -> None:
    _patch_providers(sidecar_first=bool(enabled))


def save_providers(mode: str = None, pipeline: dict = None, creds: dict = None,
                   sidecar: str = None, sidecar_first: bool = None) -> None:
    """Write the whole providers block in ONE file write.

    The settings panel used to call five setters in a row — one per provider's
    credentials, then the mode, then the pipeline, then the two side-call
    keys — and every one of them was a separate read-modify-write of
    api_keys.json. Five reads and five writes of the same file, in sequence,
    while the panel's own APPLY was already triggering a reconnect that makes
    main.py write to it too. Any interleaving loses whichever key was written
    first, and the one people noticed was the mode: set MY OWN SETUP, save,
    reopen, and it says Gemini again.

    `None` leaves a section alone; `creds` is merged per provider so a panel
    that only edited one does not have to send the others.
    """
    data = load_api_keys()

    block = data.get("providers")
    block = dict(block) if isinstance(block, dict) else {}

    if mode is not None:
        m = str(mode).strip().lower()
        block["mode"] = m if m in (PROVIDER_MODE_GEMINI,
                                   PROVIDER_MODE_PIPELINE) else PROVIDER_MODE_GEMINI
    if pipeline is not None:
        current = block.get("pipeline")
        current = dict(current) if isinstance(current, dict) else {}
        current.update({k: str(v or "").strip() for k, v in pipeline.items()})
        block["pipeline"] = current
    if creds is not None:
        store = block.get("creds")
        store = dict(store) if isinstance(store, dict) else {}
        for pid, values in (creds or {}).items():
            one = store.get(pid)
            one = dict(one) if isinstance(one, dict) else {}
            one.update(values or {})
            store[pid] = one
        block["creds"] = store
    if sidecar is not None:
        block["sidecar"] = str(sidecar or "").strip()
    if sidecar_first is not None:
        block["sidecar_first"] = bool(sidecar_first)

    data["providers"] = block
    write_config(data)


# ── Connections (Phase E) ───────────────────────────────────────────────────
# Account credentials for the services ARIA was connected to, living in
# this same file for the same reasons everything else does: one config
# to back up, one already in .gitignore, one place a user knows to look.
# Every value is a portable string (an app password, a secret URL, a
# personal access token) — nothing here is OAuth-bound, so connecting
# is copying values in, through the *_setup actions or by hand.
#
# "INSERT" is treated as unset by every getter: ARIA's keys file
# shipped with that placeholder, and a config still carrying it is an
# unconfigured config, not a wrong credential.

def _stored(key: str) -> str:
    v = load_api_keys().get(key, "")
    v = v.strip() if isinstance(v, str) else ""
    return "" if v == "INSERT" else v


# Gmail — address + app password (NOT the account password).

def get_gmail_user() -> str:
    """The connected Gmail address, or '' when not connected."""
    return _stored("gmail_user")


def get_gmail_app_password() -> str:
    """The Gmail app password as stored (spaces and all), or ''."""
    return _stored("gmail_app_password")


def save_gmail_credentials(user: str, app_password: str) -> None:
    _patch_config(gmail_user=(user or "").strip(),
                  gmail_app_password=(app_password or "").strip())


# Google Calendar — the secret iCal URL is the whole credential.

def get_ical_url() -> str:
    """The connected calendar's secret iCal URL, or ''."""
    return _stored("ical_url")


def save_ical_url(url: str) -> None:
    _patch_config(ical_url=(url or "").strip())


# GitHub — personal access token + username, behind an arming flag.
# The flag is separate from the credential on purpose (ARIA's
# GITHUB_ARMED): storing a token says who she is on GitHub; arming
# says she may create repos and push files with it. Default OFF.

def get_github_token() -> str:
    return _stored("github_token")


def get_github_username() -> str:
    return _stored("github_username")


def get_github_armed() -> bool:
    return bool(load_api_keys().get("github_armed", False))


def save_github_credentials(token: str, username: str) -> None:
    _patch_config(github_token=(token or "").strip(),
                  github_username=(username or "").strip())


def save_github_armed(enabled: bool) -> None:
    _patch_config(github_armed=bool(enabled))
