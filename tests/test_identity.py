"""Phase A1 — she is ARIA.

Pins the identity migration so a future refactor cannot quietly turn her
back into the upstream assistant: the configured-name default, the persona
text the model is actually given (core/prompt.txt and main.py's fallback
for when the file cannot be read), the UI name defaults, and the dashboard
pages a phone pairs against.
"""
import inspect
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_assistant_name_defaults_to_aria(tmp_path, monkeypatch):
    from memory import config_manager as cm

    cfg = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", cfg)

    # Unconfigured install: she is ARIA, not the upstream default.
    assert cm.get_assistant_name() == "ARIA"
    # Saving an empty name falls back to ARIA, not JARVIS.
    cm.save_assistant_config("", "Alek")
    assert json.loads(cfg.read_text(encoding="utf-8"))["assistant_name"] == "ARIA"
    # A configured name still wins — the setting was only re-defaulted.
    cm.save_assistant_config("MAX", "Alek")
    assert cm.get_assistant_name() == "MAX"


def test_prompt_carries_aria_soul():
    text = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
    # Her identity, from ARIA's soul.md: name, expansion, the bond, the law.
    assert "A.R.I.A." in text
    assert "Adaptive Robotic Intelligence Agent" in text
    assert "bound to one person, Alek" in text
    assert "His corrections are standing law" in text
    # The upstream persona is gone from what the model reads.
    assert "Tony Stark" not in text
    assert "JARVIS" not in text
    # The template tokens main.py fills must survive any persona edit.
    for token in ("{assistant_name}", "{platform}", "{capabilities}", "{limits}"):
        assert token in text


def test_fallback_prompt_is_aria(tmp_path, monkeypatch):
    import main

    monkeypatch.setattr(main, "PROMPT_PATH", tmp_path / "no-such-prompt.txt")
    text = main._load_system_prompt()
    assert text.startswith("You are ARIA")
    assert "JARVIS" not in text


def test_identity_injection_names_aria(tmp_path, monkeypatch):
    """The [IDENTITY] block is built from the configured name; with no config
    the name it announces is ARIA. Exercised at the seam main.py reads: the
    config file plus get_assistant_name(), which share one default."""
    from memory import config_manager as cm

    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    assert f"Your name is {cm.get_assistant_name()}." == "Your name is ARIA."


def test_ui_name_defaults():
    import ui

    assert inspect.signature(
        ui.CustomizeOverlay.__init__).parameters["assistant_name"].default == "ARIA"
    assert inspect.signature(
        ui.HudCanvas.__init__).parameters["assistant_name"].default == "A.R.I.A"


def test_dashboard_pages_name_aria():
    login = (ROOT / "dashboard" / "static" / "login.html").read_text(encoding="utf-8")
    app = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
    assert "<title>ARIA</title>" in login
    assert "JARVIS" not in login
    assert "<title>ARIA Dashboard</title>" in app
    assert "Send a command to ARIA" in app
