# ADR 0001: Heavenly is an agent application with native and Docker runtimes

- **Status:** Accepted; core runtime, remote MCP, storage, and sandbox slices implemented
- **Date:** 2026-07-13

## Context

Heavenly Health Protocol began with a small local MCP server. MCP is useful, but it is not the product: it does not provide a self-contained health-agent runtime, model selection, scheduling, data-release policy, or a stable terminal experience.

Users need one application that can run natively for convenience or in Docker for isolation. It must select hosted models such as Claude, Codex/OpenAI, and Gemini without borrowing the host machine's agent CLIs, Docker socket, credentials, or configuration.

## Decision

This ADR records both the implemented runtime seam and the intended end state.
The checked-in service image runs MCP, while model profiles, schedules, delivery
adapters, the full worker, and MCP enable/disable controls remain future work.
Native/Docker MCP lifecycle, private storage tools, Managed OAuth, and the generic
agent sandbox are implemented without claiming the full worker is complete.

Heavenly is a health-agent application with two runtime adapters:

```text
heavenly runtime use native   # default
heavenly runtime use docker
```

Runtime selection is persisted through the same `heavenly` CLI. Future model
profiles, schedules, policies, and worker commands must preserve this seam.

The checked-in Docker service is currently the Heavenly MCP process rather than
the future scheduled worker. It has its own non-root process, isolated named
volume, runtime-injected credentials, and no host-agent or Docker-socket access.

Models may be integrated through future first-party provider adapters. The
implemented generic agent sandbox instead runs a user-supplied CLI-agent image;
it is deliberately vendor-neutral and never invokes or mounts host-installed
CLIs. Workspace writes, network, persistent state, and individual environment
secret names are separate explicit grants.

MCP is an explicit local or remote interface into the application. The recommended
remote route uses Cloudflare Access Managed OAuth with dynamic registration and
authorization-code/PKCE; Heavenly verifies the signed Access assertion again at
the origin. A separately configured FastMCP OIDC proxy remains an alternative.
During remote setup, Heavenly asks for the exact owner identity allowlist rather
than inferring it from a connector, model subscription, or Cloudflare account.

## Consequences

- Native use remains simple and does not require Docker.
- Docker use provides a strong process/filesystem/configuration separation from other local agents.
- Provider credentials use OS secret storage in native mode and Docker secrets/runtime injection in Docker mode.
- A selected hosted model may receive all user-selected health data by default. Users may opt into narrower per-provider/model release policies. Credentials and secrets are permanently excluded from prompts.
- Scheduled jobs use the operating system scheduler in native mode and an always-on Heavenly worker in Docker mode.
- A global delivery configuration controls all scheduled summaries. Local output, WhatsApp bridge, Telegram Bot API, and Slack app adapters are supported only after explicit configuration.

## Rejected alternatives

- **Treating MCP-only Docker as the final application:** too narrow; the current
  service is a supported interface, not the future scheduled worker.
- **Bundling large model weights by default:** makes images heavy, complicates acceleration and updates, and is not needed for configurable hosted-model support.
- **Invoking host Claude/Codex/Gemini CLIs from Docker:** breaks the intended isolation by sharing host configuration and credentials.
- **Remote MCP without OAuth:** incompatible with a safe general remote-client model, especially Claude Web.
