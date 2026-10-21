"""
core/panel_data.py — what the content panel's data tabs show.

The desktop content panel (ui.py's ContentTabs) has three tabs:
News, Inbox, Calendar. News is pushed (show_content); Inbox and
Calendar are pulled — the panel asks for their data when a tab
is first shown, and again on Refresh. This module composes that
data from the same sources the tools use, so a tab and the spoken
answer can never disagree about what the inbox or the calendar
holds:

  * Inbox — core/gmail.py's list_summaries (the reader behind
    read_email and triage_email), newest first, with the triage
    classifier's IMPORTANT / FYI labels attached where a verdict
    can be tied to a specific message.
  * Calendar — core/ical.py's fetch+parse+window (the path behind
    check_calendar), rendered with ical.format_event.

Both fetches BLOCK on the network; the panel always calls them
on a worker thread. Every state is honest, in Phase E's language:
not connected says so with the setup phrase, a failed fetch says
so with the reason, and a classifier that does not answer
degrades the inbox to an unlabelled list with a note — never to
invented labels.

The Inbox tab renders RICH (fetch_inbox): one block per message —
sender and time on a table row, subject under it, triage verdicts
as coloured chips, messages grouped under day headers — because
the flat pipe-separated text read as a wall. fetch_inbox_text
keeps the plain rendering of the same fetch for any surface that
wants text. Both renderings, and the header's unseen count, come
out of ONE _inbox_data() call, so they cannot disagree either.
Mail text is untrusted input: everything rendered into HTML is
escaped first, so a subject containing markup shows as literal
characters, never as formatting.
"""

from __future__ import annotations

import html as _html
import re
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from core import gmail, ical
from memory import config_manager

INBOX_LIMIT = 15      # the tab is a glance, not a mail client
CALENDAR_DAYS = 7     # "upcoming" on the tab: the week ahead

# Injectable classifier seam — tests replace this; production
# leaves it None and the triage tool's classifier answers, so
# the tab and triage_email share one brief and one verdict
# source and can never classify the same inbox two ways.
_classify = None


def _classify_default(system: str, user_text: str) -> str:
    from actions import triage_email
    fn = triage_email._classify or triage_email._classify_default
    return fn(system, user_text)


# Rendering palette — mirrors ui.py's C defaults. The duplicates
# are deliberate layering: this module is core and does not import
# the Qt layer for hex strings. The accent is the palette's fixed
# status amber (a re-theme never moves the status colours); the
# blues are the panel's own, applied at render time like every
# other stylesheet in the panel.
_P = {
    "text":   "#8ffcff",   # ui.C.TEXT
    "white":  "#d8f8ff",   # ui.C.WHITE
    "dim":    "#3a8a9a",   # ui.C.TEXT_DIM
    "med":    "#5ab8cc",   # ui.C.TEXT_MED
    "bar":    "#0d3347",   # ui.C.BORDER — day-header band
    "chip":   "#0f4060",   # ui.C.BORDER_A — FYI chip ground
    "accent": "#ffcc00",   # ui.C.ACC2 — IMPORTANT chip, unread dot
    "ink":    "#000d14",   # ui.C.DARK — text on the accent chip
}


# ── inbox ────────────────────────────────────────────────────────────────

def _reason(err) -> str:
    """The human reason out of a model-shaped outcome string."""
    m = re.search(r'reason="([^"]*)"', str(err or ""))
    if m:
        return m.group(1)
    m = re.search(r"reason=(\S+)", str(err or ""))
    return m.group(1) if m else "Gmail did not answer."


_STOP = {"the", "and", "for", "your", "you", "are", "with",
         "from", "this", "that", "into", "has", "have"}


def _tokens(text: str) -> set:
    """Distinctive words only — short and function words match
    everything and would tie verdicts to the wrong message."""
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) >= 4 and w not in _STOP}


def _parse_verdicts(answer) -> list:
    """[(label, line)] for the IMPORTANT / FYI lines, in order.
    The NOISE line carries only a count, so noise messages stay
    unlabelled — which specific ones they are is not knowable
    from the verdict, and guessing would mislabel the rest."""
    out = []
    for raw in str(answer or "").splitlines():
        line = raw.strip()
        if line.upper().startswith("IMPORTANT"):
            out.append(("IMPORTANT", line))
        elif line.upper().startswith("FYI"):
            out.append(("FYI", line))
    return out


def _labels_for(entries) -> dict:
    """{uid: label} — best-effort. A verdict line is tied to the
    one entry whose sender/subject shares the most distinctive
    tokens with it; a line that matches nothing is dropped, and
    an entry matched by nothing stays unlabelled. A missing
    label is a smaller lie than a wrong one."""
    from actions import triage_email
    user_text = ("Triage these email summaries (newest first):\n\n"
                 + gmail.summaries_text(entries))
    classify = _classify or _classify_default
    answer = classify(triage_email._TRIAGE_SYSTEM, user_text)
    if not answer or not str(answer).strip():
        raise ValueError("the classifier gave no answer")
    entry_tokens = [
        (e, _tokens(e.get("from", "")) | _tokens(e.get("subject", "")))
        for e in entries
    ]
    labels, used = {}, set()
    for label, line in _parse_verdicts(answer):
        lt = _tokens(line)
        best, best_score = None, 0
        for e, et in entry_tokens:
            if e["uid"] in used:
                continue
            score = len(lt & et)
            if score > best_score:
                best, best_score = e, score
        if best is not None and best_score >= 1:
            labels[best["uid"]] = label
            used.add(best["uid"])
    return labels


def _inbox_data(limit: int, with_count: bool = False) -> dict:
    """The one inbox fetch behind both renderings and the header.

    Returns a dict with a "state" — "unconfigured", "error"
    (with "reason"), "empty", or "ok" (with "entries", "labels",
    "note"). "unread" is the server's unseen count when it was
    genuinely counted (only asked for with_count=True, and only
    once the list itself read cleanly) and None otherwise: an
    uncounted number is unknown, never zero."""
    if not gmail.connected():
        return {"state": "unconfigured", "unread": None}
    entries, err = gmail.list_summaries(limit=limit, action="inbox_tab")
    if err:
        return {"state": "error", "reason": _reason(err),
                "unread": None}
    unread = None
    if with_count:
        count, _count_err = gmail.unread_count(action="inbox_tab")
        if count is not None:
            unread = count
    if not entries:
        return {"state": "empty", "unread": unread}
    note = ""
    try:
        labels = _labels_for(entries)
    except Exception:
        labels = {}
        note = ("(Triage labels unavailable — the classifier "
                "did not answer, so this list is unlabelled.)")
    return {"state": "ok", "entries": entries, "labels": labels,
            "note": note, "unread": unread}


def _inbox_text_from(d: dict) -> str:
    """The plain rendering of an _inbox_data() result — the
    original Inbox tab text, one pipe-separated line per message."""
    if d["state"] == "unconfigured":
        return ("Gmail isn't connected yet.\n\n"
                "Say 'set up Gmail' and give ARIA your Gmail address "
                "and an app password to connect it.")
    if d["state"] == "error":
        return ("Couldn't read your Gmail just now.\n\n"
                f"{d['reason']}\n\nUse Refresh to try again.")
    if d["state"] == "empty":
        return "Inbox is empty — nothing to show."
    entries, labels = d["entries"], d["labels"]
    lines = [f"{len(entries)} most recent emails, newest first:", ""]
    for e in entries:
        tag = f"[{labels[e['uid']]}] " if e["uid"] in labels else ""
        lines.append(f"{tag}{e['date']} | {e['from']} | "
                     f"\"{e['subject']}\"")
    text = "\n".join(lines)
    if d["note"]:
        text += "\n\n" + d["note"]
    return text


def fetch_inbox_text(limit: int = INBOX_LIMIT) -> str:
    """The Inbox tab's plain text: newest first, triage-labelled
    where the classifier's verdicts can be tied to a message."""
    return _inbox_text_from(_inbox_data(limit))


# ── inbox, rendered rich ─────────────────────────────────────────────────

def _esc(s) -> str:
    """Escape untrusted mail text for HTML — a subject that
    contains markup must arrive as literal characters."""
    return _html.escape(str(s or ""), quote=True)


def _entry_moment(e) -> datetime | None:
    """The entry's moment, local — or None when its date string
    does not parse (the caller falls back to the raw string)."""
    try:
        dt = parsedate_to_datetime(e.get("date") or "")
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.astimezone() if dt.tzinfo is not None else dt


def _clock(dt: datetime) -> str:
    """'9:41 AM' — built by hand: strftime's %-I is not portable
    to the Windows this app lives on."""
    return (f"{dt.hour % 12 or 12}:{dt.minute:02d} "
            f"{'AM' if dt.hour < 12 else 'PM'}")


def _day_label(day, today) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%a} {day.day} {day:%b}"


def _group_entries(entries) -> list:
    """[(label, [(entry, moment), ...])] — entries arrive newest
    first, so grouping consecutive same-day runs keeps the order;
    a date that does not parse groups under its own dateline."""
    today = datetime.now().date()
    groups = []
    for e in entries:
        moment = _entry_moment(e)
        if moment is not None:
            key = ("day", moment.date())
            label = _day_label(moment.date(), today)
        else:
            label = re.sub(r"\s+\d{1,2}:\d{2}.*$", "",
                           e.get("date") or "").strip() or "Unknown date"
            key = ("raw", label)
        if groups and groups[-1][0] == key:
            groups[-1][2].append((e, moment))
        else:
            groups.append((key, label, [(e, moment)]))
    return [(label, items) for _key, label, items in groups]


def _message_html(e, moment, label) -> str:
    """One message block: sender (bold) and time on a table row —
    the subset of HTML Qt paints reliably — subject under it,
    the triage verdict as a coloured chip, and unread mail marked
    with an accent dot and a brighter, bolder subject. Every
    mail-supplied string passes through _esc first."""
    unread = not e.get("seen", True)
    when = _clock(moment) if moment is not None else (e.get("date") or "")
    dot = (f'<span style="color:{_P["accent"]};">●</span> '
           if unread else "")
    sender_c = _P["white"] if unread else _P["text"]
    subject = _esc(e.get("subject"))
    if unread:
        subject = (f'<b><span style="color:{_P["white"]};">'
                   f'{subject}</span></b>')
    else:
        subject = f'<span style="color:{_P["text"]};">{subject}</span>'
    chip = ""
    if label == "IMPORTANT":
        chip = (f'<span style="background-color:{_P["accent"]}; '
                f'color:{_P["ink"]}; font-weight:bold;">'
                f'&nbsp;IMPORTANT&nbsp;</span> ')
    elif label == "FYI":
        chip = (f'<span style="background-color:{_P["chip"]}; '
                f'color:{_P["med"]};">&nbsp;FYI&nbsp;</span> ')
    return (
        '<table width="100%" cellspacing="0" cellpadding="0">'
        f'<tr><td>{dot}<b><span style="color:{sender_c};">'
        f'{_esc(e.get("from"))}</span></b></td>'
        f'<td align="right"><span style="color:{_P["dim"]};">'
        f'{_esc(when)}</span></td></tr>'
        f'<tr><td colspan="2">{chip}{subject}</td></tr>'
        '</table>')


def _state_html(heading: str, body_html: str = "") -> str:
    """The honest states as one quiet block in the same style —
    a heading line, then the explanation dimmed — not raw text.
    The heading is ours (escaped without quote mangling, so its
    apostrophes stay readable); body_html arrives pre-escaped
    by the caller wherever it carries outside text."""
    parts = [f'<div style="color:{_P["white"]};">'
             f'{_html.escape(heading, quote=False)}</div>']
    if body_html:
        parts.append(f'<div style="color:{_P["dim"]}; margin-top:6px;">'
                     f'{body_html}</div>')
    return "".join(parts)


def _inbox_html(d: dict) -> str:
    if d["state"] == "unconfigured":
        return _state_html(
            "Gmail isn't connected yet.",
            "Say 'set up Gmail' and give ARIA your Gmail address "
            "and an app password to connect it.")
    if d["state"] == "error":
        return _state_html(
            "Couldn't read your Gmail just now.",
            f"{_esc(d['reason'])}<br>Use Refresh to try again.")
    if d["state"] == "empty":
        return _state_html("Inbox is empty — nothing to show.")
    entries, labels = d["entries"], d["labels"]
    parts = [f'<div style="color:{_P["dim"]};">'
             f'{len(entries)} most recent emails, newest first:</div>']
    for label, items in _group_entries(entries):
        parts.append(
            f'<div style="background-color:{_P["bar"]}; '
            f'color:{_P["med"]}; font-weight:bold; padding:2px 5px; '
            f'margin-top:9px; margin-bottom:5px;">{_esc(label)}</div>')
        for i, (e, moment) in enumerate(items):
            if i:
                parts.append("<hr>")
            parts.append(_message_html(e, moment, labels.get(e["uid"])))
    if d["note"]:
        parts.append(f'<div style="color:{_P["dim"]}; margin-top:8px;">'
                     f'{_esc(d["note"])}</div>')
    return "".join(parts)


def fetch_inbox(limit: int = INBOX_LIMIT) -> dict:
    """The Inbox tab's rich result: {"text", "html", "unread"}.

    "text" is the plain rendering of the same fetch (identical to
    fetch_inbox_text), "html" the block rendering the tab paints,
    and "unread" the server's unseen count — or None whenever it
    was not genuinely counted, which is how the panel header
    knows to show the bare "Gmail Inbox" title."""
    d = _inbox_data(limit, with_count=True)
    return {"text": _inbox_text_from(d), "html": _inbox_html(d),
            "unread": d["unread"]}


# ── calendar ─────────────────────────────────────────────────────────────

def fetch_calendar_text(days: int = CALENDAR_DAYS) -> str:
    """The Calendar tab's text: the week ahead, chronological,
    in check_calendar's own line format."""
    url = config_manager.get_ical_url()
    if not url:
        return ("Calendar isn't connected yet.\n\n"
                "Say 'set up my calendar' with the secret iCal URL "
                "from Google Calendar settings to connect it.")
    try:
        events = ical.upcoming_from_feed(url, days=days)
    except Exception as e:
        return (f"Couldn't load your calendar just now.\n\n{e}\n\n"
                "Use Refresh to try again.")
    if not events:
        return f"Nothing on the calendar for the next {days} days."
    lines = [f"Coming up in the next {days} days:", ""]
    lines += [ical.format_event(e) for e in events]
    return "\n".join(lines)
