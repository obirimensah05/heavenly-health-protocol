from __future__ import annotations

import json

import httpx
import pytest

from heavenly_health.health_storage import (
    HealthStorageError,
    SupabaseHealthStore,
    SupabaseSettings,
)
from heavenly_health.providers.common import MemorySecretStore, ProviderStateStore
from heavenly_health.providers.google_health import GoogleOAuthClient
from heavenly_health.providers.runtime import ProviderRuntime


def settings() -> SupabaseSettings:
    value = SupabaseSettings.from_environ(
        {
            "SUPABASE_URL": "https://project-ref.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
            "HEAVENLY_ALLOWED_METRICS": "steps,heart_rate",
        }
    )
    assert value is not None
    return value


def test_store_persists_restricted_raw_record_then_allowlisted_normalized_events() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/heavenly_health_raw_events"):
            raw = json.loads(request.content)
            assert raw["source"] == "google_health"
            assert raw["source_record_id"].startswith("google-health:steps:")
            return httpx.Response(
                201,
                json=[{"id": "00000000-0000-4000-8000-000000000099"}],
            )
        events = json.loads(request.content)
        assert events[0]["raw_event_id"] == "00000000-0000-4000-8000-000000000099"
        assert events[0]["metric_type"] == "steps"
        return httpx.Response(201, json=[{"id": "normalized-id"}])

    store = SupabaseHealthStore(
        settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    count = store.ingest_provider_resource(
        source="google_health",
        resource_type="steps",
        source_record_id="google-health:steps:" + "a" * 64,
        event_at="2026-07-14T10:00:00Z",
        payload={"name": "users/me/dataTypes/steps/dataPoints/a"},
        events=[
            {
                "source": "google_health",
                "metric_type": "steps",
                "event_at": "2026-07-14T10:00:00Z",
                "value_numeric": 40,
                "value_text": None,
                "unit": "count",
                "source_record_id": "google-health:steps:" + "a" * 64,
                "metadata": {"schema_version": "1.0"},
                "is_synthetic": False,
            }
        ],
        ingest_mode="backfill",
    )

    assert count == 1
    assert [request.method for request in requests] == ["POST", "POST"]


def test_store_rejects_unknown_provider_or_non_allowlisted_normalized_metric() -> None:
    store = SupabaseHealthStore(settings())

    with pytest.raises(HealthStorageError, match="provider source"):
        store.ingest_provider_resource(
            source="attacker",
            resource_type="steps",
            source_record_id="attacker:1",
            event_at="2026-07-14T10:00:00Z",
            payload={},
            events=[],
            ingest_mode="live",
        )

    with pytest.raises(HealthStorageError, match="not allowed"):
        store.ingest_provider_resource(
            source="google_health",
            resource_type="steps",
            source_record_id="google-health:steps:" + "a" * 64,
            event_at="2026-07-14T10:00:00Z",
            payload={},
            events=[
                {
                    "source": "google_health",
                    "metric_type": "sleep_analysis",
                    "event_at": "2026-07-14T10:00:00Z",
                    "source_record_id": "google-health:steps:" + "a" * 64,
                }
            ],
            ingest_mode="live",
        )


def test_provider_runtime_reports_and_dispatches_google_without_exposing_secrets(
    tmp_path,
) -> None:
    secrets = MemorySecretStore()
    secrets.set(GoogleOAuthClient.SERVICE, GoogleOAuthClient.CLIENT_ACCOUNT, "client-json")
    secrets.set(GoogleOAuthClient.SERVICE, GoogleOAuthClient.TOKEN_ACCOUNT, "token-json")
    state = ProviderStateStore(tmp_path / "providers")
    state.save(
        "google_health",
        {
            "connected": True,
            "identity_hash": "a" * 64,
            "last_sync_at": "2026-07-14T12:00:00Z",
            "data_types": ["steps"],
            "checkpoints": {"steps": "2026-07-14T12:00:00Z"},
        },
    )
    observed = []
    runtime = ProviderRuntime(
        secret_store=secrets,
        state_store=state,
        connector_factory=lambda source, store: type(
            "Connector",
            (),
            {"sync": lambda self, **kwargs: observed.append((source, kwargs)) or {"status": "completed"}},
        )(),
    )

    status = runtime.statuses()
    result = runtime.sync("google_health", object(), limit=25)

    assert status[0]["source"] == "google_health"
    assert status[0]["connected"] is True
    assert "client-json" not in repr(status)
    assert result == {"status": "completed"}
    assert observed == [("google_health", {"limit": 25})]

