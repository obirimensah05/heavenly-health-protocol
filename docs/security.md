# Security and privacy

## Non-negotiable separation

Health data may be stored in a user-selected destination. Credentials must not be stored in Obsidian, Google Drive, iCloud Drive, Git, public repositories, prompts, or Markdown summaries.

## Secret storage

- macOS: Keychain
- Windows: Credential Manager/DPAPI
- Linux: Secret Service/keyring
- Docker/server target: Docker secrets or a managed encrypted secret store. The
  checked-in local Compose service does not yet wire OAuth secrets and must not
  be represented as a production remote OAuth deployment.

Native Heavenly also supports an owner-only runtime file outside Git. The loader
uses no shell evaluation, imports only application/Supabase names, does not
override explicit process values, uses `O_NOFOLLOW` where available, and rejects
symlinks, group/other permissions, malformed input, oversized files, and include
cycles. Values are never included in diagnostics.

The CLI-agent sandbox imports no ambient credentials. A secret enters only when
its environment variable name is repeated with `--secret-env`; its value is
inherited by Docker without appearing in the command. The default sandbox has no
network and cannot modify the workspace. See [CLI-agent Docker sandbox](agent-sandbox.md).

## Remote origin authentication

Cloudflare Managed OAuth terminates the client authorization-code flow at Access
and forwards `Cf-Access-Jwt-Assertion` to Heavenly. The origin validates the
RS256 signature against the configured team JWKS, issuer, application audience,
required time/subject/type claims, and an exact normalized owner-email allowlist.
Managed OAuth and the legacy FastMCP OIDC proxy cannot be enabled together.

The exemption for local use is decided by the real transport peer, taken from the
ASGI connection scope, never from the `Host` header or any other caller-supplied
value. A request whose peer is not a loopback address must present one valid
assertion or receive a generic `403`, so reaching the origin port directly does
not bypass Access. An absent or unparsable peer is treated as remote. Requests
that do carry Cloudflare headers or an assertion are always verified, even from
loopback, so a spoofed `Cf-Connecting-Ip` cannot downgrade the check.

Bootstrapping origin trust (`heavenly access oauth configure-runtime`) requires
`--team-domain` and `--audience` from your own Cloudflare dashboard. These are
never read out of the assertion: any Access team can mint a well-formed token for
its own issuer and audience, so a token that selects the anchor it is verified
against proves nothing. The assertion supplies only the owner identity, and only
after its signature verifies against the operator-supplied anchor.

Without an OAuth mode configured, the server refuses to bind a reachable address.
Loopback and the container's `0.0.0.0` (which relies on a loopback-only published
port) are the only accepted binds.

## Database privilege

`sql/003_least_privilege.sql` forces row level security on both health tables,
revokes their `anon`/`authenticated` grants, closes the schema's default
privileges so later tables start closed, and creates the `heavenly_health_app`
role holding rights on those two tables only.

Prefer `SUPABASE_HEALTH_ROLE_KEY`, a PostgREST JWT whose `role` claim is
`heavenly_health_app`, over `SUPABASE_SERVICE_ROLE_KEY`. It must be paired with
`SUPABASE_PUBLISHABLE_KEY` (or `SUPABASE_ANON_KEY`): Supabase validates the
`apikey` header against the project's registered keys and reads the role from
`Authorization`, so a minted role token sent as `apikey` fails every request as
`Invalid API key` before RLS is consulted. Service-role happens to satisfy both
headers, which is exactly why the split is easy to miss. Service-role carries
`BYPASSRLS` and project-wide rights, so no policy constrains a process holding
it; the table allowlist would be enforced only in application code. Whichever key
is configured, `health_connector_status` reports `credential_scope` so the
current privilege level is visible rather than assumed.

## Default-deny sensitive data

Disable medication, reproductive, clinical, ECG, mental-state, and route/location data by default. Users opt in at the metric level.

## Revocation and deletion

Every provider connector must offer disconnect/revoke and local-data deletion instructions. Remote MCP endpoints require authentication and least-privilege tool access.

MCP cannot approve its own mutation proposals. Approval records and their signing
key are owner-only; payload integrity is checked before an approved event receives
a deterministic idempotency identity. Raw provider records are retained only in
the restricted provenance relation and are not exposed through MCP tools.

## Release checks

The release workflow keeps Python dependencies locked, pins GitHub Actions and
the Docker base image by immutable digest, and runs tests, Ruff, Pyright, compile,
build, and Compose validation. Before a reviewed push, audit the exported lock
with `pip-audit`, scan the built image for high/critical vulnerabilities, and scan
the exact staged files/history for credentials.
