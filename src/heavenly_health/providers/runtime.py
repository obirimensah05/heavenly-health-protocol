"""Runtime registry for configured provider connectors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any, Callable

from heavenly_health.providers.common import (
    KeyringSecretStore,
    ProviderConfigurationError,
    ProviderStateStore,
    SecretStore,
    default_provider_state_path,
)
from heavenly_health.providers.google_health import (
    GoogleHealthAPI,
    GoogleHealthConnector,
    GoogleOAuthClient,
    data_types_for_metrics,
    normalize_google_data_point,
)
from heavenly_health.providers.garmin import (
    GarminHealthAPI,
    GarminHealthConnector,
    GarminOAuthClient,
    resource_types_for_metrics,
)
from heavenly_health.providers.oauth_loopback import OAuthCallbackError, receive_oauth_callback
from heavenly_health.providers.oura import OuraAPI, OuraConnector, OuraOAuthClient
from heavenly_health.providers.whoop import WhoopAPI, WhoopConnector, WhoopOAuthClient

import httpx


def _provider_http_client() -> "httpx.Client":
    return httpx.Client(timeout=30, follow_redirects=False)


class ProviderRuntime:
    """Discover, report, and dispatch only explicitly supported providers."""

    SOURCES = ("google_health", "garmin", "whoop", "oura")

    def __init__(
        self,
        *,
        secret_store: SecretStore | None = None,
        state_store: ProviderStateStore | None = None,
        connector_factory: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self.secret_store = secret_store or KeyringSecretStore()
        self.state_store = state_store or ProviderStateStore(default_provider_state_path())
        self._connector_factory = connector_factory or self._default_connector

    def statuses(self) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for source in self.SOURCES:
            state = self.state_store.load(source)
            if not state:
                continue
            statuses.append(
                {
                    "source": source,
                    "connected": state.get("connected") is True,
                    "sync_supported": True,
                    "last_sync_at": state.get("last_sync_at"),
                    "data_types": list(state.get("data_types", [])),
                }
            )
        return statuses

    def import_google_client(self, path: Path) -> dict[str, Any]:
        GoogleOAuthClient.import_credentials(path, self.secret_store)
        return {"source": "google_health", "client_configured": True}

    def connect_google(self, allowed_metrics: frozenset[str]) -> dict[str, Any]:
        oauth = GoogleOAuthClient.load(self.secret_store)
        request = oauth.authorization_request(allowed_metrics)
        callback = _receive_callback(
            authorization_url=request.url,
            callback_url=oauth.credentials.redirect_uri,
            expected_state=request.state,
        )
        token = oauth.exchange_code(callback.code, code_verifier=request.code_verifier)
        identity = GoogleHealthAPI(oauth.access_token).identity()
        identity_value = str(identity["healthUserId"])
        data_types = data_types_for_metrics(allowed_metrics)
        self.state_store.save(
            "google_health",
            {
                "connected": True,
                "identity_hash": hashlib.sha256(identity_value.encode()).hexdigest(),
                "connected_at": _timestamp(datetime.now(timezone.utc)),
                "last_sync_at": None,
                "data_types": list(data_types),
                "checkpoints": {},
            },
        )
        return {
            "source": "google_health",
            "connected": True,
            "granted_scopes": len(token.scopes),
            "data_types": list(data_types),
        }

    def disconnect_google(self, *, remove_client: bool = False) -> dict[str, Any]:
        oauth = GoogleOAuthClient.load(self.secret_store)
        oauth.revoke()
        if remove_client:
            self.secret_store.delete(
                GoogleOAuthClient.SERVICE,
                GoogleOAuthClient.CLIENT_ACCOUNT,
            )
        self.state_store.delete("google_health")
        return {"source": "google_health", "connected": False}

    def import_garmin_client(self, path: Path) -> dict[str, Any]:
        credentials = GarminOAuthClient.import_credentials(path, self.secret_store)
        return {
            "source": "garmin",
            "client_configured": True,
            "resources": len(credentials.resource_paths),
        }

    def connect_garmin(self, allowed_metrics: frozenset[str]) -> dict[str, Any]:
        oauth = GarminOAuthClient.load(self.secret_store)
        request = oauth.authorization_request()
        callback = _receive_callback(
            authorization_url=request.url,
            callback_url=oauth.credentials.redirect_uri,
            expected_state=request.state,
        )
        token = oauth.exchange_code(callback.code, code_verifier=request.code_verifier)
        identity_payload = GarminHealthAPI(
            oauth.credentials,
            oauth.access_token,
        ).identity()
        identity = next(
            str(identity_payload[name])
            for name in ("userId", "user_id", "id")
            if name in identity_payload
        )
        resources = resource_types_for_metrics(
            allowed_metrics,
            configured=tuple(oauth.credentials.resource_paths),
        )
        if not resources:
            raise ProviderConfigurationError(
                "No Garmin partner resource maps to an allowlisted metric"
            )
        self.state_store.save(
            "garmin",
            {
                "connected": True,
                "identity_hash": hashlib.sha256(identity.encode()).hexdigest(),
                "connected_at": _timestamp(datetime.now(timezone.utc)),
                "last_sync_at": None,
                "data_types": list(resources),
                "checkpoints": {},
            },
        )
        return {
            "source": "garmin",
            "connected": True,
            "granted_scopes": len(token.scopes),
            "data_types": list(resources),
        }

    def disconnect_garmin(self, *, remove_client: bool = False) -> dict[str, Any]:
        oauth = GarminOAuthClient.load(self.secret_store)
        remotely_revoked = oauth.revoke()
        if remove_client:
            self.secret_store.delete(
                GarminOAuthClient.SERVICE,
                GarminOAuthClient.CLIENT_ACCOUNT,
            )
        self.state_store.delete("garmin")
        return {
            "source": "garmin",
            "connected": False,
            "remote_revocation": remotely_revoked,
        }

    def import_whoop_client(self, path: Path) -> dict[str, Any]:
        WhoopOAuthClient.import_credentials(path, self.secret_store)
        return {"source": "whoop", "client_configured": True}

    def connect_whoop(
        self,
        allowed_metrics: frozenset[str],
        *,
        authorize: Callable[[str], str],
    ) -> dict[str, Any]:
        from heavenly_health.providers.whoop import whoop_resources_for_metrics

        oauth = WhoopOAuthClient(
            WhoopOAuthClient.load(self.secret_store).credentials,
            self.secret_store,
            http_client=_provider_http_client(),
        )
        request = oauth.authorization_request()
        returned_url = authorize(request.url)
        code = oauth.parse_callback(returned_url, expected_state=request.state)
        token = oauth.exchange_code(code)
        api = WhoopAPI(oauth.access_token, http_client=_provider_http_client())
        identity = api.identity()
        resources = whoop_resources_for_metrics(allowed_metrics)
        if not resources:
            raise ProviderConfigurationError("No allowlisted metric maps to a WHOOP resource")
        self.state_store.save(
            "whoop",
            {
                "connected": True,
                "identity_hash": hashlib.sha256(str(identity["user_id"]).encode()).hexdigest(),
                "connected_at": _timestamp(datetime.now(timezone.utc)),
                "last_sync_at": None,
                "data_types": list(resources),
                "checkpoints": {},
            },
        )
        return {
            "source": "whoop",
            "connected": True,
            "granted_scopes": len(token.scopes),
            "data_types": list(resources),
        }

    def disconnect_whoop(self, *, remove_client: bool = False) -> dict[str, Any]:
        oauth = WhoopOAuthClient.load(self.secret_store)
        remotely_revoked = oauth.revoke()
        if remove_client:
            self.secret_store.delete(WhoopOAuthClient.SERVICE, WhoopOAuthClient.CLIENT_ACCOUNT)
        self.state_store.delete("whoop")
        return {"source": "whoop", "connected": False, "remote_revocation": remotely_revoked}

    def import_oura_client(self, path: Path) -> dict[str, Any]:
        OuraOAuthClient.import_credentials(path, self.secret_store)
        return {"source": "oura", "client_configured": True}

    def connect_oura(
        self,
        allowed_metrics: frozenset[str],
        *,
        authorize: Callable[[str], str],
    ) -> dict[str, Any]:
        from heavenly_health.providers.oura import oura_resources_for_metrics

        oauth = OuraOAuthClient(
            OuraOAuthClient.load(self.secret_store).credentials,
            self.secret_store,
            http_client=_provider_http_client(),
        )
        request = oauth.authorization_request()
        returned_url = authorize(request.url)
        code = oauth.parse_callback(returned_url, expected_state=request.state)
        token = oauth.exchange_code(code)
        api = OuraAPI(oauth.access_token, http_client=_provider_http_client())
        identity = api.identity()
        identity_value = str(identity.get("id") or identity.get("email"))
        resources = oura_resources_for_metrics(allowed_metrics)
        if not resources:
            raise ProviderConfigurationError("No allowlisted metric maps to an Oura resource")
        self.state_store.save(
            "oura",
            {
                "connected": True,
                "identity_hash": hashlib.sha256(identity_value.encode()).hexdigest(),
                "connected_at": _timestamp(datetime.now(timezone.utc)),
                "last_sync_at": None,
                "data_types": list(resources),
                "checkpoints": {},
            },
        )
        return {
            "source": "oura",
            "connected": True,
            "granted_scopes": len(token.scopes),
            "data_types": list(resources),
        }

    def disconnect_oura(self, *, remove_client: bool = False) -> dict[str, Any]:
        oauth = OuraOAuthClient.load(self.secret_store)
        remotely_revoked = oauth.revoke()
        if remove_client:
            self.secret_store.delete(OuraOAuthClient.SERVICE, OuraOAuthClient.CLIENT_ACCOUNT)
        self.state_store.delete("oura")
        return {"source": "oura", "connected": False, "remote_revocation": remotely_revoked}

    def sync(self, source: str, store: Any, *, limit: int = 1000) -> dict[str, Any]:
        if source not in self.SOURCES:
            raise ProviderConfigurationError("Unsupported provider source")
        connector = self._connector_factory(source, store)
        return connector.sync(limit=max(1, min(int(limit), 10_000)))

    def google_health_events(
        self,
        allowed_metrics: frozenset[str],
        *,
        days: int = 31,
        limit: int = 1_000,
    ) -> list[dict[str, Any]]:
        """Read bounded, allowlisted Google Health events without writing storage.

        This is the read-only continuity path for a daily briefing when the
        normalized store is temporarily unreachable.  It deliberately neither
        writes raw provider records nor advances synchronization checkpoints.
        """
        state = self.state_store.load("google_health")
        if state.get("connected") is not True:
            raise ProviderConfigurationError("Google Health is not connected")
        selected_metrics = frozenset({"resting_heart_rate", "heart_rate_variability"}) & allowed_metrics
        data_types = data_types_for_metrics(selected_metrics)
        if not data_types:
            raise ProviderConfigurationError("Google Health recovery metrics are not allowlisted")
        bounded_days = max(1, min(int(days), 31))
        bounded_limit = max(1, min(int(limit), 10_000))
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=bounded_days)
        oauth = GoogleOAuthClient.load(self.secret_store)
        api = GoogleHealthAPI(oauth.access_token)
        api.identity()
        events: list[dict[str, Any]] = []
        for data_type in data_types:
            remaining = bounded_limit - len(events)
            if remaining <= 0:
                break
            for point in api.list_data_points(
                data_type,
                start=_timestamp(start),
                end=_timestamp(now),
                limit=remaining,
            ):
                events.extend(
                    normalize_google_data_point(
                        data_type,
                        point,
                        allowed_metrics=selected_metrics,
                    )
                )
        return events

    def _default_connector(self, source: str, store: Any) -> Any:
        if source == "google_health":
            oauth = GoogleOAuthClient.load(self.secret_store)
            api = GoogleHealthAPI(oauth.access_token)
            return GoogleHealthConnector(api, store, self.state_store)
        if source == "garmin":
            oauth = GarminOAuthClient.load(self.secret_store)
            api = GarminHealthAPI(oauth.credentials, oauth.access_token)
            return GarminHealthConnector(api, store, self.state_store)
        if source == "whoop":
            oauth = WhoopOAuthClient.load(self.secret_store, http_client=_provider_http_client())
            api = WhoopAPI(oauth.access_token, http_client=_provider_http_client())
            return WhoopConnector(api, store, self.state_store)
        if source == "oura":
            oauth = OuraOAuthClient.load(self.secret_store, http_client=_provider_http_client())
            api = OuraAPI(oauth.access_token, http_client=_provider_http_client())
            return OuraConnector(api, store, self.state_store)
        raise ProviderConfigurationError("Unsupported provider source")


def provider_state_store(path: Path | None = None) -> ProviderStateStore:
    return ProviderStateStore(path or default_provider_state_path())


def _receive_callback(**kwargs: Any) -> Any:
    try:
        return receive_oauth_callback(**kwargs)
    except OAuthCallbackError as error:
        raise ProviderConfigurationError(str(error)) from error


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
