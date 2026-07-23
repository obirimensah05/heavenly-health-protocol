from __future__ import annotations

import os

import pytest

from heavenly_health.secret_loader import SecretFileError, load_runtime_environment


def write_private_env(path, contents: str) -> None:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)


def test_loads_only_heavenly_and_supabase_values_without_overriding_process_environment(
    tmp_path,
) -> None:
    secret_file = tmp_path / "runtime.env"
    write_private_env(
        secret_file,
        "\n".join(
            (
                "HEAVENLY_MCP_PUBLIC_HOST=health-mcp.example.com",
                "SUPABASE_URL=https://project.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY='private-test-value'",
                "OPENAI_API_KEY=must-not-be-imported",
            )
        ),
    )
    environ = {"SUPABASE_URL": "https://explicit.supabase.co"}

    loaded = load_runtime_environment(secret_file, environ=environ)

    assert loaded == {
        "HEAVENLY_MCP_PUBLIC_HOST",
        "SUPABASE_SERVICE_ROLE_KEY",
    }
    assert environ == {
        "HEAVENLY_MCP_PUBLIC_HOST": "health-mcp.example.com",
        "SUPABASE_URL": "https://explicit.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "private-test-value",
    }
    assert "OPENAI_API_KEY" not in environ


def test_runtime_file_can_include_an_owner_only_source_without_importing_unrelated_values(
    tmp_path,
) -> None:
    source = tmp_path / "second-brain.env"
    write_private_env(
        source,
        "SUPABASE_URL=https://project.supabase.co\n"
        "SUPABASE_SERVICE_ROLE_KEY=private-test-value\n"
        "NOTION_API_KEY=must-not-be-imported\n",
    )
    runtime = tmp_path / "runtime.env"
    write_private_env(
        runtime,
        f"HEAVENLY_SECRET_FILES={source}\n"
        "HEAVENLY_CONTEXT_TABLE=private_documents\n",
    )
    environ: dict[str, str] = {}

    loaded = load_runtime_environment(runtime, environ=environ)

    assert loaded == {
        "HEAVENLY_CONTEXT_TABLE",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_URL",
    }
    assert "HEAVENLY_SECRET_FILES" not in environ
    assert "NOTION_API_KEY" not in environ


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o666])
def test_rejects_secret_files_readable_or_writable_by_other_users(tmp_path, mode) -> None:
    path = tmp_path / "runtime.env"
    path.write_text("SUPABASE_SERVICE_ROLE_KEY=do-not-leak", encoding="utf-8")
    path.chmod(mode)

    with pytest.raises(SecretFileError, match="owner-only") as exc_info:
        load_runtime_environment(path, environ={})

    assert "do-not-leak" not in str(exc_info.value)


def test_rejects_relative_symlink_and_malformed_secret_files_without_leaking_values(
    tmp_path, monkeypatch
) -> None:
    real = tmp_path / "real.env"
    write_private_env(real, "SUPABASE_SERVICE_ROLE_KEY=do-not-leak")
    link = tmp_path / "link.env"
    link.symlink_to(real)

    with pytest.raises(SecretFileError, match="absolute"):
        load_runtime_environment(os.path.relpath(real), environ={})
    with pytest.raises(SecretFileError, match="symbolic link"):
        load_runtime_environment(link, environ={})

    malformed = tmp_path / "malformed.env"
    write_private_env(malformed, "SUPABASE_SERVICE_ROLE_KEY=do-not-leak\nnot valid")
    with pytest.raises(SecretFileError, match="line 2") as exc_info:
        load_runtime_environment(malformed, environ={})
    assert "do-not-leak" not in str(exc_info.value)


def test_rejects_include_cycles(tmp_path) -> None:
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    write_private_env(first, f"HEAVENLY_SECRET_FILES={second}")
    write_private_env(second, f"HEAVENLY_SECRET_FILES={first}")

    with pytest.raises(SecretFileError, match="cycle"):
        load_runtime_environment(first, environ={})


def test_every_supabase_credential_the_storage_layer_reads_is_importable(tmp_path) -> None:
    """A name missing here is dropped silently and storage looks unconfigured."""
    from heavenly_health.health_storage import SupabaseSettings

    runtime = tmp_path / "runtime.env"
    runtime.write_text(
        "SUPABASE_URL=https://project.supabase.co\n"
        "SUPABASE_HEALTH_ROLE_KEY=scoped-role-token\n"
        "SUPABASE_PUBLISHABLE_KEY=project-publishable\n"
        "SUPABASE_ANON_KEY=project-anon\n"
        "HEAVENLY_ALLOWED_METRICS=steps\n"
    )
    runtime.chmod(0o600)
    environ: dict[str, str] = {}

    load_runtime_environment(runtime, environ=environ)
    settings = SupabaseSettings.from_environ(environ)

    assert settings is not None
    assert settings.bearer_token == "scoped-role-token"
    assert settings.gateway_key == "project-publishable"
    assert settings.uses_service_role is False
