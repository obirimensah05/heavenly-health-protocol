from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from heavenly_health.providers.common import MemorySecretStore, ProviderStateStore
from heavenly_health.providers.garmin import (
    GARMIN_CALLBACK_URL,
    GarminClientCredentials,
    GarminHealthAPI,
    GarminHealthConnector,
    GarminHealthError,
    GarminOAuthClient,
    normalize_garmin_resource,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
ALLOWED = frozenset(
    {
        "steps",
        "resting_heart_rate",
        "heart_rate",
        "active_energy",
        "sleep_analysis",
        "body_mass",
        "oxygen_saturation",
        "respiratory_rate",
        "stress_level",
        "body_battery",
    }
)


def credential_payload() -> dict[str, object]:
    return {
        "client_id": "garmin-client-id",
        "client_secret": "garmin-client-secret",
        "authorization_url": "https://connect.garmin.com/oauth2/authorize",
        "token_url": "https://connect.garmin.com/oauth2/token",
        "api_base_url": "https://apis.garmin.com",
        "redirect_uri": GARMIN_CALLBACK_URL,
        "scopes": ["health"],
        "identity_path": "/wellness-api/rest/user/id",
        "resource_paths": {
            "dailies": "/wellness-api/rest/dailies",
            "sleeps": "/wellness-api/rest/sleeps",
            "body_compositions": "/wellness-api/rest/bodyComps",
        },
    }


def test_garmin_credentials_require_private_partner_configuration_and_redact(
    tmp_path: Path,
) -> None:
    path = tmp_path / "garmin-client.json"
    path.write_text(json.dumps(credential_payload()))
    path.chmod(0o600)

    credentials = GarminClientCredentials.from_private_json(path)

    assert credentials.redirect_uri == GARMIN_CALLBACK_URL
    assert credentials.resource_paths["dailies"].endswith("/dailies")
    assert "garmin-client-secret" not in repr(credentials)

    unsafe = credential_payload()
    unsafe["api_base_url"] = "http://127.0.0.1:8080"
    with pytest.raises(GarminHealthError, match="HTTPS"):
        GarminClientCredentials.from_payload(unsafe)


def test_garmin_authorization_uses_partner_scopes_state_pkce_and_exact_callback() -> None:
    oauth = GarminOAuthClient(
        GarminClientCredentials.from_payload(credential_payload()),
        MemorySecretStore(),
        clock=lambda: NOW,
    )

    request = oauth.authorization_request()
    query = parse_qs(urlparse(request.url).query)

    assert query["scope"] == ["health"]
    assert query["state"] == [request.state]
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [GARMIN_CALLBACK_URL]
    assert request.code_verifier not in request.url


def test_garmin_code_exchange_refresh_access_and_remote_revocation() -> None:
    payload = credential_payload()
    payload["revocation_url"] = "https://connect.garmin.com/oauth2/revoke"
    credentials = GarminClientCredentials.from_payload(payload)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/token"):
            body = parse_qs(request.content.decode())
            if body["grant_type"] == ["authorization_code"]:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "first-access",
                        "refresh_token": "durable-refresh",
                        "expires_in": 3600,
                        "scope": "health",
                        "token_type": "Bearer",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "second-access",
                    "expires_in": 3600,
                    "scope": "health",
                    "token_type": "Bearer",
                },
            )
        return httpx.Response(200, json={})

    secrets = MemorySecretStore()
    oauth = GarminOAuthClient(
        credentials,
        secrets,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: NOW,
    )
    first = oauth.exchange_code("one-time-code", code_verifier="verifier")
    refreshed = oauth.refresh(first)

    assert refreshed.refresh_token == "durable-refresh"
    assert oauth.access_token() == "second-access"
    assert oauth.revoke() is True
    assert secrets.get(GarminOAuthClient.SERVICE, GarminOAuthClient.TOKEN_ACCOUNT) is None
    assert requests[-1].url.path.endswith("/revoke")


def test_garmin_import_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "garmin.json"
    path.write_text(json.dumps(credential_payload()))
    path.chmod(0o600)
    secrets = MemorySecretStore()

    GarminOAuthClient.import_credentials(path, secrets)
    restored = GarminOAuthClient.load(secrets)

    assert restored.credentials.resource_paths["dailies"].endswith("dailies")
    with pytest.raises(GarminHealthError, match="not connected"):
        restored.access_token()


def test_garmin_api_uses_configured_partner_paths_and_bounded_epoch_window() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer access-token"
        if request.url.path.endswith("/user/id"):
            return httpx.Response(200, json={"userId": "garmin-user"})
        return httpx.Response(
            200,
            json=[
                {
                    "summaryId": "daily-1",
                    "calendarDate": "2026-07-14",
                    "steps": 1000,
                }
            ],
        )

    credentials = GarminClientCredentials.from_payload(credential_payload())
    api = GarminHealthAPI(
        credentials,
        token_provider=lambda: "access-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert api.identity()["userId"] == "garmin-user"
    resources = api.list_resources(
        "dailies",
        start="2026-07-13T00:00:00Z",
        end="2026-07-14T00:00:00Z",
        limit=10,
    )

    assert resources[0]["summaryId"] == "daily-1"
    assert requests[-1].url.params["uploadStartTimeInSeconds"].isdigit()
    assert requests[-1].url.params["uploadEndTimeInSeconds"].isdigit()


@pytest.mark.parametrize(
    ("resource_type", "resource", "expected"),
    [
        (
            "dailies",
            {
                "summaryId": "daily-1",
                "calendarDate": "2026-07-14",
                "steps": 1234,
                "restingHeartRateInBeatsPerMinute": 52,
                "activeKilocalories": 420,
                "averageStressLevel": 24,
                "bodyBatteryMostRecentValue": 68,
            },
            {
                "steps": (1234, "count"),
                "resting_heart_rate": (52, "bpm"),
                "active_energy": (420, "kcal"),
                "stress_level": (24, "score"),
                "body_battery": (68, "score"),
            },
        ),
        (
            "sleeps",
            {
                "summaryId": "sleep-1",
                "sleepStartTimestampGMT": 1784000000,
                "durationInSeconds": 27000,
            },
            {"sleep_analysis": (450, "min")},
        ),
        (
            "body_compositions",
            {
                "summaryId": "body-1",
                "measurementTimeInSeconds": 1784000000,
                "weightInGrams": 78200,
            },
            {"body_mass": (78.2, "kg")},
        ),
    ],
)
def test_garmin_normalizer_emits_unique_allowlisted_events_per_raw_summary(
    resource_type: str,
    resource: dict[str, object],
    expected: dict[str, tuple[float, str]],
) -> None:
    events = normalize_garmin_resource(
        resource_type,
        resource,
        allowed_metrics=ALLOWED,
    )

    assert {event["metric_type"]: (event["value_numeric"], event["unit"]) for event in events} == expected
    assert len({event["source_record_id"] for event in events}) == len(events)
    assert all(event["source"] == "garmin" for event in events)
    assert all("deviceName" not in event["metadata"] for event in events)


def test_garmin_connector_saves_checkpoint_after_storage(tmp_path: Path) -> None:
    class FakeAPI:
        credentials = GarminClientCredentials.from_payload(credential_payload())

        def identity(self):
            return {"userId": "garmin-user"}

        def list_resources(self, resource_type, *, start, end, limit):
            assert resource_type == "dailies"
            return [
                {
                    "summaryId": "daily-1",
                    "calendarDate": "2026-07-14",
                    "steps": 2000,
                }
            ]

    class FakeStore:
        settings = type("Settings", (), {"allowed_metrics": frozenset({"steps"})})()

        def __init__(self):
            self.records = []

        def ingest_provider_resource(self, **kwargs):
            self.records.append(kwargs)
            return len(kwargs["events"])

    state = ProviderStateStore(tmp_path / "state")
    store = FakeStore()
    connector = GarminHealthConnector(
        FakeAPI(),
        store,
        state,
        resource_types=("dailies",),
        clock=lambda: NOW,
    )

    result = connector.sync(days=1, limit=10)

    assert result["source"] == "garmin"
    assert result["records_processed"] == 1
    assert result["events_upserted"] == 1
    assert store.records[0]["source"] == "garmin"
    saved = state.load("garmin")
    assert saved["identity_hash"] != "garmin-user"
    assert saved["checkpoints"]["dailies"] == "2026-07-14T12:00:00Z"
    assert connector.status()["connected"] is True


def test_garmin_epoch_pulse_ox_and_respiration_normalizers() -> None:
    base = {"summaryId": "summary", "startTimeInSeconds": 1784000000}

    epochs = normalize_garmin_resource(
        "epochs",
        {**base, "steps": 10, "heartRate": 70, "activeKilocalories": 5},
        allowed_metrics=ALLOWED,
    )
    pulse = normalize_garmin_resource(
        "pulse_ox",
        {**base, "averageSpO2": 97},
        allowed_metrics=ALLOWED,
    )
    respiration = normalize_garmin_resource(
        "respiration",
        {**base, "avgWakingRespirationValue": 14.5},
        allowed_metrics=ALLOWED,
    )

    assert {event["metric_type"] for event in epochs} == {
        "steps",
        "heart_rate",
        "active_energy",
    }
    assert pulse[0]["value_numeric"] == 97
    assert respiration[0]["value_numeric"] == 14.5
    assert normalize_garmin_resource("unknown", base, allowed_metrics=ALLOWED) == []
