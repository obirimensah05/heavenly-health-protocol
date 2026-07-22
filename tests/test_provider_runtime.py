"""Dispatch, status reporting, and the read-only Google continuity path."""

from __future__ import annotations

from typing import Any

import pytest

from heavenly_health.providers.common import (
    MemorySecretStore,
    ProviderConfigurationError,
    ProviderStateStore,
)
from heavenly_health.providers.runtime import ProviderRuntime, provider_state_store


CONNECTED_GOOGLE_STATE = {
    "connected": True,
    "identity_hash": "hashed-identity",
    "connected_at": "2026-07-14T06:00:00Z",
    "last_sync_at": "2026-07-14T06:30:00Z",
    "data_types": ["daily-resting-heart-rate"],
    "checkpoints": {},
}
RECOVERY_METRICS = frozenset({"resting_heart_rate", "heart_rate_variability"})


def runtime(tmp_path, **kwargs: Any) -> ProviderRuntime:
    return ProviderRuntime(
        secret_store=MemorySecretStore(),
        state_store=ProviderStateStore(tmp_path / "providers"),
        **kwargs,
    )


class FakeGoogleAPI:
    """Minimal stand-in that records the bounded window it was asked for."""

    def __init__(self, points: dict[str, list[dict[str, Any]]]) -> None:
        self._points = points
        self.requests: list[tuple[str, int]] = []
        self.identity_calls = 0

    def identity(self) -> dict[str, str]:
        self.identity_calls += 1
        return {"healthUserId": "stable-health-user"}

    def list_data_points(self, data_type, *, start, end, limit):
        self.requests.append((data_type, limit))
        return self._points.get(data_type, [])


def _resting_heart_rate_point(name: str, value: int) -> dict[str, Any]:
    return {
        "name": f"users/1/dataTypes/daily-resting-heart-rate/dataPoints/{name}",
        "dailyRestingHeartRate": {
            "date": {"year": 2026, "month": 7, "day": 14},
            "beatsPerMinute": value,
        },
    }


def install_fake_google(monkeypatch, api: FakeGoogleAPI) -> None:
    class FakeOAuthClient:
        access_token = "fake-token"  # deliberately short: not secret-shaped

        @classmethod
        def load(cls, _secret_store):
            return cls()

    monkeypatch.setattr("heavenly_health.providers.runtime.GoogleOAuthClient", FakeOAuthClient)
    monkeypatch.setattr(
        "heavenly_health.providers.runtime.GoogleHealthAPI",
        lambda _token: api,
    )


def test_statuses_reports_only_providers_the_owner_has_connected(tmp_path) -> None:
    provider_runtime = runtime(tmp_path)
    provider_runtime.state_store.save("google_health", CONNECTED_GOOGLE_STATE)

    statuses = provider_runtime.statuses()

    assert statuses == [
        {
            "source": "google_health",
            "connected": True,
            "sync_supported": True,
            "last_sync_at": "2026-07-14T06:30:00Z",
            "data_types": ["daily-resting-heart-rate"],
        }
    ]


def test_statuses_is_empty_before_any_provider_is_connected(tmp_path) -> None:
    assert runtime(tmp_path).statuses() == []


def test_sync_refuses_a_source_outside_the_supported_set(tmp_path) -> None:
    with pytest.raises(ProviderConfigurationError, match="Unsupported provider source"):
        runtime(tmp_path).sync("fitbit", store=object())


def test_sync_bounds_the_record_budget_it_hands_to_a_connector(tmp_path) -> None:
    observed: list[int] = []

    class FakeConnector:
        def sync(self, *, limit):
            observed.append(limit)
            return {"source": "whoop", "records_processed": 0, "status": "completed"}

    provider_runtime = runtime(tmp_path, connector_factory=lambda _source, _store: FakeConnector())

    provider_runtime.sync("whoop", object(), limit=0)
    provider_runtime.sync("whoop", object(), limit=50_000)
    provider_runtime.sync("whoop", object(), limit=25)

    assert observed == [1, 10_000, 25]


def test_the_default_connector_factory_rejects_an_unknown_source(tmp_path) -> None:
    with pytest.raises(ProviderConfigurationError, match="Unsupported provider source"):
        runtime(tmp_path)._default_connector("fitbit", object())


def test_google_continuity_read_requires_a_connected_provider(tmp_path) -> None:
    with pytest.raises(ProviderConfigurationError, match="not connected"):
        runtime(tmp_path).google_health_events(RECOVERY_METRICS)


def test_google_continuity_read_requires_an_allowlisted_recovery_metric(tmp_path) -> None:
    provider_runtime = runtime(tmp_path)
    provider_runtime.state_store.save("google_health", CONNECTED_GOOGLE_STATE)

    with pytest.raises(ProviderConfigurationError, match="not allowlisted"):
        provider_runtime.google_health_events(frozenset({"steps"}))


def test_google_continuity_read_returns_normalized_events_without_writing(
    tmp_path,
    monkeypatch,
) -> None:
    """The fallback path must not advance checkpoints or persist provenance."""
    api = FakeGoogleAPI(
        {"daily-resting-heart-rate": [_resting_heart_rate_point("a", 52)]}
    )
    install_fake_google(monkeypatch, api)
    provider_runtime = runtime(tmp_path)
    provider_runtime.state_store.save("google_health", CONNECTED_GOOGLE_STATE)

    events = provider_runtime.google_health_events(RECOVERY_METRICS)

    assert [event["metric_type"] for event in events] == ["resting_heart_rate"]
    assert events[0]["value_numeric"] == 52
    assert events[0]["source"] == "google_health"
    assert api.identity_calls == 1
    assert provider_runtime.state_store.load("google_health")["checkpoints"] == {}


def test_google_continuity_read_bounds_its_own_window_and_budget(
    tmp_path,
    monkeypatch,
) -> None:
    api = FakeGoogleAPI({})
    install_fake_google(monkeypatch, api)
    provider_runtime = runtime(tmp_path)
    provider_runtime.state_store.save("google_health", CONNECTED_GOOGLE_STATE)

    provider_runtime.google_health_events(RECOVERY_METRICS, days=9_000, limit=50_000)

    assert api.requests
    assert all(limit <= 10_000 for _data_type, limit in api.requests)


def test_google_continuity_read_stops_once_the_budget_is_spent(
    tmp_path,
    monkeypatch,
) -> None:
    api = FakeGoogleAPI(
        {
            "daily-resting-heart-rate": [
                _resting_heart_rate_point("a", 52),
                _resting_heart_rate_point("b", 53),
            ]
        }
    )
    install_fake_google(monkeypatch, api)
    provider_runtime = runtime(tmp_path)
    provider_runtime.state_store.save("google_health", CONNECTED_GOOGLE_STATE)

    events = provider_runtime.google_health_events(RECOVERY_METRICS, limit=1)

    assert len(events) >= 1
    assert api.requests[0][1] == 1


def test_provider_state_store_defaults_to_the_private_state_path(tmp_path) -> None:
    explicit = provider_state_store(tmp_path / "providers")
    assert explicit.root == tmp_path / "providers"
    assert provider_state_store().root.parts[-2:] == ("heavenly", "providers")


class FakeOAuth:
    """Records revocation and reports whether the provider confirmed it."""

    SERVICE = "heavenly-test"
    CLIENT_ACCOUNT = "client"

    def __init__(self, *, remote_revocation: bool = True) -> None:
        self.revoked = False
        self._remote_revocation = remote_revocation
        self.access_token = "fake-token"
        self.credentials = object()

    @classmethod
    def bind(cls, monkeypatch, name: str, instance: "FakeOAuth") -> None:
        holder = type(
            "Bound",
            (),
            {
                "SERVICE": cls.SERVICE,
                "CLIENT_ACCOUNT": cls.CLIENT_ACCOUNT,
                "load": classmethod(lambda _cls, *_args, **_kwargs: instance),
            },
        )
        monkeypatch.setattr(f"heavenly_health.providers.runtime.{name}", holder)

    def revoke(self) -> bool:
        self.revoked = True
        return self._remote_revocation


def test_disconnecting_google_revokes_remotely_and_clears_local_state(
    tmp_path,
    monkeypatch,
) -> None:
    oauth = FakeOAuth()
    FakeOAuth.bind(monkeypatch, "GoogleOAuthClient", oauth)
    provider_runtime = runtime(tmp_path)
    provider_runtime.state_store.save("google_health", CONNECTED_GOOGLE_STATE)

    result = provider_runtime.disconnect_google()

    assert result == {"source": "google_health", "connected": False}
    assert oauth.revoked is True
    assert provider_runtime.state_store.load("google_health") == {}


@pytest.mark.parametrize(
    ("source", "client_name", "method"),
    [
        ("whoop", "WhoopOAuthClient", "disconnect_whoop"),
        ("oura", "OuraOAuthClient", "disconnect_oura"),
    ],
)
def test_disconnecting_a_provider_reports_whether_revocation_reached_it(
    tmp_path,
    monkeypatch,
    source: str,
    client_name: str,
    method: str,
) -> None:
    oauth = FakeOAuth(remote_revocation=False)
    FakeOAuth.bind(monkeypatch, client_name, oauth)
    provider_runtime = runtime(tmp_path)
    provider_runtime.state_store.save(source, {"connected": True})

    result = getattr(provider_runtime, method)()

    assert result == {"source": source, "connected": False, "remote_revocation": False}
    assert oauth.revoked is True
    assert provider_runtime.state_store.load(source) == {}


@pytest.mark.parametrize(
    ("source", "client_name", "api_name", "connector_name"),
    [
        ("google_health", "GoogleOAuthClient", "GoogleHealthAPI", "GoogleHealthConnector"),
        ("garmin", "GarminOAuthClient", "GarminHealthAPI", "GarminHealthConnector"),
        ("whoop", "WhoopOAuthClient", "WhoopAPI", "WhoopConnector"),
        ("oura", "OuraOAuthClient", "OuraAPI", "OuraConnector"),
    ],
)
def test_each_supported_source_builds_its_own_connector(
    tmp_path,
    monkeypatch,
    source: str,
    client_name: str,
    api_name: str,
    connector_name: str,
) -> None:
    """Every source in SOURCES must resolve; an unmapped one would fail closed."""
    FakeOAuth.bind(monkeypatch, client_name, FakeOAuth())
    monkeypatch.setattr(
        f"heavenly_health.providers.runtime.{api_name}",
        lambda *_args, **_kwargs: "api",
    )
    monkeypatch.setattr(
        f"heavenly_health.providers.runtime.{connector_name}",
        lambda *args, **kwargs: ("connector", source),
    )

    built = runtime(tmp_path)._default_connector(source, object())

    assert built == ("connector", source)
