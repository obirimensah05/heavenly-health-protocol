# Deployment

## Standard local setup

The default setup is local and does not require Docker, a tunnel, or a cloud database.

```bash
uv tool install --editable --force /absolute/path/to/heavenly-health-protocol
heavenly setup --preview
heavenly runtime use native
heavenly runtime install-service  # persistent macOS user service
# or: heavenly runtime start      # portable foreground-independent process backend
```

The package is not published to a public package index. `setup --preview` is a
design preview; storage and Cloudflare setup remain operator-driven. Google
Health and Garmin have real provider lifecycle commands under `heavenly
provider`. The runtime starts status-only MCP when no storage adapter is
configured. Local credentials belong in the operating-system secret store or an
owner-only runtime file outside Git. Local MCP clients do not need a public URL.

The native launcher optionally reads `~/.config/heavenly/runtime.env`. The file
must be absolute, regular, owned by the current user, and mode `0600`; symlinks,
loose permissions, malformed lines, oversized files, and include cycles fail
closed. Only `HEAVENLY_*`, `SUPABASE_URL`, and
`SUPABASE_SERVICE_ROLE_KEY` are imported. Explicit process variables win.

## Supabase route

The implemented Supabase adapter is enabled only when both connection variables,
fixed relation names, and an explicit metric allowlist are provided through the
protected runtime environment. A generic example is:

```dotenv
SUPABASE_URL=https://project-ref.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<runtime-only secret>
HEAVENLY_HEALTH_TABLE=heavenly_health_events
HEAVENLY_RAW_HEALTH_TABLE=heavenly_health_raw_events
HEAVENLY_ALLOWED_METRICS=steps,resting_heart_rate,sleep_analysis
```

The native provider connectors reuse this explicit allowlist. Configure storage
first, then follow [Google Health](providers/google-health.md) or
[Garmin](providers/garmin.md). During `provider ... connect`, stop the native MCP
so the exact one-shot loopback callback can bind `127.0.0.1:8791`; restart it
after the initial sync. The checked-in Docker profile remains status-only and
does not receive provider credentials.

Table and column settings accept safe identifiers only. The Supabase endpoint must
be a public HTTPS origin, and credentials are redacted from representations and
errors. Apply the checked-in `sql/` migrations through an owner-controlled
database migration path; Heavenly does not expose SQL execution over MCP.

Optional Health Auto Export delivery and context relations are configured with:

```dotenv
HEAVENLY_APPLE_HEALTH_DELIVERY_TABLE=health_auto_export_deliveries
HEAVENLY_CONTEXT_TABLE=private_documents
HEAVENLY_CONTEXT_ID_COLUMN=document_id
HEAVENLY_CONTEXT_TITLE_COLUMN=title
HEAVENLY_CONTEXT_BODY_COLUMN=body_text
HEAVENLY_CONTEXT_SEARCH_COLUMN=search_tsv
HEAVENLY_CONTEXT_UPDATED_COLUMN=updated_at
```

Owner-specific relation names and endpoints remain outside the protocol repository.

## Advanced Docker route

Docker is optional and intended for technical users, a Mac mini/home server, or an isolated bot runtime. The checked-in Compose service runs the current Heavenly MCP mode and provides:

- a non-root process;
- the persistent `heavenly_data` named volume;
- a loopback-only listener at `127.0.0.1:8791` (not a public port);
- health checks;
- a read-only root filesystem, temporary `/tmp`, dropped Linux capabilities, and no-new-privileges;

It does not provide OAuth secret injection, separate local/remote Compose
profiles, or provider synchronization. The checked-in Compose file is therefore
for the local status-only MCP service, not a production remote OAuth deployment.
Do not put OAuth credentials in committed `.env` files.

The separate `heavenly agent run` command can launch any CLI-agent image in a
default-deny container. It is independent from the MCP service container; see
[CLI-agent Docker sandbox](agent-sandbox.md).

## Remote MCP route

Remote MCP access is opt-in. The secure pattern is a named Cloudflare Tunnel, Cloudflare Access, and a user-controlled domain. A remote user must authenticate before health tools are available.

### Domain choice

The default recommended hostname is a dedicated subdomain:

```text
health-mcp.<your-domain>
```

Use `mcp.<your-domain>` only when this is the only MCP service planned for that domain. A dedicated `health-mcp` subdomain makes purpose, access policies, revocation, and future routing clearer.

A user can bring a domain from any registrar and manage its DNS through Cloudflare. If they do not already own one, a low-cost domain from a registrar such as Porkbun can be purchased, then added to Cloudflare before creating the named tunnel.

Cloudflare's free plan provides DNS, Tunnel, and Access tiers; it does not provide a free permanent custom domain registration. Cloudflare Quick Tunnels (`*.trycloudflare.com`) are temporary, public, and unsuitable for private health data. `*.pages.dev` is appropriate only for a public static landing/legal site, not a private MCP endpoint.

### Recommended route: Cloudflare Managed OAuth

Managed OAuth is the default remote route for an MCP server behind Cloudflare
Access. It keeps the existing self-hosted application and exact-email policies,
returns a standards-compatible `401` challenge to MCP clients, performs dynamic
client registration and authorization-code/PKCE at Access, and forwards a signed
`Cf-Access-Jwt-Assertion` to Heavenly. No SaaS OIDC client secret or FastMCP token
store is required for this mode.

Configure all four origin values through an owner-only runtime file or secret
manager; a partial trust configuration fails closed:

```dotenv
HEAVENLY_CLOUDFLARE_TEAM_DOMAIN=https://team.cloudflareaccess.com
HEAVENLY_CLOUDFLARE_ACCESS_AUDIENCE=<application AUD tag>
HEAVENLY_CLOUDFLARE_ALLOWED_EMAILS=owner@example.com
HEAVENLY_MCP_PUBLIC_HOST=health-mcp.example.com
```

The origin fetches keys only from the configured team's
`/cdn-cgi/access/certs`, accepts RS256, and checks signature, issuer, audience,
time claims, subject, application token type, and the exact identity allowlist.
The allowlist is checked again at the origin even when an Access policy also
allows that identity.

To bootstrap from a current owner Access JWT without displaying its claims:

```bash
heavenly access oauth configure-runtime \
  --assertion-file /private/path/access.jwt \
  --host health-mcp.example.com \
  --team-domain https://team.cloudflareaccess.com \
  --audience <application AUD tag>
```

`--team-domain` and `--audience` come from your own Cloudflare dashboard and are
required. They are deliberately not read from the assertion: any Access team can
mint a valid token for its own issuer and audience, so a token that nominates the
anchor it is checked against proves nothing about who owns this origin. The
assertion supplies only the owner identity, after its signature verifies against
your anchor.

The JWT and runtime file must be absolute, regular, owner-only files. Heavenly
verifies the JWT against Cloudflare before atomically updating the existing
runtime file.

For a repeatable API configuration, provide the account and application IDs plus
an `Access: Apps and Policies Write` token through the environment or the
`heavenly-cloudflare-api-token` macOS Keychain item:

```bash
heavenly access oauth plan --host health-mcp.example.com
heavenly access oauth apply --host health-mcp.example.com
```

`plan` is read-only and redacted. `apply` refuses the wrong application, a
non-self-hosted type, a different hostname, bypass/Everyone/domain-wide rules, or
anything except exact-email allow policies. It preserves the returned
application, enables Managed OAuth, permits localhost and loopback dynamic client
callbacks, keeps existing redirect URIs/grant settings, and supplies conservative
15-minute/14-day defaults only when absent. Re-running is idempotent.

The expected live boundary is:

- unauthenticated MCP request: `401`, no `Location`, with a
  `WWW-Authenticate` resource-metadata link;
- advertised protected-resource metadata: `200` JSON;
- advertised authorization-server metadata: `200` JSON;
- authenticated request: Cloudflare injects one signed Access assertion and the
  Heavenly origin independently validates it before FastMCP runs.

### Alternative route: FastMCP OIDC proxy

The earlier Cloudflare Access-for-SaaS/FastMCP OIDC proxy remains available for
deployments that specifically need Heavenly to terminate OAuth. It requires the
full `HEAVENLY_OIDC_*` and encrypted-state configuration documented in the source
settings. Discovery is restricted to the configured HTTPS
`*.cloudflareaccess.com` SaaS discovery path and all returned endpoints are
prevalidated. This mode is mutually exclusive with Managed OAuth. Do not place
client/signing/encryption secrets in a committed `.env` file.

FastMCP host/origin protection is enabled in every HTTP mode.
`HEAVENLY_MCP_PUBLIC_HOST` accepts only a public fully-qualified DNS hostname,
not a URL, IP literal, localhost, port, path, or single-label name.

### Reaching the origin directly

Access enforcement keys off the real transport peer, not the `Host` header, so a
caller that reaches the origin port directly still needs a valid assertion. Keep
the published port on host loopback anyway (`127.0.0.1:8791:8791`, as shipped in
`compose.yaml`) so the tunnel stays the only network path. Without an OAuth mode
configured the server refuses to bind anything but loopback or the container's
`0.0.0.0`, and will exit at startup rather than serve unauthenticated tools.

### Required production checks

Before publishing a remote health MCP endpoint:

1. The MCP service works through `http://127.0.0.1:<port>/mcp` locally.
2. The domain is active in Cloudflare and the user has approved the named tunnel login.
3. A DNS route points `health-mcp.<your-domain>` to that named tunnel.
4. Cloudflare Access protects the endpoint with exact-identity policies and
   Managed OAuth; the origin has the matching audience/team/identity settings.
   See [Cloudflare Access automation](cloudflare-access-automation.md).
5. Without storage configuration, only `protocol_status` is exposed. With storage
   configured, verify the exact allowlist, 31-day maximum query window, 200-row
   maximum, raw-payload exclusion, and CLI-only mutation approval.
   Apply `sql/003_least_privilege.sql`, then set `SUPABASE_HEALTH_ROLE_KEY`
   (`supabase gen bearer-jwt --role heavenly_health_app`) together with
   `SUPABASE_PUBLISHABLE_KEY`, and remove the service-role key. Confirm
   `health_connector_status` reports `"credential_scope": "scoped_role"`. A
   `service_role` scope there means the process still holds project-wide rights.
   Verify the scoping by reading a non-health table with the same token: it must
   fail, not return rows.
6. Verify the unauthenticated `401` metadata flow, then complete dynamic client
   registration/browser authorization and call a tool with a fresh MCP client.
