import json
from subprocess import CompletedProcess

import httpx
import pytest

from heavenly_health.cloudflare_access import (
    CloudflareAccessClient,
    CloudflareManagedOAuthClient,
    add_email_to_include_rules,
    load_cloudflare_api_token,
)


def managed_application(*, enabled: bool = False) -> dict[str, object]:
    registration: dict[str, object] = {
        "enabled": enabled,
        "allowed_uris": ["https://client.example/callback"],
    }
    oauth: dict[str, object] = {
        "enabled": enabled,
        "dynamic_client_registration": registration,
    }
    if enabled:
        registration.update(
            {
                "allow_any_on_localhost": True,
                "allow_any_on_loopback": True,
            }
        )
        oauth["grant"] = {
            "access_token_lifetime": "15m",
            "session_duration": "336h",
        }
    return {
        "id": "application-id",
        "name": "Heavenly Health MCP",
        "type": "self_hosted",
        "domain": "health-mcp.example.com",
        "aud": "a" * 64,
        "session_duration": "24h",
        "custom_deny_url": "https://example.com/denied",
        "policies": [
            {
                "id": "policy-id",
                "name": "Owner only",
                "decision": "allow",
                "include": [{"email": {"email": "owner@example.com"}}],
                "exclude": [],
                "require": [],
            }
        ],
        "oauth_configuration": oauth,
    }


def managed_client(handler) -> CloudflareManagedOAuthClient:
    return CloudflareManagedOAuthClient(
        account_id="account-id",
        application_id="application-id",
        api_token="test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_add_email_to_include_rules_preserves_existing_rules_and_deduplicates() -> None:
    rules = [
        {"email": {"email": "owner@example.com"}},
        {"email_domain": {"domain": "example.org"}},
    ]

    updated = add_email_to_include_rules(rules, "NEW.USER@example.com")
    duplicate = add_email_to_include_rules(updated, "new.user@EXAMPLE.com")

    assert updated == [
        {"email": {"email": "owner@example.com"}},
        {"email_domain": {"domain": "example.org"}},
        {"email": {"email": "new.user@example.com"}},
    ]
    assert duplicate == updated


def test_allow_email_updates_only_the_target_access_policy() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "id": "policy-id",
                        "name": "Initial private allowlist",
                        "decision": "allow",
                        "precedence": 1,
                        "include": [{"email": {"email": "owner@example.com"}}],
                        "exclude": [],
                        "require": [],
                    },
                },
            )
        if request.method == "PUT":
            payload = json.loads(request.content)
            assert payload["include"] == [
                {"email": {"email": "owner@example.com"}},
                {"email": {"email": "new.user@example.com"}},
            ]
            assert payload["decision"] == "allow"
            return httpx.Response(200, json={"success": True, "result": payload})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = CloudflareAccessClient(
        account_id="account-id",
        application_id="application-id",
        policy_id="policy-id",
        api_token="test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    changed = client.allow_email("new.user@example.com")

    assert changed is True
    assert len(requests) == 2
    assert str(requests[0].url).endswith("/accounts/account-id/access/apps/application-id/policies/policy-id")
    assert requests[1].headers["Authorization"] == "Bearer test-token"


def test_allow_email_fails_closed_for_a_non_allow_policy() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "policy-id",
                    "name": "Unsafe bypass",
                    "decision": "bypass",
                    "include": [],
                },
            },
        )

    client = CloudflareAccessClient(
        account_id="account-id",
        application_id="application-id",
        policy_id="policy-id",
        api_token="test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="must be an exact allow policy"):
        client.allow_email("new.user@example.com")

    assert [request.method for request in requests] == ["GET"]


def test_policy_summary_returns_only_validated_target_identity() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "id": "policy-id",
                    "name": "Private owner allowlist",
                    "decision": "allow",
                    "include": [],
                },
            },
        )

    client = CloudflareAccessClient(
        account_id="account-id",
        application_id="application-id",
        policy_id="policy-id",
        api_token="test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.policy_summary() == {
        "account_id": "account-id",
        "application_id": "application-id",
        "policy_id": "policy-id",
        "policy_name": "Private owner allowlist",
        "decision": "allow",
    }


def test_managed_oauth_plan_is_secret_free_and_validates_exact_owner_policy() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": managed_application()})

    plan = managed_client(handler).managed_oauth_plan("health-mcp.example.com")

    assert plan.summary() == {
        "application_id": "application-id",
        "application_name": "Heavenly Health MCP",
        "application_type": "self_hosted",
        "domain": "health-mcp.example.com",
        "managed_oauth_enabled": False,
        "exact_owner_identities": 1,
    }
    assert "owner@example.com" not in repr(plan)
    assert "a" * 64 not in repr(plan)


def test_enable_managed_oauth_preserves_application_and_adds_cli_redirect_support() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "result": managed_application()})
        payload = json.loads(request.content)
        assert payload["name"] == "Heavenly Health MCP"
        assert payload["custom_deny_url"] == "https://example.com/denied"
        assert payload["policies"][0]["name"] == "Owner only"
        oauth = payload["oauth_configuration"]
        assert oauth["enabled"] is True
        assert oauth["dynamic_client_registration"] == {
            "enabled": True,
            "allow_any_on_localhost": True,
            "allow_any_on_loopback": True,
            "allowed_uris": ["https://client.example/callback"],
        }
        assert oauth["grant"] == {
            "access_token_lifetime": "15m",
            "session_duration": "336h",
        }
        return httpx.Response(200, json={"success": True, "result": payload})

    changed = managed_client(handler).enable_managed_oauth("health-mcp.example.com")

    assert changed is True
    assert [request.method for request in requests] == ["GET", "PUT"]
    assert requests[1].headers["Authorization"] == "Bearer test-token"


def test_enable_managed_oauth_is_idempotent() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"success": True, "result": managed_application(enabled=True)},
        )

    changed = managed_client(handler).enable_managed_oauth("health-mcp.example.com")

    assert changed is False
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", "other-application", "different application"),
        ("type", "saas", "self-hosted"),
        ("domain", "other.example.com", "configured public host"),
    ],
)
def test_managed_oauth_refuses_the_wrong_application(
    field: str,
    value: str,
    message: str,
) -> None:
    application = managed_application()
    application[field] = value

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": application})

    with pytest.raises(RuntimeError, match=message):
        managed_client(handler).managed_oauth_plan("health-mcp.example.com")


@pytest.mark.parametrize(
    "policy",
    [
        {"id": "bad", "name": "Bypass", "decision": "bypass", "include": []},
        {
            "id": "bad",
            "name": "Everyone",
            "decision": "allow",
            "include": [{"everyone": {}}],
        },
        {
            "id": "bad",
            "name": "Domain",
            "decision": "allow",
            "include": [{"email_domain": {"domain": "example.com"}}],
        },
    ],
)
def test_managed_oauth_refuses_broad_or_bypass_policies(policy: dict[str, object]) -> None:
    application = managed_application()
    application["policies"] = [policy]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": application})

    with pytest.raises(RuntimeError, match="exact email allow policies"):
        managed_client(handler).managed_oauth_plan("health-mcp.example.com")


def test_cloudflare_token_loader_prefers_environment_and_falls_back_to_keychain() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs) -> CompletedProcess[str]:
        calls.append(command)
        return CompletedProcess(command, 0, stdout="keychain-token\n", stderr="")

    assert load_cloudflare_api_token(
        {"CLOUDFLARE_API_TOKEN": "environment-token"},
        runner=runner,
        platform="darwin",
    ) == "environment-token"
    assert calls == []
    assert load_cloudflare_api_token({}, runner=runner, platform="darwin") == "keychain-token"
    assert calls == [
        [
            "security",
            "find-generic-password",
            "-s",
            "heavenly-cloudflare-api-token",
            "-w",
        ]
    ]
