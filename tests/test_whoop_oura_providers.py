from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from typer.testing import CliRunner

from heavenly_health.cli import app
from heavenly_health.providers.common import (
    MemorySecretStore,
    ProviderConfigurationError,
    ProviderStateStore,
    read_private_env,
)
from heavenly_health.providers.oura import (
    OuraAPI,
    OuraConnector,
    normalize_oura_record,
    oura_resources_for_metrics,
)
from heavenly_health.providers.whoop import (
    WhoopClientCredentials,
    WhoopConnector,
    WhoopOAuthClient,
    normalize_whoop_record,
    whoop_resources_for_metrics,
)


runner = CliRunner()
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _write_env(path, lines) -> None:
    path.write_text("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def _whoop_env(tmp_path):
    path = tmp_path / "whoop.env"
    _write_env(
        path,
        [
            "WHOOP_CLIENT_ID=whoop-client-id",
            "WHOOP_CLIENT_SECRET=whoop-client-secret",
            "WHOOP_REDIRECT_URI=https://app.whoop.com",
            "WHOOP_SCOPES=offline read:recovery read:sleep read:cycles read:profile",
        ],
    )
    return path


def _oura_env(tmp_path):
    path = tmp_path / "oura.env"
    _write_env(
        path,
        [
            "OURA_CLIENT_ID=oura-client-id",
            "OURA_CLIENT_SECRET=oura-client-secret",
            "OURA_REDIRECT_URI=https://redirect.example.com",
            "OURA_SCOPES=daily heartrate workout",
        ],
    )
    return path


def test_private_env_reader_rejects_group_readable_files(tmp_path) -> None:
    path = tmp_path / "whoop.env"
    _write_env(path, ["WHOOP_CLIENT_ID=x"])
    os.chmod(path, 0o644)
    with pytest.raises(ProviderConfigurationError):
        read_private_env(path)


def test_whoop_credentials_import_from_owner_only_env(tmp_path) -> None:
    credentials = WhoopClientCredentials.from_private_env(_whoop_env(tmp_path))
    assert credentials.client_id == "whoop-client-id"
    assert credentials.redirect_uri == "https://app.whoop.com"
    assert "offline" in credentials.scopes


def test_whoop_authorization_request_uses_registered_redirect_and_short_state(tmp_path) -> None:
    credentials = WhoopClientCredentials.from_private_env(_whoop_env(tmp_path))
    oauth = WhoopOAuthClient(credentials, MemorySecretStore())
    request = oauth.authorization_request()
    query = parse_qs(urlparse(request.url).query)
    assert query["client_id"] == ["whoop-client-id"]
    assert query["redirect_uri"] == ["https://app.whoop.com"]
    assert len(query["state"][0]) >= 8


def test_whoop_token_requests_send_a_browser_user_agent(tmp_path) -> None:
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-access",
                "refresh_token": "fresh-refresh",
                "expires_in": 3600,
                "scope": "offline read:recovery",
                "token_type": "bearer",
            },
        )

    credentials = WhoopClientCredentials.from_private_env(_whoop_env(tmp_path))
    store = MemorySecretStore()
    oauth = WhoopOAuthClient(
        credentials,
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    token = oauth.exchange_code("auth-code")
    assert token.refresh_token == "fresh-refresh"
    assert "Mozilla" in seen_headers[0].get("User-Agent", "")


def test_whoop_pasted_callback_requires_matching_state(tmp_path) -> None:
    credentials = WhoopClientCredentials.from_private_env(_whoop_env(tmp_path))
    oauth = WhoopOAuthClient(credentials, MemorySecretStore())
    request = oauth.authorization_request()
    with pytest.raises(ProviderConfigurationError):
        oauth.parse_callback(
            "https://app.whoop.com/?code=abc&state=wrong-state",
            expected_state=request.state,
        )
    code = oauth.parse_callback(
        f"https://app.whoop.com/?code=abc&state={request.state}",
        expected_state=request.state,
    )
    assert code == "abc"


def test_whoop_recovery_normalizes_to_multiple_allowlisted_metrics() -> None:
    record = {
        "id": "rec-1",
        "created_at": "2026-07-14T06:00:00Z",
        "score": {
            "hrv_rmssd_milli": 62.5,
            "resting_heart_rate": 48,
            "spo2_percentage": 97.2,
        },
    }
    events = normalize_whoop_record(
        "recovery",
        record,
        allowed_metrics=frozenset({"heart_rate_variability", "resting_heart_rate"}),
    )
    metrics = {event["metric_type"] for event in events}
    assert metrics == {"heart_rate_variability", "resting_heart_rate"}
    assert all(event["source"] == "whoop" for event in events)
    assert all(event["source_record_id"].startswith("whoop:recovery:") for event in events)


def test_whoop_sleep_normalizes_duration_and_respiratory_rate() -> None:
    record = {
        "id": "sleep-1",
        "end": "2026-07-14T05:30:00Z",
        "score": {
            "respiratory_rate": 15.2,
            "stage_summary": {
                "total_light_sleep_time_milli": 3_600_000,
                "total_slow_wave_sleep_time_milli": 1_800_000,
                "total_rem_sleep_time_milli": 1_800_000,
            },
        },
    }
    events = normalize_whoop_record(
        "sleep",
        record,
        allowed_metrics=frozenset({"sleep_analysis", "respiratory_rate"}),
    )
    by_metric = {event["metric_type"]: event for event in events}
    assert by_metric["sleep_analysis"]["value_numeric"] == 120
    assert by_metric["sleep_analysis"]["unit"] == "min"
    assert by_metric["respiratory_rate"]["value_numeric"] == 15.2


def test_whoop_cycle_converts_kilojoules_to_kilocalories() -> None:
    record = {
        "id": 12345,
        "start": "2026-07-13T22:00:00Z",
        "end": "2026-07-14T06:00:00Z",
        "score": {"kilojoule": 8368.0},
    }
    events = normalize_whoop_record(
        "cycle", record, allowed_metrics=frozenset({"active_energy"})
    )
    assert events[0]["metric_type"] == "active_energy"
    assert events[0]["unit"] == "kcal"
    assert abs(events[0]["value_numeric"] - 2000) < 1


def test_oura_daily_activity_normalizes_steps_and_energy() -> None:
    record = {
        "id": "act-1",
        "day": "2026-07-13",
        "timestamp": "2026-07-13T04:00:00+00:00",
        "steps": 10234,
        "active_calories": 512,
    }
    events = normalize_oura_record(
        "daily_activity",
        record,
        allowed_metrics=frozenset({"steps", "active_energy"}),
    )
    by_metric = {event["metric_type"]: event for event in events}
    assert by_metric["steps"]["value_numeric"] == 10234
    assert by_metric["active_energy"]["value_numeric"] == 512
    assert all(event["source"] == "oura" for event in events)


def test_oura_sleep_normalizes_duration_hrv_and_resting_heart_rate() -> None:
    record = {
        "id": "slp-1",
        "day": "2026-07-14",
        "bedtime_end": "2026-07-14T06:10:00+00:00",
        "total_sleep_duration": 27000,
        "average_hrv": 58,
        "lowest_heart_rate": 44,
        "average_breath": 14.5,
    }
    events = normalize_oura_record(
        "sleep",
        record,
        allowed_metrics=frozenset(
            {"sleep_analysis", "heart_rate_variability", "resting_heart_rate", "respiratory_rate"}
        ),
    )
    by_metric = {event["metric_type"]: event for event in events}
    assert by_metric["sleep_analysis"]["value_numeric"] == 450
    assert by_metric["heart_rate_variability"]["value_numeric"] == 58
    assert by_metric["resting_heart_rate"]["value_numeric"] == 44
    assert by_metric["respiratory_rate"]["value_numeric"] == 14.5


def test_resource_selection_follows_the_metric_allowlist() -> None:
    assert whoop_resources_for_metrics(frozenset({"heart_rate_variability"})) == ("recovery",)
    assert "sleep" in whoop_resources_for_metrics(frozenset({"sleep_analysis"}))
    assert oura_resources_for_metrics(frozenset({"steps"})) == ("daily_activity",)
    assert "sleep" in oura_resources_for_metrics(frozenset({"sleep_analysis"}))


class _RecordingStore:
    def __init__(self, allowed_metrics: frozenset[str]) -> None:
        class _Settings:
            pass

        self.settings = _Settings()
        self.settings.allowed_metrics = allowed_metrics
        self.calls: list[dict[str, object]] = []

    def ingest_provider_resource(self, **kwargs: object) -> int:
        self.calls.append(kwargs)
        return len(kwargs.get("events") or [])


def test_whoop_sync_ingests_raw_records_and_normalized_events(tmp_path) -> None:
    class FakeAPI:
        def identity(self) -> dict[str, object]:
            return {"user_id": 7788}

        def list_records(self, resource: str, *, start: str, end: str, limit: int):
            assert resource == "recovery"
            return [
                {
                    "id": "rec-1",
                    "created_at": "2026-07-14T06:00:00Z",
                    "score": {"hrv_rmssd_milli": 61.0, "resting_heart_rate": 47},
                }
            ]

    store = _RecordingStore(frozenset({"heart_rate_variability", "resting_heart_rate"}))
    state = ProviderStateStore(tmp_path / "state")
    connector = WhoopConnector(FakeAPI(), store, state, clock=lambda: NOW)
    result = connector.sync(limit=10)
    assert result["source"] == "whoop"
    assert result["records_processed"] == 1
    assert result["events_upserted"] == 2
    saved = state.load("whoop")
    assert saved["connected"] is True


def test_oura_sync_skips_resources_the_grant_cannot_read(tmp_path) -> None:
    class FakeAPI:
        def identity(self) -> dict[str, object]:
            return {"id": "oura-user-1"}

        def list_records(self, resource: str, *, start: str, end: str, limit: int):
            if resource == "daily_spo2":
                raise OuraAPI.forbidden_error(resource)
            return [
                {
                    "id": "act-1",
                    "day": "2026-07-13",
                    "timestamp": "2026-07-13T04:00:00+00:00",
                    "steps": 9000,
                }
            ]

    store = _RecordingStore(frozenset({"steps", "oxygen_saturation"}))
    state = ProviderStateStore(tmp_path / "state")
    connector = OuraConnector(FakeAPI(), store, state, clock=lambda: NOW)
    result = connector.sync(limit=10)
    assert result["records_processed"] == 1
    assert result["skipped_resources"] == ["daily_spo2"]


def test_oura_401_on_a_resource_read_maps_to_a_scope_skip() -> None:
    from heavenly_health.providers.oura import OuraResourceForbidden

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Token is not authorized access spo2 scope."})

    api = OuraAPI(lambda: "token", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(OuraResourceForbidden):
        api.list_records("daily_spo2", start="2026-07-07", end="2026-07-14", limit=10)


def test_provider_cli_exposes_whoop_and_oura_groups() -> None:
    result = runner.invoke(app, ["provider", "--help"])
    assert result.exit_code == 0
    assert "whoop" in result.stdout
    assert "oura" in result.stdout
    for group in ("whoop", "oura"):
        detail = runner.invoke(app, ["provider", group, "--help"])
        assert detail.exit_code == 0
        for command in ("import-client", "connect", "sync", "disconnect"):
            assert command in detail.stdout


def test_runtime_connects_whoop_through_a_pasted_callback(tmp_path, monkeypatch) -> None:
    from heavenly_health.providers.runtime import ProviderRuntime

    secret_store = MemorySecretStore()
    state_store = ProviderStateStore(tmp_path / "state")
    runtime = ProviderRuntime(secret_store=secret_store, state_store=state_store)
    runtime.import_whoop_client(_whoop_env(tmp_path))

    def fake_token_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": "granted-access",
                    "refresh_token": "granted-refresh",
                    "expires_in": 3600,
                    "scope": "offline read:recovery",
                    "token_type": "bearer",
                },
            )
        return httpx.Response(200, json={"user_id": 314159})

    transport = httpx.MockTransport(fake_token_response)
    monkeypatch.setattr(
        "heavenly_health.providers.runtime._provider_http_client",
        lambda: httpx.Client(transport=transport),
    )

    def authorize(url: str) -> str:
        state = parse_qs(urlparse(url).query)["state"][0]
        return f"https://app.whoop.com/?code=granted&state={state}"

    result = runtime.connect_whoop(frozenset({"heart_rate_variability"}), authorize=authorize)
    assert result["source"] == "whoop"
    assert result["connected"] is True
    assert state_store.load("whoop")["connected"] is True
    stored_token = secret_store.get("whoop", "oauth-token")
    assert stored_token is not None
    assert json.loads(stored_token)["refresh_token"] == "granted-refresh"
