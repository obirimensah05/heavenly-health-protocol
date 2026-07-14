# Heavenly Health Protocol

Connect your health data — Apple Watch, Fitbit, Garmin, and more — to your AI
agent. Privately, on your own computer, with your own storage.

Your agent (Claude, ChatGPT, Codex, Hermes, OpenClaw, …) gets bounded access to
exactly the metrics you allow. Your credentials and raw health records never
leave your control.

## Get started

Requirements: Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/obirimensah05/heavenly-health-protocol.git
cd heavenly-health-protocol
uv sync
uv tool install .
heavenly setup
```

`heavenly setup` walks you through everything. There is nothing to configure
first, no Docker, and no jargon.

## The onboarding questions

Setup is a short guided conversation. These are the questions, in order:

1. **Which fitness or health device do you use?**
   Apple Watch / iPhone · Fitbit / Pixel Watch · Garmin · WHOOP · Oura Ring ·
   other Android wearables
2. **Which app is your health data's source of truth?**
   Apple Health · Google Health API · Garmin Connect · WHOOP · Oura ·
   Health Connect
3. **Where should your agent-readable health data live?**
   Your own free Supabase project works today; Obsidian, local SQLite,
   Google Drive, and iCloud Drive are on the roadmap.
4. **Which AI agent should read it — and where does it run?**
   Claude Code, Claude, ChatGPT, Codex, Hermes, OpenClaw, Perplexity, or any
   other compatible agent, on this computer or in the cloud.
5. **When should your health analysis arrive?**
   Daily, every 3 days, or weekly · morning or evening · your timezone is
   detected automatically.
6. **Which metrics may it track?**
   A minimal allowlist you approve. Clinical records, medication, reproductive
   data, ECG, and location stay off by default.
7. **Start and connect.**
   Heavenly starts on your computer and shows the one-line connection step for
   your agent.

That's it. Advanced extras — the Docker runtime, remote access for cloud
agents, the agent sandbox — are offered at the very end, default to off, and
most people never need them.

## What works today

| Your setup | Status |
| --- | --- |
| Apple Watch / iPhone via Apple Health + Health Auto Export | ✅ Works today |
| Fitbit / Pixel Watch via the Google Health API v4 | ✅ Works today |
| WHOOP | ✅ Works today — data needs an active WHOOP membership |
| Oura | ✅ Works today |
| Garmin via Garmin Connect | ✅ Built — requires Garmin Developer Program approval |
| Data you already keep in Supabase | ✅ Works today |
| Android Health Connect | 🔜 Planned (spec ready) |

Choosing a planned source still completes onboarding — your choice is recorded
and nothing has to be redone when its adapter ships.

### Signing in to WHOOP or Oura

Both take about two minutes and follow the same pattern:

1. Create a (free) developer app on the provider's site — [WHOOP](https://developer.whoop.com)
   or [Oura](https://cloud.ouraring.com/oauth/applications) — and note its
   client ID, client secret, and the redirect URL you register.
2. Put those four lines in an owner-only env file
   (`~/.config/heavenly/whoop.env` or `oura.env` — exact keys in
   [docs/providers](docs/providers/README.md)).
3. Sign in with your normal account:

```bash
heavenly provider whoop import-client ~/.config/heavenly/whoop.env
heavenly provider whoop connect     # browser opens; paste the redirected URL back
heavenly provider whoop sync
```

(Swap `whoop` for `oura` for an Oura Ring.) Your sign-in tokens are stored in
the operating-system credential vault — never in files, your notes, or Git.

## How it stays private

- Runs on your computer and listens only on localhost by default.
- Health data lives in storage **you** own; keys stay in an owner-only local
  file and the operating-system credential vault.
- Your agent sees a bounded, metric-allowlisted read surface — never raw
  provider payloads, credentials, or arbitrary SQL.
- Any write to your health data needs your explicit approval in the terminal.
  An agent can never approve its own writes.

Details: [SECURITY.md](SECURITY.md) and [the security design](docs/security.md).

## For technical operators (optional)

Everything below is opt-in and never required for the normal path above:

- **Remote access** for cloud agents through Cloudflare Managed OAuth —
  [Deployment](docs/deployment.md)
- **Hardened Docker runtime** — [Deployment](docs/deployment.md)
- **Generic CLI-agent sandbox** — [Agent sandbox](docs/agent-sandbox.md)
- **Architecture and MCP tool policy** — [Architecture](docs/architecture.md),
  [MCP tool policy](docs/mcp-tool-policy.md)
- **Provider onboarding contracts** — [Providers](docs/providers/README.md)

## Development

```bash
uv sync --extra dev
uv run ruff check src tests
uv run pyright src
uv run --extra dev pytest
```

The release process exports only a validated tracked-file manifest into a fresh
Git history. Local `.env`, credential, log, and state files are excluded and
rejected by the public-release guard.

## License

MIT.
