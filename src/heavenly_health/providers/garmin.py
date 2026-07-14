"""Garmin Connect Health OAuth, partner-configured pull sync, and normalization."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlencode, urlparse

import httpx

from heavenly_health.identity import (
    deterministic_source_record_id,
    normalized_event_source_record_id,
    provider_source_record_id,
)
from heavenly_health.providers.common import (
    OAuthToken,
    ProviderConfigurationError,
    ProviderStateStore,
    SecretStore,
    bounded_retry_delay,
    read_private_json,
    validate_https_url,
)


GARMIN_CALLBACK_URL = "http://127.0.0.1:8791/providers/garmin/oauth/callback"
_RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,199}$")
_RESOURCE_METRICS = {
    "dailies": frozenset(
        {
            "steps",
            "resting_heart_rate",
            "active_energy",
            "stress_level",
            "body_battery",
            "oxygen_saturation",
            "respiratory_rate",
        }
    ),
    "sleeps": frozenset({"sleep_analysis"}),
    "body_compositions": frozenset({"body_mass"}),
    "epochs": frozenset({"steps", "heart_rate", "active_energy"}),
    "pulse_ox": frozenset({"oxygen_saturation"}),
    "respiration": frozenset({"respiratory_rate"}),
}


class GarminHealthError(ProviderConfigurationError):
    """Garmin partner configuration, OAuth, API data, or sync is invalid."""


@dataclass(frozen=True)
class GarminClientCredentials:
    """Garmin-issued OAuth and Health API configuration stored as one secret."""

    client_id: str
    client_secret: str = field(repr=False)
    authorization_url: str
    token_url: str
    api_base_url: str
    redirect_uri: str
    scopes: tuple[str, ...]
    identity_path: str
    resource_paths: Mapping[str, str]
    revocation_url: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_paths", MappingProxyType(dict(self.resource_paths)))

    @classmethod
    def from_private_json(cls, path: Path) -> GarminClientCredentials:
        try:
            return cls.from_payload(read_private_json(path))
        except ProviderConfigurationError as error:
            raise GarminHealthError(str(error)) from error

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> GarminClientCredentials:
        client_id = payload.get("client_id")
        client_secret = payload.get("client_secret")
        authorization_url = payload.get("authorization_url")
        token_url = payload.get("token_url")
        api_base_url = payload.get("api_base_url")
        redirect_uri = payload.get("redirect_uri")
        scopes = payload.get("scopes")
        identity_path = payload.get("identity_path")
        resource_paths = payload.get("resource_paths")
        revocation_url = payload.get("revocation_url")
        if (
            not isinstance(client_id, str)
            or not client_id.strip()
            or not isinstance(client_secret, str)
            or not client_secret.strip()
            or not isinstance(authorization_url, str)
            or not isinstance(token_url, str)
            or not isinstance(api_base_url, str)
            or redirect_uri != GARMIN_CALLBACK_URL
            or not isinstance(scopes, list)
            or not scopes
            or len(scopes) > 20
            or not all(isinstance(scope, str) and _SCOPE.fullmatch(scope) for scope in scopes)
            or not isinstance(identity_path, str)
            or not isinstance(resource_paths, Mapping)
            or not resource_paths
        ):
            raise GarminHealthError("Garmin partner OAuth configuration is incomplete")
        try:
            authorization_url = validate_https_url(
                authorization_url,
                name="Garmin authorization URL",
            )
            token_url = validate_https_url(token_url, name="Garmin token URL")
            api_base_url = validate_https_url(api_base_url, name="Garmin API base URL").rstrip("/")
            if revocation_url is not None:
                if not isinstance(revocation_url, str):
                    raise ProviderConfigurationError("Garmin revocation URL must be HTTPS")
                revocation_url = validate_https_url(revocation_url, name="Garmin revocation URL")
            validated_identity = _api_path(identity_path, "Garmin identity path")
            validated_paths = {
                str(resource): _api_path(str(path), "Garmin resource path")
                for resource, path in resource_paths.items()
            }
        except ProviderConfigurationError as error:
            raise GarminHealthError(str(error)) from error
        if any(
            _RESOURCE_NAME.fullmatch(resource) is None or resource not in _RESOURCE_METRICS
            for resource in validated_paths
        ):
            raise GarminHealthError("Garmin resource mapping contains an unsupported name")
        return cls(
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            authorization_url=authorization_url,
            token_url=token_url,
            api_base_url=api_base_url,
            redirect_uri=GARMIN_CALLBACK_URL,
            scopes=tuple(dict.fromkeys(scopes)),
            identity_path=validated_identity,
            resource_paths=validated_paths,
            revocation_url=revocation_url,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "authorization_url": self.authorization_url,
                "token_url": self.token_url,
                "api_base_url": self.api_base_url,
                "redirect_uri": self.redirect_uri,
                "scopes": list(self.scopes),
                "identity_path": self.identity_path,
                "resource_paths": dict(self.resource_paths),
                "revocation_url": self.revocation_url,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> GarminClientCredentials:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as error:
            raise GarminHealthError("Stored Garmin partner configuration is invalid") from error
        if not isinstance(payload, Mapping):
            raise GarminHealthError("Stored Garmin partner configuration is invalid")
        return cls.from_payload(payload)


@dataclass(frozen=True)
class GarminAuthorizationRequest:
    url: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)


class GarminOAuthClient:
    """Perform partner-configured Garmin OAuth 2.0 lifecycle operations."""

    SERVICE = "garmin-health"
    CLIENT_ACCOUNT = "oauth-client"
    TOKEN_ACCOUNT = "oauth-token"

    def __init__(
        self,
        credentials: GarminClientCredentials,
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
    def load(cls, secret_store: SecretStore, **kwargs: Any) -> GarminOAuthClient:
        saved = secret_store.get(cls.SERVICE, cls.CLIENT_ACCOUNT)
        if saved is None:
            raise GarminHealthError("Garmin partner client is not imported")
        return cls(GarminClientCredentials.from_json(saved), secret_store, **kwargs)

    @classmethod
    def import_credentials(
        cls,
        path: Path,
        secret_store: SecretStore,
    ) -> GarminClientCredentials:
        credentials = GarminClientCredentials.from_private_json(path)
        secret_store.set(cls.SERVICE, cls.CLIENT_ACCOUNT, credentials.to_json())
        return credentials

    def authorization_request(self) -> GarminAuthorizationRequest:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        query = urlencode(
            {
                "client_id": self.credentials.client_id,
                "redirect_uri": self.credentials.redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.credentials.scopes),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return GarminAuthorizationRequest(
            url=f"{self.credentials.authorization_url}?{query}",
            state=state,
            code_verifier=verifier,
        )

    def exchange_code(self, code: str, *, code_verifier: str) -> OAuthToken:
        if not code.strip() or not code_verifier.strip():
            raise GarminHealthError("Garmin OAuth callback is incomplete")
        token = self._token_request(
            {
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": self.credentials.redirect_uri,
            }
        )
        self.secret_store.set(self.SERVICE, self.TOKEN_ACCOUNT, token.to_json())
        return token

    def refresh(self, token: OAuthToken) -> OAuthToken:
        if not token.refresh_token:
            raise GarminHealthError("Garmin refresh token is unavailable; reconnect is required")
        refreshed = self._token_request(
            {
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "refresh_token": token.refresh_token,
                "grant_type": "refresh_token",
            },
            previous_refresh_token=token.refresh_token,
        )
        self.secret_store.set(self.SERVICE, self.TOKEN_ACCOUNT, refreshed.to_json())
        return refreshed

    def access_token(self) -> str:
        saved = self.secret_store.get(self.SERVICE, self.TOKEN_ACCOUNT)
        if saved is None:
            raise GarminHealthError("Garmin Health is not connected")
        token = OAuthToken.from_json(saved)
        if token.needs_refresh(self._clock()):
            token = self.refresh(token)
        return token.access_token

    def revoke(self) -> bool:
        saved = self.secret_store.get(self.SERVICE, self.TOKEN_ACCOUNT)
        remotely_revoked = False
        if saved is not None and self.credentials.revocation_url:
            token = OAuthToken.from_json(saved)
            value = token.refresh_token or token.access_token
            try:
                response = self._client.post(
                    self.credentials.revocation_url,
                    data={
                        "token": value,
                        "client_id": self.credentials.client_id,
                        "client_secret": self.credentials.client_secret,
                    },
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                remotely_revoked = True
            except httpx.HTTPError as error:
                raise GarminHealthError("Garmin token revocation failed") from error
        self.secret_store.delete(self.SERVICE, self.TOKEN_ACCOUNT)
        return remotely_revoked

    def _token_request(
        self,
        data: Mapping[str, str],
        *,
        previous_refresh_token: str | None = None,
    ) -> OAuthToken:
        try:
            response = self._client.post(
                self.credentials.token_url,
                data=data,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise GarminHealthError("Garmin OAuth token exchange failed") from error
        if not isinstance(payload, Mapping):
            raise GarminHealthError("Garmin OAuth token response is invalid")
        try:
            return OAuthToken.from_response(
                payload,
                now=self._clock(),
                previous_refresh_token=previous_refresh_token,
            )
        except ProviderConfigurationError as error:
            raise GarminHealthError(str(error)) from error


class GarminHealthAPI:
    """Read bounded Garmin Health resources from partner-issued endpoint paths."""

    def __init__(
        self,
        credentials: GarminClientCredentials,
        token_provider: Callable[[], str],
        *,
        http_client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.credentials = credentials
        self._token_provider = token_provider
        self._client = http_client or httpx.Client(timeout=30, follow_redirects=False)
        self._sleep = sleeper or time.sleep

    def identity(self) -> dict[str, Any]:
        payload = self._get(self.credentials.identity_path)
        if not isinstance(payload, Mapping):
            raise GarminHealthError("Garmin identity response is invalid")
        identity = next(
            (
                payload.get(name)
                for name in ("userId", "user_id", "id")
                if isinstance(payload.get(name), (str, int))
            ),
            None,
        )
        if identity is None:
            raise GarminHealthError("Garmin identity response is invalid")
        return dict(payload)

    def list_resources(
        self,
        resource_type: str,
        *,
        start: str,
        end: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        path = self.credentials.resource_paths.get(resource_type)
        if path is None:
            raise GarminHealthError("Garmin resource is not configured")
        start_at = _parse_timestamp(start)
        end_at = _parse_timestamp(end)
        if end_at <= start_at or end_at - start_at > timedelta(days=32):
            raise GarminHealthError("Garmin sync window is invalid")
        bounded_limit = max(1, min(int(limit), 10_000))
        params = {
            "uploadStartTimeInSeconds": str(int(start_at.timestamp())),
            "uploadEndTimeInSeconds": str(int(end_at.timestamp())),
        }
        resources: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        for _ in range(100):
            payload = self._get(path, params=params)
            if isinstance(payload, list):
                page: list[Any] = payload
                next_token = None
            elif isinstance(payload, Mapping):
                page_value = next(
                    (
                        payload.get(key)
                        for key in (resource_type, "summaries", "data", "items")
                        if isinstance(payload.get(key), list)
                    ),
                    [],
                )
                page = page_value if isinstance(page_value, list) else []
                next_token = payload.get("nextPageToken")
            else:
                raise GarminHealthError("Garmin resource response is invalid")
            if not all(isinstance(item, Mapping) for item in page):
                raise GarminHealthError("Garmin resource response is invalid")
            resources.extend(dict(item) for item in page[: bounded_limit - len(resources)])
            if len(resources) >= bounded_limit or not isinstance(next_token, str) or not next_token:
                break
            if next_token in seen_tokens:
                raise GarminHealthError("Garmin pagination token repeated")
            seen_tokens.add(next_token)
            params["pageToken"] = next_token
        return resources

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any] | list[Any]:
        for attempt in range(3):
            try:
                response = self._client.get(
                    f"{self.credentials.api_base_url}{path}",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._token_provider()}",
                        "Accept": "application/json",
                    },
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        self._sleep(bounded_retry_delay(response.headers, attempt))
                        continue
                response.raise_for_status()
                payload = response.json()
            except httpx.TransportError as error:
                if attempt < 2:
                    self._sleep(bounded_retry_delay({}, attempt))
                    continue
                raise GarminHealthError("Garmin Health API request failed") from error
            except (httpx.HTTPError, ValueError, TypeError) as error:
                raise GarminHealthError("Garmin Health API request failed") from error
            if not isinstance(payload, (Mapping, list)):
                raise GarminHealthError("Garmin Health API response is invalid")
            return payload
        raise GarminHealthError("Garmin Health API request failed")


class ProviderHealthStore(Protocol):
    settings: Any

    def ingest_provider_resource(self, **kwargs: Any) -> int: ...


class GarminHealthConnector:
    """Synchronize configured Garmin resources into Heavenly storage."""

    SOURCE = "garmin"

    def __init__(
        self,
        api: GarminHealthAPI | Any,
        store: ProviderHealthStore,
        state_store: ProviderStateStore,
        *,
        resource_types: tuple[str, ...] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.api = api
        self.store = store
        self.state_store = state_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        configured = tuple(api.credentials.resource_paths)
        selected = resource_types or resource_types_for_metrics(
            store.settings.allowed_metrics,
            configured=configured,
        )
        self.resource_types = tuple(dict.fromkeys(selected))
        if not self.resource_types:
            raise GarminHealthError("No configured Garmin resource maps to an allowlisted metric")

    def sync(self, *, days: int = 7, limit: int = 1000) -> dict[str, Any]:
        bounded_days = max(1, min(int(days), 31))
        bounded_limit = max(1, min(int(limit), 10_000))
        now = self._aware_now()
        state = self.state_store.load(self.SOURCE)
        checkpoints = dict(state.get("checkpoints", {})) if isinstance(state.get("checkpoints"), Mapping) else {}
        identity_payload = self.api.identity()
        identity = next(
            str(identity_payload[name])
            for name in ("userId", "user_id", "id")
            if name in identity_payload
        )
        records_processed = 0
        events_upserted = 0
        next_checkpoints = dict(checkpoints)
        for resource_type in self.resource_types:
            remaining = bounded_limit - records_processed
            if remaining <= 0:
                break
            checkpoint = checkpoints.get(resource_type)
            start = (
                _parse_timestamp(checkpoint) - timedelta(hours=1)
                if isinstance(checkpoint, str)
                else now - timedelta(days=bounded_days)
            )
            resources = self.api.list_resources(
                resource_type,
                start=_timestamp(start),
                end=_timestamp(now),
                limit=remaining,
            )
            for resource in resources:
                events = normalize_garmin_resource(
                    resource_type,
                    resource,
                    allowed_metrics=self.store.settings.allowed_metrics,
                )
                source_record_id = _garmin_source_record_id(resource_type, resource)
                event_at = str(events[0]["event_at"]) if events else _timestamp(now)
                events_upserted += self.store.ingest_provider_resource(
                    source=self.SOURCE,
                    resource_type=resource_type,
                    source_record_id=source_record_id,
                    event_at=event_at,
                    payload=resource,
                    events=events,
                    ingest_mode="backfill" if bounded_days > 1 else "live",
                )
                records_processed += 1
            next_checkpoints[resource_type] = _timestamp(now)
        self.state_store.save(
            self.SOURCE,
            {
                "connected": True,
                "identity_hash": hashlib.sha256(identity.encode()).hexdigest(),
                "checkpoints": next_checkpoints,
                "last_sync_at": _timestamp(now),
                "data_types": list(self.resource_types),
            },
        )
        return {
            "source": self.SOURCE,
            "records_processed": records_processed,
            "events_upserted": events_upserted,
            "status": "completed",
        }

    def status(self) -> dict[str, Any]:
        state = self.state_store.load(self.SOURCE)
        return {
            "source": self.SOURCE,
            "connected": state.get("connected") is True,
            "sync_supported": True,
            "last_sync_at": state.get("last_sync_at"),
            "data_types": list(state.get("data_types", self.resource_types)),
        }

    def disconnect(self, oauth: GarminOAuthClient) -> bool:
        remotely_revoked = oauth.revoke()
        self.state_store.delete(self.SOURCE)
        return remotely_revoked

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise GarminHealthError("Garmin Health clock must be timezone-aware")
        return value.astimezone(timezone.utc)


def resource_types_for_metrics(
    allowed_metrics: frozenset[str],
    *,
    configured: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        resource
        for resource in configured
        if resource in _RESOURCE_METRICS and _RESOURCE_METRICS[resource] & allowed_metrics
    )


def normalize_garmin_resource(
    resource_type: str,
    resource: Mapping[str, Any],
    *,
    allowed_metrics: frozenset[str],
) -> list[dict[str, Any]]:
    if resource_type not in _RESOURCE_METRICS:
        return []
    event_at = _garmin_event_time(resource)
    if event_at is None:
        return []
    raw_identity = _garmin_source_record_id(resource_type, resource)
    values = _garmin_values(resource_type, resource)
    events: list[dict[str, Any]] = []
    for metric, (value, unit) in values.items():
        if metric not in allowed_metrics or value is None:
            continue
        events.append(
            {
                "source": "garmin",
                "metric_type": metric,
                "event_at": event_at,
                "value_numeric": value,
                "value_text": None,
                "unit": unit,
                "source_record_id": normalized_event_source_record_id(raw_identity, metric),
                "metadata": {
                    "schema_version": "1.0",
                    "provider_resource_type": resource_type,
                },
                "is_synthetic": False,
            }
        )
    return events


def _garmin_values(
    resource_type: str,
    resource: Mapping[str, Any],
) -> dict[str, tuple[float | int | None, str]]:
    if resource_type == "dailies":
        return {
            "steps": (_number(resource.get("steps")), "count"),
            "resting_heart_rate": (
                _number(resource.get("restingHeartRateInBeatsPerMinute")),
                "bpm",
            ),
            "active_energy": (_number(resource.get("activeKilocalories")), "kcal"),
            "stress_level": (_number(resource.get("averageStressLevel")), "score"),
            "body_battery": (
                _number(resource.get("bodyBatteryMostRecentValue")),
                "score",
            ),
            "oxygen_saturation": (
                _first_number(resource, "averageSpO2", "averageSpo2"),
                "%",
            ),
            "respiratory_rate": (
                _first_number(resource, "averageRespirationValue", "avgWakingRespirationValue"),
                "breaths/min",
            ),
        }
    if resource_type == "sleeps":
        seconds = _number(resource.get("durationInSeconds"))
        return {
            "sleep_analysis": (
                round(float(seconds) / 60, 3) if seconds is not None else None,
                "min",
            )
        }
    if resource_type == "body_compositions":
        grams = _number(resource.get("weightInGrams"))
        return {
            "body_mass": (
                round(float(grams) / 1000, 3) if grams is not None else None,
                "kg",
            )
        }
    if resource_type == "epochs":
        return {
            "steps": (_number(resource.get("steps")), "count"),
            "heart_rate": (
                _first_number(resource, "heartRate", "heartRateInBeatsPerMinute"),
                "bpm",
            ),
            "active_energy": (_number(resource.get("activeKilocalories")), "kcal"),
        }
    if resource_type == "pulse_ox":
        return {
            "oxygen_saturation": (
                _first_number(resource, "averageSpO2", "averageSpo2", "spo2"),
                "%",
            )
        }
    if resource_type == "respiration":
        return {
            "respiratory_rate": (
                _first_number(resource, "avgWakingRespirationValue", "averageRespirationValue"),
                "breaths/min",
            )
        }
    return {}


def _garmin_source_record_id(resource_type: str, resource: Mapping[str, Any]) -> str:
    native_id = next(
        (
            str(resource[name]).strip()
            for name in ("summaryId", "summary_id", "id")
            if resource.get(name) is not None and str(resource[name]).strip()
        ),
        None,
    )
    if native_id is not None:
        return provider_source_record_id("garmin", resource_type, native_id)
    return deterministic_source_record_id(
        "garmin",
        resource_type,
        {
            "event_at": _garmin_event_time(resource),
            "payload_sha256": hashlib.sha256(
                json.dumps(resource, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
    )


def _garmin_event_time(resource: Mapping[str, Any]) -> str | None:
    for name in (
        "startTimeInSeconds",
        "sleepStartTimestampGMT",
        "measurementTimeInSeconds",
        "uploadStartTimeInSeconds",
    ):
        value = _number(resource.get(name))
        if value is not None:
            try:
                return _timestamp(datetime.fromtimestamp(float(value), timezone.utc))
            except (OverflowError, OSError, ValueError):
                return None
    calendar_date = resource.get("calendarDate")
    if isinstance(calendar_date, str):
        try:
            parsed = datetime.fromisoformat(calendar_date).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return _timestamp(parsed)
    return None


def _first_number(resource: Mapping[str, Any], *names: str) -> float | int | None:
    for name in names:
        value = _number(resource.get(name))
        if value is not None:
            return value
    return None


def _number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _api_path(value: str, name: str) -> str:
    parsed = urlparse(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or len(value) > 512
    ):
        raise ProviderConfigurationError(f"{name} must be one absolute API path")
    return value


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise GarminHealthError("Garmin sync timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GarminHealthError("Garmin sync timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GarminHealthError("Garmin sync timestamp is invalid")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
