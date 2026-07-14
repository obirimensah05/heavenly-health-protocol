"""Oura OAuth, bounded synchronization, and normalization.

Mirrors the owner's proven manual setup: client credentials in an owner-only
env file, browser authorization, and the redirected URL pasted back because
Oura apps commonly register a non-loopback redirect URI. Resources the token's
grant cannot read are skipped and reported, never fatal.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import httpx

from heavenly_health.providers.common import (
    OAuthToken,
    ProviderConfigurationError,
    ProviderStateStore,
    SecretStore,
    bounded_retry_delay,
    read_private_env,
    validate_https_url,
)
from heavenly_health.providers.whoop import parse_pasted_callback

OURA_AUTHORIZATION_URL = "https://cloud.ouraring.com/oauth/authorize"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
OURA_API_BASE = "https://api.ouraring.com/v2"

_RESOURCE_METRICS: dict[str, frozenset[str]] = {
    "daily_activity": frozenset({"steps", "active_energy"}),
    "sleep": frozenset(
        {"sleep_analysis", "heart_rate_variability", "resting_heart_rate", "respiratory_rate"}
    ),
    "daily_spo2": frozenset({"oxygen_saturation"}),
}


class OuraError(ProviderConfigurationError):
    """Oura credentials, OAuth, API data, or synchronization is invalid."""


class OuraResourceForbidden(OuraError):
    """The connected Oura grant cannot read one requested resource."""

    def __init__(self, resource: str) -> None:
        self.resource = resource
        super().__init__(f"Oura grant cannot read resource: {resource}")


@dataclass(frozen=True)
class OuraClientCredentials:
    """Validated Oura application credentials from an owner-only env file."""

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    scopes: tuple[str, ...]

    @classmethod
    def from_private_env(cls, path: Path) -> OuraClientCredentials:
        return cls.from_mapping(read_private_env(path))

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> OuraClientCredentials:
        client_id = values.get("OURA_CLIENT_ID", "").strip()
        client_secret = values.get("OURA_CLIENT_SECRET", "").strip()
        redirect_uri = values.get("OURA_REDIRECT_URI", "").strip()
        scopes = tuple(scope for scope in values.get("OURA_SCOPES", "").split() if scope)
        if not client_id or not client_secret or not redirect_uri or not scopes or any(
            "PASTE_" in value for value in (client_id, client_secret, redirect_uri)
        ):
            raise OuraError(
                "Oura credentials need OURA_CLIENT_ID, OURA_CLIENT_SECRET,"
                " OURA_REDIRECT_URI, and OURA_SCOPES"
            )
        validate_https_url(redirect_uri, name="Oura redirect URI")
        return cls(client_id, client_secret, redirect_uri, scopes)

    def to_json(self) -> str:
        return json.dumps(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "scopes": list(self.scopes),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> OuraClientCredentials:
        try:
            payload = json.loads(value)
            if not isinstance(payload, Mapping):
                raise ValueError
            return cls.from_mapping(
                {
                    "OURA_CLIENT_ID": str(payload.get("client_id", "")),
                    "OURA_CLIENT_SECRET": str(payload.get("client_secret", "")),
                    "OURA_REDIRECT_URI": str(payload.get("redirect_uri", "")),
                    "OURA_SCOPES": " ".join(
                        scope for scope in payload.get("scopes", []) if isinstance(scope, str)
                    ),
                }
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OuraError("Stored Oura OAuth client is invalid") from error


@dataclass(frozen=True)
class OuraAuthorizationRequest:
    url: str
    state: str = field(repr=False)


class OuraOAuthClient:
    """Oura authorization-code exchange, refresh, and revocation."""

    SERVICE = "oura"
    CLIENT_ACCOUNT = "oauth-client"
    TOKEN_ACCOUNT = "oauth-token"

    def __init__(
        self,
        credentials: OuraClientCredentials,
        secret_store: SecretStore,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.credentials = credentials
        self.secret_store = secret_store
        self._client = http_client or httpx.Client(timeout=30, follow_redirects=False)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def load(cls, secret_store: SecretStore, **kwargs: Any) -> OuraOAuthClient:
        saved = secret_store.get(cls.SERVICE, cls.CLIENT_ACCOUNT)
        if saved is None:
            raise OuraError("Oura client credentials are not imported")
        return cls(OuraClientCredentials.from_json(saved), secret_store, **kwargs)

    @classmethod
    def import_credentials(cls, path: Path, secret_store: SecretStore) -> OuraClientCredentials:
        credentials = OuraClientCredentials.from_private_env(path)
        secret_store.set(cls.SERVICE, cls.CLIENT_ACCOUNT, credentials.to_json())
        return credentials

    def authorization_request(self) -> OuraAuthorizationRequest:
        state = secrets.token_urlsafe(24)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.credentials.client_id,
                "redirect_uri": self.credentials.redirect_uri,
                "scope": " ".join(self.credentials.scopes),
                "state": state,
            }
        )
        return OuraAuthorizationRequest(url=f"{OURA_AUTHORIZATION_URL}?{query}", state=state)

    def parse_callback(self, returned_url: str, *, expected_state: str) -> str:
        return parse_pasted_callback(
            returned_url,
            redirect_uri=self.credentials.redirect_uri,
            expected_state=expected_state,
            error_class=OuraError,
        )

    def exchange_code(self, code: str) -> OAuthToken:
        if not code.strip():
            raise OuraError("Oura authorization code is missing")
        token = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.credentials.redirect_uri,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            }
        )
        if token.refresh_token is None:
            raise OuraError("Oura did not return a refresh token")
        self.secret_store.set(self.SERVICE, self.TOKEN_ACCOUNT, token.to_json())
        return token

    def refresh(self, token: OAuthToken) -> OAuthToken:
        if not token.refresh_token:
            raise OuraError("Oura refresh token is unavailable; reconnect is required")
        refreshed = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
            },
            previous_refresh_token=token.refresh_token,
        )
        self.secret_store.set(self.SERVICE, self.TOKEN_ACCOUNT, refreshed.to_json())
        return refreshed

    def access_token(self) -> str:
        saved = self.secret_store.get(self.SERVICE, self.TOKEN_ACCOUNT)
        if saved is None:
            raise OuraError("Oura is not connected")
        token = OAuthToken.from_json(saved)
        if token.needs_refresh(self._clock()):
            token = self.refresh(token)
        return token.access_token

    def revoke(self) -> bool:
        """Delete local tokens; Oura access is revoked from the Oura account page."""
        self.secret_store.delete(self.SERVICE, self.TOKEN_ACCOUNT)
        return False

    def _token_request(
        self,
        data: Mapping[str, str],
        *,
        previous_refresh_token: str | None = None,
    ) -> OAuthToken:
        try:
            response = self._client.post(
                OURA_TOKEN_URL,
                data=data,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise OuraError("Oura OAuth token exchange failed") from error
        if not isinstance(payload, Mapping):
            raise OuraError("Oura OAuth token response is invalid")
        try:
            return OAuthToken.from_response(
                payload,
                now=self._clock(),
                previous_refresh_token=previous_refresh_token,
            )
        except ProviderConfigurationError as error:
            raise OuraError(str(error)) from error


class OuraAPI:
    """Bounded read-only Oura API v2 client."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        http_client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._client = http_client or httpx.Client(timeout=30, follow_redirects=False)
        self._sleep = sleeper or time.sleep

    @staticmethod
    def forbidden_error(resource: str) -> OuraResourceForbidden:
        return OuraResourceForbidden(resource)

    def identity(self) -> dict[str, Any]:
        payload = self._get("/usercollection/personal_info")
        if not any(key in payload for key in ("id", "email")):
            raise OuraError("Oura identity response is invalid")
        return payload

    def list_records(
        self,
        resource: str,
        *,
        start: str,
        end: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if resource not in _RESOURCE_METRICS:
            raise OuraError("Unsupported Oura resource")
        bounded_limit = max(1, min(int(limit), 10_000))
        params: dict[str, str] = {
            "start_date": start[:10],
            "end_date": end[:10],
        }
        records: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        for _ in range(100):
            payload = self._get(f"/usercollection/{resource}", params=params, resource=resource)
            page = payload.get("data", [])
            if not isinstance(page, list) or not all(isinstance(item, Mapping) for item in page):
                raise OuraError("Oura data page is invalid")
            records.extend(dict(item) for item in page[: bounded_limit - len(records)])
            if len(records) >= bounded_limit:
                break
            next_token = payload.get("next_token")
            if not isinstance(next_token, str) or not next_token:
                break
            if next_token in seen_tokens:
                raise OuraError("Oura pagination token repeated")
            seen_tokens.add(next_token)
            params["next_token"] = next_token
        return records

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        resource: str | None = None,
    ) -> dict[str, Any]:
        for attempt in range(3):
            try:
                response = self._client.get(
                    f"{OURA_API_BASE}{path}",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._token_provider()}",
                        "Accept": "application/json",
                    },
                )
                # Oura reports a missing per-resource scope as 401 or 403 on the
                # collection endpoint; identity calls (resource=None) still fail hard.
                if response.status_code in {401, 403} and resource is not None:
                    raise OuraResourceForbidden(resource)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        self._sleep(bounded_retry_delay(response.headers, attempt))
                        continue
                response.raise_for_status()
                payload = response.json()
            except OuraResourceForbidden:
                raise
            except httpx.TransportError as error:
                if attempt < 2:
                    self._sleep(bounded_retry_delay({}, attempt))
                    continue
                raise OuraError("Oura API request failed") from error
            except (httpx.HTTPError, ValueError, TypeError) as error:
                raise OuraError("Oura API request failed") from error
            if not isinstance(payload, dict):
                raise OuraError("Oura API response is invalid")
            return payload
        raise OuraError("Oura API request failed")


class OuraConnector:
    """Synchronize selected Oura resources into Heavenly storage."""

    SOURCE = "oura"

    def __init__(
        self,
        api: OuraAPI | Any,
        store: Any,
        state_store: ProviderStateStore,
        *,
        resources: tuple[str, ...] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.api = api
        self.store = store
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        selected = resources or oura_resources_for_metrics(store.settings.allowed_metrics)
        self.resources = tuple(dict.fromkeys(selected))
        if not self.resources:
            raise OuraError("No allowlisted metric maps to an Oura resource")

    def sync(self, *, days: int = 7, limit: int = 1000) -> dict[str, Any]:
        bounded_days = max(1, min(int(days), 31))
        bounded_limit = max(1, min(int(limit), 10_000))
        now = self._aware_now()
        state = self.state_store.load(self.SOURCE)
        checkpoints = dict(state.get("checkpoints", {})) if isinstance(state.get("checkpoints"), Mapping) else {}
        identity = self.api.identity()
        identity_value = str(identity.get("id") or identity.get("email"))
        records_processed = 0
        events_upserted = 0
        skipped: list[str] = []
        next_checkpoints = dict(checkpoints)
        for resource in self.resources:
            remaining = bounded_limit - records_processed
            if remaining <= 0:
                break
            checkpoint = checkpoints.get(resource)
            start = (
                _parse_timestamp(checkpoint) - timedelta(days=1)
                if isinstance(checkpoint, str)
                else now - timedelta(days=bounded_days)
            )
            try:
                records = self.api.list_records(
                    resource,
                    start=_timestamp(start),
                    end=_timestamp(now),
                    limit=remaining,
                )
            except OuraResourceForbidden:
                skipped.append(resource)
                continue
            for record in records:
                events = normalize_oura_record(
                    resource,
                    record,
                    allowed_metrics=self.store.settings.allowed_metrics,
                )
                event_at = str(events[0]["event_at"]) if events else _timestamp(now)
                events_upserted += self.store.ingest_provider_resource(
                    source=self.SOURCE,
                    resource_type=resource,
                    source_record_id=_oura_source_record_id(resource, record),
                    event_at=event_at,
                    payload=record,
                    events=events,
                    ingest_mode="backfill" if bounded_days > 1 else "live",
                )
                records_processed += 1
            next_checkpoints[resource] = _timestamp(now)
        self.state_store.save(
            self.SOURCE,
            {
                "connected": True,
                "identity_hash": hashlib.sha256(identity_value.encode()).hexdigest(),
                "checkpoints": next_checkpoints,
                "last_sync_at": _timestamp(now),
                "data_types": [r for r in self.resources if r not in skipped],
            },
        )
        return {
            "source": self.SOURCE,
            "records_processed": records_processed,
            "events_upserted": events_upserted,
            "skipped_resources": skipped,
            "status": "completed",
        }

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise OuraError("Oura clock must be timezone-aware")
        return now.astimezone(timezone.utc)


def oura_resources_for_metrics(allowed_metrics: frozenset[str]) -> tuple[str, ...]:
    return tuple(
        resource
        for resource, metrics in _RESOURCE_METRICS.items()
        if metrics & allowed_metrics
    )


def normalize_oura_record(
    resource: str,
    record: Mapping[str, Any],
    *,
    allowed_metrics: frozenset[str],
) -> list[dict[str, Any]]:
    if resource not in _RESOURCE_METRICS:
        return []
    event_at = _oura_event_timestamp(record)
    if event_at is None:
        return []
    values: list[tuple[str, float | int | None, str]] = []
    if resource == "daily_activity":
        values = [
            ("steps", _number(record.get("steps")), "count"),
            ("active_energy", _number(record.get("active_calories")), "kcal"),
        ]
    elif resource == "sleep":
        duration = _number(record.get("total_sleep_duration"))
        values = [
            ("sleep_analysis", round(duration / 60, 3) if duration else None, "min"),
            ("heart_rate_variability", _number(record.get("average_hrv")), "ms"),
            ("resting_heart_rate", _number(record.get("lowest_heart_rate")), "bpm"),
            ("respiratory_rate", _number(record.get("average_breath")), "breaths/min"),
        ]
    elif resource == "daily_spo2":
        percentage = record.get("spo2_percentage")
        average = _number(percentage.get("average")) if isinstance(percentage, Mapping) else None
        values = [("oxygen_saturation", average, "%")]
    events: list[dict[str, Any]] = []
    source_record_id = _oura_source_record_id(resource, record)
    for metric, value, unit in values:
        if metric not in allowed_metrics or value is None:
            continue
        events.append(
            {
                "source": "oura",
                "metric_type": metric,
                "event_at": event_at,
                "value_numeric": value,
                "value_text": None,
                "unit": unit,
                "source_record_id": f"{source_record_id}:{metric}",
                "metadata": {"schema_version": "1.0", "provider_data_type": resource},
                "is_synthetic": False,
            }
        )
    return events


def _oura_event_timestamp(record: Mapping[str, Any]) -> str | None:
    for key in ("timestamp", "bedtime_end", "day"):
        value = record.get(key)
        if not isinstance(value, str):
            continue
        candidate = value if "T" in value else f"{value}T00:00:00+00:00"
        parsed = _physical_time(candidate)
        if parsed is not None:
            return _timestamp(parsed)
    return None


def _oura_source_record_id(resource: str, record: Mapping[str, Any]) -> str:
    identity = record.get("id")
    if identity is None or (isinstance(identity, str) and not identity.strip()):
        identity = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
    return f"oura:{resource}:{hashlib.sha256(str(identity).encode()).hexdigest()}"


def _number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not (float("-inf") < numeric < float("inf")):
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _physical_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime:
    parsed = _physical_time(value)
    if parsed is None:
        raise OuraError("Oura sync timestamp is invalid")
    return parsed


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
