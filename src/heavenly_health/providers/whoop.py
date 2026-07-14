"""WHOOP OAuth, bounded synchronization, and normalization.

The connect flow mirrors the owner's proven manual setup: client credentials
live in an owner-only env file, authorization happens in the user's browser,
and the redirected URL is pasted back because WHOOP developer apps commonly
register a non-loopback redirect URI. WHOOP's edge WAF rejects Python's
default HTTP user agent, so every request sends a browser user agent.
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
from urllib.parse import parse_qs, urlencode, urlparse

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

WHOOP_AUTHORIZATION_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API_BASE = "https://api.prod.whoop.com/developer"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36"
)
_KJ_PER_KCAL = 4.184

_RESOURCE_METRICS: dict[str, frozenset[str]] = {
    "recovery": frozenset({"heart_rate_variability", "resting_heart_rate", "oxygen_saturation"}),
    "sleep": frozenset({"sleep_analysis", "respiratory_rate"}),
    "cycle": frozenset({"active_energy"}),
}
_RESOURCE_PATHS = {
    "recovery": "/v2/recovery",
    "sleep": "/v2/activity/sleep",
    "cycle": "/v2/cycle",
}


class WhoopError(ProviderConfigurationError):
    """WHOOP credentials, OAuth, API data, or synchronization is invalid."""


@dataclass(frozen=True)
class WhoopClientCredentials:
    """Validated WHOOP developer-app credentials from an owner-only env file."""

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    scopes: tuple[str, ...]

    @classmethod
    def from_private_env(cls, path: Path) -> WhoopClientCredentials:
        values = read_private_env(path)
        return cls.from_mapping(values)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> WhoopClientCredentials:
        client_id = values.get("WHOOP_CLIENT_ID", "").strip()
        client_secret = values.get("WHOOP_CLIENT_SECRET", "").strip()
        redirect_uri = values.get("WHOOP_REDIRECT_URI", "").strip()
        scopes = tuple(scope for scope in values.get("WHOOP_SCOPES", "").split() if scope)
        if not client_id or not client_secret or not redirect_uri or not scopes or any(
            "PASTE_" in value for value in (client_id, client_secret, redirect_uri)
        ):
            raise WhoopError(
                "WHOOP credentials need WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET,"
                " WHOOP_REDIRECT_URI, and WHOOP_SCOPES"
            )
        validate_https_url(redirect_uri, name="WHOOP redirect URI")
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
    def from_json(cls, value: str) -> WhoopClientCredentials:
        try:
            payload = json.loads(value)
            if not isinstance(payload, Mapping):
                raise ValueError
            return cls.from_mapping(
                {
                    "WHOOP_CLIENT_ID": str(payload.get("client_id", "")),
                    "WHOOP_CLIENT_SECRET": str(payload.get("client_secret", "")),
                    "WHOOP_REDIRECT_URI": str(payload.get("redirect_uri", "")),
                    "WHOOP_SCOPES": " ".join(
                        scope for scope in payload.get("scopes", []) if isinstance(scope, str)
                    ),
                }
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise WhoopError("Stored WHOOP OAuth client is invalid") from error


@dataclass(frozen=True)
class PastedAuthorizationRequest:
    url: str
    state: str = field(repr=False)


class WhoopOAuthClient:
    """WHOOP authorization-code exchange, refresh, and local revocation."""

    SERVICE = "whoop"
    CLIENT_ACCOUNT = "oauth-client"
    TOKEN_ACCOUNT = "oauth-token"

    def __init__(
        self,
        credentials: WhoopClientCredentials,
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
    def load(cls, secret_store: SecretStore, **kwargs: Any) -> WhoopOAuthClient:
        saved = secret_store.get(cls.SERVICE, cls.CLIENT_ACCOUNT)
        if saved is None:
            raise WhoopError("WHOOP client credentials are not imported")
        return cls(WhoopClientCredentials.from_json(saved), secret_store, **kwargs)

    @classmethod
    def import_credentials(cls, path: Path, secret_store: SecretStore) -> WhoopClientCredentials:
        credentials = WhoopClientCredentials.from_private_env(path)
        secret_store.set(cls.SERVICE, cls.CLIENT_ACCOUNT, credentials.to_json())
        return credentials

    def authorization_request(self) -> PastedAuthorizationRequest:
        # WHOOP documents a minimum eight-character state value.
        state = secrets.token_hex(8)
        query = urlencode(
            {
                "client_id": self.credentials.client_id,
                "redirect_uri": self.credentials.redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.credentials.scopes),
                "state": state,
            }
        )
        return PastedAuthorizationRequest(url=f"{WHOOP_AUTHORIZATION_URL}?{query}", state=state)

    def parse_callback(self, returned_url: str, *, expected_state: str) -> str:
        return parse_pasted_callback(
            returned_url,
            redirect_uri=self.credentials.redirect_uri,
            expected_state=expected_state,
            error_class=WhoopError,
        )

    def exchange_code(self, code: str) -> OAuthToken:
        if not code.strip():
            raise WhoopError("WHOOP authorization code is missing")
        token = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "redirect_uri": self.credentials.redirect_uri,
            }
        )
        if token.refresh_token is None:
            raise WhoopError("WHOOP did not return a refresh token; include the offline scope")
        self.secret_store.set(self.SERVICE, self.TOKEN_ACCOUNT, token.to_json())
        return token

    def refresh(self, token: OAuthToken) -> OAuthToken:
        if not token.refresh_token:
            raise WhoopError("WHOOP refresh token is unavailable; reconnect is required")
        refreshed = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "scope": "offline",
            },
            previous_refresh_token=token.refresh_token,
        )
        self.secret_store.set(self.SERVICE, self.TOKEN_ACCOUNT, refreshed.to_json())
        return refreshed

    def access_token(self) -> str:
        saved = self.secret_store.get(self.SERVICE, self.TOKEN_ACCOUNT)
        if saved is None:
            raise WhoopError("WHOOP is not connected")
        token = OAuthToken.from_json(saved)
        if token.needs_refresh(self._clock()):
            token = self.refresh(token)
        return token.access_token

    def revoke(self) -> bool:
        """Delete local tokens. WHOOP has no public token revocation endpoint."""
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
                WHOOP_TOKEN_URL,
                data=data,
                headers={
                    "Accept": "application/json",
                    "User-Agent": BROWSER_USER_AGENT,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as error:
            raise WhoopError("WHOOP OAuth token exchange failed") from error
        if not isinstance(payload, Mapping):
            raise WhoopError("WHOOP OAuth token response is invalid")
        try:
            return OAuthToken.from_response(
                payload,
                now=self._clock(),
                previous_refresh_token=previous_refresh_token,
            )
        except ProviderConfigurationError as error:
            raise WhoopError(str(error)) from error


class WhoopAPI:
    """Bounded read-only WHOOP developer API client."""

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

    def identity(self) -> dict[str, Any]:
        payload = self._get("/v2/user/profile/basic")
        if "user_id" not in payload:
            raise WhoopError("WHOOP identity response is invalid")
        return payload

    def list_records(
        self,
        resource: str,
        *,
        start: str,
        end: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        path = _RESOURCE_PATHS.get(resource)
        if path is None:
            raise WhoopError("Unsupported WHOOP resource")
        bounded_limit = max(1, min(int(limit), 10_000))
        params: dict[str, str] = {
            "start": start,
            "end": end,
            "limit": str(min(bounded_limit, 25)),
        }
        records: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        for _ in range(100):
            payload = self._get(path, params=params)
            page = payload.get("records", [])
            if not isinstance(page, list) or not all(isinstance(item, Mapping) for item in page):
                raise WhoopError("WHOOP data page is invalid")
            records.extend(dict(item) for item in page[: bounded_limit - len(records)])
            if len(records) >= bounded_limit:
                break
            next_token = payload.get("next_token")
            if not isinstance(next_token, str) or not next_token:
                break
            if next_token in seen_tokens:
                raise WhoopError("WHOOP pagination token repeated")
            seen_tokens.add(next_token)
            params["nextToken"] = next_token
        return records

    def _get(self, path: str, *, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        for attempt in range(3):
            try:
                response = self._client.get(
                    f"{WHOOP_API_BASE}{path}",
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._token_provider()}",
                        "Accept": "application/json",
                        "User-Agent": BROWSER_USER_AGENT,
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
                raise WhoopError("WHOOP API request failed") from error
            except (httpx.HTTPError, ValueError, TypeError) as error:
                raise WhoopError("WHOOP API request failed") from error
            if not isinstance(payload, dict):
                raise WhoopError("WHOOP API response is invalid")
            return payload
        raise WhoopError("WHOOP API request failed")


class WhoopConnector:
    """Synchronize selected WHOOP resources into Heavenly storage."""

    SOURCE = "whoop"

    def __init__(
        self,
        api: WhoopAPI | Any,
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
        selected = resources or whoop_resources_for_metrics(store.settings.allowed_metrics)
        self.resources = tuple(dict.fromkeys(selected))
        if not self.resources:
            raise WhoopError("No allowlisted metric maps to a WHOOP resource")

    def sync(self, *, days: int = 7, limit: int = 1000) -> dict[str, Any]:
        bounded_days = max(1, min(int(days), 31))
        bounded_limit = max(1, min(int(limit), 10_000))
        now = self._aware_now()
        state = self.state_store.load(self.SOURCE)
        checkpoints = dict(state.get("checkpoints", {})) if isinstance(state.get("checkpoints"), Mapping) else {}
        identity = self.api.identity()
        identity_value = str(identity["user_id"])
        records_processed = 0
        events_upserted = 0
        next_checkpoints = dict(checkpoints)
        for resource in self.resources:
            remaining = bounded_limit - records_processed
            if remaining <= 0:
                break
            checkpoint = checkpoints.get(resource)
            start = (
                _parse_timestamp(checkpoint) - timedelta(hours=1)
                if isinstance(checkpoint, str)
                else now - timedelta(days=bounded_days)
            )
            records = self.api.list_records(
                resource,
                start=_timestamp(start),
                end=_timestamp(now),
                limit=remaining,
            )
            for record in records:
                events = normalize_whoop_record(
                    resource,
                    record,
                    allowed_metrics=self.store.settings.allowed_metrics,
                )
                event_at = str(events[0]["event_at"]) if events else _timestamp(now)
                events_upserted += self.store.ingest_provider_resource(
                    source=self.SOURCE,
                    resource_type=resource,
                    source_record_id=_whoop_source_record_id(resource, record),
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
                "data_types": list(self.resources),
            },
        )
        return {
            "source": self.SOURCE,
            "records_processed": records_processed,
            "events_upserted": events_upserted,
            "status": "completed",
        }

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise WhoopError("WHOOP clock must be timezone-aware")
        return now.astimezone(timezone.utc)


def whoop_resources_for_metrics(allowed_metrics: frozenset[str]) -> tuple[str, ...]:
    return tuple(
        resource
        for resource, metrics in _RESOURCE_METRICS.items()
        if metrics & allowed_metrics
    )


def normalize_whoop_record(
    resource: str,
    record: Mapping[str, Any],
    *,
    allowed_metrics: frozenset[str],
) -> list[dict[str, Any]]:
    metrics = _RESOURCE_METRICS.get(resource)
    if metrics is None:
        return []
    event_at = _whoop_event_timestamp(resource, record)
    if event_at is None:
        return []
    score = record.get("score")
    score = score if isinstance(score, Mapping) else {}
    values: list[tuple[str, float | int | None, str]] = []
    if resource == "recovery":
        values = [
            ("heart_rate_variability", _number(score.get("hrv_rmssd_milli")), "ms"),
            ("resting_heart_rate", _number(score.get("resting_heart_rate")), "bpm"),
            ("oxygen_saturation", _number(score.get("spo2_percentage")), "%"),
        ]
    elif resource == "sleep":
        stages = score.get("stage_summary")
        stages = stages if isinstance(stages, Mapping) else {}
        asleep_milli = sum(
            value
            for value in (
                _number(stages.get("total_light_sleep_time_milli")),
                _number(stages.get("total_slow_wave_sleep_time_milli")),
                _number(stages.get("total_rem_sleep_time_milli")),
            )
            if value is not None
        )
        values = [
            ("sleep_analysis", round(asleep_milli / 60_000, 3) if asleep_milli else None, "min"),
            ("respiratory_rate", _number(score.get("respiratory_rate")), "breaths/min"),
        ]
    elif resource == "cycle":
        kilojoule = _number(score.get("kilojoule"))
        values = [
            ("active_energy", round(kilojoule / _KJ_PER_KCAL, 3) if kilojoule else None, "kcal"),
        ]
    events: list[dict[str, Any]] = []
    source_record_id = _whoop_source_record_id(resource, record)
    for metric, value, unit in values:
        if metric not in allowed_metrics or value is None:
            continue
        events.append(
            {
                "source": "whoop",
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


def parse_pasted_callback(
    returned_url: str,
    *,
    redirect_uri: str,
    expected_state: str,
    error_class: type[ProviderConfigurationError] = ProviderConfigurationError,
) -> str:
    """Validate a pasted OAuth redirect URL and return its authorization code."""
    returned = urlparse(returned_url.strip())
    configured = urlparse(redirect_uri)
    if (
        returned.scheme != configured.scheme
        or returned.netloc != configured.netloc
        or returned.path.rstrip("/") != configured.path.rstrip("/")
    ):
        raise error_class("The pasted URL does not match the registered redirect URI")
    query = parse_qs(returned.query)
    if query.get("state", [None])[0] != expected_state:
        raise error_class("OAuth state did not match; authorization was not accepted")
    code = query.get("code", [None])[0]
    if not code:
        reason = query.get("error", ["no authorization code returned"])[0]
        raise error_class(f"Authorization did not return a code: {reason}")
    return code


def _whoop_event_timestamp(resource: str, record: Mapping[str, Any]) -> str | None:
    for key in ("end", "created_at", "updated_at", "start"):
        value = record.get(key)
        parsed = _physical_time(value)
        if parsed is not None:
            return _timestamp(parsed)
    return None


def _whoop_source_record_id(resource: str, record: Mapping[str, Any]) -> str:
    identity = record.get("id")
    if identity is None or (isinstance(identity, str) and not identity.strip()):
        identity = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
    return f"whoop:{resource}:{hashlib.sha256(str(identity).encode()).hexdigest()}"


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
        raise WhoopError("WHOOP sync timestamp is invalid")
    return parsed


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
