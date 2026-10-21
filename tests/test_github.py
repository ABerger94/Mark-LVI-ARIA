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
import actions.github_push_file as gpf
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
    for name in ("github_setup", "github_create_repo", "github_push_file"):
        assert actions.has(name), name
