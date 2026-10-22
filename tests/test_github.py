"""Phase E3 — GitHub, behind ARIA's armed gate.

The two-step trust model, pinned against a stubbed HTTP layer:

  * No token stored → both write tools refuse "not connected".
  * Token stored, flag OFF (the default — storing never arms) →
    both refuse with the arming instruction, and NO request is made.
  * Armed → the requests go out in ARIA's shapes: Bearer auth,
    /user/repos with auto_init for creation, contents GET-then-PUT
    with the existing sha for a push; a bare repo name gains the
    configured username; an API error is a failure with its body.
"""
import base64
import io
import json
import urllib.error
from pathlib import Path

import pytest

import actions.github_create_repo as gcr
import actions.github_list_files as glf
import actions.github_push_file as gpf
import actions.github_read_file as grf
import actions.github_repo_info as gri
import actions.github_setup as gsu
from core import github
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    return path


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class FakeHTTP:
    def __init__(self):
        self.requests = []
        self.get_payload = {}          # what a contents GET answers
        self.error = None              # (code, body) to raise

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if self.error:
            code, body = self.error
            raise urllib.error.HTTPError(
                req.full_url, code, "err", None, io.BytesIO(body))
        if req.method == "GET":
            return FakeResponse(self.get_payload)
        if req.full_url.endswith("/user/repos"):
            return FakeResponse(
                {"html_url": "https://github.com/ABerger94/newrepo"})
        return FakeResponse({"content": {"path": "x"}})


@pytest.fixture()
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(github, "_urlopen", fake)
    return fake


def _connect_and_arm():
    cm.save_github_credentials("tok-abc", "ABerger94")
    cm.save_github_armed(True)


# ── the gate ─────────────────────────────────────────────────────────────────

def test_not_connected_refuses(cfg, http):
    for result in (gcr.github_create_repo({"name": "x"}),
                   gpf.github_push_file({"filepath": "a.txt",
                                         "content": "hi"})):
        assert result.startswith("status=failed"), result
        assert "set up GitHub" in result, result
    assert http.requests == []


def test_connected_but_not_armed_refuses(cfg, http):
    cm.save_github_credentials("tok-abc", "ABerger94")
    assert github.state() == "disarmed"       # storing never arms
    for result in (gcr.github_create_repo({"name": "x"}),
                   gpf.github_push_file({"filepath": "a.txt",
                                         "content": "hi"})):
        assert result.startswith("status=failed"), result
        assert "not armed" in result and "arm GitHub" in result, result
    assert http.requests == []                 # the refusal makes no call


def test_setup_connects_then_arms(cfg):
    assert gsu.github_setup({}).startswith("status=failed")
    result = gsu.github_setup({"github_token": "tok-abc",
                               "github_username": "ABerger94"})
    assert result.startswith("status=done")
    assert "NOT armed" in result
    result = gsu.github_setup({"arm": True})
    assert "armed=true" in result
    assert github.state() == "armed"
    # A later credential-only call keeps the flag and the username.
    gsu.github_setup({"github_token": "tok-new"})
    assert github.state() == "armed"
    assert cm.get_github_username() == "ABerger94"


# ── the requests ─────────────────────────────────────────────────────────────

def test_create_repo_request_shape(cfg, http):
    _connect_and_arm()
    result = gcr.github_create_repo({"name": "newrepo",
                                     "description": "d"})
    assert result.startswith("status=done"), result
    assert "https://github.com/ABerger94/newrepo" in result
    req = http.requests[-1]
    assert req.method == "POST"
    assert req.full_url == "https://api.github.com/user/repos"
    assert req.headers["Authorization"] == "Bearer tok-abc"
    payload = json.loads(req.data)
    assert payload["name"] == "newrepo"
    assert payload["private"] is True
    assert payload["auto_init"] is True


def test_push_file_updates_with_sha(cfg, http):
    _connect_and_arm()
    http.get_payload = {"sha": "abc123"}
    result = gpf.github_push_file({"repo": "notes", "filepath": "a/b.md",
                                   "content": "hello"})
    assert result.startswith("status=done"), result
    get_req, put_req = http.requests
    assert get_req.method == "GET"
    assert "/repos/ABerger94/notes/contents/a/b.md" in get_req.full_url
    payload = json.loads(put_req.data)
    assert payload["sha"] == "abc123"
    assert base64.b64decode(payload["content"]).decode() == "hello"
    assert payload["message"] == "ARIA: update a/b.md"


def test_push_file_creates_without_sha(cfg, http):
    _connect_and_arm()
    http.get_payload = {}                      # GET found no file
    result = gpf.github_push_file(
        {"repo": "ABerger94/x", "filepath": "new.txt", "content": "n"})
    assert result.startswith("status=done"), result
    assert "sha" not in json.loads(http.requests[-1].data)


def test_api_error_is_a_failure_with_its_body(cfg, http):
    _connect_and_arm()
    http.error = (422, b'{"message":"name already exists"}')
    result = gcr.github_create_repo({"name": "taken"})
    assert result.startswith("status=failed"), result
    assert "GitHub API error 422" in result
    assert "already exists" in result


def test_registry_lists_the_github_tools():
    import main
    from core.action_loader import discover_actions

    reserved = {t["name"] for t in main.TOOL_DECLARATIONS}
    actions = discover_actions(ROOT / "actions", reserved_names=reserved,
                               logger=lambda msg: None)
    for name in ("github_setup", "github_create_repo", "github_push_file",
                 "github_list_files", "github_read_file", "github_repo_info"):
        assert actions.has(name), name


# ── the reads (connected is enough — arming is the write side) ───────────────

def test_reads_refuse_when_not_connected(cfg, http):
    for result in (glf.github_list_files({"repo": "x"}),
                   grf.github_read_file({"repo": "x", "filepath": "a.py"}),
                   gri.github_repo_info({"repo": "x"})):
        assert result.startswith("status=failed"), result
        assert "set up GitHub" in result, result
    assert http.requests == []


def test_list_files_works_while_disarmed(cfg, http):
    """The pinned asymmetry: token stored, flag OFF — the write
    tools refuse (see above) but a read goes through. Reviewing a
    codebase must not require arming her."""
    cm.save_github_credentials("tok-abc", "ABerger94")
    assert github.state() == "disarmed"
    http.get_payload = [
        {"name": "core", "type": "dir", "size": 0},
        {"name": "main.py", "type": "file", "size": 152340},
    ]
    result = glf.github_list_files({"repo": "Mark-LVI-Alek"})
    assert result.startswith("status=done"), result
    assert "core/" in result and "main.py" in result, result
    assert "152,340" in result, result
    assert "tok-abc" not in result
    req = http.requests[-1]
    assert req.method == "GET"
    assert "/repos/ABerger94/Mark-LVI-Alek/contents" in req.full_url


def test_read_file_decodes_base64(cfg, http):
    cm.save_github_credentials("tok-abc", "ABerger94")
    http.get_payload = {
        "content": base64.b64encode(b"print('hi')\n").decode(),
        "encoding": "base64", "size": 11}
    result = grf.github_read_file({"repo": "notes", "filepath": "a.py"})
    assert result.startswith("status=done"), result
    assert "print('hi')" in result, result
    assert "tok-abc" not in result
    assert http.requests[-1].method == "GET"
    assert "/repos/ABerger94/notes/contents/a.py" in \
        http.requests[-1].full_url


def test_read_file_truncates_with_the_total_stated(cfg, http):
    cm.save_github_credentials("tok-abc", "ABerger94")
    body = "y" * 13000
    http.get_payload = {
        "content": base64.b64encode(body.encode()).decode(),
        "encoding": "base64", "size": 13000}
    result = grf.github_read_file({"repo": "notes", "filepath": "big.txt"})
    assert result.startswith("status=done"), result
    assert "Truncated" in result and "13,000" in result, result
    assert ("y" * 12000) in result            # the cap is shown…
    assert ("y" * 12001) not in result         # …and not one char more


def test_read_file_binary_is_named_not_dumped(cfg, http):
    cm.save_github_credentials("tok-abc", "ABerger94")
    http.get_payload = {
        "content": base64.b64encode(bytes(range(256))).decode(),
        "encoding": "base64", "size": 256}
    result = grf.github_read_file({"repo": "notes", "filepath": "x.bin"})
    assert result.startswith("status=done"), result
    assert "binary" in result and "not text" in result, result


def test_read_file_404_says_check_the_name(cfg, http):
    cm.save_github_credentials("tok-abc", "ABerger94")
    http.error = (404, b'{"message":"Not Found"}')
    result = grf.github_read_file({"repo": "notes", "filepath": "nope.py"})
    assert result.startswith("status=failed"), result
    assert "couldn't find" in result and "404" in result, result


def test_read_file_401_names_the_dead_token(cfg, http):
    cm.save_github_credentials("tok-abc", "ABerger94")
    http.error = (401, b'{"message":"Bad credentials"}')
    result = glf.github_list_files({"repo": "notes"})
    assert result.startswith("status=failed"), result
    assert "401" in result and "token" in result, result
    assert "tok-abc" not in result             # never echo the secret


def test_repo_info_reports_the_facts(cfg, http):
    cm.save_github_credentials("tok-abc", "ABerger94")
    http.get_payload = {
        "description": "the codebase", "default_branch": "main",
        "language": "Python", "updated_at": "2026-10-22T09:00:00Z",
        "open_issues_count": 3, "private": False}
    result = gri.github_repo_info({"repo": "Mark-LVI-Alek"})
    assert result.startswith("status=done"), result
    assert "main" in result and "Python" in result, result
    assert "public" in result and "3" in result, result
    assert http.requests[-1].full_url.endswith(
        "/repos/ABerger94/Mark-LVI-Alek")
