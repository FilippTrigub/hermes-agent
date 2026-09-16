"""Control-plane FastAPI service for the frank-ingest campaign integration.

Why this is a standalone service and not a dashboard plugin
-------------------------------------------------------------
Hermes's plugin system (``hermes_cli/plugins.py``) lets a plugin register
tools, CLI commands, platform adapters, and dashboard *auth providers* — but
it has no facility for a plugin to add a brand-new FastAPI route. The one
existing precedent for "an external service authenticates with a shared
secret and calls a Hermes HTTP endpoint" — ``plugins/dashboard_auth/drain/``
— only supplies the auth *provider* for a route that is itself defined in
core ``hermes_cli/web_server.py`` (``/api/gateway/drain``). Confirmed by
grepping ``web_server.py`` for that route: it's a core route, registered as
token-authable via ``register_token_route``, not something the plugin itself
added.

So rather than editing the large, actively-upstreamed ``web_server.py`` core
file (this repo tracks ``upstream`` NousResearch/hermes-agent and we want
frank-specific changes to stay out of the way of future merges), this is its
own small FastAPI app, run as its own s6-supervised process
(``docker/s6-rc.d/frank-control/``), that imports Hermes's internal
``hermes_cli.profiles`` / ``hermes_cli.config`` functions directly — the same
functions the dashboard's own ``POST /api/profiles`` handler uses.

Two endpoints, one shared bearer secret
----------------------------------------
Both routes are gated by a single ``FRANK_PROVISION_SECRET`` (entropy-gated,
``hmac.compare_digest`` on the request path — mirrors
``plugins/dashboard_auth/drain/__init__.py``'s bar, reimplemented locally
here since this process doesn't load the dashboard's plugin/auth framework):

  POST /frank/provision  — create + start a campaign's admin+volunteer profiles
  POST /frank/chat       — reverse-proxy a chat-completions call to the
                            resolved campaign+role profile's own API server

frank-ingest only ever needs this one shared secret and this one base URL —
Hermes is the single source of truth for per-profile ports/keys; nothing is
duplicated in frank-ingest's own database.
"""
from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger("frank_control")

app = FastAPI(title="frank-control")

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_MIN_SECRET_CHARS = 43  # ~256 bits of url-safe-base64, matches the drain plugin's bar


def _secret() -> str:
    value = os.environ.get("FRANK_PROVISION_SECRET", "").strip()
    if not value:
        raise HTTPException(
            status_code=503,
            detail="FRANK_PROVISION_SECRET is not configured on this deployment",
        )
    if len(value) < _MIN_SECRET_CHARS:
        raise HTTPException(
            status_code=503,
            detail=f"FRANK_PROVISION_SECRET is too short (need >= {_MIN_SECRET_CHARS} chars)",
        )
    return value


def _check_auth(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    token = token.strip() if scheme.lower() == "bearer" else ""
    if not token or not hmac.compare_digest(token.encode("utf-8"), _secret().encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthenticated")


# ---------------------------------------------------------------------------
# Profile naming, ports, and per-role tool allowlists
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
_ROLES = ("admin", "volunteer")
_PORT_RANGE = range(21000, 29000)

# Kept in sync with frank-ingest's mcp/server.ts tool registration. Excludes
# the 3 operator-only pipeline tools (create_upload/run_pipeline/get_source_status)
# since a campaign-scoped MCP key rejects those outright (requireOperatorScope).
ADMIN_TOOLS = [
    "get_voters_schema", "query_voters",
    "add_diary_entry", "add_contact", "get_contacts", "add_finance_record",
    "get_field_status", "get_precinct",
    "analyze_voters", "analyze_results",
    "list_target_groups", "get_target_group", "create_target_group", "update_target_group",
    "generate_lists", "get_list_generation_job",
    "assign_list", "record_voter_result", "get_volunteers", "distribute_lists",
    # Registered on frank-ingest's MCP server 2026-09-16. They had been declared in
    # lib/admin-assistant-tools.ts since 2026-06-26 and never registered, so the admin
    # prompt used to say the assistant had no way to set a goal or a target.
    "add_goal", "complete_goal", "who_am_i",
]
VOLUNTEER_TOOLS = ["get_my_lists", "get_list_voters", "submit_result"]

# The Hermes-side toolset allowlist, which is a different thing from the MCP
# allowlists above and guards a much larger hole.
#
# Hermes resolves a profile's tools per *platform*. frank_control drives the
# `api_server` platform (the OpenAI-compatible gateway that /frank/chat proxies
# to), and when a profile's config.yaml has no `platform_toolsets` key at all,
# hermes_cli/tools_config.py falls through to that platform's default toolset --
# `hermes-api-server`, which grants `terminal`, `execute_code`, `write_file`,
# `patch`, `delegate_task`, `cronjob` and full browser automation.
#
# Every campaign's profile directory lives on one shared VM, and each profile's
# .env holds that campaign's MCP API key plus the Azure model key. So the
# fall-through gave every campaign's assistant -- including the VOLUNTEER role,
# the least-trusted user in the product -- a shell capable of reading every other
# campaign's credentials. Hermes's own approval engine does not close this on
# this path: register_gateway_notify is wired only in _handle_runs
# (gateway/platforms/api_server.py), not in the _run_agent path that serves
# /v1/chat/completions, so a gated call has nobody to prompt and soft-denies
# into the model's context instead.
#
# These three are local-state only: no shell, no filesystem, no network egress,
# no code execution. MCP servers are resolved separately from toolsets (see
# include_default_mcp_servers in tools_config.py), so naming an explicit list
# here does not disturb ADMIN_TOOLS/VOLUNTEER_TOOLS above -- verified live
# before this went in.
FRANK_PLATFORM_TOOLSETS = ["memory", "todo", "session_search"]

# Toolset groups that must never resolve for a frank profile. Named here rather
# than left implicit so the test has something to assert against, and so that a
# future edit widening FRANK_PLATFORM_TOOLSETS fails loudly.
FRANK_FORBIDDEN_TOOLSETS = frozenset({
    "terminal", "code_execution", "file", "browser",
    "delegation", "cronjob", "skills", "web", "vision", "image_gen",
})


def _validate_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise HTTPException(status_code=400, detail=f"invalid campaign_slug: {slug!r}")
    return slug


def _profile_name(campaign_slug: str, role: str) -> str:
    if role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"invalid role: {role!r}")
    return f"frank-{campaign_slug}-{role}"


def _profiles_root() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "profiles"


def _used_ports() -> set[int]:
    used: set[int] = set()
    root = _profiles_root()
    if not root.is_dir():
        return used
    for env_path in root.glob("*/.env"):
        try:
            text = env_path.read_text(errors="ignore")
        except OSError:
            continue
        match = re.search(r"^API_SERVER_PORT=(\d+)", text, re.MULTILINE)
        if match:
            used.add(int(match.group(1)))
    return used


def _allocate_port() -> int:
    used = _used_ports()
    for port in _PORT_RANGE:
        if port not in used:
            return port
    raise HTTPException(status_code=500, detail="no free port in range")


def _read_env(profile_dir: Path) -> dict[str, str]:
    env_path = profile_dir / ".env"
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def _write_env(profile_dir: Path, values: dict[str, str]) -> None:
    """Upsert KEY=value lines into a profile's .env, preserving other lines."""
    env_path = profile_dir / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    remaining = dict(values)
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out) + "\n")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


def _write_soul(profile_dir: Path, role: str, campaign_slug: str) -> None:
    template_path = Path(__file__).parent / "templates" / role / "SOUL.md"
    text = template_path.read_text().replace("{{CAMPAIGN_SLUG}}", campaign_slug)
    (profile_dir / "SOUL.md").write_text(text)


def _refresh_profile(profile_dir: Path, role: str, campaign_slug: str) -> None:
    """Rewrite the parts of an existing profile that are pure policy.

    Provisioning short-circuits on ``profile_exists``, so a profile is written once and never
    again. That is correct for everything that carries state, and wrong for everything that is
    just a rendering of what is in this repo: the role's prompt, the MCP tool allowlist and the
    platform toolsets. Before this existed, a prompt or tool change reached only campaigns
    created after it, and the eight live profiles had to be edited by hand on the VM.

    Deliberately untouched, because each holds state a rewrite would destroy:

    * ``.env`` — ``API_SERVER_PORT`` (rewriting it hands out a new port while the running
      gateway still listens on the old one, and /frank/chat reads the port back from here),
      ``API_SERVER_KEY`` (the bearer the proxy presents) and ``AZURE_FOUNDRY_API_KEY``.
    * the MCP ``x-api-key`` header — every provision call from frank-ingest mints a fresh
      campaign key, so rewriting it on a refresh would burn a key row per call.
    * ``model`` — pinned per profile at creation on purpose; see docs/azure-deployment.md.
    * ``memories/``, ``sessions/``, the databases, and the gateway's own runtime files.

    No gateway restart is needed. /frank/chat proxies to the profile's api_server, which builds
    a fresh agent per request: config.yaml is re-read through an mtime-keyed cache and SOUL.md is
    read off disk by load_soul_md each time. Only .env is read at process start, and .env is
    exactly what this does not touch.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.config import load_config, save_config

    _write_soul(profile_dir, role, campaign_slug)

    tools_include = ADMIN_TOOLS if role == "admin" else VOLUNTEER_TOOLS
    token = set_hermes_home_override(str(profile_dir))
    try:
        cfg = load_config()
        server = cfg.setdefault("mcp_servers", {}).get("frank-ingest")
        if isinstance(server, dict):
            server.setdefault("tools", {})["include"] = list(tools_include)
        cfg.setdefault("platform_toolsets", {})["api_server"] = list(FRANK_PLATFORM_TOOLSETS)
        save_config(cfg)
    finally:
        reset_hermes_home_override(token)


def _write_mcp_and_model(
    profile_dir: Path, *, mcp_url: str, mcp_api_key: str, tools_include: list[str]
) -> None:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.config import load_config, save_config
    from hermes_cli.mcp_security import validate_mcp_server_entry

    token = set_hermes_home_override(str(profile_dir))
    try:
        cfg = load_config()

        entry = {
            "url": mcp_url,
            "headers": {"x-api-key": mcp_api_key},
            "tools": {"include": tools_include},
        }
        issues = validate_mcp_server_entry("frank-ingest", entry)
        if issues:
            raise HTTPException(
                status_code=500, detail=f"MCP entry rejected: {'; '.join(issues)}"
            )
        cfg.setdefault("mcp_servers", {})["frank-ingest"] = entry

        # Explicit, so the api_server platform never falls through to the
        # `hermes-api-server` default toolset. See FRANK_PLATFORM_TOOLSETS.
        cfg.setdefault("platform_toolsets", {})["api_server"] = list(
            FRANK_PLATFORM_TOOLSETS
        )

        provider = os.environ.get("FRANK_MODEL_PROVIDER", "").strip()
        model = os.environ.get("FRANK_MODEL_NAME", "").strip()
        if provider and model:
            from hermes_cli.web_server import (
                _apply_main_model_assignment,
                _normalize_main_model_assignment,
            )

            base_url = os.environ.get("FRANK_MODEL_BASE_URL", "")
            api_key = os.environ.get("FRANK_MODEL_API_KEY", "")
            norm_provider, norm_model = _normalize_main_model_assignment(provider, model)
            cfg["model"] = _apply_main_model_assignment(
                cfg.get("model", {}), norm_provider, norm_model,
                base_url=base_url, api_key=api_key,
            )

            # azure-foundry's own runtime credential resolver
            # (hermes_cli/runtime_provider.py's _resolve_azure_foundry_runtime)
            # reads the key from AZURE_FOUNDRY_API_KEY in the profile's own
            # .env / process env, not from config.yaml's model.api_key above
            # — writing only to config.yaml leaves every provisioned profile
            # one gateway restart away from "Azure Foundry requires an API
            # key" on its first real chat request.
            if norm_provider == "azure-foundry" and api_key:
                _write_env(profile_dir, {"AZURE_FOUNDRY_API_KEY": api_key})

        save_config(cfg)
    finally:
        reset_hermes_home_override(token)


def _start_gateway(profile_name: str) -> None:
    cmd = [sys.executable, "-m", "hermes_cli.main", "-p", profile_name, "gateway", "start"]
    subprocess.run(
        cmd,
        check=True,
        env={**os.environ, "HERMES_NONINTERACTIVE": "1"},
        timeout=60,
    )


# ---------------------------------------------------------------------------
# POST /frank/provision
# ---------------------------------------------------------------------------

class ProvisionRequest(BaseModel):
    campaign_id: str
    campaign_slug: str
    mcp_url: str
    mcp_api_key: str
    # Rewrite an existing profile's prompt and tool allowlist instead of skipping it.
    # Off by default, so both of frank-ingest's callers keep the no-op semantics they
    # rely on: lib/hermes-chat.ts calls provision on every "not provisioned" error and
    # mints a fresh campaign MCP key each time, and coupling a prompt refresh to that
    # would burn a key row per refresh.
    refresh: bool = False


@app.post("/frank/provision")
async def provision(body: ProvisionRequest, request: Request) -> dict[str, Any]:
    _check_auth(request)
    from hermes_cli import profiles as profiles_mod

    slug = _validate_slug(body.campaign_slug)
    created: dict[str, Any] = {}
    for role in _ROLES:
        name = _profile_name(slug, role)
        if profiles_mod.profile_exists(name):
            if body.refresh:
                _refresh_profile(profiles_mod.get_profile_dir(name), role, slug)
                created[role] = {"name": name, "already_existed": True, "refreshed": True}
            else:
                created[role] = {"name": name, "already_existed": True}
            # No _start_gateway here either way: the profile's gateway is already running,
            # and a refresh needs no restart to be picked up (see _refresh_profile).
            continue

        path = profiles_mod.create_profile(
            name=name,
            clone_from=None,
            clone_config=False,
            no_skills=True,
            description=f"Frank campaign {slug} ({role})",
        )
        _write_soul(path, role, slug)
        tools = ADMIN_TOOLS if role == "admin" else VOLUNTEER_TOOLS
        _write_mcp_and_model(
            path, mcp_url=body.mcp_url, mcp_api_key=body.mcp_api_key, tools_include=tools
        )
        port = _allocate_port()
        _write_env(path, {
            "API_SERVER_HOST": "127.0.0.1",
            "API_SERVER_PORT": str(port),
            "API_SERVER_KEY": secrets.token_urlsafe(32),
        })
        _start_gateway(name)
        created[role] = {"name": name, "already_existed": False, "port": port}

    return {"ok": True, "campaign_id": body.campaign_id, "profiles": created}


# ---------------------------------------------------------------------------
# POST /frank/chat — reverse proxy to the resolved profile's own API server
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    campaign_slug: str
    role: str
    # Trusted, server-derived identity — e.g. f"web:{userId}". Passed through
    # as Hermes's own X-Hermes-Session-Key so conversation memory is isolated
    # per (campaign, role, user) without any frank-ingest-side storage. Never
    # populate this from anything the browser could influence beyond the
    # already-authenticated session's own user id.
    session_key: str
    body: dict[str, Any]


@app.post("/frank/chat")
async def chat(payload: ChatRequest, request: Request):
    _check_auth(request)
    slug = _validate_slug(payload.campaign_slug)
    name = _profile_name(slug, payload.role)

    from hermes_cli import profiles as profiles_mod

    if not profiles_mod.profile_exists(name):
        raise HTTPException(status_code=404, detail=f"profile {name} not provisioned")
    profile_dir = profiles_mod.get_profile_dir(name)
    env = _read_env(profile_dir)
    port = env.get("API_SERVER_PORT")
    api_key = env.get("API_SERVER_KEY")
    if not port or not api_key:
        raise HTTPException(
            status_code=409, detail=f"profile {name} has no API server configured"
        )

    upstream_url = f"http://127.0.0.1:{port}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Hermes-Session-Key": payload.session_key,
    }
    stream = bool(payload.body.get("stream"))

    if not stream:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(upstream_url, json=payload.body, headers=headers)
        return resp.json()

    async def _relay():
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", upstream_url, json=payload.body, headers=headers) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(_relay(), media_type="text/event-stream")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}
