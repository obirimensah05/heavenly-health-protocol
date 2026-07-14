from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from heavenly_health.cli import app


class FakeProviderRuntime:
    def __init__(self) -> None:
        self.calls = []

    def import_google_client(self, path: Path):
        self.calls.append(("import", path))
        return {"source": "google_health", "client_configured": True}

    def connect_google(self, allowed_metrics):
        self.calls.append(("connect", allowed_metrics))
        return {
            "source": "google_health",
            "connected": True,
            "granted_scopes": 3,
            "data_types": ["steps"],
        }

    def statuses(self):
        self.calls.append(("status",))
        return [{"source": "google_health", "connected": True}]

    def sync(self, source, store, *, limit):
        self.calls.append(("sync", source, store, limit))
        return {
            "source": source,
            "records_processed": 2,
            "events_upserted": 2,
            "status": "completed",
        }

    def disconnect_google(self, *, remove_client=False):
        self.calls.append(("disconnect", remove_client))
        return {"source": "google_health", "connected": False}

    def import_garmin_client(self, path: Path):
        self.calls.append(("garmin-import", path))
        return {"source": "garmin", "client_configured": True, "resources": 3}

    def connect_garmin(self, allowed_metrics):
        self.calls.append(("garmin-connect", allowed_metrics))
        return {
            "source": "garmin",
            "connected": True,
            "granted_scopes": 1,
            "data_types": ["dailies"],
        }

    def disconnect_garmin(self, *, remove_client=False):
        self.calls.append(("garmin-disconnect", remove_client))
        return {"source": "garmin", "connected": False, "remote_revocation": True}


def test_google_provider_import_and_connect_are_redacted(monkeypatch, tmp_path) -> None:
    runtime = FakeProviderRuntime()
    client_file = tmp_path / "client.json"
    client_file.write_text("{}")
    monkeypatch.setattr("heavenly_health.cli._provider_runtime", lambda: runtime)
    monkeypatch.setattr(
        "heavenly_health.cli._configured_health_store",
        lambda: type("Store", (), {"settings": type("Settings", (), {"allowed_metrics": frozenset({"steps"})})()})(),
    )

    imported = CliRunner().invoke(
        app,
        ["provider", "google-health", "import-client", str(client_file)],
    )
    connected = CliRunner().invoke(app, ["provider", "google-health", "connect"])

    assert imported.exit_code == 0
    assert connected.exit_code == 0
    assert "client_configured" in imported.stdout
    assert "connected" in connected.stdout
    assert "client_secret" not in imported.stdout + connected.stdout
    assert runtime.calls == [
        ("import", client_file),
        ("connect", frozenset({"steps"})),
    ]


def test_provider_status_sync_and_disconnect_dispatch_without_secret_output(monkeypatch) -> None:
    runtime = FakeProviderRuntime()
    store = object()
    monkeypatch.setattr("heavenly_health.cli._provider_runtime", lambda: runtime)
    monkeypatch.setattr("heavenly_health.cli._configured_health_store", lambda: store)

    status = CliRunner().invoke(app, ["provider", "status"])
    sync = CliRunner().invoke(
        app,
        ["provider", "google-health", "sync", "--limit", "25"],
    )
    disconnected = CliRunner().invoke(
        app,
        ["provider", "google-health", "disconnect", "--yes", "--remove-client"],
    )

    assert status.exit_code == sync.exit_code == disconnected.exit_code == 0
    assert "google_health" in status.stdout
    assert "completed" in sync.stdout
    assert "connected" in disconnected.stdout
    assert runtime.calls == [
        ("status",),
        ("sync", "google_health", store, 25),
        ("disconnect", True),
    ]


def test_garmin_operator_lifecycle_uses_same_redacted_provider_commands(
    monkeypatch,
    tmp_path,
) -> None:
    runtime = FakeProviderRuntime()
    store = type(
        "Store",
        (),
        {"settings": type("Settings", (), {"allowed_metrics": frozenset({"steps"})})()},
    )()
    client_file = tmp_path / "garmin-client.json"
    client_file.write_text("{}")
    monkeypatch.setattr("heavenly_health.cli._provider_runtime", lambda: runtime)
    monkeypatch.setattr("heavenly_health.cli._configured_health_store", lambda: store)

    imported = CliRunner().invoke(
        app,
        ["provider", "garmin", "import-client", str(client_file)],
    )
    connected = CliRunner().invoke(app, ["provider", "garmin", "connect"])
    synced = CliRunner().invoke(
        app,
        ["provider", "garmin", "sync", "--limit", "30"],
    )
    disconnected = CliRunner().invoke(
        app,
        ["provider", "garmin", "disconnect", "--yes"],
    )

    assert imported.exit_code == connected.exit_code == synced.exit_code == disconnected.exit_code == 0
    assert "client_secret" not in imported.stdout + connected.stdout + synced.stdout
    assert runtime.calls == [
        ("garmin-import", client_file),
        ("garmin-connect", frozenset({"steps"})),
        ("sync", "garmin", store, 30),
        ("garmin-disconnect", False),
    ]
