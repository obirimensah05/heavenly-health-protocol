from pathlib import Path

import pytest

from heavenly_health.public_release import (
    PublicReleaseError,
    export_public_tree,
    validate_public_tree,
)


def test_public_release_rejects_private_markers_without_echoing_private_content(
    tmp_path: Path,
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("Deployed for PRIVATE-OWNER at a private endpoint.\n")

    with pytest.raises(PublicReleaseError) as captured:
        validate_public_tree(
            tmp_path,
            ["README.md"],
            forbidden_markers=["private-owner"],
        )

    assert "README.md" in str(captured.value)
    assert "forbidden private marker" in str(captured.value)
    assert "PRIVATE-OWNER" not in str(captured.value)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        ".env",
        ".env.production",
        "handover.md",
        "runtime.env",
        "secrets/client.pem",
        "state/access.jwt",
        "credentials.json",
        "auth.json",
    ],
)
def test_public_release_rejects_local_or_credential_artifact_names(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    path = tmp_path / unsafe_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("placeholder\n")

    with pytest.raises(PublicReleaseError, match="unsafe public path"):
        validate_public_tree(tmp_path, [unsafe_path])


def test_public_release_allows_placeholder_environment_template(tmp_path: Path) -> None:
    template = tmp_path / ".env.example"
    template.write_text("PUBLIC_HOST=health-mcp.example.com\nCLIENT_SECRET=\n")

    assert validate_public_tree(tmp_path, [".env.example"]) == (Path(".env.example"),)


def test_public_release_allows_documented_container_service_home_paths(
    tmp_path: Path,
) -> None:
    compose = tmp_path / "compose.yaml"
    service_home = "/" + "home" + "/heavenly/data"
    agent_home = "/" + "home" + "/agent"
    compose.write_text(f"data: {service_home}\nagent: {agent_home}\n")

    assert validate_public_tree(tmp_path, ["compose.yaml"]) == (Path("compose.yaml"),)


def test_public_release_rejects_absolute_home_paths_and_secret_shaped_content(
    tmp_path: Path,
) -> None:
    absolute_home = "/" + "Users" + "/alice/private-project"
    fake_token = "gh" + "p_" + ("A" * 40)
    document = tmp_path / "notes.md"
    document.write_text(f"path={absolute_home}\ntoken={fake_token}\n")

    with pytest.raises(PublicReleaseError) as captured:
        validate_public_tree(tmp_path, ["notes.md"])

    message = str(captured.value)
    assert "absolute user home path" in message
    assert "secret-shaped content" in message
    assert absolute_home not in message
    assert fake_token not in message


@pytest.mark.parametrize("unsafe_manifest_path", ["../private.txt", "/tmp/private.txt"])
def test_public_release_rejects_manifest_paths_outside_the_source(
    tmp_path: Path,
    unsafe_manifest_path: str,
) -> None:
    with pytest.raises(PublicReleaseError, match="unsafe manifest path"):
        validate_public_tree(tmp_path, [unsafe_manifest_path])


def test_public_export_copies_only_validated_manifest_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "public"
    source.mkdir()
    (source / "README.md").write_text("Use https://health-mcp.example.com/mcp.\n")
    package = source / "src" / "package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (source / "local-only.txt").write_text("must not be exported\n")

    exported = export_public_tree(
        source,
        destination,
        ["README.md", "src/package/__init__.py"],
        forbidden_markers=["private-owner"],
    )

    assert exported == (Path("README.md"), Path("src/package/__init__.py"))
    assert (destination / "README.md").read_text().startswith("Use https://")
    assert (destination / "src" / "package" / "__init__.py").is_file()
    assert not (destination / "local-only.txt").exists()


def test_public_export_refuses_to_merge_into_an_existing_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "public"
    source.mkdir()
    destination.mkdir()
    (destination / "existing.txt").write_text("do not overwrite\n")
    (source / "README.md").write_text("public\n")

    with pytest.raises(PublicReleaseError, match="destination must not exist"):
        export_public_tree(source, destination, ["README.md"])
