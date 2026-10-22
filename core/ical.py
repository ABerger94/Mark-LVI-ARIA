"""
core/ical.py — the secret-iCal calendar feed, ARIA's parser ported.

What Google Calendar's "Secret address in iCal format" serves, and
all this reads: VEVENTs with DTSTART/DTEND in UTC (...Z), floating
local, TZID, or date-only (all-day) forms. The port is verbatim where
ARIA's semantics were deliberate:

  * **Recurring events ARE expanded** (Phase H3 — this supersedes
    ARIA's documented not-expanded limitation, and the fixture in
    tests/test_calendar.py was updated in the same commit to pin
    the new behaviour). parse_ical keeps the RRULE/EXDATE/RDATE
    lines on the event; upcoming() expands occurrences inside its
    window via python-dateutil's rrule: FREQ (DAILY/WEEKLY/
    MONTHLY/YEARLY), INTERVAL, COUNT and UNTIL are honoured,
    EXDATE removes occurrences, RDATE adds them, and generation
    is capped per event (MAX_OCCURRENCES). A malformed RRULE — or
    dateutil missing — degrades to ARIA's behaviour for that
    event (first occurrence only) rather than breaking the
    calendar. (Google's feed also emits each recurring instance's
    exceptions as separate VEVENTs, which parse independently.)
  * The secret URL is the whole credential — whoever holds it reads
    the calendar. It lives in config (key ical_url) and is never
    logged or echoed.
  * Times come back from `upcoming` as naive LOCAL datetimes in the
    window [today 00:00, +days), sorted — the shape the briefing and
    check_calendar both consume.
"""

from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

USER_AGENT = "ARIA-Agent/8.0"

# Per-event occurrence cap for one upcoming() call. A daily event
# in a 7-day window needs 7; the cap exists so a pathological
# rule (FREQ=DAILY over a year-wide window, a runaway COUNT)
# cannot turn one calendar read into ten thousand dicts.
MAX_OCCURRENCES = 512

# Network seam — replaced by tests, never by production code.
_urlopen = urllib.request.urlopen


def fetch_ical(url: str, timeout: int = 20) -> str:
    """Download raw ICS text from a (secret) iCal URL."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _unfold(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape(s: str) -> str:
    return (s.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";"))


def _tzid_zone(params: str):
    m = re.search(r"TZID=([^;:]+)", params or "")
    if not m:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(m.group(1))
    except Exception:
        return None


def _parse_dt(value: str, params: str) -> Optional[datetime]:
    """Parse an ICS date/time into a datetime (aware when UTC/TZID known)."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S") \
                .replace(tzinfo=timezone.utc)
        mt = re.match(r"(\d{8})T(\d{6})$", value)
        if mt:
            dt = datetime.strptime(mt.group(1) + "T" + mt.group(2),
                                   "%Y%m%dT%H%M%S")
            tz = _tzid_zone(params)
            return dt.replace(tzinfo=tz) if tz else dt
        if re.match(r"^\d{8}$", value):  # date-only: all-day
            return datetime.strptime(value, "%Y%m%d")
    except ValueError:
        pass
    return None


def _build_event(props: Dict[str, Tuple[str, str]]) -> Optional[Dict]:
    raw_start = props.get("DTSTART")
    if not raw_start:
        return None
    start = _parse_dt(raw_start[1], raw_start[0])
    if not start:
        return None
    all_day = "T" not in raw_start[1].strip().upper()
    raw_end = props.get("DTEND")
    end = _parse_dt(raw_end[1], raw_end[0]) if raw_end else None
    if end is None:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

    def txt(name: str) -> str:
        return _unescape(props.get(name, ("", ""))[1]).strip()

    summary = txt("SUMMARY") or "(no title)"
    loc = txt("LOCATION")
    if loc:
        summary = f"{summary} @ {loc}"
    ev = {"summary": summary, "description": txt("DESCRIPTION"),
          "start": start, "end": end, "all_day": all_day}
    rrule = props.get("RRULE")
    if rrule and rrule[1].strip():
        ev["rrule"] = rrule[1].strip()
    for key, field in (("EXDATE", "exdates"), ("RDATE", "rdates")):
        raw = props.get(key)
        if raw:
            dates = [d for d in
                     (_parse_dt(part, raw[0])
                      for part in raw[1].split(",") if part.strip())
                     if d is not None]
            if dates:
                ev[field] = dates
            if key == "EXDATE":
                # Date-only exclusions match an all-day series by
                # DATE, not by midnight instant — kept separately
                # so upcoming() can honour that.
                days_only = [part.strip() for part in
                             raw[1].split(",")
                             if part.strip() and "T" not in part]
                parsed_days = [d.date() for d in
                               (_parse_dt(part, raw[0])
                                for part in days_only)
                               if d is not None]
                if parsed_days:
                    ev["exdate_days"] = parsed_days
    return ev


def parse_ical(text: str) -> List[Dict]:
    """Parse ICS text into event dicts. Times stay as parsed (see upcoming)."""
    events: List[Dict] = []
    in_event = False
    props: Dict[str, Tuple[str, str]] = {}
    for line in _unfold(text or ""):
        if line == "BEGIN:VEVENT":
            in_event, props = True, {}
        elif line == "END:VEVENT":
            if in_event:
                ev = _build_event(props)
                if ev:
                    events.append(ev)
            in_event = False
        elif in_event and ":" in line:
            prop, _, val = line.partition(":")
            name = prop.split(";")[0].strip().upper()
            if name in ("EXDATE", "RDATE") and name in props:
                # Several lines (or one comma list) accumulate;
                # the last-wins assignment below would silently
                # keep only the final line's dates.
                params, prev = props[name]
                props[name] = (params, prev + "," + val)
            else:
                props[name] = (prop[len(name):], val)
    return events


def _as_local_naive(dt: datetime, local_tz) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(local_tz).replace(tzinfo=None)
    return dt


def _in_frame(naive_local: datetime, start: datetime, local_tz) -> datetime:
    """A naive local wall time re-expressed in DTSTART's frame —
    the shape dateutil's rrule generates occurrences in."""
    if start.tzinfo is None:
        return naive_local
    return naive_local.replace(tzinfo=local_tz).astimezone(start.tzinfo)


_MAX_RULE_STEPS = 20000   # total rrule iterations per event, see below


def _occurrence_starts(e: Dict, win_lo: datetime, win_hi: datetime,
                       local_tz) -> List[datetime]:
    """Occurrence starts for one event, in the event's own frame:
    the RRULE's occurrences inside the (duration-widened) window,
    plus RDATEs, minus EXDATEs. Every failure mode — no dateutil,
    a malformed rule, a rule that will not iterate — degrades to
    [DTSTART] (ARIA's behaviour), never to an exception: a broken
    recurrence must not be able to break the whole calendar."""
    start = e["start"]
    text = e.get("rrule")
    starts: List[datetime] = []
    if not text:
        starts = [start]
    else:
        try:
            from dateutil.rrule import rrulestr
        except Exception:
            return [start]
        if start.tzinfo is not None:
            # dateutil requires UNTIL in UTC when DTSTART is aware;
            # a naive UNTIL beside an aware start is wall time in
            # the start's zone to the feed's author — convert it.
            m = re.search(r"UNTIL=([^;]+)", text, re.IGNORECASE)
            if m and not m.group(1).strip().endswith("Z"):
                naive = _parse_dt(m.group(1).strip(), "")
                if naive is not None and naive.tzinfo is None:
                    utc = naive.replace(tzinfo=start.tzinfo) \
                        .astimezone(timezone.utc)
                    text = (text[:m.start(1)]
                            + utc.strftime("%Y%m%dT%H%M%SZ")
                            + text[m.end(1):])
        try:
            rule = rrulestr(text, dtstart=start)
        except Exception:
            return [start]
        duration = e["end"] - start
        lo = _in_frame(win_lo, start, local_tz) - duration \
            - timedelta(days=1)
        hi = _in_frame(win_hi, start, local_tz)
        try:
            steps = 0
            for occ in rule:
                steps += 1
                if steps > _MAX_RULE_STEPS or occ > hi:
                    break
                if occ >= lo:
                    starts.append(occ)
                    if len(starts) >= MAX_OCCURRENCES:
                        break
        except Exception:
            return [start]
    for extra in e.get("rdates") or []:
        if extra not in starts:
            starts.append(extra)
    exdates = e.get("exdates") or []
    ex_days = set(e.get("exdate_days") or [])
    if exdates or ex_days:
        ex_instants = {_as_local_naive(x, local_tz) for x in exdates}
        kept = []
        for occ in starts:
            occ_local = _as_local_naive(occ, local_tz)
            if occ_local in ex_instants:
                continue
            if e.get("all_day") and occ_local.date() in ex_days:
                continue
            kept.append(occ)
        starts = kept
    return sorted(starts)


def upcoming(events: List[Dict], days: int = 1,
             now: Optional[datetime] = None) -> List[Dict]:
    """Events overlapping [local today 00:00, +days), times as naive
    local. A recurring event expands into its occurrences in the
    window (see _occurrence_starts); a plain event takes exactly
    the path it always did."""
    now = now or datetime.now()
    local_tz = now.astimezone().tzinfo
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = start_of_today + timedelta(days=max(1, days))
    out = []
    for e in events:
        if not e.get("rrule") and not e.get("rdates"):
            s = _as_local_naive(e["start"], local_tz)
            en = _as_local_naive(e["end"], local_tz)
            if s < window_end and en > start_of_today:
                out.append({**e, "start": s, "end": en})
            continue
        duration = e["end"] - e["start"]
        base = {k: v for k, v in e.items()
                if k not in ("rrule", "exdates", "exdate_days",
                             "rdates")}
        for occ in _occurrence_starts(e, start_of_today, window_end,
                                      local_tz):
            s = _as_local_naive(occ, local_tz)
            en = _as_local_naive(occ + duration, local_tz)
            if s < window_end and en > start_of_today:
                out.append({**base, "start": s, "end": en})
    return sorted(out, key=lambda e: e["start"])


def upcoming_from_feed(url: str, days: int = 1,
                       now: Optional[datetime] = None) -> List[Dict]:
    """Fetch + parse + window in one call — what both calendar
    consumers (check_calendar, morning_briefing) actually want."""
    return upcoming(parse_ical(fetch_ical(url)), days=days, now=now)


def _ft(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'PM' if dt.hour >= 12 else 'AM'}"


def format_event(e: Dict) -> str:
    """ARIA's check_calendar line for one (already-localised) event."""
    s, en = e["start"], e["end"]
    day = f"{s.strftime('%a %b')} {s.day}"
    tm = f"{day} (all day)" if e["all_day"] else f"{day}, {_ft(s)} - {_ft(en)}"
    return f"- {tm}: {e['summary']}"
