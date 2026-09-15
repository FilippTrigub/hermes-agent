"""The Hermes-side toolset allowlist written into every frank profile.

This is a security boundary, not a preference. Without an explicit
``platform_toolsets`` key a profile falls through to the ``hermes-api-server``
default toolset, which carries ``terminal``, ``execute_code``, ``write_file``
and friends. Every campaign's profile directory (and its ``.env``, holding that
campaign's MCP key and the shared Azure model key) lives on one VM, so the
fall-through let any campaign's assistant -- volunteer role included -- read
every other campaign's credentials.

The third test deliberately asserts that the *default* is dangerous. If Hermes
upstream ever makes ``hermes-api-server`` safe, that test fails and this whole
file can be reconsidered; until then it documents why the allowlist exists.
"""
from frank_control.app import (
    FRANK_FORBIDDEN_TOOLSETS,
    FRANK_PLATFORM_TOOLSETS,
)

PLATFORM = "api_server"


def _resolve(platform_toolsets):
    """Enabled toolset groups for a synthetic profile config."""
    from hermes_cli.tools_config import _get_platform_tools

    cfg = {"mcp_servers": {"frank-ingest": {"url": "https://example.invalid/api/mcp"}}}
    if platform_toolsets is not None:
        cfg["platform_toolsets"] = {PLATFORM: list(platform_toolsets)}
    return set(_get_platform_tools(cfg, PLATFORM))


def test_allowlist_names_nothing_forbidden():
    assert not set(FRANK_PLATFORM_TOOLSETS) & FRANK_FORBIDDEN_TOOLSETS


def test_allowlist_resolves_without_dangerous_toolsets():
    enabled = _resolve(FRANK_PLATFORM_TOOLSETS)
    assert not enabled & FRANK_FORBIDDEN_TOOLSETS


def test_allowlist_keeps_the_mcp_server():
    # Toolsets and MCP servers are resolved separately; restricting one must not
    # silently drop the other, which is the whole campaign tool surface.
    assert "frank-ingest" in _resolve(FRANK_PLATFORM_TOOLSETS)


def test_the_default_fallthrough_is_what_we_are_guarding_against():
    enabled = _resolve(None)
    dangerous = enabled & FRANK_FORBIDDEN_TOOLSETS
    assert "terminal" in dangerous
    assert "code_execution" in dangerous
