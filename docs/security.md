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
Public or Cloudflare-forwarded requests without one valid assertion receive a
generic `403`; loopback-native MCP remains available. Managed OAuth and the
legacy FastMCP OIDC proxy cannot be enabled together.

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
