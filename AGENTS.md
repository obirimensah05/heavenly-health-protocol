# AGENTS.md

## Project scope

Heavenly Health Protocol is an LLM-agnostic MCP boundary over user-controlled
health data. Preserve compatibility with generic MCP clients; never make one
model, agent vendor, provider account, deployment domain, or operator identity a
runtime requirement.

## Non-negotiable privacy rules

- Never commit `.env`, `runtime.env`, handover files, logs, database dumps,
  access/refresh tokens, OAuth callback codes, assertions, API keys, private
  keys, provider payloads, health records, or local agent state.
- Keep owner names, emails, domains, Cloudflare team/account/application IDs,
  Supabase project references, table overrides, and absolute workstation paths
  in runtime variables outside Git.
- Examples use reserved domains such as `example.com`, generic identities such
  as `owner@example.com`, and placeholder identifiers.
- Never print credential values in tests, exceptions, previews, diffs, or MCP
  tool results. Error messages may name a missing variable, not its value.
- Do not make an existing private repository or its history public. Public
  releases use a validated tracked-file export and a fresh Git history.

## Product communication

- Lead ordinary users through the native path: configured data → native MCP →
  client connection.
- Cloudflare Managed OAuth uses Dynamic Client Registration. Compatible MCP
  clients normally leave optional client ID and client secret fields blank.
- Keep Docker, tunnel reconciliation, origin JWT validation, and the CLI-agent
  sandbox in advanced operator documentation.
- Clearly distinguish implemented adapters from provider specifications. Do not
  describe WHOOP, Oura, Google Health/Fitbit, Garmin, or Health Connect OAuth and
  sync as implemented until corresponding code and live verification exist.

## Runtime invariants

- Local MCP remains loopback-only by default.
- Unconfigured storage exposes only `protocol_status`.
- Configured storage uses fixed validated identifiers, an explicit metric
  allowlist, a 31-day maximum window, and a 200-row result limit.
- Remote traffic fails closed unless Cloudflare's assertion is independently
  verified for signature, issuer, audience, time, token type, and exact allowed
  identity.
- MCP cannot approve its own writes. Owner approval remains local and separate.
- The Docker MCP and agent-sandbox hardening controls may not be weakened to
  simplify onboarding.

## Engineering workflow

Use test-first development for behavior changes and preserve RED/GREEN evidence.
Before a release, run:

```bash
uv lock --check
uv run ruff check src tests
uv run pyright src
uv run --extra dev pytest
uv run python -m compileall -q src tests
uv build
docker compose config
```

Also audit locked dependencies, scan the built image, review the exact Git diff,
and run the public-release guard with deployment-specific forbidden markers.
Security findings must be fixed without silently removing required product
functionality.
