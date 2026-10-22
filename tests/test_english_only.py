"""English only — her output language is pinned, never mirrored.

The defect this pins against: the [LANGUAGE] rule told her to reply in
the language of the user's most recent message, the startup briefing
re-seeded itself from a stored identity language ("Speak this briefing
in Spanish…"), the memory block rendered that stored language with a
mirror instruction attached, and a dozen announcement paths composed
their sentences "in the user's own language". One misheard exchange
flipped her to Spanish and every layer kept her there.

The ruling (Alek, 2026-10-22): output is English, always — spoken and
written — regardless of the user's message language, stored memory or
tool results. Understanding other languages as INPUT is untouched:
speech recognition still detects what it hears, and a stored identity
language may still exist as an observation. It just never steers what
she says.
"""
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _language_section(prompt: str) -> str:
    start = prompt.index("[LANGUAGE]")
    end = prompt.index("\n[", start + 1)
    return prompt[start:end]


def test_prompt_language_section_pins_english():
    section = _language_section(_read("core/prompt.txt"))
    assert "You speak and write English only" in section
    assert "you still answer in English" in section
    # The mirror rule is gone from the section that used to carry it.
    assert "MOST RECENT message" not in section
    assert "answer in that one" not in section


def test_prompt_has_no_mirror_language_left():
    text = _read("core/prompt.txt")
    assert "language of the conversation" not in text
    assert "own language" not in text
    assert "in the\nuser's language" not in text
    # The aligned call sites now name English outright.
    assert "say exactly ONE short sentence in English" in text
    assert "Speak it naturally and briefly in English." in text


def test_briefing_ignores_stored_language():
    import main

    src = _read("main.py")
    assert "Speak this briefing in" not in src
    assert "lang_clause" not in src
    method = inspect.getsource(main.JarvisLive._send_startup_briefing)
    assert "identity" not in method
    assert "_val(" not in method


def test_session_summary_always_english():
    import main

    method = inspect.getsource(main.JarvisLive._save_session_summary)
    assert "in 1-2 sentences in English" in method
    assert 'get("identity"' not in method
    assert "lang_entry" not in method
    assert 'save_session_summary(summary, "English")' in method


def test_background_monitor_alerts_in_english():
    import main

    method = inspect.getsource(main.JarvisLive._run_background_monitor)
    assert "identity" not in method
    assert "in English. One brief sentence only." in method


def test_announcement_strings_pin_english():
    """No functional instruction anywhere composes output 'in the user's
    language' any more — confirmations, tool outcomes, diagnostics,
    self-heal, system alerts, video and the failover/scheduler paths in
    main.py all say English. (Comments about INPUT language elsewhere
    are not instructions and are not pinned here.)"""
    for rel in (
        "main.py",
        "core/confirm.py",
        "core/outcome.py",
        "actions/diagnostics.py",
        "actions/self_heal.py",
        "actions/system_monitor.py",
        "actions/video_player.py",
        "actions/youtube_video.py",
    ):
        text = _read(rel)
        assert "own language" not in text, rel
        assert "in their language" not in text, rel
        assert "in the user's language" not in text, rel
    youtube = _read("actions/youtube_video.py")
    assert "Write the summary in English." in youtube


def test_memory_language_label_never_mirrors():
    """A stored identity language still reaches the prompt — as a past
    observation with the English pin attached, never as the old
    'answer in the language of their CURRENT message' instruction."""
    from memory.memory_manager import format_memory_for_prompt

    out = format_memory_for_prompt(
        {"identity": {"language": {"value": "Spanish"}}}
    )
    assert "Has spoken to you in: Spanish" in out
    assert "you always answer in English" in out
    assert "CURRENT message" not in out


def test_live_speech_config_pins_en_us():
    """The Live session's speech config carries language_code='en-US':
    pinned in main.py's config builder, and the pinned google-genai SDK
    really accepts the field on SpeechConfig."""
    import main
    from google.genai import types

    method = inspect.getsource(main.JarvisLive._build_config)
    assert 'language_code="en-US"' in method

    cfg = types.SpeechConfig(
        language_code="en-US",
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
        ),
    )
    assert cfg.language_code == "en-US"
