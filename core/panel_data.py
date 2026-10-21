"""
core/panel_data.py — what the content panel's data tabs show.

The desktop content panel (ui.py's ContentTabs) has three tabs:
News, Inbox, Calendar. News is pushed (show_content); Inbox and
Calendar are pulled — the panel asks for their text when a tab
is first shown, and again on Refresh. This module composes that
text from the same sources the tools use, so a tab and the spoken
answer can never disagree about what the inbox or the calendar
holds:

  * Inbox — core/gmail.py's list_summaries (the reader behind
    read_email and triage_email), newest first, with the triage
    classifier's IMPORTANT / FYI labels attached where a verdict
    can be tied to a specific message.
  * Calendar — core/ical.py's fetch+parse+window (the path behind
    check_calendar), rendered with ical.format_event.

Both functions BLOCK on the network and return finished plain
text; the panel always calls them on a worker thread. Every
state is honest, in Phase E's language: not connected says so
with the setup phrase, a failed fetch says so with the reason,
and a classifier that does not answer degrades the inbox to an
unlabelled list with a note — never to invented labels.
"""

from __future__ import annotations

import re

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


def fetch_inbox_text(limit: int = INBOX_LIMIT) -> str:
    """The Inbox tab's text: newest first, triage-labelled where
    the classifier's verdicts can be tied to a message."""
    if not gmail.connected():
        return ("Gmail isn't connected yet.\n\n"
                "Say 'set up Gmail' and give ARIA your Gmail address "
                "and an app password to connect it.")
    entries, err = gmail.list_summaries(limit=limit, action="inbox_tab")
    if err:
        return ("Couldn't read your Gmail just now.\n\n"
                f"{_reason(err)}\n\nUse Refresh to try again.")
    if not entries:
        return "Inbox is empty — nothing to show."
    note = ""
    try:
        labels = _labels_for(entries)
    except Exception:
        labels = {}
        note = ("\n\n(Triage labels unavailable — the classifier "
                "did not answer, so this list is unlabelled.)")
    lines = [f"{len(entries)} most recent emails, newest first:", ""]
    for e in entries:
        tag = f"[{labels[e['uid']]}] " if e["uid"] in labels else ""
        lines.append(f"{tag}{e['date']} | {e['from']} | "
                     f"\"{e['subject']}\"")
    return "\n".join(lines) + note


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
