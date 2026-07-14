"""Runtime registry for configured provider connectors."""

from __future__ import annotations

from datetime import datetime, timezone
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
)
from heavenly_health.providers.garmin import (
    GarminHealthAPI,
    GarminHealthConnector,
    GarminOAuthClient,
    resource_types_for_metrics,
)
from heavenly_health.providers.oauth_loopback import receive_oauth_callback


class ProviderRuntime:
    """Discover, report, and dispatch only explicitly supported providers."""

    SOURCES = ("google_health", "garmin")

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
        callback = receive_oauth_callback(
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
        callback = receive_oauth_callback(
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

    def sync(self, source: str, store: Any, *, limit: int = 1000) -> dict[str, Any]:
        if source not in self.SOURCES:
            raise ProviderConfigurationError("Unsupported provider source")
        connector = self._connector_factory(source, store)
        return connector.sync(limit=max(1, min(int(limit), 10_000)))

    def _default_connector(self, source: str, store: Any) -> Any:
        if source == "google_health":
            oauth = GoogleOAuthClient.load(self.secret_store)
            api = GoogleHealthAPI(oauth.access_token)
            return GoogleHealthConnector(api, store, self.state_store)
        if source == "garmin":
            oauth = GarminOAuthClient.load(self.secret_store)
            api = GarminHealthAPI(oauth.credentials, oauth.access_token)
            return GarminHealthConnector(api, store, self.state_store)
        raise ProviderConfigurationError("Unsupported provider source")


def provider_state_store(path: Path | None = None) -> ProviderStateStore:
    return ProviderStateStore(path or default_provider_state_path())


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
