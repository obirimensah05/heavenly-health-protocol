# Onboarding protocol

Heavenly starts with the user's data source and MCP client. Credentials and
advanced infrastructure come only when the selected route requires them.

## Normal user path

### 1. Choose an implemented data route

Version 0.1 supports existing normalized Supabase data and bounded
normalization from a configured Health Auto Export delivery table. Apply the
checked-in migrations, select only the required metrics, and keep the Supabase
service-role credential in an owner-only runtime file.

WHOOP, Oura, Google Health/Fitbit, Garmin, and Health Connect documents describe
the security and provider contracts for future adapters. The CLI must not claim
that those OAuth or sync implementations exist until their code and live tests
ship.

### 2. Start the native service

Native is the ordinary default. It binds only to loopback and does not require
Docker, a tunnel, or a public hostname.

```bash
heavenly runtime use native
heavenly runtime start
heavenly runtime status
```

### 3. Connect the AI/MCP client

For a client on the same machine, use:

```text
http://127.0.0.1:8791/mcp
```

For a remote client, use the operator-provided HTTPS MCP URL. With Cloudflare
Managed OAuth, compatible clients discover the authorization server and
dynamically register themselves. Optional client ID and client secret fields
stay blank; the user completes the browser login instead.

The choice of Claude, Codex, Hermes, ChatGPT, or another MCP client changes only
the client configuration instructions. It does not change the protocol, health
schema, or authorization policy.

## Data permissions

The user chooses an explicit metric allowlist. Medical/clinical records,
medications, reproductive data, ECG, mental-state data, and routes/location stay
off unless a future adapter implements and the user explicitly enables them.

The MCP service never receives provider OAuth client secrets from an AI agent.
Provider credentials belong to the provider adapter's protected secret store;
MCP clients receive only the resulting allowlisted tools.

## Advanced operator path

Technical operators can optionally add:

- a named Cloudflare Tunnel and exact-email Access policy;
- Cloudflare Managed OAuth with short access tokens and renewable sessions;
- Docker for the status-only public profile or reviewed secret injection;
- custom Supabase relation names and private context search;
- the generic default-deny CLI-agent sandbox.

These controls are documented in [Deployment](deployment.md). They should not be
presented as prerequisites for a local native user.

## Provider-specific legal requirements

Some future provider adapters require public privacy, terms, or application
review URLs. Generate or publish those resources only with explicit owner
approval. They must never contain secrets, health records, private endpoints, or
local paths.
