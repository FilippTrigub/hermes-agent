"""Refreshing an existing profile's prompt and tool allowlist.

``/frank/provision`` short-circuits on ``profile_exists``, so a profile is written once at
creation and never again. That is right for everything holding state and wrong for everything
that is only a rendering of what is in this repo: the role's prompt, the MCP tool allowlist and
the platform toolsets. Before ``_refresh_profile`` existed, a prompt change reached only
campaigns created after it, and the eight live profiles had to be edited by hand on the VM.

The tests that matter here are the negative ones. A refresh that quietly rewrote ``.env`` would
hand out a new API_SERVER_PORT while the running gateway still listened on the old one, and one
that rewrote the MCP header would burn a campaign key row on every call.
"""
import yaml

from frank_control.app import (
    ADMIN_TOOLS,
    FRANK_PLATFORM_TOOLSETS,
    VOLUNTEER_TOOLS,
    _refresh_profile,
)

EXISTING_KEY = "campaign-key-written-at-creation"


def _make_profile(tmp_path, *, with_toolsets: bool):
    """A profile directory as it exists on the VM, optionally predating the toolset fix."""
    profile = tmp_path / "frank-abc-admin"
    profile.mkdir()

    cfg = {
        "mcp_servers": {
            "frank-ingest": {
                "url": "https://app.example.invalid/api/mcp",
                "headers": {"x-api-key": EXISTING_KEY},
                "tools": {"include": ["get_field_status"]},
            }
        },
        # The canonical shape hermes_cli.config normalizes to on load.
        "model": {"provider": "azure-foundry", "default": "gpt-5.6-luna"},
    }
    if with_toolsets:
        cfg["platform_toolsets"] = {"api_server": list(FRANK_PLATFORM_TOOLSETS)}
    (profile / "config.yaml").write_text(yaml.safe_dump(cfg))

    (profile / ".env").write_text(
        "API_SERVER_HOST=127.0.0.1\nAPI_SERVER_PORT=21001\n"
        "API_SERVER_KEY=bearer-the-proxy-presents\nAZURE_FOUNDRY_API_KEY=model-key\n"
    )
    (profile / "SOUL.md").write_text("# stale prompt, written at creation\n")
    return profile


def _config(profile):
    return yaml.safe_load((profile / "config.yaml").read_text())


def test_rewrites_the_prompt_from_the_template(tmp_path):
    profile = _make_profile(tmp_path, with_toolsets=True)

    _refresh_profile(profile, "admin", "acme-2026")

    soul = (profile / "SOUL.md").read_text()
    assert "stale prompt" not in soul
    assert "acme-2026" in soul
    assert "{{CAMPAIGN_SLUG}}" not in soul


def test_brings_the_tool_allowlist_up_to_date(tmp_path):
    profile = _make_profile(tmp_path, with_toolsets=True)

    _refresh_profile(profile, "admin", "acme-2026")

    include = _config(profile)["mcp_servers"]["frank-ingest"]["tools"]["include"]
    assert include == ADMIN_TOOLS
    # The three that were declared for months and only registered on 2026-09-16.
    assert {"add_goal", "complete_goal", "who_am_i"} <= set(include)


def test_a_volunteer_profile_gets_the_volunteer_list(tmp_path):
    profile = _make_profile(tmp_path, with_toolsets=True)

    _refresh_profile(profile, "volunteer", "acme-2026")

    include = _config(profile)["mcp_servers"]["frank-ingest"]["tools"]["include"]
    assert include == VOLUNTEER_TOOLS
    assert "add_goal" not in include


def test_repairs_a_profile_that_predates_the_toolset_allowlist(tmp_path):
    """The regression this whole path exists for.

    A profile created before the 2026-09-16 security fix has no ``platform_toolsets`` key at
    all, which falls through to the ``hermes-api-server`` default and its shell.
    """
    profile = _make_profile(tmp_path, with_toolsets=False)

    _refresh_profile(profile, "admin", "acme-2026")

    assert _config(profile)["platform_toolsets"]["api_server"] == FRANK_PLATFORM_TOOLSETS


def test_leaves_the_env_file_untouched(tmp_path):
    profile = _make_profile(tmp_path, with_toolsets=True)
    before = (profile / ".env").read_bytes()

    _refresh_profile(profile, "admin", "acme-2026")

    assert (profile / ".env").read_bytes() == before


def test_keeps_the_campaign_key_and_the_pinned_model(tmp_path):
    profile = _make_profile(tmp_path, with_toolsets=True)
    model_before = _config(profile).get("model")

    _refresh_profile(profile, "admin", "acme-2026")

    cfg = _config(profile)
    # Every provision call from frank-ingest mints a fresh campaign MCP key, so a refresh that
    # rewrote this header would burn a key row each time it ran.
    assert cfg["mcp_servers"]["frank-ingest"]["headers"]["x-api-key"] == EXISTING_KEY
    assert cfg["mcp_servers"]["frank-ingest"]["url"] == "https://app.example.invalid/api/mcp"
    # The model is pinned per profile at creation on purpose (docs/azure-deployment.md), so a
    # refresh must not migrate a live campaign onto a different one. Note the round trip does
    # apply Hermes's own config normalization — it stamps `_config_version` and rewrites a
    # legacy `model.name` to `model.default` — which is a format migration, not a model change.
    assert cfg.get("model") == model_before
    assert cfg["model"]["default"] == "gpt-5.6-luna"
