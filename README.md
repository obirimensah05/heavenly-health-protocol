# Heavenly Health Protocol

A private, LLM-agnostic bridge between user-controlled health data and any
compatible Model Context Protocol client.

## What works today

Heavenly provides a tested native MCP service, an optional hardened Docker
runtime, a bounded Supabase health-data adapter, Health Auto Export
normalization, private context search, owner-approved mutations, and an optional
Cloudflare Managed OAuth boundary for remote clients.

The normal path is deliberately short:

1. Put normalized health data in your own Supabase project, either directly or
   through the implemented Health Auto Export delivery-table adapter.
2. Start Heavenly natively.
3. Give your AI/MCP client the local MCP URL or your OAuth-protected remote URL.
4. Let the client perform OAuth. Cloudflare Managed OAuth supports Dynamic
   Client Registration, so compatible clients do not need a manually copied
   OAuth client ID or client secret.

With no storage configuration, MCP exposes only `protocol_status`. With an
explicit Supabase configuration and metric allowlist, it exposes ten bounded
tools for status, reads, provenance, sync, context, and owner-approved writes.
It never exposes arbitrary SQL, raw provider payloads, credentials, or MCP-side
approval.

## Five-minute native setup

Requirements: Python 3.10+, `uv`, and a user-controlled Supabase project if you
want the private health tools.

```bash
uv sync --extra dev
uv tool install --editable --force .

install -d -m 700 "$HOME/.config/heavenly"
install -m 600 .env.example "$HOME/.config/heavenly/runtime.env"
${EDITOR:-vi} "$HOME/.config/heavenly/runtime.env"

heavenly runtime use native
heavenly runtime start
heavenly runtime status
```

Apply the migrations in [`sql/`](sql/) through your own reviewed Supabase
migration workflow. In `runtime.env`, set the Supabase URL, service-role key,
fixed table names, and a narrow metric allowlist. The file is outside the
repository, owner-only, and never copied into an MCP response.

Connect a local MCP client to:

```text
http://127.0.0.1:8791/mcp
```

For a remote client, deploy a named tunnel and Cloudflare Access application,
enable Managed OAuth, and connect the client to:

```text
https://health-mcp.example.com/mcp
```

Leave optional OAuth Client ID and Client Secret fields blank when the client
supports Dynamic Client Registration. The browser login and Cloudflare policy
establish the user identity. See [Deployment](docs/deployment.md) for the
operator setup.

## Data-source status

The repository separates implemented adapters from reviewed provider designs:

| Source | Version 0.1 status |
| --- | --- |
| Existing normalized Supabase data | Implemented |
| Apple Health through a Health Auto Export delivery table | Implemented bounded normalization/sync |
| WHOOP | Security and onboarding specification; OAuth/sync adapter not yet implemented |
| Oura | Security and onboarding specification; OAuth/sync adapter not yet implemented |
| Fitbit / Google Health API | Security and onboarding specification; OAuth/sync adapter not yet implemented |
| Garmin | Guided Developer Program specification; adapter not yet implemented |
| Android Health Connect | Companion/export specification; adapter not yet implemented |

Provider documents are implementation contracts, not claims that every provider
can already be connected by this package. See [Provider onboarding](docs/providers/README.md).

## Optional advanced runtimes

Docker is for technical operators and is not required for ordinary native use:

```bash
heavenly runtime use docker
heavenly runtime start
heavenly runtime status
```

The Compose service is non-root, loopback-only, read-only at the root
filesystem, capability-dropped, and health checked. Private storage settings are
not injected by default, so the public Docker profile intentionally exposes only
`protocol_status`.

The independent generic agent sandbox can run any user-supplied CLI-agent image:

```bash
heavenly agent run --image <agent-image> --workspace "$PWD" -- <agent-command>
```

It starts with no network, a read-only workspace and root filesystem, no host
home or Docker socket, no ambient secrets, no Linux capabilities, and a non-root
user. Every additional grant is explicit. See [Agent sandbox](docs/agent-sandbox.md).

## Security and privacy

- Owner-specific domains, identities, account IDs, table overrides, paths, and
  credentials are runtime variables, never repository defaults.
- Runtime files must be regular, owner-only files; symlinks and loose modes fail
  closed.
- Remote health access requires Cloudflare Access plus independent origin JWT
  verification and an exact identity allowlist.
- Reads are metric-allowlisted, time-bounded to 31 days, and limited to 200 rows.
- Raw provenance stays in its restricted relation and is not returned over MCP.
- Health writes require a separate owner approval from the local CLI.

Read [`SECURITY.md`](SECURITY.md) and [the security design](docs/security.md)
before exposing a remote endpoint. Never put personal health data, tokens,
callback codes, runtime files, or deployment identifiers in an issue.

## Development

```bash
uv run ruff check src tests
uv run pyright src
uv run --extra dev pytest
uv build
```

The release process exports only a validated tracked-file manifest into a fresh
Git history. Local `.env`, handover, state, credential, log, and agent-auth files
are excluded and rejected by the public-release guard.

## Documentation

- [Onboarding](docs/onboarding.md)
- [Deployment](docs/deployment.md)
- [Provider onboarding](docs/providers/README.md)
- [Architecture](docs/architecture.md)
- [MCP tool policy](docs/mcp-tool-policy.md)
- [Security](docs/security.md)

## License

MIT.
