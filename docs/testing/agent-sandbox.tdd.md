# CLI-agent sandbox TDD and live evidence

## Contract

The generic launcher accepts an OCI image and in-container command. Its defaults
are network-none, read-only root/workspace, ephemeral home, numeric non-root user,
no-new-privileges, all capabilities dropped, no Docker socket/host home, bounded
CPU/memory/PIDs, and no ambient secrets. Network, workspace writes, persistent
state, and each secret environment name are explicit grants.

## RED to GREEN

The initial RED run failed collection because `heavenly_health.agent_sandbox`
did not exist. Unit and CLI tests were then implemented and passed. A real Codex
run exposed Docker's implicit `noexec` on the ephemeral home; the CLI installed
but could not execute. A regression assertion now requires an executable agent
home while `/tmp` remains `noexec`.

## Real container checks on 2026-07-14

Using a pinned Node 22 image:

```text
OpenAI Codex CLI: codex-cli 0.144.4
Anthropic Claude Code: 2.1.209
default workspace write probe: blocked
default outbound network probe: blocked
```

Both CLIs were downloaded and executed inside ephemeral hardened containers.
No host agent binary, agent configuration, home directory, Docker socket,
Heavenly runtime file, or health credential was mounted.

## Reusable-image authentication regression

A real Codex device-login attempt later exposed a narrower image bug: npm HTTPS
worked in the generic slim Node image, but Codex's native HTTP client could not
reach the device-auth endpoint because the image had no system CA bundle. The
same failure reproduced in an unrestricted `docker run`, ruling out Heavenly's
sandbox controls. A direct Node HTTPS request to the same endpoint returned 200.

RED tests required maintained Codex and Claude Code image recipes with pinned
base/CLI versions, system CA roots, no copied repository data or credentials,
and a final non-root user. They failed because the recipes did not exist. The
GREEN implementation added `agent-images/codex/Dockerfile` and
`agent-images/claude/Dockerfile`.

Live acceptance of the exact checked-in images:

```text
Codex image build: passed; codex-cli 0.144.4
Claude image build: passed; Claude Code 2.1.209
Codex device authorization through heavenly agent run: reached one-time-code prompt
runtime package download: none
runtime user: 1000:1000
Docker Scout critical/high findings: 0/0 for both images
```

The generated test authorization was cancelled without completing login. The
owner performs the real login using a private mode-`0700` state directory.
Build-only npm/npx and the unused Perl runtime are removed after installing the
CLIs; Node, CA roots, the selected agent, and its packaged runtime resources
remain. A separate RED/GREEN packaging test also requires `.dockerignore` to
exclude `.env`, `handover.md`, and nested `runtime.env` files from the root
Docker build context.
