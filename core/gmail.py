"""
core/gmail.py — Gmail over IMAP + SMTP, ARIA's comms layer ported.

ARIA read and sent mail with a Google **app password** — no OAuth, no
token refresh, just IMAP (imap.gmail.com:993) for reading and SMTP
(smtp.gmail.com:587, STARTTLS) for sending, both authenticated with the
same stored pair. That shape is kept exactly, for the reason the
migration inventory gave: the credential on Alek's laptop is a portable
string, so connecting this build is a copy, not a re-authorization.

Semantics carried over from aria/tools/builtins.py, deliberately:

  * The app password is stored and used with spaces stripped — Google
    shows app passwords grouped in fours ("abcd efgh ijkl mnop"), and
    everybody pastes them that way.
  * The literal "INSERT" is treated as unset. ARIA's keys file shipped
    with that placeholder, and a config carrying it is an unconfigured
    config, not a wrong password.
  * Reading is READ-ONLY: the mailbox is selected readonly and fetches
    use BODY.PEEK, so listing or reading mail never marks it read.
  * Search is Gmail's own syntax via X-GM-RAW, falling back to a plain
    TEXT search when the server refuses the extension.
  * A missing configuration is a clean outcome, never an exception:
    every public function returns either its result or an outcome.failed
    string telling the model (and through it, the user) to run the
    setup action first.

The two factories below are the network seams. Production code leaves
them alone; the tests replace them with fakes so the whole layer is
exercised with no socket opened and no credential present.
"""

from __future__ import annotations

import email as _email
import imaplib
import re
import smtplib
import time
from email.message import EmailMessage
from core import outcome
from memory import config_manager

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# Network seams — replaced by tests, never by production code.
_imap_factory = imaplib.IMAP4_SSL
_smtp_factory = smtplib.SMTP
_sleep = time.sleep

BODY_LIMIT = 4000


def credentials() -> tuple[str, str]:
    """(address, app password) with ARIA's normalisation applied."""
    user = (config_manager.get_gmail_user() or "").strip()
    pw = (config_manager.get_gmail_app_password() or "") \
        .replace(" ", "").strip()
    if pw == "INSERT":
        pw = ""
    return user, pw


def connected() -> bool:
    user, pw = credentials()
    return bool(user and pw)


def not_connected(action: str) -> str:
    """The one shape every Gmail action answers with when unconfigured."""
    return outcome.failed(
        action,
        "Gmail is not connected yet. Tell the user to say 'set up Gmail' "
        "and give their Gmail address and an app password "
        "(myaccount.google.com/apppasswords — needs 2-Step Verification "
        "turned on), then call gmail_setup with them.")


def _auth_failed(action: str) -> str:
    return outcome.failed(
        action,
        "Gmail rejected the login — the app password is wrong or was "
        "revoked. Ask the user to generate a fresh one at "
        "myaccount.google.com/apppasswords, then call gmail_setup again.")


def _open_imap(action: str):
    """(connection, None) or (None, outcome string)."""
    user, pw = credentials()
    if not user or not pw:
        return None, not_connected(action)
    try:
        m = _imap_factory(IMAP_HOST, IMAP_PORT, timeout=30)
        m.login(user, pw)
        return m, None
    except imaplib.IMAP4.error:
        return None, _auth_failed(action)
    except Exception as e:
        return None, outcome.failed(
            action, f"could not reach Gmail over IMAP: {e}")


def _text_part(msg) -> str:
    """Best-effort plain-text body from a parsed email message (ARIA's)."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(
                        part.get_content_charset() or "utf-8",
                        errors="replace")
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8",
                              errors="replace")
    except Exception:
        return str(msg.get_payload())


def _clamp_limit(limit, default: int = 10) -> int:
    try:
        return max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        return default


def list_summaries(query: str = "", limit: int = 10,
                   unread_only: bool = False, action: str = "read_email"):
    """Newest matching messages as structured entries.

    Returns (entries, None) — entries are dicts with uid/date/from/
    subject/seen — or (None, outcome string) when Gmail is not connected or
    the read failed. This is the shared source for read_email,
    triage_email and the morning briefing, so the three can never
    disagree about what the inbox holds.
    """
    m, err = _open_imap(action)
    if err:
        return None, err
    try:
        m.select("INBOX", readonly=True)
        if query:
            # Gmail's raw search: full Gmail query syntax over IMAP.
            try:
                typ, data = m.uid("SEARCH", None, "X-GM-RAW", query)
                if typ != "OK":
                    raise imaplib.IMAP4.error("X-GM-RAW failed")
            except Exception:
                typ, data = m.uid("SEARCH", None, "TEXT", query)
        else:
            criteria = ["UNSEEN"] if unread_only else ["ALL"]
            typ, data = m.uid("SEARCH", None, *criteria)
        if typ != "OK":
            return None, outcome.failed(action, "Gmail search failed")
        uids = (data[0] or b"").split()
        uids = uids[-_clamp_limit(limit):][::-1]  # newest first
        entries = []
        for raw_uid in uids:
            uid = raw_uid.decode()
            typ, data = m.uid(
                "FETCH", uid,
                "(FLAGS BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])")
            if typ != "OK" or not data or not data[0]:
                continue
            raw = data[0][1] if isinstance(data[0], tuple) else data[0]
            # The flags ride in the response's first element, beside
            # the header bytes — b'101 (FLAGS (\\Seen) BODY[...])'.
            # BODY.PEEK means listing never moves them, so the entry
            # can carry read/unread honestly, from the same fetch.
            flags = data[0][0] if isinstance(data[0], tuple) else b""
            h = _email.message_from_bytes(raw)
            entries.append({
                "uid": uid,
                "date": (h.get("Date") or "?")[:31],
                "from": (h.get("From") or "?").replace("\n", " ")[:60],
                "subject": (h.get("Subject") or "(no subject)")
                           .replace("\n", " ")[:80],
                "seen": b"\\Seen" in (flags or b""),
            })
        return entries, None
    except Exception as e:
        return None, outcome.failed(action, f"could not read Gmail: {e}")
    finally:
        try:
            m.logout()
        except Exception:
            pass


def unread_count(action: str = "inbox_tab"):
    """(count, None) or (None, outcome string): how many INBOX
    messages the server counts as unseen — an IMAP STATUS query,
    so no mailbox is selected and no flag can move. This is the
    number the content panel's header shows on the Inbox tab;
    callers treat an error as "unknown", never as zero."""
    m, err = _open_imap(action)
    if err:
        return None, err
    try:
        typ, data = m.status("INBOX", "(UNSEEN)")
        if typ != "OK" or not data or not data[0]:
            return None, outcome.failed(action, "Gmail status failed")
        raw = data[0].decode() if isinstance(data[0], bytes) \
            else str(data[0])
        match = re.search(r"UNSEEN\s+(\d+)", raw)
        if not match:
            return None, outcome.failed(action, "Gmail status failed")
        return int(match.group(1)), None
    except Exception as e:
        return None, outcome.failed(action, f"could not read Gmail: {e}")
    finally:
        try:
            m.logout()
        except Exception:
            pass


def summaries_text(entries) -> str:
    """ARIA's one-line-per-message rendering of list_summaries()."""
    lines = [f"[uid={e['uid']}] {e['date']} | {e['from']} | "
             f"\"{e['subject']}\"" for e in entries]
    return f"{len(lines)} email(s), newest first:\n" + "\n".join(lines)


def read_body(uid: str, action: str = "read_email"):
    """One message in full. (dict, None) or (None, outcome string)."""
    m, err = _open_imap(action)
    if err:
        return None, err
    try:
        m.select("INBOX", readonly=True)
        typ, data = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or not data[0]:
            return None, outcome.failed(
                action, f"no message found with uid {uid}")
        raw = data[0][1] if isinstance(data[0], tuple) else data[0]
        msg = _email.message_from_bytes(raw)
        body = _text_part(msg).strip()
        if len(body) > BODY_LIMIT:
            body = body[:BODY_LIMIT] + "\n[...truncated...]"
        return {
            "from": msg.get("From", "?"),
            "date": msg.get("Date", "?"),
            "subject": msg.get("Subject", "(no subject)"),
            "body": body,
        }, None
    except Exception as e:
        return None, outcome.failed(action, f"could not read Gmail: {e}")
    finally:
        try:
            m.logout()
        except Exception:
            pass


def send(to: str, subject: str, body: str,
         action: str = "send_email") -> str:
    """Send via SMTP. Returns an outcome string — never raises.

    Called from the confirmation gate's `run` callable (see
    actions/send_email.py), so by the time this runs a human has
    already said yes; what remains is ARIA's delivery discipline: an
    authentication failure is final (retrying a revoked password just
    fails twice), anything else gets one retry after a pause.
    """
    user, pw = credentials()
    if not user or not pw:
        return not_connected(action)
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject or "(no subject)"
    msg.set_content(body or "")

    for attempt in range(2):
        try:
            with _smtp_factory(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.starttls()
                s.login(user, pw)
                refused = s.send_message(msg)
            if refused:
                return outcome.unverified(
                    action,
                    "the server refused some recipients: "
                    + ", ".join(sorted(refused)),
                    to=to, subject=subject)
            return outcome.done(action, to=to, subject=subject)
        except smtplib.SMTPAuthenticationError:
            return _auth_failed(action)
        except Exception:
            if attempt == 0:
                _sleep(3)
    return outcome.failed(action, f"could not send email to {to}",
                          to=to, subject=subject)


# Kept for callers that think in ARIA's names.
__all__ = ["credentials", "connected", "not_connected", "list_summaries",
           "summaries_text", "read_body", "send",
           "IMAP_HOST", "IMAP_PORT", "SMTP_HOST", "SMTP_PORT"]
