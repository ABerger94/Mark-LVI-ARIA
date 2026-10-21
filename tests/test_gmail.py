"""Phase E1 — Gmail, ARIA's comms on LVI's frame.

Pinned here, against fake IMAP/SMTP servers (no socket, no credential):

  * Unconfigured is a clean outcome, not a crash: every action answers
    "not connected yet — say 'set up Gmail'" until gmail_setup saves a
    credential pair, and the pair round-trips through the config store.
  * Reading is ARIA's read: newest-first one-line summaries carrying
    uids, Gmail search syntax, a full body by uid (truncated where she
    truncated), and a READ-ONLY mailbox — selecting readonly and
    BODY.PEEK fetches, so reading never marks mail read.
  * Triage is ARIA's classifier brief over the same summaries, through
    an injected classifier; an empty inbox and a silent classifier are
    both reported plainly.
  * Sending sits behind core/confirm.py's token (Phase C gate class):
    nothing reaches SMTP without a human CONFIRM, a cancel sends
    nothing, a server refusal is unverified — never "sent".
"""
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

import pytest

import actions.gmail_setup as gs
import actions.read_email as re_mod
import actions.send_email as se
import actions.triage_email as te
from core import confirm, gmail
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent


# ── config + confirm fixtures ────────────────────────────────────────────────

@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    return path


@pytest.fixture()
def connected(cfg):
    cm.save_gmail_credentials("alek@example.com", "abcd efgh ijkl mnop")
    return cfg


@pytest.fixture()
def ui_bound():
    shown = []
    confirm.bind(show=lambda t, d: shown.append((t, d)),
                 hide=lambda: None, log=None)
    yield shown
    if confirm.pending_title():
        confirm.resolve(False)
    confirm.bind(None, None)


# ── fake servers ─────────────────────────────────────────────────────────────

def _message(frm, subject, date, body="hello body"):
    msg = EmailMessage()
    msg["From"] = frm
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content(body)
    return msg.as_bytes()


class FakeIMAP:
    instances = []
    raw_search_fails = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.login_args = None
        self.selected = None
        self.messages = {
            "101": _message("Boss <boss@work.example>", "Q3 report due",
                            "Mon, 20 Oct 2026 09:00:00 -0400"),
            "102": _message("Shop <deals@shop.example>", "50% off everything",
                            "Mon, 20 Oct 2026 10:00:00 -0400"),
            "103": _message("Mum <mum@family.example>", "Sunday dinner?",
                            "Mon, 20 Oct 2026 11:00:00 -0400",
                            body="Are you coming on Sunday?"),
        }
        FakeIMAP.instances.append(self)

    def login(self, user, pw):
        self.login_args = (user, pw)
        return "OK", [b"Logged in"]

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [b"3"]

    def uid(self, command, *args):
        if command == "SEARCH":
            if "X-GM-RAW" in args and FakeIMAP.raw_search_fails:
                return "NO", [b""]
            return "OK", [b"101 102 103"]
        if command == "FETCH":
            uid, spec = str(args[0]), str(args[1])
            raw = self.messages[uid]
            if "HEADER.FIELDS" in spec:
                head = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                return "OK", [(b"hdr", head)]
            return "OK", [(b"msg", raw)]
        raise AssertionError(f"unexpected uid command {command}")

    def logout(self):
        return "BYE", [b"bye"]


class FakeSMTP:
    instances = []
    refused = {}
    raise_auth = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        self.login_args = (user, pw)
        if FakeSMTP.raise_auth:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, msg):
        self.sent.append(msg)
        return dict(FakeSMTP.refused)


@pytest.fixture()
def fake_servers(monkeypatch):
    FakeIMAP.instances = []
    FakeIMAP.raw_search_fails = False
    FakeSMTP.instances = []
    FakeSMTP.refused = {}
    FakeSMTP.raise_auth = False
    monkeypatch.setattr(gmail, "_imap_factory", FakeIMAP)
    monkeypatch.setattr(gmail, "_smtp_factory", FakeSMTP)
    monkeypatch.setattr(gmail, "_sleep", lambda s: None)
    return FakeIMAP, FakeSMTP


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── setup / not connected ────────────────────────────────────────────────────

def test_not_connected_is_a_clean_outcome(cfg):
    for result in (re_mod.read_email({}),
                   te.triage_email({}),
                   se.send_email({"to": "a@b.example", "subject": "s",
                                  "body": "b"})):
        assert result.startswith("status=failed"), result
        assert "set up Gmail" in result, result


def test_gmail_setup_round_trip(cfg):
    result = gs.gmail_setup({"gmail_user": " alek@example.com ",
                             "app_password": "abcd efgh"})
    assert result.startswith("status=done"), result
    assert cm.get_gmail_user() == "alek@example.com"
    # ARIA's normalisation: the pasted grouping spaces never reach IMAP.
    user, pw = gmail.credentials()
    assert (user, pw) == ("alek@example.com", "abcdefgh")
    assert gmail.connected()


def test_gmail_setup_needs_both_values(cfg):
    assert gs.gmail_setup({"gmail_user": "", "app_password": "x"}) \
        .startswith("status=failed")
    assert gs.gmail_setup({"gmail_user": "a@b.example"}) \
        .startswith("status=failed")
    assert not gmail.connected()


# ── reading ──────────────────────────────────────────────────────────────────

def test_read_lists_newest_first_readonly(connected, fake_servers):
    result = re_mod.read_email({})
    assert result.startswith("status=done"), result
    assert "count=3" in result
    assert result.index("uid=103") < result.index("uid=101")
    assert "Sunday dinner?" in result
    fake = FakeIMAP.instances[-1]
    assert fake.selected == ("INBOX", True)          # readonly: no \Seen
    assert fake.login_args == ("alek@example.com", "abcdefghijklmnop")


def test_read_search_falls_back_to_text(connected, fake_servers):
    FakeIMAP.raw_search_fails = True
    result = re_mod.read_email({"query": "from:boss newer_than:7d"})
    assert result.startswith("status=done"), result
    assert "Q3 report due" in result


def test_read_body_by_uid(connected, fake_servers):
    result = re_mod.read_email({"uid": "103"})
    assert result.startswith("status=done"), result
    assert "Are you coming on Sunday?" in result
    assert "<<<UNTRUSTED source=gmail>>>" in result


def test_read_body_truncates_like_aria(connected, fake_servers, monkeypatch):
    big = _message("A <a@x.example>", "big", "Mon, 20 Oct 2026 08:00:00 -0400",
                   body="x" * 5000)
    orig_init = FakeIMAP.__init__

    def patched(self, host, port, timeout=None):
        orig_init(self, host, port, timeout)
        self.messages["109"] = big

    monkeypatch.setattr(FakeIMAP, "__init__", patched)
    # uid 109 is not in the search set; read it directly through the core.
    msg, err = gmail.read_body("109")
    assert err is None
    assert msg["body"].endswith("[...truncated...]")
    assert len(msg["body"]) <= gmail.BODY_LIMIT + 30


# ── triage ───────────────────────────────────────────────────────────────────

def test_triage_classifies_with_arias_brief(connected, fake_servers,
                                            monkeypatch):
    seen = {}

    def fake_classify(system, user_text):
        seen["system"] = system
        seen["user"] = user_text
        return ("IMPORTANT (act now): Q3 report due from Boss\n"
                "FYI: Sunday dinner? from Mum\n"
                "NOISE (ignored): 1")

    monkeypatch.setattr(te, "_classify", fake_classify)
    result = te.triage_email({})
    assert result.startswith("status=done"), result
    assert "IMPORTANT (act now)" in result
    assert "err toward important" in seen["system"]
    assert "Sunday dinner?" in seen["user"]


def test_triage_silent_classifier_is_a_failure(connected, fake_servers,
                                                monkeypatch):
    monkeypatch.setattr(te, "_classify", lambda s, u: "")
    assert te.triage_email({}).startswith("status=failed")


# ── sending, behind the gate ─────────────────────────────────────────────────

def test_send_is_gated(connected, fake_servers, ui_bound):
    result = se.send_email({"to": "mum@family.example",
                            "subject": "Re: Sunday", "body": "Yes!"})
    assert "[CONFIRMATION_PENDING]" in result
    assert FakeSMTP.instances == []            # nothing sent yet
    confirm.resolve(True)
    assert _wait_for(lambda: FakeSMTP.instances
                     and FakeSMTP.instances[-1].sent)
    msg = FakeSMTP.instances[-1].sent[0]
    assert msg["To"] == "mum@family.example"
    assert msg["Subject"] == "Re: Sunday"


def test_send_cancelled_sends_nothing(connected, fake_servers, ui_bound):
    se.send_email({"to": "a@b.example", "subject": "s", "body": "b"})
    confirm.resolve(False)
    time.sleep(0.2)
    assert FakeSMTP.instances == []


def test_send_refusal_is_unverified_not_sent(connected, fake_servers):
    FakeSMTP.refused = {"bad@x.example": (550, b"no such user")}
    result = gmail.send("bad@x.example", "s", "b")
    assert result.startswith("status=unverified"), result


def test_send_auth_failure_is_final(connected, fake_servers):
    FakeSMTP.raise_auth = True
    result = gmail.send("a@b.example", "s", "b")
    assert result.startswith("status=failed"), result
    assert "rejected the login" in result
    assert len(FakeSMTP.instances) == 1        # no retry on a revoked password


# ── registry ─────────────────────────────────────────────────────────────────

def test_registry_lists_the_gmail_tools():
    import main
    from core.action_loader import discover_actions

    reserved = {t["name"] for t in main.TOOL_DECLARATIONS}
    actions = discover_actions(ROOT / "actions", reserved_names=reserved,
                               logger=lambda msg: None)
    for name in ("gmail_setup", "read_email", "triage_email", "send_email"):
        assert actions.has(name), name
