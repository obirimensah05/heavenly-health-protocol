from typer.testing import CliRunner

import pytest

from heavenly_health.config import ConfigError, LocalConfigStore, RuntimeConfiguration
from heavenly_health.cli import app


def test_new_store_defaults_to_native_runtime_and_empty_references(tmp_path) -> None:
    config = LocalConfigStore(tmp_path / "runtime.json").load()

    assert config.version == 1
    assert config.runtime == "native"
    assert config.selected_model_profile is None
    assert config.delivery_references == {}


def test_runtime_selection_persists_without_overwriting_profile_or_delivery_references(tmp_path) -> None:
    path = tmp_path / "runtime.json"
    store = LocalConfigStore(path)
    store.save(
        RuntimeConfiguration(
            selected_model_profile="local-analysis",
            delivery_references={"daily_briefing": "delivery:daily-briefing"},
        )
    )

    store.set_runtime("docker")
    persisted = LocalConfigStore(path).load()

    assert persisted.runtime == "docker"
    assert persisted.selected_model_profile == "local-analysis"
    assert persisted.delivery_references == {"daily_briefing": "delivery:daily-briefing"}


def test_rejects_unknown_runtime_name(tmp_path) -> None:
    with pytest.raises(ValueError, match="Unsupported runtime"):
        LocalConfigStore(tmp_path / "runtime.json").set_runtime("podman")


@pytest.mark.parametrize(
    "contents, reason",
    [
        ("{", "valid JSON"),
        ("[]", "JSON object"),
        ('{"runtime": "native", "unknown": true}', "unknown field"),
        ('{"version": "1"}', "version"),
        ('{"selected_model_profile": 42}', "selected_model_profile"),
        ('{"delivery_references": []}', "delivery_references"),
        ('{"delivery_references": {"daily": 42}}', "delivery_references"),
    ],
)
def test_load_translates_malformed_or_invalid_configuration_to_domain_error(tmp_path, contents, reason) -> None:
    path = tmp_path / "runtime.json"
    path.write_text(contents)

    with pytest.raises(ConfigError, match=reason):
        LocalConfigStore(path).load()


def test_load_translates_io_errors_to_domain_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "runtime.json"
    path.write_text("{}")

    def fail_read_text(_path):
        raise OSError("disk error")

    monkeypatch.setattr(type(path), "read_text", fail_read_text)

    with pytest.raises(ConfigError, match="could not read configuration"):
        LocalConfigStore(path).load()


def test_configuration_copies_delivery_references_into_an_immutable_mapping() -> None:
    references = {"daily_briefing": "delivery:daily-briefing"}
    configuration = RuntimeConfiguration(delivery_references=references)
    references["daily_briefing"] = "changed"

    assert configuration.delivery_references == {"daily_briefing": "delivery:daily-briefing"}
    with pytest.raises(TypeError):
        configuration.delivery_references["weekly_briefing"] = "delivery:weekly-briefing"  # type: ignore[index]


def test_save_rejects_a_symlink_config_path(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    path = tmp_path / "runtime.json"
    path.symlink_to(target)

    with pytest.raises(ConfigError, match="symbolic link"):
        LocalConfigStore(path).save(RuntimeConfiguration())


def test_runtime_show_displays_selected_runtime_from_injected_path(tmp_path, monkeypatch) -> None:
    path = tmp_path / "runtime.json"
    LocalConfigStore(path).save(RuntimeConfiguration(runtime="docker"))
    monkeypatch.setattr("heavenly_health.cli.default_config_path", lambda: path)

    result = CliRunner().invoke(app, ["runtime", "show"])

    assert result.exit_code == 0
    assert "docker" in result.stdout


def test_runtime_commands_offer_repair_guidance_for_corrupt_configuration(tmp_path, monkeypatch) -> None:
    path = tmp_path / "runtime.json"
    path.write_text("{")
    monkeypatch.setattr("heavenly_health.cli.default_config_path", lambda: path)

    runner = CliRunner()
    shown = runner.invoke(app, ["runtime", "show"])
    selected = runner.invoke(app, ["runtime", "use", "docker"])

    for result in (shown, selected):
        assert result.exit_code == 1
        assert "Repair the configuration" in result.stdout
        assert "remove" in result.stdout
        assert isinstance(result.exception, SystemExit)
        assert result.exception.code == 1


def test_runtime_use_persists_the_selected_runtime(tmp_path, monkeypatch) -> None:
    path = tmp_path / "runtime.json"
    monkeypatch.setattr("heavenly_health.cli.default_config_path", lambda: path)
    runner = CliRunner()

    result = runner.invoke(app, ["runtime", "use", "docker"])
    shown = runner.invoke(app, ["runtime", "show"])

    assert result.exit_code == 0
    assert "docker" in result.stdout
    assert shown.exit_code == 0
    assert "docker" in shown.stdout
