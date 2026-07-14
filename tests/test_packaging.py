from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_private_operator_files_are_excluded_from_release_artifacts() -> None:
    config = (PROJECT_ROOT / "pyproject.toml").read_text()
    _, marker, build_config = config.partition("[tool.hatch.build]")
    assert marker
    build_config = build_config.partition("\n[")[0]

    assert '"/handover.md"' in build_config
    assert '"/.env"' in build_config
    assert '"**/runtime.env"' in build_config


def test_private_operator_files_are_excluded_from_docker_build_context() -> None:
    excluded = (PROJECT_ROOT / ".dockerignore").read_text().splitlines()

    assert ".env" in excluded
    assert "handover.md" in excluded
    assert "**/runtime.env" in excluded
