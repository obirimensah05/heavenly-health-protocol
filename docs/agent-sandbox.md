# CLI-agent Docker sandbox

`heavenly agent run` launches any user-selected CLI-agent image without making
that agent a Heavenly dependency. The host's Codex, Claude Code, Gemini CLI,
credentials, configuration, home directory, and Docker socket are never mounted.

## Default boundary

```bash
heavenly agent run \
  --image <pinned-agent-image> \
  --workspace "$PWD" \
  -- <agent-command>
```

The default container is ephemeral and has:

- `--network=none`;
- a read-only root and workspace;
- a private in-memory home and `/tmp` (only the agent home is executable, so
  package-based CLIs can run; `/tmp` remains `noexec`);
- UID/GID `1000:1000`, all capabilities dropped, no-new-privileges, Docker's
  default seccomp policy, an init process, and CPU/memory/PID limits;
- no Docker socket, host home, Heavenly runtime file, health database credential,
  or inherited environment secret.

Docker is a process/filesystem boundary, not a virtual machine. It shares the
host kernel. Keep Docker patched and use a VM when the threat model requires a
separate kernel.

## Explicit grants

```bash
chmod 700 ~/.local/state/heavenly/agents/codex

heavenly agent run \
  --image <pinned-agent-image> \
  --workspace "$PWD" \
  --write-workspace \
  --network bridge \
  --state-dir ~/.local/state/heavenly/agents/codex \
  --secret-env OPENAI_API_KEY \
  -- codex
```

- `--write-workspace` allows changes only inside the selected resolved directory.
- `--network bridge` enables ordinary Docker network access. This can reach the
  Internet and potentially other reachable hosts; Docker does not make it
  Internet-only. Use `none` unless the agent needs a cloud API.
- `--network <name>` joins an explicitly selected non-host Docker network, for
  example to reach a containerized MCP service by its service name. Host network
  mode is rejected.
- `--state-dir` mounts only an existing, non-symlink, owner-private `0700`
  directory at `/home/agent` for login/config persistence.
- Each `--secret-env NAME` grants one existing host variable by name. Values are
  never put in the generated command or logs. No unlisted variable enters the
  container.
- `--user UID:GID` remains numeric and non-root for images whose non-root user is
  not `1000:1000`.

## Reusable Codex and Claude Code images

Build the checked-in recipes once. Each recipe receives only its own Dockerfile
as build context, installs system CA roots, bakes the pinned CLI, and ends as
numeric user `1000:1000`:

```bash
docker build \
  --tag heavenly-agent-codex:0.144.4 \
  agent-images/codex

docker build \
  --tag heavenly-agent-claude:2.1.209 \
  agent-images/claude
```

Verify both without network or package installation at container startup:

```bash
heavenly agent run \
  --image heavenly-agent-codex:0.144.4 \
  --workspace "$PWD" -- codex --version

heavenly agent run \
  --image heavenly-agent-claude:2.1.209 \
  --workspace "$PWD" -- claude --version
```

For Codex subscription login inside the container, use device authorization so
the flow does not depend on a localhost callback crossing the container boundary:

```bash
CODEX_STATE="$HOME/.local/state/heavenly/agents/codex"
mkdir -p "$CODEX_STATE"
chmod 700 "$CODEX_STATE"

heavenly agent run \
  --image heavenly-agent-codex:0.144.4 \
  --workspace "$PWD" \
  --network bridge \
  --state-dir "$CODEX_STATE" \
  -- codex login --device-auth
```

After login, launch Codex with the same state directory and add
`--write-workspace` only when it should edit the selected repository. Never use
the generic slim Node image for native CLI authentication unless its image also
installs system CA roots. Node/npm can have bundled trust roots while a native
agent binary still requires the container CA bundle.

The launcher remains OCI- and agent-agnostic; these recipes are maintained
examples, not hard-coded runtime dependencies.
