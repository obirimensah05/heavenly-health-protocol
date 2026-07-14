# Heavenly agent runtime architecture

## Implementation status

This is the accepted **target architecture**, not a description of the complete
current product. The implemented slice is runtime preference plus native/Docker
lifecycle for the MCP service, real bounded storage tools when configured,
Cloudflare Managed OAuth, and an optional generic CLI-agent sandbox. The always-on
agent worker, model profiles, additional provider adapters, scheduler, delivery
adapters, MCP enable command, and generic schema-discovery tools below are not
implemented yet.

## Product boundary

Heavenly is a private health-agent application. MCP, scheduled delivery, and a terminal CLI are interfaces to the same application; none is the product by itself.

```text
terminal CLI / local MCP / remote OAuth MCP / scheduler
                         │
                         ▼
              Heavenly agent application
       ┌──────────────────────────────────────┐
       │ model profile + policy engine         │
       │ source and storage adapters           │
       │ analysis, audit, scheduling           │
       │ delivery adapters                     │
       └──────────────────────────────────────┘
```

## Runtime seam

The CLI persists one active runtime:

```bash
heavenly runtime use native
heavenly runtime use docker
```

| Concern | Native | Docker |
|---|---|---|
| Process | User-local process | Non-root Heavenly container |
| Credentials | OS keyring | Docker secrets/runtime injection |
| Data | User-controlled local directory | Isolated named volume by default |
| Target scheduling | OS-native scheduler | Always-on Heavenly worker (future) |
| CLI agents | Host CLI may connect over MCP | User-supplied image runs inside an explicit sandbox |

A Docker user can optionally bind mount an explicit export/backup directory. The default Docker volume is isolated from other applications.

## Planned model profiles

A model profile records a provider, model identifier, secret reference, and data-release policy. Exactly one profile is selected per run.

```bash
heavenly model add claude-health
heavenly model use claude-health
heavenly agent ask "Review recovery and make today's plan."
```

First adapters use direct provider APIs. Optional CLI adapters, if added later, execute inside the Heavenly runtime only. Heavenly does not invoke the host's Codex, Claude, Gemini, Hermes, or other agent installation.

The implemented `heavenly agent run` path accepts any OCI image and command. It
does not make an agent vendor part of Heavenly and never invokes a host-installed
agent. The default sandbox has no network, read-only workspace/root filesystems,
an ephemeral home, a numeric non-root user, no capabilities, no-new-privileges,
resource limits, and no Docker socket or ambient secrets. Each broader grant is
an explicit CLI option. See [Agent sandbox](agent-sandbox.md).

A new hosted-model profile may receive all health data the user selected. Optional restrictions may narrow data by provider or model profile. The agent never supplies credentials, OAuth material, API keys, database service keys, tunnel credentials, or secrets to a model prompt.

## Planned interfaces

MCP is disabled by default:

```bash
heavenly mcp enable --local
```

Local MCP binds only to loopback. Remote MCP is an explicit OAuth-protected deployment. Its default onboarding asks for the **owner identity email** and must never infer it from a Claude/Codex/Gemini subscription, Cloudflare account, or connector account: those can legitimately be different. The owner identity is the application-level operator record and audit principal.

Remote OAuth uses a registered client ID and, where the client is confidential, a client secret; public clients use OAuth 2.1 PKCE instead. The client credential identifies the connector—not the human owner—and is not an email allowlist. Cloudflare Access is an optional second gate. When enabled, setup separately asks which exact email(s) Cloudflare should allow, including the owner email when desired; those identities may differ from the provider/subscription and OAuth connector identities.

## Planned MCP data actions

Once the owner enables an MCP storage adapter, MCP provides generic, schema-aware actions over the configured Heavenly/Supabase data model rather than a status-only interface. It supports structured table discovery, reads, and approved writes—never arbitrary SQL.

A caller with `health.read` can query configured tables and views. A caller with `health.write` can prepare a generic insert, update, delete, or upsert, but each mutation requires a separate, exact owner approval before it can commit. OAuth authenticates the caller; it does not authorize an unreviewed health-data mutation. Full policy: [MCP data tool policy](mcp-tool-policy.md).

## Planned scheduled summaries and delivery

One global delivery configuration applies to every scheduled summary. A schedule can write a local summary and send it through any explicitly configured delivery adapter:

- WhatsApp through a compatible WhatsApp bridge; when absent, setup must direct the user to the bridge project rather than attempting unsupported direct delivery.
- Telegram through a BotFather-created bot token.
- Slack through an installed Slack app token.

Other actions—provider changes, model changes, new data destinations, security/routing changes, and remote delivery configuration—require explicit confirmation. Approved recurring schedules may send their configured summaries automatically.
