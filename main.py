import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen


# ── Console must survive non-UTF-8 code pages ────────────────────────────────
# Every status line in this file carries an emoji, and on a legacy Windows
# console the active code page is the system one — cp1254 in Turkey, cp1251 in
# Russia, cp932 in Japan. Printing an emoji there raises UnicodeEncodeError, and
# because most of these prints sit inside the receive loop it takes the session
# down on startup. Reconfiguring to UTF-8 with a replacement fallback costs
# nothing and makes the app launch the same way in every locale.
import sys as _sys

from core.streams import ensure_streams
ensure_streams()   # windowless launches (pythonw) have NO stdout/stderr — give prints a file before anything can die on one (core/streams.py)

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw / redirected pipes / anything exotic — never fatal

# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

try:
    import sounddevice as sd
except Exception as _sd_error:
    # sounddevice initialises PortAudio AT IMPORT on some builds, and
    # on a machine with no working audio stack — a CI runner with no
    # PulseAudio server, a headless box — that raises PortAudioError,
    # which used to kill `import main` itself and every non-audio
    # feature and test with it (CI red on every push 2026-10-23→24
    # while the same suites passed on machines with audio). Degrade
    # instead: sd becomes a stub that raises this one clear error
    # only when an audio path is actually used — and the mic/speaker
    # open sites already treat an open failure as a device problem,
    # falling back or reporting it, which is what this is.
    # NB: the text is copied out of the except variable — Python
    # deletes the `as` name when the block ends, so a stub that read
    # _sd_error at call time would NameError instead of reporting.
    _SD_ERROR_TEXT = str(_sd_error)

    class _UnavailableSoundDevice:
        def __getattr__(self, _name):
            raise RuntimeError(
                f"sounddevice unavailable on this machine: {_SD_ERROR_TEXT}")

    sd = _UnavailableSoundDevice()
import numpy as np
from google import genai
from google.genai import types
from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary,
    search_memory, set_trim_notifier, forget_by_keyword,
)

# The file-backed tools (open_app, web_search, browser_control, …) are no longer
# imported or declared here — they self-describe via a TOOL dict in their own
# actions/*.py file and are auto-discovered by core.action_loader at startup.
# Only tools that are tied to live-session state stay inline in this file
# (screen_process, close_camera, save_memory, manage_monitor, shutdown_jarvis,
# restart_program, system_status).
from core import outcome
from core import power_intent
from core.action_loader import strip_meta
from core.playback import PlaybackWriter, cursor_horizon
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from actions                   import morning_briefing as _briefing
from memory.config_manager     import (
    get_brief_enabled, get_media_resolution, get_proactive_audio_enabled,
    get_push_to_talk_enabled, get_thinking_enabled, get_turn_tuning, get_voice,
    get_watchdog_enabled,
    get_wake_word_enabled, save_wake_word_enabled,    get_input_device, get_output_device,
)
from core                     import gemini as _gemini
from core.plugin_loader        import discover_plugins
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import routines as routines_core
from core                      import scheduler as scheduler_core
from core                      import diagnostics as diagnostics_core
from core                      import watchdog as watchdog_core
from core                      import codewatch as codewatch_core
from core                      import delegation as delegation_core
from core                      import consolidation as consolidation_core
from core                      import fleet as fleet_core
from core                      import notify as notify_core
from core                      import mcp_client as mcp_client_core
from types                     import SimpleNamespace
from core                      import audio_devices
from core.action_loader        import discover_actions
from core.echo                 import EchoGuard
from core.viseme               import VisemeStream
from core.wake_word            import (
    WakeWordDetector, is_ready as wake_is_ready, install_and_download as wake_install,
)

# How long the assistant stays awake with no user speech before it auto-sleeps
# again (wake-word mode only).
WAKE_SLEEP_TIMEOUT = 120.0   # seconds (2 minutes)

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
# The conversation's model. A NAME, not a decision: the ladder lives in
# core/gemini.py and this is only whichever rung is currently in use, kept here
# as a module attribute because plugins read it (chat_takeover asks main for it
# so that upgrading the assistant upgrades the plugin too).
#
# It is reassigned on every connect, so a model that runs out of quota is
# stepped over and the assistant keeps talking instead of failing to start.
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000 
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


# ── Viseme extraction ─────────────────────────────────────────────────────────
# The avatar's mouth used to be driven by one RMS value per ~200 ms write batch,
# which is five updates a second averaged over a fifth of a second — it could
# only ever flap. These read the *shape* of each 20 ms slice straight from the
# spectrum of the audio being played, so no transcript, no forced alignment and
# no language assumption: it works the same for Turkish and English.
#
# Two numbers come out. Openness tracks the first formant — F1 climbs as the jaw
# drops, so /a/ reads open and /i/ or /u/ read closed. Width tracks the second —
# F2 is high for spread vowels (/i/, /e/) and low for rounded ones (/u/, /o/).
# Extra time beyond the device's reported output latency before the microphone
# is trusted again: covers room decay and the speaker's own settling.
_TAIL_MARGIN = 0.25

# How long a sent turn may go unanswered before the session is presumed dead.
#
# A Live session can go half-dead: the server stops answering but the socket is
# never closed, so send_client_content() still succeeds — it is a write into a
# buffer — and `session.receive()` simply awaits forever. Nothing raises. The
# user's message reaches the activity log, the HUD stays on LISTENING, the
# terminal prints nothing, and the assistant is gone until it is restarted.
# That was the single worst failure this app could have, because it looked
# exactly like the model choosing not to reply.
#
# The threshold is turn-scoped rather than idle-scoped, and measured rather than
# guessed. An idle session receives NOTHING: watched for 150 s after a reply,
# not one frame arrived unprompted, so "silence" cannot mean "dead". But an
# ANSWERED turn is fast — first audio at 1.2 s, turn_complete at 3.6 s. Thirty
# seconds is twenty-five times the measured first response, which leaves room
# for a slow tool call (those send their own traffic, which clears the deadline)
# while turning an unbounded hang into a blip.
_STALL_LIMIT = 30.0

# Engine failover (Phase G2): how many unrecovered Live failures inside
# how long a window mean the session is not coming back by reconnecting.
# The existing cadence is the yardstick: a generic failure retries after
# 3 s, network backoff doubles to a 60 s cap, and the stall watchdog
# already calls a session dead after 30 s of silence. Three failures
# inside two minutes is at least three full reconnect cycles the
# machinery could not recover — spanning several times the longest
# single wait below the cap — while one transient drop, or two failures
# minutes apart, never reach it. (The other trigger needs no numbers:
# every Live rung resting at once — core/gemini.all_live_resting.)
_FAILOVER_WINDOW_SECONDS = 120.0
_FAILOVER_FAILURE_LIMIT  = 3

_VIS_WIN = 1024        # ~43 ms analysis window at 24 kHz: enough for formants
_VIS_HOP = 480         # 20 ms between frames, i.e. 50 shapes a second

# Delay from handing the first bytes of a reply to an already-running output
# stream to hearing them: one callback period, plus whatever the DAC adds.
_FIRST_SOUND = CHUNK_SIZE / RECEIVE_SAMPLE_RATE      # ~43 ms
# How far past the device's own buffer the mouth's timeline may drift before it
# is re-anchored. The buffer is the hard limit on how much audio can be queued
# ahead, so anything beyond it plus a margin for clock error is impossible.
_CURSOR_SLACK = 0.15

# Erring early is the safe direction. A viewer tolerates a mouth that moves
# slightly before the sound far better than one that moves after it — the
# broadcast limits are about 45 ms of lag against 125 ms of lead — so where
# this is uncertain it is biased to lead.


def _pcm_visemes(samples, sr: int = 24000):
    """Slice a PCM block into (level, openness, width) frames, one per 20 ms.

    Returns [] on anything unexpected — the mouth falls back to loudness-only
    articulation rather than the caller having to handle an error.
    """
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size < _VIS_WIN:
            return []
        win = np.hanning(_VIS_WIN).astype(np.float32)
        freqs = np.fft.rfftfreq(_VIS_WIN, 1.0 / sr)
        b_f1_lo = (freqs >= 150) & (freqs < 450)     # F1 of close vowels
        b_f1_hi = (freqs >= 450) & (freqs < 1100)    # F1 of open vowels
        b_f2_bk = (freqs >= 600) & (freqs < 1300)    # F2 of rounded vowels
        b_f2_fr = (freqs >= 1700) & (freqs < 3200)   # F2 of spread vowels
        b_hiss = (freqs >= 3800) & (freqs < 8000)    # fricatives

        # One frame per hop across the *whole* block. Stepping only while a full
        # window fits stopped 1024 - 480 samples short of the end, so a 200 ms
        # batch yielded 160 ms of schedule: the mouth ran out of frames before
        # the audio ran out of sound, and each batch no longer lined up with the
        # end of the one before it. Losing 20 % of every batch is most of why
        # the mouth did not track the words.
        out = []
        for start in range(0, x.size, _VIS_HOP):
            # The level gates closures, so it is measured over exactly this
            # 20 ms and never looks ahead. The spectrum needs a longer window
            # to resolve formants and may be short-filled at the very end.
            level = _pcm_level(x[start:start + _VIS_HOP])
            seg = x[start:start + _VIS_WIN]
            if seg.size < _VIS_WIN:
                seg = np.concatenate([seg, np.zeros(_VIS_WIN - seg.size,
                                                    dtype=np.float32)])
            if level <= 0.0:
                out.append((0.0, 0.0, 0.0))
                continue
            mag = np.abs(np.fft.rfft((seg - seg.mean()) * win))
            f1l, f1h = float(mag[b_f1_lo].sum()), float(mag[b_f1_hi].sum())
            f2b, f2f = float(mag[b_f2_bk].sum()), float(mag[b_f2_fr].sum())
            hiss = float(mag[b_hiss].sum())

            openness = f1h / (f1l + f1h + 1e-6)
            width = (f2f - f2b) / (f2f + f2b + 1e-6)
            # A wide-open jaw physically cannot purse, so openness damps width.
            # /a/ has a low enough F2 to read as "rounded" on the bands alone;
            # letting openness suppress the width term is what keeps an open
            # vowel from pursing.
            width *= (1.0 - openness) ** 0.8
            # Fricatives are formed with a nearly closed mouth.
            h = hiss / (f1l + f1h + f2b + f2f + hiss + 1e-6)
            openness *= 1.0 - 0.65 * min(1.0, h * 2.5)
            out.append((level,
                        float(min(1.0, max(0.0, openness))),
                        float(min(1.0, max(-1.0, width)))))
        return out
    except Exception:
        return []


def _describe_tools(declarations) -> str:
    """One line per capability, straight from the live tool declarations.

    Derived rather than written down: the action and plugin registries are
    discovered at startup, so whatever the user has installed is what the model
    is told it can do. Adding a plugin extends this by itself, and removing one
    stops the model from claiming an ability it no longer has.
    """
    lines = []
    for d in declarations or ():
        try:
            name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
            desc = (d.get("description") if isinstance(d, dict)
                    else getattr(d, "description", "")) or ""
        except Exception:
            continue
        if not name:
            continue
        desc = " ".join(str(desc).split())
        lines.append(f"- {name}: {desc[:150]}" if desc else f"- {name}")
    return "\n".join(lines)


def _describe_limits(has_vision: bool, has_mic: bool) -> str:
    """The other half of self-knowledge: what is out of reach, and why.

    Derived from how the program is actually built, not from a list of refusals.
    A model that knows its boundaries stops improvising around them, and stating
    them as architecture rather than as rules keeps the answer honest in any
    language.
    """
    out = [
        "- Anything not listed above is outside your reach. Say so in one clause "
        "and offer the nearest thing you can actually do — never mime an action "
        "you cannot take, and never report a result you did not get.",
        "- You act on this machine only. You cannot reach the user's other "
        "devices, accounts or hardware except through the tools listed above.",
        "- You remember what is in the memory block and what has been said this "
        "session. Anything else you were told before is gone unless it was saved.",
    ]
    # What the PLATFORM forbids, before what the build lacks.
    #
    # On Wayland, and on a macOS without Accessibility, the keyboard, mouse and
    # screen are simply not available to any program — and the APIs report
    # success anyway. A model that does not know this reports clicks that never
    # happened, which is how "it does not work properly on Linux" was described
    # for a long time without anybody being able to name the failing part.
    try:
        from core import platform_support
        out.extend(platform_support.describe_for_model(platform_support.detect()))
    except Exception as _e:
        print(f"[Limits] platform capabilities unavailable: {_e}")

    if has_vision:
        out.append(
            "- Your sight is not continuous. You see nothing until you call a "
            "vision tool, and then only that single frame at that moment — you "
            "cannot watch, monitor or notice something changing on screen.")
    else:
        out.append("- You have no sight at all in this build.")
    if has_mic:
        out.append(
            "- You hear nothing while the microphone is muted, and you cannot "
            "unmute it yourself.")
    return "\n".join(out)


def _render_prompt(template: str, values: dict) -> str:
    """Fill {tokens} in the prompt template.

    A plain replace rather than str.format: the file is meant to be edited by
    hand, and a stray brace in someone's own wording must never take the app
    down at startup.
    """
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are ARIA (A.R.I.A. — Adaptive Robotic Intelligence Agent), "
            "a familiar bound to Alek: warm, direct, and concise. "
            "Always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# Transcript chunks shorter than this may legitimately repeat ("evet, evet"),
# so only longer ones are treated as duplicates.
_REPEAT_MIN = 12


def _is_repeat_chunk(txt: str, buf: list) -> bool:
    """True if this transcript chunk has already been seen this turn.

    Guards against the API re-sending the tail of a response across the several
    turn_completes a tool-using turn produces.
    """
    if len(txt) < _REPEAT_MIN:
        return bool(buf) and txt == buf[-1]
    joined = " ".join(buf)
    return txt in joined

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    # ── Inline tools ─────────────────────────────────────────────────────────
    # These stay here (rather than in an actions/*.py TOOL dict) because their
    # handling is woven into live-session state — vision capture/injection,
    # camera stream, memory writes, the monitor engine, and shutdown. All other
    # tools live in their own action file and are auto-discovered by
    # core.action_loader (see JarvisLive.__init__).
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when the user says (in ANY language): close camera, stop camera, "
            "turn off camera, that's creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "You check these topics once a day and alert the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop the assistant. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "restart_program",
        "description": (
            "Restarts the assistant's own program: this app relaunches "
            "itself on this machine. "
            "Call this when the user tells you to restart your program, "
            "restart yourself, or restart the app. "
            "This is NOT a computer restart — when the user means the "
            "PC itself, that is computer_settings, never this."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "forget_memory",
        "description": (
            "Forget stored facts about the user whose key or value contains "
            "a keyword. Call this when the user says 'forget X', 'forget "
            "about my ...', or asks you to delete something you remember. "
            "This permanently deletes EVERY matching entry from long-term "
            "memory — one keyword can match several facts — and the result "
            "names exactly what was removed, so report it accurately. "
            "A query that matches nothing changes nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to forget — a name, a topic, or a word "
                        "from the fact (e.g. 'playlist', 'old address')."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


class JarvisLive:
    def __init__(self, ui: JarvisUI):
        # Platform limits are announced once per run, not once per
        # reconnect: a dropped connection is not news about Wayland.
        self._platform_notes_shown = False
        self.ui             = ui
        self._asst_name     = "ARIA"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                     = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        # Which engine is holding the conversation, and what it can do.
        # None means Gemini Live — the default and, unless somebody has gone to
        # the providers settings and chosen otherwise, always the case. Every
        # branch below that reads it treats None as "everything is available",
        # so the Live path is exactly what it was.
        self._engine_caps          = None
        # Set only when a local pipeline is running, so push-to-talk can tell it
        # the turn is over. Live decides that on the server; nothing does here.
        self._engine               = None
        # Engine failover (Phase G2). _failed_over latches for the rest
        # of the process run once Live is unrecoverable and a complete
        # pipeline is configured: _resolve_engine_caps then resolves the
        # pipeline whatever the saved mode says, and nothing switches
        # back mid-run — Live returns on the next launch, or when the
        # user changes the engine in the panel (_on_engine_applied).
        # _failover_dead remembers that the pipeline would not build at
        # switch time, so the attempt is never repeated as a loop of
        # its own. _failover_notice holds the provider label until the
        # spoken notice has been delivered, exactly once.
        self._failed_over          = False
        self._failover_dead        = False
        self._failover_notice: str | None = None
        self._failover_outage_noted = False
        self._live_failure_times: list[float] = []
        # Announcements queued while she slept (Phase G4): the
        # scheduler's say hook parks lines here instead of speaking
        # them into an empty room; wake() speaks the queue, once.
        self._pending_announce: list[str] = []
        # Phone notifications (core/notify.py): run() binds this to
        # notify_core.notify; the say-hook's asleep branch pushes its
        # queued lines through it. None until bound — a session that
        # never bound it (tests, headless) queues exactly as before.
        self._notify_hook = None
        # Code watcher (core/codewatch.py — the old ARIA's in-process
        # watcher, ported 2026-10-22; it retires the updater sidecar).
        # It snapshots her own source tree NOW, at boot; after every
        # completed turn the receive loop asks it to check (see
        # _maybe_check_codewatch). Announcing goes through speak(), the
        # restart through _restart_after_self_edit — announced in her
        # voice, never confirm-gated, exactly as ARIA's self-restart
        # was. run() stamps the boot into its ledger (mark_boot) where
        # the sidecar's boot stamp used to be written.
        self._codewatch_busy       = False
        self._codewatch            = codewatch_core.CodeWatcher(
            announce=self.speak,
            restart=self._restart_after_self_edit,
            speech_idle=lambda: not self._is_speaking,
        )
        # Deadline for "we sent a turn and the server has said nothing since".
        # None means nothing is outstanding. Armed by the send sites, cleared by
        # the first frame of ANY response, watched by _watch_stall().
        self._rx_deadline          = None
        # Transcript-driven mouth shapes for the avatar. Fed from the receive
        # loop as words arrive, drained by the playback loop against the audio.
        self._visemes              = VisemeStream()
        self._last_out_logged      = ""      # de-dupes a re-sent transcript tail
        # Push-to-talk
        self._ptt_enabled          = False
        self._ptt_held             = False
        self._ptt                  = None    # core.hotkey.PushToTalk
        self._out_level            = 0.0     # level of the audio being played right now
        self._echo                 = EchoGuard()
        # `stream.write()` returns when the buffer accepts the audio, not when the
        # speaker has finished with it, so sound is still in the room after the
        # speaking flag drops. Streaming the microphone during that gap is how an
        # assistant ends up answering itself. Measured from the device rather than
        # guessed; see _play_audio.
        self._out_latency          = 0.20    # seconds, replaced with the real value
        self._tail_until           = 0.0     # monotonic time the echo tail expires
        # Wall-clock time at which the audio written next will begin to sound.
        # The mouth is scheduled against this, never against "now": batches are
        # handed to the device far faster than they play, so "now" ran the lips
        # ahead of the words and cut every schedule short. 0 = nothing playing.
        self._play_cursor          = 0.0
        # The playback writer thread for the running _play_audio, if any
        # (core/playback.py). interrupt() flushes it so a barge-in leaves
        # at most the in-flight ~200 ms batch sounding.
        self._playback_writer      = None
        self.ui.on_push_to_talk   = self.set_push_to_talk
        self.ui.ptt_hold          = self._on_ptt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self.ui.on_engine_applied = self._on_engine_applied   # providers panel: a manual engine choice outranks the failover latch
        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

        self._enhanced_live = True  # proactive audio; auto-disabled if the server rejects it
        self._tuned_live    = True  # turn-taking / media / thinking knobs; same fallback

        _base_dir = Path(__file__).resolve().parent
        _inline_names = {t["name"] for t in TOOL_DECLARATIONS}

        # File-backed tools: every actions/*.py with a TOOL dict, discovered the
        # same way plugins are. Reserved names = the inline tools above, so an
        # action can never shadow one.
        self._action_registry = discover_actions(
            actions_dir=_base_dir / "actions",
            reserved_names=_inline_names,
            logger=lambda msg: print(f"[Actions] {msg}"),
        )

        # Plugins must not collide with either an inline tool or a discovered action.
        _core_names = _inline_names | self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=_base_dir / "plugins",
            core_tool_names=_core_names,
            # Console gets the full boot transcript; the activity log gets only
            # what the user has to know about. Every plugin loading correctly is
            # the expected case and does not belong in their conversation.
            logger=lambda msg: print(f"[Plugins] {msg}"),
            notify=lambda msg: self.ui.write_log(f"SYS: {msg}"),
        )
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.get_plugin_settings = self._plugin_registry.settings_schemas  # ⚙ settings tab
        self.ui.get_commands = self._commands_for_ui  # Commands screen rows
        self.ui.get_engine_info = self._engine_info_for_ui  # ENGINE status plaque (ui.py)
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel
        self.ui.on_run_diagnostics = self._diagnostics_for_ui  # 🩺 DIAGNOSTICS button

        # ── Wake word ────────────────────────────────────────────────────────
        # _awake gates the mic (see _listen_audio) and the background speakers.
        # It is True whenever wake word is OFF, so default behaviour is unchanged.
        self._wake_enabled     = get_wake_word_enabled()
        self._awake            = not self._wake_enabled
        self._wake_detector: WakeWordDetector | None = None
        self._wake_sleep_timeout = WAKE_SLEEP_TIMEOUT

        # ── Presence (v1) ────────────────────────────────────────────────
        # The presence monitor (core/presence.py) is built and registered
        # in run() by _start_presence; None until then, and None forever
        # when presence_enabled is off. Deliberately unrelated to _awake:
        # presence naps the HUD and pauses proactive speech, it never
        # gates the mic, typing, or a command.
        self._presence_monitor = None
        # The idle window _presence_busy judges session speech
        # against; _start_presence overwrites it from config. 600
        # matches presence_idle_seconds' own default, so the probe
        # is sane even before (or entirely without) a monitor.
        self._presence_idle_s = 600

        # Restore the saved push-to-talk preference. Doing it here rather than
        # in __init__ means the hotkey thread only exists once there is a
        # session to talk to.
        if get_push_to_talk_enabled():
            try:
                self.set_push_to_talk(True)
            except Exception as e:
                print(f"[JARVIS] ⚠ Push-to-talk unavailable: {e}")
        # UI control surface for the Wake Word settings section.
        self.ui.wake_is_ready    = wake_is_ready          # () -> bool
        self.ui.wake_get_state   = self._wake_state       # () -> dict
        self.ui.on_wake_toggle   = self._ui_wake_toggle   # (enable: bool) -> str
        self.ui.on_presence_toggle = self._ui_presence_toggle  # SETUP drawer PRESENCE toggle
        self.ui.on_wake_manual   = self._ui_wake_manual   # () -> toggle awake/asleep
        self.ui.on_wake_install  = self._ui_wake_install  # () -> (ok, msg)

    def _commands_for_ui(self) -> list[dict]:
        """Rows for the Commands screen, built fresh from the three live
        registries every time the panel opens (core/commands.py) — so the
        screen describes the assistant as she is configured now, not as
        she was when a guide was last edited."""
        from core.commands import build_command_list
        return build_command_list(
            TOOL_DECLARATIONS,
            self._action_registry.get_tool_declarations(),
            self._plugin_registry.get_tool_declarations(),
            mcp_client_core.declarations(),
        )

    def _diagnostics_for_ui(self) -> None:
        """The DIAGNOSTICS button in the SYS rail: the same pass
        the run_diagnostics tool runs, delivered the same way —
        checklist to the panel, the [Diagnostics] block in the
        log, an incident on failure, one spoken line — through
        core/diagnostics.deliver(). The pass itself runs off
        both the Qt thread and the event loop (run_all pools its
        probes). Re-entry while a pass is running is refused:
        the probes are read-only, but two spoken summaries at
        once would be noise, not information."""
        if getattr(self, "_diagnostics_running", False):
            return
        self._diagnostics_running = True

        def _deliver(results):
            diagnostics_core.deliver(
                results,
                show_content=self.ui.show_content,
                write_log=self.ui.write_log,
                announce=lambda text: self.plugin_say(
                    "Announce this to the user now, in one short "
                    "sentence, in English: " + text))

        def _work():
            try:
                _deliver(diagnostics_core.run_all())
            finally:
                self._diagnostics_running = False

        loop = getattr(self, "_loop", None)
        try:
            if loop:
                asyncio.run_coroutine_threadsafe(
                    asyncio.to_thread(_work), loop)
                return
        except Exception:
            pass   # no usable loop — the plain thread below serves
        threading.Thread(target=_work, daemon=True,
                         name="diagnostics").start()

    def _all_tool_names(self) -> set:
        """Every tool name the model can call right now — the same three
        registries the Commands screen reads, as a set. core/routines.py
        pre-flights a replay against this: a step naming a tool that no
        longer exists aborts the routine before anything runs."""
        return ({t["name"] for t in TOOL_DECLARATIONS}
                | self._action_registry.names()
                | {d["name"] for d in
                   self._plugin_registry.get_tool_declarations()}
                | mcp_client_core.tool_names())

    def _execute_routine_step(self, name: str, args: dict) -> str:
        """The routines executor, bound in run() (core/routines.py).

        A replayed step goes through _execute_tool itself — a throwaway
        call object in, the FunctionResponse's result text out — so it
        receives the inline branches, the gates, the outcome text and
        the capture exclusion exactly as a spoken call does. This is
        invoked from whichever worker thread the replay is running on;
        the coroutine is scheduled onto the session loop and waited on.
        The wait cannot starve the loop: _execute_tool hands registry
        work to the default executor pool, which has other threads."""
        if self._loop is None:
            raise RuntimeError("the session loop is not running")
        fc = SimpleNamespace(id=f"routine-{name}", name=name,
                             args=dict(args or {}))
        future = asyncio.run_coroutine_threadsafe(
            self._execute_tool(fc), self._loop)
        response = future.result(timeout=600)
        payload = getattr(response, "response", None) or {}
        return str(payload.get("result", "Done."))

    # ── Delegated background tasks (core/delegation.py) ──────────────

    # Tool names never offered to a delegated task's model — the rule
    # and its reasons live in core/delegation.py's docstring.
    _DELEGATED_EXCLUDE = frozenset({
        "delegate_task", "shutdown_jarvis", "restart_program",
        "run_routine", "edit_own_code", "patch_own_code",
        "replace_lines_own_code",
        "run_shell_command", "notify_phone", "update_status",
        "task_resume", "task_cancel",
    })

    def _execute_delegated_step(self, name: str, args: dict) -> str:
        """The delegation executor, bound in run() (core/delegation.py).

        A delegated task's tool call goes through _execute_tool
        itself — a throwaway call object in, the FunctionResponse's
        result text out — so it receives the inline branches, the
        confirmation gate and the outcome text exactly as a spoken
        call does. Invoked from a delegation worker thread; the
        coroutine is scheduled onto the session loop and waited on,
        the routines executor's shape (with which it shares the
        funnel, deliberately)."""
        if self._loop is None:
            raise RuntimeError("the session loop is not running")
        fc = SimpleNamespace(id=f"task-{name}", name=name,
                             args=dict(args or {}))
        future = asyncio.run_coroutine_threadsafe(
            self._execute_tool(fc), self._loop)
        response = future.result(timeout=600)
        payload = getattr(response, "response", None) or {}
        return str(payload.get("result", "Done."))

    def _delegated_tool_declarations(self) -> list[dict]:
        """The declarations a delegated task's model may call: the
        conversation's own three registries, minus the exclusions,
        with the bookkeeping keys stripped — the same list the
        Commands screen is built from, so what she can be asked to
        do in the background is what the screen says she can do."""
        decls = (list(TOOL_DECLARATIONS)
                 + self._action_registry.get_tool_declarations()
                 + self._plugin_registry.get_tool_declarations())
        kept = [d for d in decls
                if d.get("name") not in self._DELEGATED_EXCLUDE]
        return strip_meta(kept)

    def _delegated_model_turn(self, messages, tool_decls):
        """One model turn for a delegated task (core/delegation.py's
        call_model seam), over core/gemini's one-shot ladder: the
        plain message protocol becomes genai contents, the tool
        declarations ride in the call's config, and the reply's
        text + function calls come back in the same plain shape.
        Runs on a delegation worker thread; gemini.call is a
        blocking one-shot, which is what a worker is for. NOTE:
        a PROVIDER rung answers text-only in this app (its bridge
        carries no tools), so on such a rung a delegated task
        answers from its prompt alone — a limit of the bridge,
        stated here rather than discovered mid-task."""
        from google.genai import types as _gtypes
        contents = []
        for m in messages:
            role = m.get("role")
            if role == "user":
                contents.append(_gtypes.Content(
                    role="user",
                    parts=[_gtypes.Part(text=m.get("text", ""))]))
            elif role == "model":
                parts = []
                if m.get("text"):
                    parts.append(_gtypes.Part(text=m["text"]))
                for c in m.get("calls") or []:
                    parts.append(_gtypes.Part(function_call=
                        _gtypes.FunctionCall(
                            name=c["name"], args=c.get("args") or {})))
                contents.append(_gtypes.Content(role="model",
                                                parts=parts))
            elif role == "tool":
                parts = [_gtypes.Part(function_response=
                    _gtypes.FunctionResponse(
                        name=r["name"],
                        response={"result": r["result"]}))
                    for r in m.get("results") or []]
                contents.append(_gtypes.Content(role="user",
                                                parts=parts))
        config = _gtypes.GenerateContentConfig(
            tools=[_gtypes.Tool(function_declarations=tool_decls)])
        resp = _gemini.call(contents, tier=_gemini.SMART,
                            config=config, timeout_ms=120_000)
        if resp is None:
            raise RuntimeError("no model answered — Gemini is "
                               "unreachable or every rung is resting")
        text, calls = "", []
        try:
            parts = resp.candidates[0].content.parts or []
        except Exception:
            parts = []
        for part in parts:
            if getattr(part, "text", None):
                text += part.text
            fc = getattr(part, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                calls.append({"name": fc.name,
                              "args": dict(fc.args or {})})
        if not calls and not text:
            text = getattr(resp, "text", None) or ""
        return text.strip(), calls

    def _consolidation_propose(self, system_text: str,
                               user_text: str) -> str:
        """The consolidation pass's one-shot model seam
        (core/consolidation.py, bound in run()). One user turn, the
        editor's instructions as the system prompt, the SMART rung
        — judgement work, not transcription — and the delegated
        ladder's honest failure: no text raises, and the pass
        records the night as skipped and writes nothing."""
        from google.genai import types as _types
        resp = _gemini.call(
            [_types.Content(role="user",
                            parts=[_types.Part(text=user_text)])],
            tier=_gemini.SMART,
            config=_types.GenerateContentConfig(
                system_instruction=system_text),
            timeout_ms=120_000)
        if resp is None:
            raise RuntimeError("consolidation model call failed")
        text = (getattr(resp, "text", "") or "").strip()
        if not text:
            cand = (getattr(resp, "candidates", None) or [None])[0]
            parts = []
            if cand is not None:
                parts = getattr(getattr(cand, "content", None),
                                "parts", None) or []
            text = "".join(getattr(p, "text", "") or ""
                           for p in parts).strip()
        if not text:
            raise RuntimeError("consolidation turn returned no text")
        return text

    # ── Wake word: state machine ─────────────────────────────────────────────

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: print(f"[Wake] {m}"),
                notify=lambda m: self.ui.write_log(f"SYS: {m}"),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")
        self._deliver_failover_notice()
        self._flush_pending_announce()

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")

    # ── Presence (v1) ────────────────────────────────────────────────────
    # A visual + proactive-speech layer only (core/presence.py owns the
    # logic and the hard rules): the HUD naps when Alek steps away and
    # a plain, time-aware greeting is spoken when a long absence ends.
    # Nothing here touches _awake, _last_user_speech, sleep()/wake(),
    # or any command path — presence can never refuse him anything.

    def _presence_media_playing(self) -> bool:
        """Media veto probe: ARIA speaking, or Spotify reporting
        playback. Best-effort — any failure reads as not playing."""
        with self._speaking_lock:
            if self._is_speaking:
                return True
        try:
            from core import spotify as _spotify
            return bool(_spotify.now_playing().get("playing"))
        except Exception:
            return False

    def _presence_busy(self) -> bool:
        """Busy veto probe: ARIA speaking, a live conversation
        session with recent speech from him, or work in flight —
        the fleet count (delegated tasks plus in-flight tool calls)
        is what main already tracks for the HUD. Best-effort.

        The session clause needs the speech recency: voice input
        never resets the Windows idle clock, so his speech is the
        presence evidence the idle signal cannot see — but the
        session alone is not evidence, it outlives him walking
        away by hours. Speech inside the idle window counts; an
        open-but-silent session does not (core/presence.py's
        session_counts_as_activity)."""
        with self._speaking_lock:
            if self._is_speaking:
                return True
        try:
            from core import presence as _presence
            if _presence.session_counts_as_activity(
                    bool(self.session),
                    time.monotonic() - self._last_user_speech,
                    getattr(self, "_presence_idle_s", 600)):
                return True
        except Exception:
            pass
        try:
            return fleet_core.running_count() > 0
        except Exception:
            return False

    def _presence_allows_speech(self) -> bool:
        """May she speak UNPROMPTED right now? Presence is the gate:
        while Alek is away, every background voice path stays silent
        — the proactive check-ins and the two monitor loops included,
        not just the scheduler's alerts (live witness 2026-10-24: the
        state machine marked him away correctly and the proactive
        engine still asked the empty room about the Android app).
        Fail-open, like everything presence touches: no monitor,
        presence off, or any error answers yes — presence can only
        ever silence her, never mute a conversation he is having."""
        try:
            from core import presence as _presence
            return bool(_presence.is_present())
        except Exception:
            return True

    def _presence_on_away(self, reason: str) -> None:
        try:
            self.ui.write_log(
                f"SYS: Presence — you stepped away ({reason}); "
                "I'll keep quiet until you're back.")
            if not self.ui.muted:
                self.ui.set_state("SLEEPING")
        except Exception:
            pass

    def _presence_on_return(self, away_s: float, greet: bool) -> None:
        try:
            self.ui.write_log(
                f"SYS: Presence — you're back "
                f"(away {int(away_s // 60)}m).")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
        except Exception:
            pass
        if not greet:
            return
        try:
            from core import presence as _presence
            now = datetime.now()
            line = _presence.greeting_for(
                now.hour, away_s, now.toordinal())
        except Exception:
            return
        # The greeting is always on the record; it is spoken only
        # when a session is live, through the proactive-speech path
        # (plugin_say, the _scheduler_say channel) — and never
        # queued for later if it cannot go out now.
        try:
            self.ui.write_log(f"SYS: {line}")
        except Exception:
            pass
        if self.session:
            self.plugin_say(
                "Announce this greeting to the user now, in one "
                "short sentence, in English: " + line)

    def _start_presence(self) -> None:
        """Build and register the presence monitor (called once in
        run(), beside the other service bindings). Disabled in
        config = no monitor, no registration, no loop work."""
        try:
            from core import presence as _presence
            from memory.config_manager import get_presence_settings
            settings = get_presence_settings()
            if not settings.get("presence_enabled", True):
                self.ui.write_log(
                    "SYS: Presence is off (presence_enabled=false).")
                return
            self._presence_idle_s = settings["presence_idle_seconds"]
            monitor = _presence.PresenceMonitor(
                idle_signal=_presence.idle_seconds,
                locked_signal=_presence.locked,
                media_signal=self._presence_media_playing,
                busy_probe=self._presence_busy,
                idle_after_s=settings["presence_idle_seconds"],
                greet_after_s=settings["presence_greet_after_seconds"],
                quiet_hours=(settings["presence_quiet_start"],
                             settings["presence_quiet_end"]),
                enabled=True,
                on_away=self._presence_on_away,
                on_return=self._presence_on_return)
            _presence.set_monitor(monitor)
            self._presence_monitor = monitor
            self.ui.write_log(
                "SYS: Presence on — I'll nap when you step away "
                "and say hello when you're back.")
        except Exception as e:
            print(f"[Presence] ⚠️ start failed: {e}")

    async def _run_presence_watch(self) -> None:
        """Presence v1: tick the monitor every 5s. The probes touch
        the window list and the fleet stores, so the tick runs off
        the event loop; a failed tick is skipped, never fatal."""
        while True:
            await asyncio.sleep(5)
            monitor = self._presence_monitor
            if monitor is None:
                continue
            try:
                await asyncio.to_thread(monitor.tick)
            except Exception:
                pass

    # ── Presence: UI callback (SETUP drawer toggle) ─────────────────────

    def _ui_presence_toggle(self, enable: bool) -> None:
        """Apply the SETUP drawer's PRESENCE toggle live.

        Off unregisters the monitor (the watch loop no-ops on None;
        scheduler and screen watches fall back to fail-open present)
        and restores the HUD if an absence had napped it — never a
        greeting, never touching _awake or the wake-word path. On
        rebuilds through _start_presence, which re-reads config
        (the drawer saves presence_enabled before calling this)."""
        try:
            if enable:
                if self._presence_monitor is None:
                    self._start_presence()
                else:
                    self._presence_monitor.enabled = True
                    self.ui.write_log("SYS: Presence on.")
                return
            mon = self._presence_monitor
            if mon is not None:
                was_away = not mon.present
                from core import presence as _presence
                _presence.set_monitor(None)
                self._presence_monitor = None
                if was_away and not self.ui.muted:
                    self.ui.set_state("LISTENING")
            self.ui.write_log(
                "SYS: Presence off — I'll stay put until you turn me "
                "back on.")
        except Exception as e:
            print(f"[Presence] ⚠️ toggle failed: {e}")

    # ── Wake word: UI callbacks (called from the Qt thread) ──────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        # Triggered by the user pressing the button, so its progress is exactly
        # what they are waiting to see.
        return wake_install(logger=lambda m: print(f"[Wake] {m}"),
                            notify=lambda m: self.ui.write_log(f"SYS: {m}"))

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask JARVIS to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in
        English. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": instruction}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    # ── stall watchdog ───────────────────────────────────────────────────────

    def _arm_stall(self) -> None:
        """A user turn has gone out; start the clock if one is not already running.

        Deliberately does NOT push an existing deadline forward. The question is
        "how long since the server last said anything while we were waiting",
        not "how long since the most recent send" — otherwise a burst of sends
        into a dead socket would keep resetting the very timer meant to catch it.
        """
        if self._rx_deadline is None:
            self._rx_deadline = time.monotonic() + _STALL_LIMIT

    def _clear_stall(self) -> None:
        """The server spoke, so the return path is alive."""
        self._rx_deadline = None

    async def _watch_stall(self) -> None:
        """Rebuild the session when a sent turn goes unanswered past the limit.

        The reconnect keeps the resumption handle, so the conversation survives:
        this costs a couple of seconds and a line in the log, where before it
        cost the rest of the session with no indication anything was wrong.
        """
        while True:
            await asyncio.sleep(2)
            deadline = self._rx_deadline
            if deadline is None or time.monotonic() < deadline:
                continue
            self._clear_stall()
            print(f"[JARVIS] ⚠️  No response for {_STALL_LIMIT:.0f}s — "
                  f"the live session is not answering. Rebuilding it.")
            self.ui.write_log("SYS: The model stopped answering — reconnecting.")
            self.request_reconnect(keep_context=True, reason="no response")
            return          # this session is going away; the new one gets its own watcher

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    def _on_engine_applied(self):
        """The providers panel applied a new engine choice.

        A deliberate choice outranks a failover: clear the latch (and
        the dead-target memory) so the session the panel's own
        reconnect is already rebuilding resolves whatever the user
        just picked. If they picked Gemini and it is still down, the
        triggers simply fire again — a new episode, judged on its own
        failures."""
        if self._failed_over or self._failover_dead:
            print("[Engine] Failover latch cleared — engine changed "
                  "in the panel.")
        self._failed_over     = False
        self._failover_dead   = False
        self._failover_notice = None

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        # The bind error travels with the URL because the UI has no reference to
        # this object — it only ever sees what this callback returns.
        return (url, key, f"{url}/auto-login?key={key}", manual,
                getattr(self._dashboard, "_bind_error", "") or "")

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        # Respect wake-word sleep: a typed command must not be answered while
        # asleep either (the sleep gate is not just for the mic). Wake first with
        # "Hey Jarvis" or the WAKE NOW button.
        if self._wake_enabled and not self._awake:
            self.ui.write_log("SYS: I'm asleep — say 'Hey Jarvis' or tap WAKE NOW first.")
            return
        # The user's own typed words — one of the only seams allowed to
        # arm a computer power action (core/power_intent.py).
        power_intent.note_user_text(text)
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _tail_active(self) -> bool:
        """True while the speakers may still be finishing our last sentence."""
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
        else:
            # Hold the guard open across the device's own output latency plus a
            # margin for the room. The microphone is NOT muted during it — the
            # guard still lets a genuine reply through, so answering instantly
            # still works. Only our own echo is dropped.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
        if not value:
            # The echo history is deliberately NOT cleared here: the tail above
            # still needs it to recognise our own voice. It is dropped when the
            # tail expires. What the guard learned about the room always stays.
            self._out_level = 0.0
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def set_push_to_talk(self, enabled: bool) -> str:
        """Turn hold-to-talk on or off. Returns the scope actually achieved."""
        from core.hotkey import PushToTalk

        self._ptt_enabled = bool(enabled)
        self._ptt_held = False
        if not enabled:
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return "off"

        if self._ptt is None:
            self._ptt = PushToTalk(self._on_ptt)
        scope = self._ptt.start()
        # A window-scoped chord is a real limitation, not a detail — say it once
        # in the log so nobody wonders why it does nothing while another app is
        # focused. Reporting it must never be able to undo the thing it reports.
        try:
            self.ui.write_log(
                f"SYS: Push-to-talk on — hold {self._ptt.label}"
                + ("." if scope == "global"
                   else " (works while this window is focused)."))
        except Exception:
            pass
        return scope

    def _on_ptt(self, held: bool) -> None:
        """Chord pressed or released — may arrive on the hotkey thread."""
        self._ptt_held = held
        if held:
            # Holding the key is also a way to wake it, so push-to-talk works
            # without having to say the wake word first.
            if self._wake_enabled and not self._awake:
                self._awake = True
                self._last_user_speech = time.monotonic()
        # Releasing the key ends the turn. On Live the server works that out from
        # the audio; a local pipeline has no server, so the release IS the
        # signal — which is also why it is the more reliable of the two ways to
        # talk to one. Scheduled onto the loop because this may arrive on the
        # hotkey thread.
        if not held and self._engine is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._engine.flush_microphone(SEND_SAMPLE_RATE), self._loop)
            except Exception as e:
                print(f"[JARVIS] PTT flush failed: {e}")
        try:
            if held:
                state = "LISTENING"
            else:
                # Releasing the key does NOT mean the assistant went to sleep.
                #
                # This used to paint SLEEPING unconditionally, and SLEEPING in
                # this app means one specific thing — the wake-word gate is
                # closed, the lids are down, nothing is being listened to at all.
                # With push-to-talk on, every key release asserted that, so
                # tapping WAKE NOW appeared to do nothing: the assistant really
                # did wake, and the next release immediately painted it asleep
                # again. Two different ideas were sharing one state.
                state = ("SLEEPING" if (self._wake_enabled and not self._awake)
                         else "LISTENING")
            self.ui.set_state(state)
        except Exception:
            pass

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[JARVIS] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        # Drop any batches the playback writer has not written yet, so the
        # speakers stop with the queue: at most the in-flight ~200 ms batch
        # is still sounding — the bound the batching cap has always promised.
        _writer = self._playback_writer
        if _writer is not None:
            try:
                _writer.flush()
            except Exception:
                pass
        # The words we were about to mouth are never going to be spoken now.
        self._visemes.reset()
        self._play_cursor = 0.0     # next batch starts a fresh timeline
        if self._turn_done_event:
            self._turn_done_event.clear()
        # Draining the queue stops the speakers, but a local engine is still
        # generating the rest of the answer and would keep filling it back up.
        # Live has nothing to cancel here — the server stops on its own.
        if self._engine is not None:
            try:
                self._engine.interrupt()
            except Exception:
                pass
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        # Tool exceptions never become outcomes, so core/outcome.py's record
        # never sees them — this is the one place they can be written down.
        try:
            from core import incidents
            incidents.record(f"tool:{tool_name}", "exception", str(error))
        except Exception:
            pass
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"{tool_name} encountered an error. {short}")

    def _maybe_check_codewatch(self):
        """Post-turn code-watch check, called from the receive loop's
        turn_complete handling — the ONE completion path both engines
        share: providers/base.py's contract is that a pipeline turn ends
        with the same server_content.turn_complete a Live turn does, so
        this covers Gemini Live and the local pipeline alike. The check
        itself (hashing the tree, compiling, a subprocess smoke import)
        runs on a worker thread — the receive loop must never wait for
        it, and while speech is playing out the restart it may arm
        waits for the speaking to finish on the watcher's own thread."""
        if self._codewatch_busy:
            return
        self._codewatch_busy = True

        async def _run():
            try:
                await asyncio.to_thread(self._codewatch.check_after_turn)
            except Exception:
                pass
            finally:
                self._codewatch_busy = False

        asyncio.create_task(_run())

    # ── program restart (the restart_program tool) ──────────────────

    def _restart_program(self) -> str:
        """The user told her to restart her program ("restart your
        program", "restart yourself", "restart the app"): announce
        it, let the announcement finish speaking, then relaunch —
        riding the code watcher's machinery exactly: the announce
        goes through speak(), the shared restart arbiter is armed
        (so no second restart path can race this one), a waiter
        thread holds for the bounded speech drain, and the relaunch
        itself is _restart_after_self_edit (summary save, Popen of
        the same interpreter + script, exit). NOT confirm-gated:
        the user's direct command is the authorisation, and the
        announcement is the notice.

        Availability is checked BEFORE anything is armed or
        announced: with no session loop there is nothing to
        schedule the relaunch onto (_restart_after_self_edit would
        silently do nothing), and with no script file there is
        nothing to relaunch. Either reads as an honest refusal in
        her voice — never an announced restart that doesn't happen."""
        import os as _os
        script = _os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        if self._loop is None or not script or not _os.path.isfile(script):
            return ("I can't restart my program from here — there's "
                    "no running program for me to relaunch. Start me "
                    "again from the desktop shortcut or the terminal "
                    "instead.")
        if not codewatch_core.PROCESS_ARBITER.try_arm("restart_program"):
            return ("A restart is already on its way — give it a "
                    "moment and I'll be back.")
        self.ui.write_log("SYS: Program restart requested.")
        self.speak("Restarting my program now — back in a moment.")
        threading.Thread(target=self._wait_and_restart_program,
                         daemon=True, name="program-restart").start()
        return "Restarting my program now."

    def _wait_and_restart_program(self) -> None:
        """The code watcher's waiter, mirrored (core/codewatch.py):
        give speech up to the drain bound to finish, settle briefly,
        then relaunch. Runs on its own thread — the turn that asked
        for the restart is long over — and _restart_after_self_edit
        is built for off-loop callers: the watcher's waiter calls it
        the same way."""
        deadline = time.monotonic() + codewatch_core.DRAIN_TIMEOUT_S
        while time.monotonic() < deadline:
            if not self._is_speaking:
                break
            time.sleep(0.5)
        time.sleep(2.0)
        try:
            self._restart_after_self_edit()
        except Exception as e:
            print(f"[Restart] relaunch failed: {e}")

    def _restart_after_self_edit(self):
        """Relaunch this process on the watcher's word. Mirrors the
        shutdown path's lifecycle — save the session summary first —
        then ARIA's relaunch: same interpreter, same script, same
        arguments, then exit so the new process owns the machine.
        Called from the watcher's timer thread."""
        import os as _os
        import subprocess as _sp
        script = _os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        if not script or not _os.path.isfile(script):
            print("[SelfEdit] restart skipped — no script to relaunch")
            return

        async def _do_restart():
            try:
                await self._save_session_summary()
            except Exception:
                pass
            print("[SelfEdit] relaunching with updated code…")
            try:
                _sp.Popen([sys.executable, script] + sys.argv[1:])
            except Exception as e:
                print(f"[SelfEdit] relaunch failed: {e}")
                return
            _os._exit(0)

        if self._loop:
            asyncio.run_coroutine_threadsafe(_do_restart(), self._loop)

    # ── main-process watchdog (core/watchdog.py) ─────────────────────

    async def _run_watchdog_heartbeat(self) -> None:
        """Beat the "loop" heartbeat every 2 seconds, for the life of
        the process. Created once in run(), OUTSIDE the session
        TaskGroup: the beat must survive session rebuilds and
        reconnect backoffs, because those are exactly the moments a
        false wedge verdict would be most damaging. If this
        coroutine cannot run, the loop is wedged — which is the
        verdict the watchdog exists to reach."""
        while True:
            watchdog_core.beat("loop")
            await asyncio.sleep(2)

    def _restart_for_watchdog(self):
        """Relaunch after a wedged loop, called from the watchdog's
        supervisor thread. The self-edit restart's shape — spawn the
        replacement first, then exit — but deliberately NOT routed
        through the event loop and with no session summary: the loop
        is the thing that is wedged, so anything scheduled onto it
        would queue forever, and a summary it cannot produce is not
        worth a corpse. The watchdog's incident record, written
        before this is called, is the evidence instead."""
        # The shared restart guard (core/codewatch.py): if the code
        # watcher already armed a restart, this process is already
        # going away on that schedule — a second relaunch racing it
        # could leave two of her running. First armer wins.
        if not codewatch_core.PROCESS_ARBITER.try_arm("watchdog"):
            print("[Watchdog] restart suppressed — a restart is "
                  "already armed.")
            return
        import os as _os
        import subprocess as _sp
        script = _os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
        if not script or not _os.path.isfile(script):
            print("[Watchdog] restart requested — no script to relaunch")
            return
        print("[Watchdog] relaunching — a loop wedged past the threshold…")
        try:
            _sp.Popen([sys.executable, script] + sys.argv[1:])
        except Exception as e:
            print(f"[Watchdog] relaunch failed: {e}")
            return
        _os._exit(0)

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "ARIA").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "ARIA"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        try:
            card = consolidation_core.resume_card_text()
        except Exception:
            card = None
        if card:
            mem_str = (mem_str + "\n\n" + card) if mem_str else card
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        # Address form is a property of the language being spoken, so it is
        # stated as a principle rather than a two-language lookup — the model
        # already knows the respectful register of whatever language it is in.
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else 'ADDRESS: Call the user by the name you know for them '
                      'from your memory; until you know it, use the plain '
                      'direct second person of the language you are currently '
                      'speaking. No honorifics and no butler register — you are '
                      'his familiar, not his staff — and never a form from a '
                      'different language than the one you are speaking in this '
                      'sentence.')
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        # Everything the model is told about *itself* is derived here, not
        # written into prompt.txt: the name comes from config, the platform from
        # the host, the capability list from the registries that were just
        # discovered. Rename the assistant, add a plugin or move to another OS
        # and this follows without anyone editing a prompt.
        _all_decls = (TOOL_DECLARATIONS
                      + self._action_registry.get_tool_declarations()
                      + self._plugin_registry.get_tool_declarations()
                      + mcp_client_core.declarations())

        # An engine that cannot do something is not offered the tool for it.
        #
        # This is why capabilities are data rather than a pile of conditions: the
        # SAME filtering feeds `_describe_limits` two lines below, through
        # `_names`, so an engine with no eyes ends up with the model being TOLD
        # it cannot see. Without that it would be handed a tool it cannot serve,
        # call it, get nothing back, and describe a screen it never received.
        #
        # `self._engine_caps is None` is Gemini Live and skips all of it, so the
        # declaration list is byte-for-byte what it has always been.
        if self._engine_caps is not None:
            _caps = self._engine_caps
            _blocked = set()
            if not _caps.get("vision"):
                _blocked |= {"screen_process", "close_camera"}
            if not _caps.get("tools"):
                _blocked = {(d.get("name") if isinstance(d, dict)
                             else getattr(d, "name", "")) for d in _all_decls}
            if _blocked:
                _all_decls = [d for d in _all_decls
                              if (d.get("name") if isinstance(d, dict)
                                  else getattr(d, "name", "")) not in _blocked]
                print(f"[JARVIS] {len(_blocked)} tool(s) withheld — "
                      f"this engine cannot serve them")

        _names = {(d.get("name") if isinstance(d, dict) else getattr(d, "name", ""))
                  for d in _all_decls}
        sys_prompt = _render_prompt(sys_prompt, {
            "assistant_name": self._asst_name,
            "platform": f"{_platform.system()} {_platform.release()}".strip(),
            "capabilities": _describe_tools(_all_decls),
            "limits": _describe_limits(
                has_vision="screen_process" in _names,
                has_mic=True,
            ),
        })

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            # strip_meta drops this project's own `tier` / `brief` keys, which
            # exist for the local-engine tool budget and are not part of the
            # Gemini function-declaration schema — an unknown field there is a
            # validation error, not something the API ignores. A declaration
            # that carries neither comes back as the same object, so what
            # Gemini receives is unchanged from the previous release.
            tools=[{"function_declarations": strip_meta(_all_decls)}],
            # Asks the server to issue session-resumption handles. Enabled but
            # NOT yet used: no handle is captured from the resumption update and
            # none is passed back here, so a reconnect starts a fresh session
            # rather than continuing the old one. Left on because the handles
            # cost nothing and are the prerequisite for doing it properly.
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — JARVIS can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                # English only, pinned at the voice layer as well as in
                # [LANGUAGE]: the spoken language never follows the user's
                # message or a stored memory entry.
                language_code="en-US",
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )
        if self._enhanced_live:
            # Proactive audio: JARVIS stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            # (Affective dialog was dropped: gemini-3.1-flash-live does not
            #  support it, and it never reliably detected tone in practice.
            #  To restore it on a 2.5 native-audio model, add back:
            #  cfg["enable_affective_dialog"] = True )
            if get_proactive_audio_enabled():
                cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)

        if self._tuned_live:
            cfg.update(self._tuning_config())

        return types.LiveConnectConfig(**cfg)

    def _tuning_config(self) -> dict:
        """The optional knobs, kept apart so one bad field can be dropped wholesale.

        Every one of these is a preview-API field. If a future model release
        stops accepting any of them the connection fails at setup, so the run
        loop turns `_tuned_live` off and reconnects on the plain config rather
        than leaving the user with an assistant that will not start.
        """
        out: dict = {}

        # How long the server waits through a pause before deciding your turn is
        # over. This — not the size of the prompt — is what most of the delay
        # before a reply actually is, and the default has to suit everybody, so
        # it is necessarily cautious.
        turn = get_turn_tuning()
        if turn.get("enabled", True):
            detect = types.AutomaticActivityDetection(
                silence_duration_ms=turn["silence_ms"],
                prefix_padding_ms=turn["prefix_ms"],
            )
            if turn["end_sensitivity"] == "high":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_HIGH
            elif turn["end_sensitivity"] == "low":
                detect.end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW
            if turn["start_sensitivity"] == "high":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH
            elif turn["start_sensitivity"] == "low":
                detect.start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_LOW
            out["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=detect)

        # Screenshots and camera frames are tokenised at this resolution and then
        # stay in the session's context. 'medium' keeps on-screen text legible
        # for a fraction of a full-resolution frame.
        res = get_media_resolution()
        if res != "default":
            out["media_resolution"] = {
                "low":    types.MediaResolution.MEDIA_RESOLUTION_LOW,
                "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
                "high":   types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            }[res]

        # Thinking is left at the server default deliberately. Forcing the budget
        # to zero was measured on gemini-3.1-flash-live over interleaved trials
        # and did not make the first word arrive sooner — this model does not
        # appear to deliberate on the Live path, so pinning the field only adds a
        # way for a future release to behave differently. Set "thinking_enabled"
        # in config/api_keys.json to true to let it reason instead.
        if get_thinking_enabled():
            out["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)

        return out

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        """The one execution funnel — every tool call, from the
        Live session, the providers pipeline, a routine replay or
        a delegated task, passes through here (the paths below
        all call this method). So this is where the Fleet
        activity registry (core/fleet.py) hooks in: begin on
        entry, finish in a finally — the registry then holds what
        is genuinely in flight and what genuinely just ran,
        whatever the model later says about it. The registry
        never raises; a failure to record must never cost a tool
        its result, and an exception in the call still finishes
        the record (as a failure) on its way out."""
        token = fleet_core.begin(
            getattr(fc, "name", ""),
            fleet_core.detail_for(getattr(fc, "name", ""),
                                  getattr(fc, "args", None)))
        try:
            resp = await self._execute_tool_impl(fc)
        except Exception:
            fleet_core.finish(token, ok=False)
            raise
        result_text = ""
        try:
            payload = getattr(resp, "response", None) or {}
            result_text = str(payload.get("result", "") or "")
        except Exception:
            result_text = ""
        fleet_core.finish(token, ok=fleet_core.result_ok(result_text))
        return resp

    async def _execute_tool_impl(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")


        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "forget_memory":
                # Keyword delete over the same store recall reads — ARIA's
                # forget semantics (memory_forget_entries): key or value
                # contains the query, case-insensitive. Same local-scan
                # reasoning as recall above. The names of what went are the
                # answer; a no-match is a clean report, never an error.
                _q = (args.get("query") or "").strip()
                if not _q:
                    result = "Nothing to forget: empty query."
                else:
                    _n, _gone = forget_by_keyword(_q)
                    if _n:
                        result = (f"Forgot {_n} "
                                  f"{'memory' if _n == 1 else 'memories'} "
                                  f"matching '{_q}': {', '.join(_gone)}.")
                    else:
                        result = (f"Nothing stored matches '{_q}' — "
                                  f"nothing was forgotten.")

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(None, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    # The image is attached to this same exchange, so there is
                    # nothing to stall for and nothing to announce. Asking for an
                    # acknowledgement here is what produced two spoken answers —
                    # the model filled that turn by answering the question from
                    # imagination, then answered it again once it could see.
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured and attached to this "
                        f"same exchange. Do not acknowledge and do not answer yet — the image "
                        f"is arriving with this result. Reply once, from what you actually see "
                        f"in it."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif name == "restart_program":
                result = self._restart_program()

            elif self._action_registry.has(name):
                # file_processor: fall back to the currently-uploaded file when none is given
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {"player": self.ui, "speak": self.speak,
                        "response": None, "session_memory": None}
                r = await loop.run_in_executor(None, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: mirror results to the on-screen content panel
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    # unwrap: the untrusted markers are for the MODEL. Showing
                    # them to the person who asked for the search is the
                    # machinery leaking into the interface.
                    self.ui.show_content(_label, outcome.unwrap(r))

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        None,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                elif mcp_client_core.has_tool(name):
                    # MCP (core/mcp_client.py): a connected server's
                    # tool, namespaced mcp_<server>_<tool>. The call
                    # blocks on the server's own loop thread, so it
                    # runs on the executor like every other registry.
                    r = await loop.run_in_executor(
                        None,
                        lambda: mcp_client_core.run_tool(name, args)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")

        # Routine capture (Phase F1, core/routines.py): every executed call
        # funnels through this method — inline tools, actions and plugins,
        # for the Live session and the providers pipeline alike, since both
        # feed the receive loop that calls this — so this is the one place
        # a recording can see them all. After execution completes, as in
        # ARIA's dispatch hook; the recording module decides what counts
        # (the routine tools and save_memory are excluded there, and steps
        # executed BY a replay are never captured). Guarded: a capture
        # failure must never cost the user a tool result.
        try:
            routines_core.maybe_capture(name, args)
        except Exception:
            pass

        # A tool that declared itself NON_BLOCKING also says when its answer may
        # re-enter the conversation. Without this the model finishes whatever it
        # was saying and then reads the result out on top of it — which, for
        # something like a phone call already ringing, is exactly the noise the
        # non-blocking call was meant to avoid. Tools that declared nothing get
        # the API default and behave as they always have.
        _sched = (self._action_registry.scheduling(name)
                  or self._plugin_registry.scheduling(name))
        _extra = {"scheduling": _sched} if _sched else {}
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result},
            **_extra
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            # ── Wake-word gate ───────────────────────────────────────────────
            # While asleep, the mic audio NEVER goes to Gemini (nothing is
            # streamed, so JARVIS can't respond to speech not addressed to it and
            # nothing leaves the machine). Frames are instead handed to the local
            # detector, which runs its model in ITS OWN thread — the cost here is
            # only a queue push, so the audio path is never slowed. When wake word
            # is off (default) or we're awake, this is a single boolean check.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            # ── Barge-in ─────────────────────────────────────────────────────
            # While JARVIS talks the mic is not streamed, but it is still worth
            # listening to locally: if the user starts speaking, cut the answer
            # short the way a person would stop when interrupted.
            #
            # The whole difficulty is echo — on speakers the mic hears JARVIS.
            # So the test is not "is the mic loud" but "is the mic louder than
            # the echo of what we are playing right now", sustained long enough
            # that a cough or a keystroke cannot trigger it.
            if jarvis_speaking:
                # Nothing is streamed while JARVIS talks.
                #
                # Interrupting by voice used to live here: `EchoGuard` can pick a
                # user out from under our own echo, and `core/echo.py` still does
                # that for the tail below. Re-enabling is small — classify each
                # block here and call interrupt() after `required_blocks` of
                # agreement — but it depends on the listener's room, so it stays
                # out until it can be tried on real hardware.
                return

            # ── Echo tail ────────────────────────────────────────────────────
            # The speaking flag has dropped but the speakers have not finished.
            # Sending this to the model is how an assistant hears itself, decides
            # it was addressed, and answers its own last sentence. The microphone
            # stays OPEN — the guard only drops blocks that are our own voice, so
            # replying the instant it stops still works.
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        return
                    self._tail_until = 0.0      # a real voice ends the tail early
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            # ── Push-to-talk ─────────────────────────────────────────────────
            # When it is on the microphone is closed by default and the chord
            # opens it, which is the whole point: nothing leaves the machine
            # unless you are holding the key.
            if self._ptt_enabled and not self._ptt_held:
                return
            
            if not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _flush_pending_vision(self) -> bool:
        """Send a captured frame immediately after its tool response.

        The frame is already in hand by the time `screen_process` returns — the
        capture happened inside the tool call. The old flow still made the model
        speak a turn first and only injected the image on that turn's
        turn_complete, which cost a whole extra round trip AND produced two
        spoken answers: one improvised without the picture, then the real one.
        Sending it here means the model has the tool result and the image before
        it generates anything, so the user gets one answer, sooner.
        """
        if not (self._pending_vision and self.session):
            return False

        import base64 as _b64
        img_b, mime_t, question, angle = self._pending_vision
        self._pending_vision = None
        b64 = _b64.b64encode(img_b).decode("ascii")
        print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

        # Label the source. Without it the image arrives carrying nothing but
        # the user's own sentence, and a screenshot of this app — which has a
        # face in the middle of it — got read as a photo of the user. What the
        # label *means* is explained once, in the generated [SELF] block.
        src = ("[IMAGE SOURCE: WEBCAM]" if angle == "camera"
               else "[IMAGE SOURCE: SCREEN CAPTURE]")
        await self.session.send_client_content(
            turns={"role": "user", "parts": [
                {"inline_data": {"mime_type": mime_t, "data": b64}},
                {"text": f"{src}\n\n{question}"},
            ]},
            turn_complete=True,
        )

        if self._vision_cam_active:
            # Camera: stay busy until JARVIS has finished speaking the answer,
            # then close the preview.
            self._vision_cam_active    = False
            self._vision_close_pending = True
        else:
            self._vision_busy = False
        return True

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():
                    # Anything at all from the server proves the return path is
                    # alive, so the stall clock stops here rather than in any of
                    # the branches below — a tool call, a transcript or a bare
                    # keepalive all count.
                    self._clear_stall()

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            # A turn that involves a tool call passes through
                            # several turn_completes, and the API re-sends the
                            # tail of the transcript across them. Comparing only
                            # against the previous chunk missed that — once
                            # out_buf had been flushed and emptied, the repeat
                            # sailed straight back in, which logged the answer
                            # twice AND made the avatar mouth it twice.
                            if txt and not _is_repeat_chunk(txt, out_buf):
                                out_buf.append(txt)
                                # Hand the words to the mouth as they arrive, so
                                # the avatar can form the consonants the audio
                                # alone cannot show. Pure string work — it adds
                                # nothing measurable to the response path.
                                self._visemes.feed_text(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                self._visemes.reset()
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                # The user's spoken words for this turn
                                # (input_transcription of their mic audio —
                                # nothing else writes in_buf). A direct
                                # power command arms its action here;
                                # ARIA's own output can never reach this.
                                power_intent.note_user_text(full_in)
                                self._last_out_logged = ""   # new exchange
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            # Second line of defence: even if a repeat slips
                            # into a *fresh* buffer after a flush, never log the
                            # same answer (or a tail of it) twice in a row.
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                self._last_out_logged = full_out
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            if self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                            # Code watcher (core/codewatch.py): a turn
                            # that changed her own code ends here —
                            # validate it, announce it, restart or
                            # restore. See _maybe_check_codewatch.
                            self._maybe_check_codewatch()

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
                        await self._flush_pending_vision()
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[JARVIS] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        def _spk_fallback(_e):
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            # PRINT ONLY: the playback writer invokes this on its own thread,
            # and Qt calls from a non-GUI thread are unsafe. The HUD log line
            # is written after writer.start() returns — see fallback_exc.
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")

        # The stream lives on a dedicated writer thread (core/playback.py).
        # Writes used to go through the loop's default executor — the same
        # pool that carries essentially all tool execution — so the heavier
        # a session's tool use, the longer each write waited, the device
        # buffer starved, and her speech degraded into crackle that worsened
        # with use. The writer opens the stream on its own thread and heals
        # it there if a write fails or stalls; start() blocks until the open
        # succeeds, raising exactly as the inline open did (chosen device,
        # fallback to default, otherwise the error propagates).
        writer = PlaybackWriter(_open_spk, device=_spk_dev,
                                on_fallback=_spk_fallback)
        writer.start()
        if writer.fallback_exc is not None:
            # The chosen speaker failed to open and the default took over;
            # _spk_fallback already printed the console line on the writer
            # thread. The HUD line belongs here, on the loop thread.
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
        self._playback_writer = writer

        # Ask the device how far behind the speakers actually are, rather than
        # assuming. This is what the echo tail is sized from, so a machine with a
        # large audio buffer gets a correspondingly longer guard — and one with a
        # tiny buffer is not penalised with a delay it does not need.
        try:
            lat = writer.latency
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[JARVIS] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if writer.error is not None:
                        break   # output stream died for good (see writer)
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                if writer.error is not None:
                    break       # output stream died for good (see writer)

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Hand the batch to the writer thread FIRST. The analysis
                # below (viseme FFT, echo band energies) runs synchronously
                # on this loop and used to sit between the queue and the
                # write, adding jitter to when the device got fed. It may
                # still delay the mouth; it must never delay the speaker.
                writer.submit(bytes(batch))

                # Drive the HUD waveform and the avatar's mouth from JARVIS's
                # own voice. The batch is up to 200 ms long, so we hand over a
                # *schedule* of 20 ms viseme frames instead of a single averaged
                # level and let the HUD play it out in step with the audio.
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = _pcm_visemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    # When does this batch become audible? The stream was
                    # started at launch and its callback has been pulling
                    # silence ever since, so the first bytes of a reply reach
                    # the speaker about one callback period later — NOT one
                    # buffer later. `stream.latency` reports the buffer's
                    # capacity, which is how much can be queued ahead, and on
                    # Windows that is commonly 300-500 ms. Anchoring on it put
                    # the entire schedule a buffer late; that is the half second
                    # of lag, and it grew with whatever the device reported.
                    #
                    # After the anchor nothing needs measuring: the device
                    # consumes at exactly realtime, so each batch sounds one
                    # batch-duration after the one before it. The cursor is
                    # re-anchored only when it leaves the range physically
                    # possible — behind `now` means the device drained and this
                    # batch starts a fresh stretch of speech, while further
                    # ahead than the buffer can hold means it has drifted.
                    now = time.time()
                    horizon = cursor_horizon(
                        self._out_latency,
                        writer.queued_seconds(RECEIVE_SAMPLE_RATE),
                        slack_s=_CURSOR_SLACK)
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    # Advance by the batch's own duration whether or not it
                    # yielded frames, so a block too short to analyse cannot
                    # shift everything after it out of step with the audio.
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        frames = self._visemes.frames(frames, hop)
                        self.ui.push_visemes(frames, hop, at)
                        # Barge-in needs to know what we are playing, not just
                        # how loud: the guard subtracts this from the microphone.
                        self._out_level = max(f[0] for f in frames)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE,
                                               self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    pass

        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            if self._playback_writer is writer:
                self._playback_writer = None
            writer.close()   # stream stop/close happens on the writer thread

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        The greeting IS the briefing, composed in code — the model
        presents it, it does not improvise it.

        The composition is the morning_briefing tool's own
        (actions/morning_briefing.compose_startup_briefing): a
        time-aware salutation, today's and tomorrow's calendar, the
        inbox's notable mail, the weather, and headlines for the
        monitored topics — each section bounded, so a slow or dead
        source degrades to an honest note in its section instead of
        stalling or killing the greeting. The news list also goes
        to the UI content panel, as it did before.

        The composed text goes out as ONE [STARTUP_BRIEFING] turn
        instructing her to present it in full and add nothing: the
        opening is confined to the composed sections. What this
        replaced was a two-phase freelance — phase 1 told the model
        to "greet warmly, mention the time, and say you are
        fetching today's news", plus a clause riffing on the last
        session's summary — and the model filled the space with
        plausible invention, including promoting a niece mentioned
        in conversation into a definite plan for later that day.
        Plans are asserted now only when the calendar or the inbox
        actually carries them.

        Callers: the run loop at launch (once, gated on
        get_brief_enabled) and the scheduler's daily briefing task,
        via _scheduler_briefing (Phase G4) — one delivery path, so
        a scheduled briefing is composed and confined exactly like
        the greeting.

        Engine-agnostic: this talks to self.session only, and both
        engines (Gemini Live, the providers pipeline) implement the
        same send_client_content session interface.
        """
        await asyncio.sleep(0.3)
        if not self.session:
            return

        try:
            topics = await asyncio.to_thread(list_monitors)
        except Exception:
            topics = []

        text, panel = await _briefing.compose_startup_briefing(
            topics=topics, news_fetch=_fetch_news_sync)

        if panel:
            try:
                self.ui.show_content(panel[0], panel[1])
            except Exception:
                pass

        p = (
            "[STARTUP_BRIEFING] The briefing below was composed just now "
            "from the calendar, the inbox, the weather and the news. "
            "Present it as your greeting, in full, in your own natural "
            "voice — every fact in it and no facts beyond it: add "
            "nothing from memory or earlier conversation, and treat "
            "nothing as a plan unless it is written here. Do not call "
            f"any tools.\n\n{text}"
        )

        await self.session.send_client_content(
            turns={"role": "user", "parts": [{"text": p}]},
            turn_complete=True,
        )
        print("[JARVIS] Startup briefing sent (composed in code).")

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        # English always (see [LANGUAGE] in core/prompt.txt): a stored
        # identity language never steers what she writes.
        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            "Summarize this conversation in 1-2 sentences in English. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from core import gemini
            summary = await asyncio.to_thread(
                gemini.text, prompt, gemini.SMART, None, 30_000,
            )
            if summary:
                save_session_summary(summary, "English")
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session or not self._awake:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            if not self._presence_allows_speech():
                continue
            try:
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session and self._awake:
                # Don't interrupt if user spoke recently or JARVIS is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if (not speaking and not recent_speech
                        and self._presence_allows_speech()):
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                "Inform the user about this development naturally, "
                                "in English. One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"role": "user", "parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            print("[JARVIS] Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session or not self._awake:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._presence_allows_speech():
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"role": "user", "parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                print("[JARVIS] Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── In-app scheduler ─────────────────────────────────────────

    def _notify_broadcast(self, msg: dict) -> None:
        """core/notify's toast sink: schedule the dashboard broadcast
        on the session loop. notify() calls this from whatever
        thread noticed something — the scheduler's tick, a tool —
        so a call with no running loop in it hops through
        run_coroutine_threadsafe. No dashboard (it failed to start,
        or has not started yet): the toast is skipped — the push
        and the missed queue already carried the notification."""
        dash = getattr(self, "_dashboard", None)
        if dash is None:
            return
        try:
            asyncio.create_task(dash.broadcast(msg))
            return
        except RuntimeError:
            pass
        loop = getattr(self, "_loop", None)
        if loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    dash.broadcast(msg), loop)
            except RuntimeError:
                pass


    def _scheduler_say(self, text: str) -> bool:
        """Delivery hook for core/scheduler: announce an exact line
        (a timer finishing, a reminder, a watch alert). Returns False
        when there is no session to speak into, so the scheduler
        keeps the task due and retries on a later tick rather than
        consuming it silently.

        Awake-gated (Phase G4) — the failover notice's discipline,
        generalised to every scheduler announcement: while she
        sleeps (wake-word mode) the line is QUEUED rather than
        spoken into an empty room or dropped, and wake() announces
        the queue. Accepting a line for the queue returns True: the
        hook's background consumers (the price and screen passes)
        mark their events as they go, so answering False would lose
        the alert outright. Every announcement, spoken now or
        queued for later, also lands in the SYS log."""
        if not self.session:
            return False
        try:
            self.ui.write_log(f"SYS: {text}")
        except Exception:
            pass
        if not self._awake:
            self._pending_announce.append(text)
            # The queue speaks it when she wakes; the phone carries
            # it NOW (core/notify.py, bound in run()). A price
            # crossing arrives here too, seconds after the price
            # pass pushed it under its own title — notify()'s
            # same-body window folds the two into one event.
            hook = getattr(self, "_notify_hook", None)
            if hook is not None:
                try:
                    from memory.config_manager import get_assistant_name
                    hook(get_assistant_name(), text, source="scheduler")
                except Exception:
                    pass
            return True
        self.plugin_say(
            "Announce this to the user now, in one short sentence, "
            "in English: " + text)
        return True

    def _scheduler_run(self, prompt: str) -> bool:
        """Delivery hook for recurring tasks: the prompt arrives
        already framed as a scheduled-task turn."""
        if not self.session:
            return False
        self.plugin_say(prompt)
        return True

    def _scheduler_briefing(self) -> bool:
        """Delivery hook for the scheduler's daily briefing task
        (Phase G4): the composed briefing, delivered as a spoken
        turn through _send_startup_briefing — the startup
        greeting's own composition and delivery path, called
        directly, so the two can never diverge. Returns False when
        there is no session or she is
        asleep: the task stays due and retries on a later tick, so
        a scheduled briefing is never spoken into a sleeping room
        and never silently eaten either. Past acceptance it is
        fire-and-forget, exactly the say hook's honesty level — the
        composition's sections degrade individually rather than
        the delivery failing as a whole."""
        if not self.session or not self._awake:
            return False
        loop = getattr(self, "_loop", None)
        if loop is None:
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self._send_startup_briefing(), loop)
        except Exception as e:
            print(f"[Scheduler] ⚠️ briefing delivery failed: {e}")
            return False
        return True

    async def _run_scheduler(self) -> None:
        """Drive core/scheduler.tick on the session lifecycle — a
        sibling of _run_proactive_mode in the same TaskGroup, riding
        its lifecycle rather than running as a second unmanaged
        loop. The store work itself runs off the event loop."""
        while True:
            await asyncio.sleep(2)
            try:
                logs = await asyncio.to_thread(scheduler_core.tick)
                for line in logs:
                    print(f"[Scheduler] {line}")
            except Exception as e:
                print(f"[Scheduler] ⚠️ {e}")

    async def _run_consolidation_watch(self):
        """The consolidation pass's always-running driver
        (core/consolidation.py). scheduler.tick() only turns while
        a Live session is connected — at 03:00 with the engine
        down there would be no tick, and the night's run would
        wait for the next session to start. This loop is
        process-lifetime, like the watchdog heartbeat beside it: a
        first pass with startup semantics (the >20h catch-up),
        then a due-check every fifteen minutes. run_if_due is the
        gate; the ordinary answer is "not due" and costs one state
        read."""
        first = True
        while True:
            try:
                logs = await asyncio.to_thread(
                    consolidation_core.run_if_due, startup=first)
                for line in logs:
                    print(f"[Consolidation] {line}")
            except Exception:
                pass
            first = False
            await asyncio.sleep(900)

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    # A remote command is deliberate control and the phone user
                    # has no desktop WAKE button — so it wakes JARVIS if asleep.
                    if self._wake_enabled and not self._awake:
                        self.wake(reason="remote command")
                    # Text typed on the owner's paired phone is the
                    # user's own words — the third seam allowed to arm
                    # a computer power action (core/power_intent.py).
                    power_intent.note_user_text(text)
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    # ── Which engine holds the conversation ──────────────────────────────────
    # Everything in this section is inert on a default install: `_engine_mode()`
    # reads one key out of the config that is already loaded, gets "gemini", and
    # returns before the providers package is imported. Delete the folder and
    # the import below raises ImportError, which is caught and reported as
    # "providers are not installed" — the assistant then runs exactly as it did
    # before any of this existed.
    def _engine_mode(self) -> str:
        try:
            from memory.config_manager import get_provider_mode
            return get_provider_mode()
        except Exception:
            return "gemini"

    def _engine_info_for_ui(self) -> tuple:
        """(mode, model, failover_label) for the ENGINE status plaque
        (ui.py's engine_plaque_text, pulled once a second through the
        get_engine_info bridge). Read-only and total: the plaque is
        furniture, so any failure degrades to the plain Live default
        rather than reaching the window's clock tick. A latched
        failover reports its provider notice as the label — that is
        what makes the plaque render "PIPELINE (FO)" instead of the
        switch passing silently."""
        try:
            if getattr(self, "_failed_over", False):
                return ("pipeline", None,
                        getattr(self, "_failover_notice", None) or "pipeline")
            if self._engine_mode() == "pipeline":
                _ok, label = self._pipeline_failover_target()
                return ("pipeline", label, None)
            from core import gemini as _gemini
            return ("gemini", _gemini.live_model(), None)
        except Exception:
            return ("gemini", None, None)

    # ── Engine failover (Phase G2) ─────────────────────────────────────
    # Gemini Live is the default brain, and its own recovery is good:
    # the ladder steps down on quota, rejected handles are discarded,
    # dropped sockets reconnect with backoff. What it cannot survive is
    # EVERY rung resting at once, or a session that fails faster than
    # reconnects can land — those two states are the triggers. The
    # switch itself is not new machinery: the latch below makes
    # _resolve_engine_caps resolve the pipeline — the exact construction
    # the providers panel's manual selection uses — on the next pass of
    # the run loop, and _open_session builds the engine it describes.

    def _pipeline_failover_target(self) -> tuple[bool, str]:
        """(True, provider label) when the pipeline could take over —
        or (False, why-not).

        The validation is the startup path's own, verbatim: a language
        model chosen for the pipeline slot, and registry.missing_fields
        empty for it. A half-configured pipeline (provider named, model
        or key still missing) fails this exactly as it fails startup's
        resolution, and is treated as no provider at all."""
        try:
            from memory.config_manager import get_pipeline, get_provider_config
            from providers import registry

            pipe = get_pipeline()
            llm_id = str(pipe.get("llm") or "").strip()
            if not llm_id:
                return False, "no language model is chosen for the pipeline"
            cfg = get_provider_config(llm_id)
            missing = registry.missing_fields(llm_id, cfg)
            if missing:
                return False, f"{llm_id} needs: {', '.join(missing)}"
            # The spoken label: the catalog name the user picked
            # (Groq, OpenRouter…) when the config names one, else the
            # provider id — and the model either way, when there is one.
            name = llm_id
            catalog_id = str(cfg.get("catalog") or "").strip()
            if catalog_id:
                decl = registry.get_declaration(llm_id) or {}
                for entry in decl.get("catalog", ()):
                    if entry.get("id") == catalog_id:
                        name = entry.get("label") or llm_id
                        break
            model = str(cfg.get("model") or "").strip()
            return True, f"{name} ({model})" if model else name
        except Exception as e:
            return False, f"pipeline check failed: {e}"

    def _maybe_failover(self, reason: str) -> bool:
        """Latch this process onto the pipeline engine. True when the
        latch is set (or already was); the run loop's next pass builds
        the pipeline through _resolve_engine_caps, unchanged.

        Latched means latched: no switch-back mid-run. Live returns on
        the next launch, or when the user changes the engine in the
        panel (_on_engine_applied clears the latch)."""
        if getattr(self, "_failed_over", False):
            return True
        if getattr(self, "_failover_dead", False):
            return False
        if self._engine_mode() != "gemini":
            return False     # the pipeline already holds the conversation
        try:
            from memory.config_manager import get_engine_auto_failover
            if not get_engine_auto_failover():
                return False
        except Exception:
            pass             # an unreadable flag defaults to on, as elsewhere
        ready, label = self._pipeline_failover_target()
        if not ready:
            return False
        self._failed_over = True
        self._failover_notice = label
        print(f"[Engine] Gemini Live is unrecoverable ({reason}) — "
              f"failing over to {label} for the rest of this run.")
        self.ui.write_log(
            f"SYS: Gemini connection lost — switching to {label}.")
        try:
            from core import incidents
            incidents.record("engine", "engine_failover",
                             f"{reason} — switched to {label}")
        except Exception:
            pass
        return True

    def _note_failover_unavailable(self) -> None:
        """Live is down and failover cannot help — say why, once per
        outage episode (the flag resets when a session next comes up).

        The ordinary outage lines keep their own cadence; this is the
        one addition: the fix, named, once."""
        if getattr(self, "_failover_outage_noted", False):
            return
        self._failover_outage_noted = True
        try:
            from memory.config_manager import get_engine_auto_failover
            if not get_engine_auto_failover():
                self.ui.write_log(
                    "SYS: Automatic failover is switched off — set "
                    "\"engine_auto_failover\": true to enable it, or "
                    "pick the pipeline engine in ⚙ ENGINE & PROVIDERS.")
                return
        except Exception:
            pass
        self.ui.write_log(
            "SYS: No backup engine is configured — a provider key in "
            "⚙ ENGINE & PROVIDERS enables automatic failover.")

    def _note_live_failure(self) -> None:
        """One more unrecovered Live failure: feed the window trigger.

        Called from the run loop's generic error tail — the failures
        that arrive there are the ones none of the specific recoveries
        (resumption, ladder step-down, config drops) could absorb.
        Three inside _FAILOVER_WINDOW_SECONDS and the session is
        failing faster than reconnecting can fix; a lone transient
        drop just ages out of the window."""
        if getattr(self, "_failed_over", False):
            return
        if getattr(self, "_failover_dead", False):
            return
        if self._engine_caps is not None or self._engine_mode() != "gemini":
            return     # the failure was the pipeline's, or a manual choice
        now = time.monotonic()
        times = [t for t in getattr(self, "_live_failure_times", [])
                 if now - t < _FAILOVER_WINDOW_SECONDS]
        times.append(now)
        self._live_failure_times = times
        if len(times) >= _FAILOVER_FAILURE_LIMIT:
            reason = (f"{len(times)} Live session failures inside "
                      f"{int(_FAILOVER_WINDOW_SECONDS)} seconds")
            if self._maybe_failover(reason):
                self._live_failure_times = []
                self._conn_backoff = 0
            else:
                self._note_failover_unavailable()

    def _deliver_failover_notice(self) -> None:
        """Speak the failover notice — once, when a session is up and
        she is awake to be heard. Called as each session comes up and
        again on wake(), so a failover that lands while she sleeps is
        announced when she wakes instead of being lost, or shouted
        into an empty room. The label is cleared BEFORE speaking, so
        no delivery error can ever make it repeat.

        The words go through plugin_say, the announcement channel
        everything else uses (the scheduler's timers speak the same
        way): the engine phrases the sentence; the facts are fixed.
        A pipeline engine starts with an empty history — it is a
        fresh engine, exactly as a manual panel switch builds one —
        so the notice says the conversation restarted, because it has."""
        label = getattr(self, "_failover_notice", None)
        if not label or not self.session or not self._awake:
            return
        self._failover_notice = None
        self.plugin_say(
            "Announce this to the user now, in one short sentence, in "
            f"English: My Gemini connection is down, so I've "
            f"switched to {label}. The conversation has restarted.")

    def _flush_pending_announce(self) -> None:
        """Speak the announcements the scheduler queued while she
        slept — once each, in the order they arrived. The queue is
        swapped out BEFORE speaking, the failover notice's
        discipline, so no delivery error can ever repeat one.
        Called from wake() and as each session comes up (a queue
        can outlive the session under which it accumulated)."""
        queue = getattr(self, "_pending_announce", None)
        if not queue or not self.session or not self._awake:
            return
        self._pending_announce = []
        for text in queue:
            self.plugin_say(
                "Announce this to the user now, in one short "
                "sentence, in English: " + text)

    def _resolve_engine_caps(self):
        """What the chosen engine can do — or None for Gemini Live.

        A latched failover (see _maybe_failover) counts as "pipeline"
        here whatever the saved mode says: this method IS the manual
        selection's construction path, and failover deliberately goes
        through it unchanged rather than growing a second builder.

        Called BEFORE `_build_config()`, because the tool declarations depend on
        the answer. Building the providers here rather than later is deliberate:
        the registry caches them by configuration, so the objects made now are
        the same ones the engine gets a moment later, and a misconfiguration is
        reported while there is still a Gemini session to fall back to.
        """
        forced = getattr(self, "_failed_over", False)
        if self._engine_mode() != "pipeline" and not forced:
            return None
        try:
            from memory.config_manager import get_pipeline, get_provider_config
            from providers import registry

            pipe = get_pipeline()
            llm_id = str(pipe.get("llm") or "").strip()
            if not llm_id:
                raise RuntimeError("no language model is chosen")
            missing = registry.missing_fields(llm_id, get_provider_config(llm_id))
            if missing:
                raise RuntimeError(f"{llm_id} needs: {', '.join(missing)}")
            llm = registry.build(llm_id, get_provider_config(llm_id))

            # Ask the model whether it can see, once, and remember the answer.
            #
            # This decides whether `screen_process` is declared at all, so it
            # has to be settled BEFORE the config is built. Leaving it to the
            # panel's TEST button meant that anybody who never pressed it ran a
            # vision model with its eyes switched off — silently, and for every
            # session. Measured once per model, in about a second, then read
            # from the settings for ever after.
            from memory.config_manager import save_provider_config
            ensure = getattr(llm, "ensure_vision_known", None)
            if ensure is not None:
                try:
                    seen = ensure(lambda v, _id=llm_id: save_provider_config(_id, v))
                    self.ui.write_log(
                        f"SYS: {llm_id} "
                        + ("can see images." if seen else "is text only."))
                except Exception as e:
                    print(f"[JARVIS] vision check skipped: {e}")

            caps = dict(getattr(llm, "caps", {}) or {})
            caps["audio_out"] = bool(str(pipe.get("tts") or "").strip())
            return caps
        except Exception as e:
            # A local engine that will not start must never mean no assistant.
            # Falling back to Live is the whole reason the ladder exists, and
            # the reason is said out loud rather than left in a console.
            self.ui.write_log(f"SYS: local engine unavailable ({e}) — "
                              f"using Gemini Live.")
            print(f"[JARVIS] Pipeline engine unavailable: {e}")
            return None

    @asynccontextmanager
    async def _open_session(self, live_model: str, config):
        """The conversation, from whichever engine is configured.

        Both branches yield an object with the same four methods and the same
        event shapes, which is the entire contract — see providers/base.py. The
        Live branch is unchanged from what it has always been, down to the
        client being rebuilt on every reconnect.
        """
        if self._engine_caps is None:
            client = genai.Client(
                api_key=_get_api_key(),
                http_options={"api_version":
                              "v1alpha" if self._enhanced_live else "v1beta"},
            )
            async with client.aio.live.connect(model=live_model,
                                               config=config) as session:
                yield session
            return

        from memory.config_manager import get_pipeline, get_provider_config
        from providers import registry
        from providers.engine_pipeline import PipelineEngine

        pipe = get_pipeline()

        def part(slot):
            pid = str(pipe.get(slot) or "").strip()
            if not pid:
                return None
            # `log` so a provider can say what it is doing — downloading a
            # voice, loading a model — in the activity log rather than only on a
            # console the user may not be watching.
            cfg = dict(get_provider_config(pid))
            cfg["log"] = self.ui.write_log
            return registry.build(pid, cfg)

        engine = PipelineEngine(part("llm"), part("stt"), part("tts"),
                                config=config, log=self.ui.write_log)
        # Without push-to-talk nothing else knows when a sentence ended, so the
        # engine finds it in the audio. With push-to-talk the key release is the
        # signal and the detector stays out of the way.
        engine.auto_turn = not self._ptt_enabled
        self._engine = engine
        try:
            yield engine
        finally:
            self._engine = None
            await engine.close()

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Semantic-recall backfill (Phase G1): queue embeddings for
        # any stored facts lacking a fresh vector. Fire-and-forget —
        # a daemon worker drains the queue off the session path, and
        # recall is lexical until vectors land (permanently, if the
        # embedding API never answers; that is the designed floor).
        try:
            from memory import embeddings as _memory_embeddings
            _memory_embeddings.kick_backfill()
        except Exception:
            pass

        # Routines (Phase F1) replay through the same funnel live calls
        # use, bound here with the other shared services: the executor
        # routes each step back through _execute_tool, and pre-flight
        # reads the live tool names from the three registries.
        routines_core.bind_executor(self._execute_routine_step,
                                    self._all_tool_names)

        # Scheduler (Phase F-REST): timers, recurring tasks and the
        # background passes (screen watches, price checks) fire
        # through the same session speech as everything else; the
        # hooks report delivery, so a task that comes due while no
        # session is up stays due and retries instead of being
        # eaten. Bound here with the other shared services.
        scheduler_core.bind(say=self._scheduler_say,
                            run_prompt=self._scheduler_run,
                            briefing=self._scheduler_briefing)

        # Presence (v1): build + register the monitor now so the
        # scheduler/screenwatch choke points can consult it from
        # the first tick; the 5s driver task rides the session
        # TaskGroup beside _run_sleep_watch.
        self._start_presence()

        # Phone notifications (core/notify.py): the say-hook's
        # asleep branch pushes through notify_core.notify, and
        # notify's live-toast sink is the dashboard's broadcast —
        # bound here with the other shared services. The toast half
        # tolerates the dashboard not existing yet (it is created
        # below and may fail to start): the push and missed-queue
        # sinks do not depend on it.
        self._notify_hook = notify_core.notify
        notify_core.bind_broadcaster(self._notify_broadcast)

        # Main-process watchdog (core/watchdog.py): supervises the
        # asyncio loop (heartbeat coroutine below) and the Qt loop
        # (the window's clock tick beats from ui.py). A loop silent
        # past the threshold is restarted through
        # _restart_for_watchdog, at most three times an hour — the
        # incident record is the evidence. Off only by explicit
        # config ("watchdog_enabled": false), for a human who wants
        # to watch a stall happen instead of being restarted out of
        # it. Arming is guarded like every startup service here: a
        # watchdog that cannot start must never cost the assistant.
        try:
            if get_watchdog_enabled():
                watchdog_core.arm(restart=self._restart_for_watchdog)
                asyncio.create_task(self._run_watchdog_heartbeat())
        except Exception as e:
            print(f"[Watchdog] ⚠️ not armed: {e}")

        # Memory consolidation driver (core/consolidation.py): a
        # process-lifetime due-check loop, because scheduler.tick
        # only turns while a session is live and the nightly run is
        # at 03:00. Guarded like the watchdog above it: a driver
        # that cannot start must never cost the assistant.
        try:
            asyncio.create_task(self._run_consolidation_watch())
        except Exception as e:
            print(f"[Consolidation] ⚠️ driver not started: {e}")

        # Code watcher boot record (core/codewatch.py): the watcher
        # was built at startup — its boot snapshot is her source tree
        # as this process found it — and here, where the retired
        # updater sidecar's boot stamp used to be written, run()
        # stamps the boot into the watcher's ledger: the record
        # update_status reads back. Guarded like the watchdog: a
        # broken ledger must never cost her startup.
        try:
            self._codewatch.mark_boot()
            self.ui.write_log(
                "SYS: Code watcher running — my code is checked "
                "after every turn; changes announce themselves "
                "before I restart.")
        except Exception as e:
            print(f"[CodeWatch] ⚠️ boot record failed: {e}")

        # Delegated background tasks (core/delegation.py): prompts
        # the model works on without the conversation, through the
        # same seam as everything else — the model turn over
        # gemini's one-shot ladder, tool calls back through
        # _execute_tool itself. Bound here so the store's stale
        # records from any previous run are closed out at startup.
        delegation_core.bind(
            call_model=self._delegated_model_turn,
            dispatch=self._execute_delegated_step,
            tool_declarations=self._delegated_tool_declarations)
        consolidation_core.bind(propose=self._consolidation_propose)

        # Content panel tabs (UI TABS): the Inbox and Calendar tabs
        # pull from the same Gmail / iCal sources the tools speak
        # from, composed by core/panel_data off the Qt thread — the
        # tab and the spoken answer can never disagree. The Fleet
        # tab renders core/fleet's snapshot of live runtime state
        # (no fetch at all — registry reads, on the panel's tick).
        from core import panel_data as _panel_data
        self.ui.set_content_tab_providers(
            inbox_fetcher=_panel_data.fetch_inbox,
            calendar_fetcher=_panel_data.fetch_calendar_text,
            fleet_fetcher=fleet_core.render_text)

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[JARVIS] Connecting...")
                self.ui.set_state("THINKING")
                # Pick the rung to open the conversation on. A model resting
                # off a quota limit is skipped; the name is published back to
                # LIVE_MODEL so plugins follow whatever is actually in use.
                global LIVE_MODEL
                LIVE_MODEL = _gemini.live_model()
                live_model = LIVE_MODEL
                print(f"[JARVIS] Live model: {live_model}")

                _resumed_with = self._resume_handle is not None

                # Decided BEFORE the config is built, because the tool
                # declarations depend on what the engine can serve. On a default
                # install this returns None in a few microseconds and nothing
                # below behaves any differently.
                self._engine_caps = self._resolve_engine_caps()
                if getattr(self, "_failed_over", False) \
                        and self._engine_caps is None:
                    # Latched, but the pipeline would not build (the
                    # reason is in _resolve_engine_caps' own log line).
                    # Say so, write it down, and never try again this
                    # run: the latch clears, _failover_dead remembers,
                    # and Live's ordinary retries carry on below — the
                    # assistant is exactly as well off as it was without
                    # failover, and there is no second loop to flap in.
                    self._failed_over     = False
                    self._failover_dead   = True
                    self._failover_notice = None
                    print("[Engine] Failover target would not build — "
                          "staying on Gemini Live.")
                    self.ui.write_log(
                        "SYS: The backup engine would not start — "
                        "staying with Gemini.")
                    try:
                        from core import incidents
                        incidents.record(
                            "engine", "engine_failover_failed",
                            "pipeline would not build at failover time")
                    except Exception:
                        pass
                config = self._build_config()

                # The conversation itself. `_open_session` gives back a Gemini
                # Live session or a local pipeline wearing the same interface —
                # the Live branch still builds a fresh client on every reconnect
                # to avoid stale HTTP state, and still asks for v1alpha when
                # proactive audio is on.
                async with (
                    self._open_session(live_model, config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    # Every user turn arms the stall watchdog. Wrapping the
                    # session once covers all eleven send sites — and every one
                    # added later — where instrumenting them by hand would work
                    # until somebody adds the twelfth and it silently is not
                    # covered. The wrapper is per-session, so it goes away with
                    # the session it belongs to.
                    _orig_send = session.send_client_content

                    async def _send_and_arm(*a, **kw):
                        result = await _orig_send(*a, **kw)
                        self._arm_stall()
                        return result

                    session.send_client_content = _send_and_arm

                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()
                    self._rx_deadline     = None

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[JARVIS] Connected.")
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")

                    # Wake word: if enabled, come up ASLEEP (mic gated, silent)
                    # until the user says "Hey Jarvis" or taps wake in the UI.
                    if self._wake_enabled:
                        self._ensure_wake_detector()
                        self._awake = False
                        self.ui.set_state("SLEEPING")
                        self.ui.write_log(f"SYS: {self._asst_name} online — sleeping. Say 'Hey Jarvis' to wake me.")
                    else:
                        self._awake = True
                        self.ui.set_state("LISTENING")
                        self.ui.write_log(f"SYS: {self._asst_name} online.")

                    # A session is up: the outage episode (if any) is
                    # over, and a failover notice still owed is spoken
                    # now — it waits for _awake, and wake() retries
                    # delivery for one that lands while she sleeps.
                    self._failover_outage_noted = False
                    self._deliver_failover_notice()
                    self._flush_pending_announce()

                    # What this machine will not permit, said once, on screen.
                    #
                    # A Wayland session or a macOS without Accessibility cannot
                    # be typed into, clicked or captured — and the APIs report
                    # success regardless. Telling the user at startup turns
                    # "the assistant is broken" into one sentence they can act
                    # on, and it is said once per session rather than on every
                    # attempt.
                    if not self._platform_notes_shown:
                        self._platform_notes_shown = True
                        try:
                            from core import platform_support
                            for _line in platform_support.describe_for_user(
                                    platform_support.detect()):
                                self.ui.write_log(_line)
                        except Exception as _e:
                            print(f"[Platform] capability notes unavailable: {_e}")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._watch_stall())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_scheduler())
                    tg.create_task(self._run_sleep_watch())
                    tg.create_task(self._run_presence_watch())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Bring a local engine's speech model up before anybody
                    # speaks. Measured: without it the FIRST sentence waits 8.9
                    # seconds and every one after it 1.2 — and eight seconds of
                    # nothing is how a working assistant gets reported broken.
                    if self._engine is not None:
                        tg.create_task(self._engine.warmup())

                    # Morning briefing — fires once per process launch (if enabled).
                    # Skipped in wake-word mode: it comes up asleep, and a briefing
                    # would mean talking while "asleep".
                    if not self._briefing_sent and get_brief_enabled() and self._awake:
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[JARVIS] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[JARVIS] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Out of quota, or this model is not available to this key —
                # step down the ladder and reconnect straight away. This is the
                # difference between "JARVIS is quieter today" and "JARVIS does
                # not start today": one model means one daily limit, and the
                # limit always arrives mid-conversation.
                if _gemini.note_live_failure(live_model, err_str):
                    # Every rung resting at once is the ladder
                    # exhausted — the failover trigger (Phase G2). It
                    # is asked directly, via all_live_resting(), rather
                    # than inferred from nxt == live_model below: the
                    # two coincide (a just-resting rung is only handed
                    # back by live_model() once all are resting), but
                    # the trigger should say what it means.
                    if _gemini.all_live_resting():
                        if self._maybe_failover(
                                "every Live model is out of quota or "
                                "unavailable to this key"):
                            self._conn_backoff = 0
                            continue
                        self.ui.write_log(
                            "SYS: Every live model is rate-limited — "
                            "retrying.")
                        self._note_failover_unavailable()
                        self._conn_backoff = 15
                        await asyncio.sleep(self._conn_backoff)
                        continue
                    nxt = _gemini.live_model()
                    self.ui.write_log(
                        f"SYS: Switching to {nxt.split('/')[-1]} — the previous "
                        f"model is out of quota.")
                    self._conn_backoff = 0
                    continue

                # Turn-taking / media / thinking knobs rejected by the server
                # (preview API drift) — drop them first, because they are the
                # newest fields and the cheapest to lose. Proactive audio is
                # tried again on the next pass if the error persists.
                if self._tuned_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                    or "realtime_input" in err_str.lower()
                    or "media_resolution" in err_str.lower()
                    or "thinking" in err_str.lower()
                ):
                    self._tuned_live = False
                    print("[JARVIS] Live tuning rejected — reconnecting without it.")
                    continue

                # Proactive audio rejected by the server (preview API drift) —
                # drop it and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Proactive audio unavailable — reconnecting without it."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration
                if "API key not valid" in err_str or "1007" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Connection failed — retrying in {_conn_backoff}s. "
                        "(a VPN may be required)"
                    )
                else:
                    self._conn_backoff = 3

                # Whatever shape the failure had, it reached the bottom
                # of this handler unrecovered. Feed the failover window
                # — a no-op unless it was a Live session that died.
                self._note_live_failure()
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[JARVIS] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = JarvisUI("face.png")

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()