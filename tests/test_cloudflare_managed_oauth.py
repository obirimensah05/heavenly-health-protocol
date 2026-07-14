from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from heavenly_health.cloudflare_managed_oauth import (
    CloudflareAccessJWTMiddleware,
    CloudflareAccessJWTVerifier,
    CloudflareManagedOAuthError,
    CloudflareManagedOAuthSettings,
    configure_runtime_from_access_assertion,
)


AUDIENCE = "a" * 64
TEAM_DOMAIN = "https://team.cloudflareaccess.com"
PUBLIC_HOST = "health-mcp.example.com"
OWNER_EMAIL = "owner@example.com"


def managed_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "HEAVENLY_CLOUDFLARE_TEAM_DOMAIN": TEAM_DOMAIN,
        "HEAVENLY_CLOUDFLARE_ACCESS_AUDIENCE": AUDIENCE,
        "HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS": OWNER_EMAIL,
        "HEAVENLY_MCP_PUBLIC_HOST": PUBLIC_HOST,
    }
    environment.update(overrides)
    return environment


def test_managed_oauth_settings_are_all_or_nothing_and_reject_unsafe_origins() -> None:
    assert CloudflareManagedOAuthSettings.from_environ({}) is None
    assert (
        CloudflareManagedOAuthSettings.from_environ(
            {"HEAVENLY_MCP_PUBLIC_HOST": PUBLIC_HOST}
        )
        is None
    )

    with pytest.raises(CloudflareManagedOAuthError, match="Incomplete Cloudflare Managed OAuth"):
        CloudflareManagedOAuthSettings.from_environ(
            {"HEAVENLY_CLOUDFLARE_TEAM_DOMAIN": TEAM_DOMAIN}
        )

    for unsafe in (
        "http://team.cloudflareaccess.com",
        "https://team.cloudflareaccess.com/path",
        "https://team.cloudflareaccess.com.evil.example",
        "https://user@team.cloudflareaccess.com",
        "https://127.0.0.1",
    ):
        with pytest.raises(CloudflareManagedOAuthError, match="Cloudflare Access team origin"):
            CloudflareManagedOAuthSettings.from_environ(
                managed_environment(HEAVENLY_CLOUDFLARE_TEAM_DOMAIN=unsafe)
            )


def test_managed_oauth_settings_normalize_owner_email_without_exposing_it() -> None:
    settings = CloudflareManagedOAuthSettings.from_environ(
        managed_environment(HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS=" OWNER@EXAMPLE.COM ")
    )

    assert settings is not None
    assert settings.allowed_emails == frozenset({OWNER_EMAIL})
    assert OWNER_EMAIL not in repr(settings)
    assert AUDIENCE not in repr(settings)
    assert settings.jwks_url == f"{TEAM_DOMAIN}/cdn-cgi/access/certs"


def _signed_token(
    private_key: rsa.RSAPrivateKey,
    *,
    audience: str = AUDIENCE,
    issuer: str = TEAM_DOMAIN,
    email: str = OWNER_EMAIL,
    token_type: str = "app",
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "aud": [audience],
            "email": email,
            "exp": now + timedelta(minutes=5),
            "iat": now,
            "nbf": now - timedelta(seconds=1),
            "iss": issuer,
            "sub": "owner-subject",
            "type": token_type,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )


def _client() -> tuple[TestClient, rsa.RSAPrivateKey]:
    settings = CloudflareManagedOAuthSettings.from_environ(managed_environment())
    assert settings is not None
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = CloudflareAccessJWTVerifier(
        settings,
        signing_key_resolver=lambda _token: private_key.public_key(),
    )

    async def status(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", status, methods=["GET", "POST"])])
    wrapped = CloudflareAccessJWTMiddleware(app, verifier)
    return TestClient(wrapped), private_key


def test_loopback_requests_remain_available_without_cloudflare_headers() -> None:
    client, _private_key = _client()

    response = client.get("http://127.0.0.1:8791/mcp")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_public_host_requires_a_verified_cloudflare_access_assertion() -> None:
    client, private_key = _client()

    missing = client.get(f"https://{PUBLIC_HOST}/mcp")
    valid = client.get(
        f"https://{PUBLIC_HOST}/mcp",
        headers={"Cf-Access-Jwt-Assertion": _signed_token(private_key)},
    )

    assert missing.status_code == 403
    assert missing.json() == {"error": "Cloudflare Access authentication failed"}
    assert valid.status_code == 200


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("audience", "b" * 64),
        ("issuer", "https://other.cloudflareaccess.com"),
        ("email", "not-owner@example.com"),
        ("token_type", "org"),
    ],
)
def test_public_host_rejects_wrong_audience_issuer_identity_or_token_type(
    claim: str,
    value: str,
) -> None:
    client, private_key = _client()
    token = _signed_token(private_key, **{claim: value})

    response = client.get(
        f"https://{PUBLIC_HOST}/mcp",
        headers={"Cf-Access-Jwt-Assertion": token},
    )

    assert response.status_code == 403
    assert response.json() == {"error": "Cloudflare Access authentication failed"}


def test_spoofed_cloudflare_forwarding_header_also_requires_an_assertion() -> None:
    client, _private_key = _client()

    response = client.get(
        "http://127.0.0.1:8791/mcp",
        headers={"Cf-Connecting-Ip": "203.0.113.9"},
    )

    assert response.status_code == 403


def test_verified_access_assertion_bootstraps_protected_runtime_settings(tmp_path) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assertion = tmp_path / "access.jwt"
    assertion.write_text(_signed_token(private_key))
    assertion.chmod(0o600)
    runtime = tmp_path / "runtime.env"
    runtime.write_text("SUPABASE_URL=https://private.example\n# keep this comment\n")
    runtime.chmod(0o600)

    configured = configure_runtime_from_access_assertion(
        assertion,
        public_host=PUBLIC_HOST,
        runtime_path=runtime,
        verifier_factory=lambda settings: CloudflareAccessJWTVerifier(
            settings,
            signing_key_resolver=lambda _token: private_key.public_key(),
        ),
    )

    contents = runtime.read_text()
    assert configured == runtime
    assert "SUPABASE_URL=https://private.example" in contents
    assert "# keep this comment" in contents
    assert f'HEAVENLY_CLOUDFLARE_TEAM_DOMAIN="{TEAM_DOMAIN}"' in contents
    assert f'HEAVENLY_CLOUDFLARE_ACCESS_AUDIENCE="{AUDIENCE}"' in contents
    assert f'HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS="{OWNER_EMAIL}"' in contents
    assert f'HEAVENLY_MCP_PUBLIC_HOST="{PUBLIC_HOST}"' in contents
    assert runtime.stat().st_mode & 0o777 == 0o600
