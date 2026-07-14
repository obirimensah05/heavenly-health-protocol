from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import stat

import pytest

from heavenly_health.providers.common import (
    MemorySecretStore,
    OAuthToken,
    ProviderConfigurationError,
    ProviderStateStore,
    validate_https_url,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def test_oauth_token_parses_expiry_preserves_rotating_refresh_and_redacts() -> None:
    token = OAuthToken.from_response(
        {
            "access_token": "access-secret",
            "expires_in": 3600,
            "scope": "scope-a scope-b",
            "token_type": "Bearer",
        },
        now=NOW,
        previous_refresh_token="refresh-secret",
    )

    assert token.refresh_token == "refresh-secret"
    assert token.expires_at == NOW + timedelta(hours=1)
    assert token.scopes == frozenset({"scope-a", "scope-b"})
    assert token.needs_refresh(NOW + timedelta(minutes=59)) is True
    assert "access-secret" not in repr(token)
    assert "refresh-secret" not in repr(token)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/token",
        "https://localhost/token",
        "https://127.0.0.1/token",
        "https://user@api.example.com/token",
        "https://api.example.com/token#fragment",
    ],
)
def test_https_provider_url_rejects_unsafe_origins(url: str) -> None:
    with pytest.raises(ProviderConfigurationError):
        validate_https_url(url, name="provider URL")


def test_memory_secret_store_never_serializes_values() -> None:
    store = MemorySecretStore()
    store.set("google-health", "token", "private-value")

    assert store.get("google-health", "token") == "private-value"
    assert "private-value" not in repr(store)
    store.delete("google-health", "token")
    assert store.get("google-health", "token") is None


def test_provider_state_is_owner_only_atomic_and_rejects_secret_fields(tmp_path) -> None:
    root = tmp_path / "providers"
    store = ProviderStateStore(root)
    store.save(
        "google_health",
        {
            "connected": True,
            "identity_hash": "a" * 64,
            "checkpoints": {"steps": "2026-07-14T11:00:00Z"},
        },
    )

    target = root / "google_health.json"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text())["connected"] is True
    assert store.load("google_health")["checkpoints"]["steps"].endswith("Z")

    with pytest.raises(ProviderConfigurationError, match="secret material"):
        store.save("google_health", {"access_token": "must-not-be-written"})

    store.delete("google_health")
    assert store.load("google_health") == {}

