"""PaperClip work-protocol tools — native replacement for curl-over-terminal.

The PaperClip control plane (see `~/src/paperclip`) wakes an agent with an
issue to work, and requires a mandatory final disposition call
(`PATCH /api/issues/{id}` with `{status, comment}`). Until now the only
documented way to do that was shelling out to curl from
`infra/vault/skills/paperclip-work-protocol/SKILL.md` in master_console —
which is a dead end for any profile without a terminal tool (e.g.
`browser-specialist`, `visual-analysis`).

These two tools give every PaperClip-onboarded profile (gated on
``PAPERCLIP_AGENT_API_KEY`` being present in the environment — wired into
``terminal.docker_env`` by the console's provisioning step) a structured,
terminal-free path to:

* record the mandatory final disposition (``paperclip_set_disposition``)
* create a tracked issue assigned to another agent, for delegation/consult
  routing (``paperclip_create_issue``)

Deliberately NOT a generic HTTP tool — only these two write operations the
work-protocol skill actually needs. No comment-only tool either:
``paperclip_set_disposition`` is the sole way to attach a comment during
disposition, mirroring the skill's "ONE call, ALWAYS" rule.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def _check_paperclip_mode() -> bool:
    """PaperClip tools appear automatically for any profile whose environment
    carries an agent API key — no per-profile manifest/toolset edit required.
    Mirrors the identity wiring itself, which is already universal (P5).
    """
    return bool(os.environ.get("PAPERCLIP_AGENT_API_KEY"))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REQUEST_TIMEOUT_SECONDS = 20

_DISPOSITION_STATUSES = frozenset(
    {"done", "in_review", "blocked", "cancelled", "in_progress"}
)

# companyId rarely if ever changes mid-run for a given agent identity, so
# cache it per (api_url, api_key) to skip a redundant /api/agents/me round
# trip on every paperclip_create_issue call within a process.
_company_id_cache: Dict[tuple, str] = {}


def _paperclip_env() -> Optional[tuple]:
    """Return ``(api_url, api_key)`` or ``None`` if PaperClip isn't configured.

    Reads the same env vars the skill's curl examples expand
    (``$PAPERCLIP_API_URL`` / ``$PAPERCLIP_AGENT_API_KEY``) via ``os.environ``
    instead of shell expansion.
    """
    api_url = os.environ.get("PAPERCLIP_API_URL")
    api_key = os.environ.get("PAPERCLIP_AGENT_API_KEY")
    if not api_url or not api_key:
        return None
    return api_url.rstrip("/"), api_key


def _auth_headers(api_key: str) -> dict:
    # Never log this dict — it carries the bearer key.
    return {"Authorization": f"Bearer {api_key}"}


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _response_detail(resp: "httpx.Response"):
    """Best-effort JSON body for error reporting; falls back to raw text."""
    try:
        return resp.json()
    except ValueError:
        return resp.text


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_set_disposition(args: dict, **kw) -> str:
    """Record the mandatory final disposition for a woken PaperClip issue."""
    env = _paperclip_env()
    if not env:
        return tool_error(
            "PaperClip is not configured for this session (PAPERCLIP_API_URL "
            "/ PAPERCLIP_AGENT_API_KEY missing from the environment)"
        )
    api_url, api_key = env

    issue_id = args.get("issue_id")
    if not issue_id or not str(issue_id).strip():
        return tool_error("issue_id is required — the issue id from the wake payload")

    run_id = args.get("run_id")
    if not run_id or not str(run_id).strip():
        return tool_error("run_id is required — the run id from the wake payload")

    status = args.get("status")
    if status not in _DISPOSITION_STATUSES:
        return tool_error(
            f"status must be one of {sorted(_DISPOSITION_STATUSES)}, got {status!r}"
        )

    comment = args.get("comment")
    if not comment or not str(comment).strip():
        return tool_error(
            "comment is required — evidence of what you did, artifacts, decisions"
        )

    try:
        resp = httpx.patch(
            f"{api_url}/api/issues/{issue_id}",
            json={"status": status, "comment": str(comment)},
            headers={
                **_auth_headers(api_key),
                "X-Paperclip-Run-Id": str(run_id),
            },
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        logger.warning(
            "paperclip_set_disposition request failed: %s", type(e).__name__
        )
        return tool_error(
            f"paperclip_set_disposition: request to PaperClip failed "
            f"({type(e).__name__})"
        )

    if resp.status_code == 409:
        detail = _response_detail(resp)
        if isinstance(detail, dict) and detail.get("error") == "Issue run ownership conflict":
            return tool_error(
                "Issue run ownership conflict: your run_id no longer matches "
                "this issue's active run. Re-fetch the issue "
                "(GET /api/issues/{issue_id}) and retry paperclip_set_disposition "
                "using its current executionRunId / checkoutRunId / "
                "activeRun.id as run_id.",
                detail=detail,
            )
        return tool_error(
            f"paperclip_set_disposition: 409 conflict for issue {issue_id}",
            detail=detail,
        )

    if not (200 <= resp.status_code < 300):
        return tool_error(
            f"paperclip_set_disposition failed: HTTP {resp.status_code}",
            detail=_response_detail(resp),
        )

    try:
        return json.dumps(resp.json(), ensure_ascii=False)
    except ValueError:
        return _ok(issue_id=str(issue_id), status=status)


def _resolve_company_id(api_url: str, api_key: str):
    """Return ``(company_id, error_message)``; exactly one is non-None."""
    cache_key = (api_url, api_key)
    cached = _company_id_cache.get(cache_key)
    if cached:
        return cached, None

    try:
        resp = httpx.get(
            f"{api_url}/api/agents/me",
            headers=_auth_headers(api_key),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        return None, f"could not reach PaperClip (/api/agents/me): {type(e).__name__}"

    if resp.status_code != 200:
        return None, f"/api/agents/me failed: HTTP {resp.status_code}: {_response_detail(resp)}"

    try:
        data = resp.json()
    except ValueError:
        return None, "/api/agents/me returned a non-JSON response"

    company_id = data.get("companyId") if isinstance(data, dict) else None
    if not company_id:
        return None, "/api/agents/me response is missing companyId"

    _company_id_cache[cache_key] = company_id
    return company_id, None


def _resolve_assignee_agent_id(api_url: str, api_key: str, company_id: str, assignee_name: str):
    """Return ``(agent_id, error_message)``; exactly one is non-None."""
    try:
        resp = httpx.get(
            f"{api_url}/api/companies/{company_id}/agents",
            headers=_auth_headers(api_key),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        return None, (
            f"could not reach PaperClip (/api/companies/{{companyId}}/agents): "
            f"{type(e).__name__}"
        )

    if resp.status_code != 200:
        return None, (
            f"/api/companies/{{companyId}}/agents failed: HTTP {resp.status_code}: "
            f"{_response_detail(resp)}"
        )

    try:
        agents_list = resp.json()
    except ValueError:
        return None, "/api/companies/{companyId}/agents returned a non-JSON response"

    if not isinstance(agents_list, list):
        return None, "/api/companies/{companyId}/agents returned an unexpected shape"

    # Case-sensitive exact match first; no fork convention exists for
    # fuzzy-name-match, so a no-match is a hard error rather than a guess.
    for agent in agents_list:
        if isinstance(agent, dict) and agent.get("name") == assignee_name:
            agent_id = agent.get("id")
            if agent_id:
                return agent_id, None

    return None, f"no PaperClip agent named '{assignee_name}'"


def _handle_create_issue(args: dict, **kw) -> str:
    """Create a PaperClip issue assigned to another agent (delegate/consult)."""
    env = _paperclip_env()
    if not env:
        return tool_error(
            "PaperClip is not configured for this session (PAPERCLIP_API_URL "
            "/ PAPERCLIP_AGENT_API_KEY missing from the environment)"
        )
    api_url, api_key = env

    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")

    description = args.get("description")
    if not description or not str(description).strip():
        return tool_error("description is required")

    assignee_name = args.get("assignee_name")
    if not assignee_name or not str(assignee_name).strip():
        return tool_error("assignee_name is required")

    parent_issue_id = args.get("parent_issue_id")

    company_id, err = _resolve_company_id(api_url, api_key)
    if err:
        return tool_error(f"paperclip_create_issue: {err}")

    assignee_agent_id, err = _resolve_assignee_agent_id(
        api_url, api_key, company_id, str(assignee_name)
    )
    if err:
        return tool_error(f"paperclip_create_issue: {err}")

    body = {
        "title": str(title),
        "description": str(description),
        "assigneeAgentId": assignee_agent_id,
    }
    if parent_issue_id:
        body["parentId"] = str(parent_issue_id)

    # Deliberately no "status" field — the server defaults it to "todo"
    # because assigneeAgentId is set, which is what triggers the server-side
    # auto-wake. Passing an explicit status here could set "backlog" and
    # silently skip the wake.
    try:
        resp = httpx.post(
            f"{api_url}/api/companies/{company_id}/issues",
            json=body,
            headers=_auth_headers(api_key),
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        return tool_error(
            f"paperclip_create_issue: request to PaperClip failed ({type(e).__name__})"
        )

    if not (200 <= resp.status_code < 300):
        return tool_error(
            f"paperclip_create_issue failed: HTTP {resp.status_code}",
            detail=_response_detail(resp),
        )

    try:
        data = resp.json()
    except ValueError:
        return tool_error("paperclip_create_issue: response was not valid JSON")

    return _ok(
        issue_id=data.get("id") if isinstance(data, dict) else None,
        identifier=data.get("identifier") if isinstance(data, dict) else None,
        status=data.get("status") if isinstance(data, dict) else None,
        assignee_agent_id=assignee_agent_id,
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

PAPERCLIP_SET_DISPOSITION_SCHEMA = {
    "name": "paperclip_set_disposition",
    "description": (
        "Record the mandatory final disposition for a PaperClip issue you "
        "were woken to work — the last thing every woken run must do. "
        "Mirrors `PATCH /api/issues/{id}` with an X-Paperclip-Run-Id header. "
        "A stale/wrong run_id 409s with an 'Issue run ownership conflict' — "
        "re-fetch the issue and retry with its current run id if that "
        "happens. This is the only way to attach a disposition comment; "
        "there is no separate comment tool, so don't call this more than "
        "once per turn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "issue_id": {
                "type": "string",
                "description": "The issue id from the wake payload.",
            },
            "run_id": {
                "type": "string",
                "description": (
                    "The run id from the wake payload. Sent as "
                    "X-Paperclip-Run-Id; a stale or wrong value 409s."
                ),
            },
            "status": {
                "type": "string",
                "enum": sorted(_DISPOSITION_STATUSES),
                "description": "Final disposition status for the issue.",
            },
            "comment": {
                "type": "string",
                "description": (
                    "Evidence: what you did, artifacts produced, decisions "
                    "made. This is the durable record of the run."
                ),
            },
        },
        "required": ["issue_id", "run_id", "status", "comment"],
    },
}

PAPERCLIP_CREATE_ISSUE_SCHEMA = {
    "name": "paperclip_create_issue",
    "description": (
        "Create a PaperClip issue assigned to another agent — for "
        "delegate/consult routing, or any 'hand this to another agent, "
        "tracked' need. Resolves the target agent's name to its id "
        "internally; you never need a separate lookup call. The "
        "assignment itself wakes the assignee server-side — no follow-up "
        "call needed or possible."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short issue title.",
            },
            "description": {
                "type": "string",
                "description": "Full description of the work for the assignee.",
            },
            "assignee_name": {
                "type": "string",
                "description": (
                    "The target agent's name (e.g. 'skills-consultant'). "
                    "Resolved to its PaperClip agent id internally."
                ),
            },
            "parent_issue_id": {
                "type": "string",
                "description": (
                    "Optional. Set when delegating a sub-piece of the issue "
                    "you're already working on — projectId is inherited "
                    "from it automatically."
                ),
            },
        },
        "required": ["title", "description", "assignee_name"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="paperclip_set_disposition",
    toolset="paperclip",
    schema=PAPERCLIP_SET_DISPOSITION_SCHEMA,
    handler=_handle_set_disposition,
    check_fn=_check_paperclip_mode,
    emoji="📎",
)

registry.register(
    name="paperclip_create_issue",
    toolset="paperclip",
    schema=PAPERCLIP_CREATE_ISSUE_SCHEMA,
    handler=_handle_create_issue,
    check_fn=_check_paperclip_mode,
    emoji="📎",
)
