from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE_IMAGE = (
    "node:22-bookworm-slim@"
    "sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3"
)


@pytest.mark.parametrize(
    ("agent", "version_arg", "version", "package", "command"),
    [
        ("codex", "CODEX_VERSION", "0.144.4", "@openai/codex", "codex"),
        (
            "claude",
            "CLAUDE_CODE_VERSION",
            "2.1.209",
            "@anthropic-ai/claude-code",
            "claude",
        ),
    ],
)
def test_agent_image_bakes_pinned_cli_ca_roots_and_non_root_runtime(
    agent: str,
    version_arg: str,
    version: str,
    package: str,
    command: str,
) -> None:
    dockerfile = (PROJECT_ROOT / "agent-images" / agent / "Dockerfile").read_text()

    assert f"FROM {NODE_IMAGE}" in dockerfile
    assert f"ARG {version_arg}={version}" in dockerfile
    assert "ca-certificates" in dockerfile
    assert f'"{package}@${{{version_arg}}}"' in dockerfile
    assert f'RUN {command} --version' in dockerfile
    assert "USER 1000:1000" in dockerfile
    assert f'CMD ["{command}"]' in dockerfile
    assert "rm -rf /usr/local/lib/node_modules/npm" in dockerfile
    assert "--force-remove-essential perl-base" in dockerfile
    assert "COPY " not in dockerfile
    assert "API_KEY" not in dockerfile
