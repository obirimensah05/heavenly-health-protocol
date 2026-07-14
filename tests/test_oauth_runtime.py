"""OAuth runtime configuration is deliberately safe to inspect and log."""

from __future__ import annotations

from cryptography.fernet import Fernet
import pytest

from heavenly_health.oauth_runtime import OAuthRuntimeSettings, OAuthSettingsError


REQUIRED_OAUTH_ENV = {
    "HEAVENLY_OIDC_CONFIG_URL": "https://team.cloudflareaccess.com/cdn-cgi/access/sso/oidc/test-client/.well-known/openid-configuration",
    "HEAVENLY_OIDC_CLIENT_ID": "test-client-id",
    "HEAVENLY_OIDC_CLIENT_SECRET": "test-client-secret-not-for-production",
    "HEAVENLY_OIDC_AUDIENCE": "test-access-audience",
    "HEAVENLY_MCP_BASE_URL": "https://mcp.example.test",
    "HEAVENLY_OAUTH_JWT_SIGNING_KEY": "test-jwt-signing-key-not-for-production",
    "HEAVENLY_OAUTH_ENCRYPTION_KEY": Fernet.generate_key().decode(),
}


def oauth_environ(**overrides: str | None) -> dict[str, str]:
    environ = REQUIRED_OAUTH_ENV.copy()
    for name, value in overrides.items():
        if value is None:
            environ.pop(name, None)
        else:
            environ[name] = value
    return environ


def test_no_oauth_environment_selects_local_mode() -> None:
    settings = OAuthRuntimeSettings.from_environ({})

    assert settings is None


def test_partial_oauth_environment_is_rejected_without_leaking_values() -> None:
    environment = oauth_environ(HEAVENLY_OAUTH_ENCRYPTION_KEY=None)

    with pytest.raises(OAuthSettingsError) as exc_info:
        OAuthRuntimeSettings.from_environ(environment)

    message = str(exc_info.value)
    assert "HEAVENLY_OAUTH_ENCRYPTION_KEY" in message
    for value in environment.values():
        assert value not in message


def test_complete_oauth_environment_validates_and_never_serializes_secrets() -> None:
    settings = OAuthRuntimeSettings.from_environ(oauth_environ())

    assert settings is not None
    assert settings.oidc_config_url == REQUIRED_OAUTH_ENV["HEAVENLY_OIDC_CONFIG_URL"]
    assert settings.mcp_base_url == REQUIRED_OAUTH_ENV["HEAVENLY_MCP_BASE_URL"]
    safe = settings.safe_summary()
    rendered = repr(settings)
    assert safe == {
        "mode": "oauth",
        "oidc_config_url_configured": True,
        "client_id_configured": True,
        "client_secret_configured": True,
        "audience_configured": True,
        "mcp_base_url": "https://mcp.example.test",
        "jwt_signing_key_configured": True,
        "encryption_key_configured": True,
    }
    for name in (
        "HEAVENLY_OIDC_CLIENT_ID",
        "HEAVENLY_OIDC_CLIENT_SECRET",
        "HEAVENLY_OAUTH_JWT_SIGNING_KEY",
        "HEAVENLY_OAUTH_ENCRYPTION_KEY",
    ):
        assert REQUIRED_OAUTH_ENV[name] not in rendered
        assert REQUIRED_OAUTH_ENV[name] not in str(safe)


@pytest.mark.parametrize(
    ("jwt_signing_key", "expected"),
    [
        ("short-key", "32 non-whitespace"),
        (" " * 32, "Incomplete OAuth configuration"),
        ("x" * 31, "32 non-whitespace"),
    ],
)
def test_oauth_rejects_trivially_weak_jwt_signing_keys_without_leaking_them(
    jwt_signing_key: str,
    expected: str,
) -> None:
    with pytest.raises(OAuthSettingsError, match=expected) as exc_info:
        OAuthRuntimeSettings.from_environ(
            oauth_environ(HEAVENLY_OAUTH_JWT_SIGNING_KEY=jwt_signing_key)
        )

    assert jwt_signing_key not in str(exc_info.value)


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("HEAVENLY_OIDC_CONFIG_URL", "http://access.example.test/openid", "HTTPS"),
        ("HEAVENLY_MCP_BASE_URL", "http://mcp.example.test", "HTTPS"),
        ("HEAVENLY_MCP_BASE_URL", "https://mcp.example.test/mcp", "origin"),
        ("HEAVENLY_MCP_BASE_URL", "https://localhost:8791", "public hostname"),
        ("HEAVENLY_MCP_BASE_URL", "https://127.0.0.1:8791", "public hostname"),
        ("HEAVENLY_MCP_BASE_URL", "https://10.0.0.1:8791", "public hostname"),
        ("HEAVENLY_MCP_BASE_URL", "https://mcp_example.test", "valid hostname"),
        ("HEAVENLY_MCP_BASE_URL", "https://single-label", "public hostname"),
        ("HEAVENLY_MCP_BASE_URL", "https://mcp.example.test:not-a-port", "numeric port"),
        ("HEAVENLY_OIDC_CONFIG_URL", "https://access.example.test:not-a-port/openid", "numeric port"),
        ("HEAVENLY_OAUTH_ENCRYPTION_KEY", "not-a-fernet-key", "Fernet"),
    ],
)
def test_oauth_urls_are_validated(name: str, value: str, expected: str) -> None:
    with pytest.raises(OAuthSettingsError, match=expected):
        OAuthRuntimeSettings.from_environ(oauth_environ(**{name: value}))


def test_oauth_requires_an_explicit_access_audience_without_leaking_values() -> None:
    environment = oauth_environ(HEAVENLY_OIDC_AUDIENCE=None)

    with pytest.raises(OAuthSettingsError, match="HEAVENLY_OIDC_AUDIENCE") as exc_info:
        OAuthRuntimeSettings.from_environ(environment)

    assert REQUIRED_OAUTH_ENV["HEAVENLY_OIDC_CLIENT_SECRET"] not in str(exc_info.value)


@pytest.mark.parametrize(
    "discovery_url",
    [
        "https://access.example.test/.well-known/openid-configuration",
        "https://team.cloudflareaccess.com/.well-known/openid-configuration",
        "https://team.cloudflareaccess.com/cdn-cgi/access/sso/oidc/test-client/not-openid.json",
        "https://team.cloudflareaccess.com/cdn-cgi/access/sso/oidc/test-client/extra/.well-known/openid-configuration",
        "https://127.0.0.1/cdn-cgi/access/sso/oidc/test-client/.well-known/openid-configuration",
    ],
)
def test_oauth_rejects_discovery_urls_outside_the_cloudflare_access_saas_boundary(
    discovery_url: str,
) -> None:
    with pytest.raises(OAuthSettingsError, match="Cloudflare Access SaaS"):
        OAuthRuntimeSettings.from_environ(
            oauth_environ(HEAVENLY_OIDC_CONFIG_URL=discovery_url)
        )


def test_oauth_state_dir_uses_explicit_absolute_or_native_xdg_path() -> None:
    explicit = OAuthRuntimeSettings.from_environ(
        {**oauth_environ(), "HEAVENLY_OAUTH_STATE_DIR": "/durable/oauth-state"}
    )
    xdg = OAuthRuntimeSettings.from_environ(
        {**oauth_environ(), "XDG_STATE_HOME": "/native-state"}
    )

    assert explicit is not None and str(explicit.oauth_state_dir) == "/durable/oauth-state"
    assert xdg is not None and str(xdg.oauth_state_dir) == "/native-state/heavenly/oauth"


def test_oauth_rejects_relative_state_dir() -> None:
    with pytest.raises(OAuthSettingsError, match="HEAVENLY_OAUTH_STATE_DIR"):
        OAuthRuntimeSettings.from_environ(
            {**oauth_environ(), "HEAVENLY_OAUTH_STATE_DIR": "relative/oauth"}
        )
