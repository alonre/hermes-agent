"""Tests for the PaperClip tool surface (tools/paperclip_tools.py).

Verifies:
  - Tools are gated on PAPERCLIP_AGENT_API_KEY: no key means zero
    paperclip_* tools in the schema.
  - paperclip_set_disposition happy path, validation, 409 ownership
    conflict, and generic non-2xx handling.
  - paperclip_create_issue happy path (agents/me + agent lookup + create),
    no-match assignee, and the parent_issue_id passthrough.
"""
from __future__ import annotations

import json

import httpx
import pytest


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_paperclip_tools_hidden_without_api_key(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)

    import tools.paperclip_tools  # noqa: F401 ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    paperclip = {n for n in names if n and n.startswith("paperclip_")}
    assert paperclip == set(), f"paperclip tools leaked without an api key: {paperclip}"


def test_paperclip_tools_visible_with_api_key(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://host.docker.internal:3100")

    import tools.paperclip_tools  # noqa: F401 ensure registered
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    paperclip = {n for n in names if n and n.startswith("paperclip_")}
    assert paperclip == {"paperclip_set_disposition", "paperclip_create_issue"}


# ---------------------------------------------------------------------------
# paperclip_set_disposition
# ---------------------------------------------------------------------------

@pytest.fixture
def paperclip_env(monkeypatch):
    monkeypatch.setenv("PAPERCLIP_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://host.docker.internal:3100")
    from tools import paperclip_tools as pt
    pt._company_id_cache.clear()
    return pt


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text if json_body is None else json.dumps(json_body)

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


def test_set_disposition_requires_all_fields(paperclip_env):
    pt = paperclip_env
    assert "issue_id is required" in pt._handle_set_disposition({})
    assert "run_id is required" in pt._handle_set_disposition({"issue_id": "i1"})
    assert "status must be one of" in pt._handle_set_disposition(
        {"issue_id": "i1", "run_id": "r1", "status": "bogus"}
    )
    assert "comment is required" in pt._handle_set_disposition(
        {"issue_id": "i1", "run_id": "r1", "status": "done"}
    )


def test_set_disposition_happy_path(monkeypatch, paperclip_env):
    pt = paperclip_env
    captured = {}

    def fake_patch(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200, json_body={"id": "i1", "status": "done"})

    monkeypatch.setattr(httpx, "patch", fake_patch)

    out = pt._handle_set_disposition({
        "issue_id": "i1", "run_id": "r1", "status": "done", "comment": "did the thing",
    })
    data = json.loads(out)
    assert data == {"id": "i1", "status": "done"}
    assert captured["url"] == "http://host.docker.internal:3100/api/issues/i1"
    assert captured["json"] == {"status": "done", "comment": "did the thing"}
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["headers"]["X-Paperclip-Run-Id"] == "r1"


def test_set_disposition_ownership_conflict_surfaced_verbatim(monkeypatch, paperclip_env):
    pt = paperclip_env

    def fake_patch(url, json=None, headers=None, timeout=None):
        return _FakeResponse(409, json_body={"error": "Issue run ownership conflict"})

    monkeypatch.setattr(httpx, "patch", fake_patch)

    out = pt._handle_set_disposition({
        "issue_id": "i1", "run_id": "stale-run", "status": "done", "comment": "x",
    })
    data = json.loads(out)
    assert "Issue run ownership conflict" in data["error"]
    assert "re-fetch" in data["error"].lower() or "re-fetch the issue" in data["error"].lower()


def test_set_disposition_generic_error(monkeypatch, paperclip_env):
    pt = paperclip_env

    def fake_patch(url, json=None, headers=None, timeout=None):
        return _FakeResponse(500, json_body={"error": "boom"})

    monkeypatch.setattr(httpx, "patch", fake_patch)

    out = pt._handle_set_disposition({
        "issue_id": "i1", "run_id": "r1", "status": "done", "comment": "x",
    })
    data = json.loads(out)
    assert "HTTP 500" in data["error"]


def test_set_disposition_not_configured(monkeypatch):
    monkeypatch.delenv("PAPERCLIP_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("PAPERCLIP_API_URL", raising=False)
    from tools import paperclip_tools as pt
    out = pt._handle_set_disposition({
        "issue_id": "i1", "run_id": "r1", "status": "done", "comment": "x",
    })
    assert "not configured" in json.loads(out)["error"]


# ---------------------------------------------------------------------------
# paperclip_create_issue
# ---------------------------------------------------------------------------

def test_create_issue_happy_path(monkeypatch, paperclip_env):
    pt = paperclip_env
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(("get", url))
        if url.endswith("/api/agents/me"):
            return _FakeResponse(200, json_body={"id": "me", "companyId": "co1"})
        if url.endswith("/api/companies/co1/agents"):
            return _FakeResponse(200, json_body=[
                {"id": "agent-a", "name": "skills-consultant"},
                {"id": "agent-b", "name": "other-agent"},
            ])
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(("post", url, json))
        assert url == "http://host.docker.internal:3100/api/companies/co1/issues"
        assert "status" not in json
        return _FakeResponse(200, json_body={"id": "iss1", "identifier": "KOA-1", "status": "todo"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    out = pt._handle_create_issue({
        "title": "help me",
        "description": "please review this",
        "assignee_name": "skills-consultant",
    })
    data = json.loads(out)
    assert data["ok"] is True
    assert data["issue_id"] == "iss1"
    assert data["identifier"] == "KOA-1"
    assert data["assignee_agent_id"] == "agent-a"

    # companyId should be cached: a second call must not re-hit /agents/me.
    calls.clear()
    out2 = pt._handle_create_issue({
        "title": "help me again",
        "description": "please review this too",
        "assignee_name": "other-agent",
    })
    data2 = json.loads(out2)
    assert data2["assignee_agent_id"] == "agent-b"
    assert not any(c[0] == "get" and c[1].endswith("/api/agents/me") for c in calls)


def test_create_issue_parent_id_passthrough(monkeypatch, paperclip_env):
    pt = paperclip_env

    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/api/agents/me"):
            return _FakeResponse(200, json_body={"companyId": "co1"})
        return _FakeResponse(200, json_body=[{"id": "agent-a", "name": "reviewer"}])

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _FakeResponse(200, json_body={"id": "iss2", "identifier": "KOA-2", "status": "todo"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)

    pt._handle_create_issue({
        "title": "sub-task",
        "description": "part of the bigger issue",
        "assignee_name": "reviewer",
        "parent_issue_id": "parent-1",
    })
    assert captured["json"]["parentId"] == "parent-1"


def test_create_issue_no_matching_assignee(monkeypatch, paperclip_env):
    pt = paperclip_env

    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/api/agents/me"):
            return _FakeResponse(200, json_body={"companyId": "co1"})
        return _FakeResponse(200, json_body=[{"id": "agent-a", "name": "someone-else"}])

    monkeypatch.setattr(httpx, "get", fake_get)

    out = pt._handle_create_issue({
        "title": "t", "description": "d", "assignee_name": "ghost-agent",
    })
    data = json.loads(out)
    assert "no PaperClip agent named 'ghost-agent'" in data["error"]


def test_create_issue_requires_fields(paperclip_env):
    pt = paperclip_env
    assert "title is required" in pt._handle_create_issue({})
    assert "description is required" in pt._handle_create_issue({"title": "t"})
    assert "assignee_name is required" in pt._handle_create_issue({"title": "t", "description": "d"})
