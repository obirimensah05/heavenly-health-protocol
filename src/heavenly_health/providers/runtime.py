"""Runtime registry for configured provider connectors."""

from __future__ import annotations

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
)


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
        raise ProviderConfigurationError(
            "Garmin connector is not available until its implementation is installed"
        )


def provider_state_store(path: Path | None = None) -> ProviderStateStore:
    return ProviderStateStore(path or default_provider_state_path())

