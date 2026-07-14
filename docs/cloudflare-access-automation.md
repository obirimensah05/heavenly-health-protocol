# Cloudflare Access automation

Cloudflare Access is the authorization server and edge policy gate for the
recommended Managed OAuth route. For the separately configured FastMCP OIDC
proxy route, it can instead serve as defense in depth. The connector account,
model subscription, Cloudflare account, and Access allowlist identity can all be
different; setup must ask for exact allowed email addresses rather than infer
them from another connected account.

Heavenly supports a reviewed command path for maintaining a pre-existing Cloudflare Access policy. The command adds exact email rules only; it never creates an `Everyone` rule, changes tunnel routing, or prints secrets.

## Runtime-only configuration

Keep the Cloudflare API token in Keychain, a secret manager, or injected CI/runtime environment. Do not store it in Git, project `.env` files, documentation, agent prompts, or chat.

The applying process needs these environment variables:

```text
CLOUDFLARE_API_TOKEN
HEAVENLY_CLOUDFLARE_ACCOUNT_ID
HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID
HEAVENLY_CLOUDFLARE_ACCESS_POLICY_ID
```

On macOS, the Managed OAuth commands also look for the API token in the dedicated
Keychain item when `CLOUDFLARE_API_TOKEN` is absent:

```bash
read -s "CF_TOKEN?Cloudflare token: "
security add-generic-password -U -a "$USER" \
  -s heavenly-cloudflare-api-token -w "$CF_TOKEN"
unset CF_TOKEN
```

The API token must be narrowly scoped to edit the selected account's Cloudflare Zero Trust Access applications and policies. The account, application, and policy IDs identify the pre-existing private MCP policy; they are not credential values.

## Safe workflow

Preview first:

```bash
heavenly access allow approved.person@example.com
```

With runtime configuration present, preview performs a read-only API lookup and
shows the account/application/policy IDs, policy name, and decision. It fails if
the returned policy ID differs or the decision is not exactly `allow`. Without
runtime configuration, preview remains non-mutating but marks the target policy
as unresolved; configure and rerun preview before applying.

Apply only after reviewing the target email and validated policy identity:

```bash
heavenly access allow approved.person@example.com --apply
```

The command reads the policy again, fails closed unless it is the exact configured
`allow` policy with a readable name and include list, appends one exact `email`
include rule if absent, and writes the updated policy back through the Cloudflare
API. It preserves existing include rules such as other exact emails or domain rules.

If the email already exists, the command is idempotent and makes no Cloudflare API update.

## Managed OAuth reconciliation

Managed OAuth uses the account and application IDs but does not require the
policy ID because it validates every returned application policy:

```text
HEAVENLY_CLOUDFLARE_ACCOUNT_ID
HEAVENLY_CLOUDFLARE_ACCESS_APPLICATION_ID
HEAVENLY_MCP_PUBLIC_HOST
```

Preview the redacted target first, then apply:

```bash
heavenly access oauth plan --host health-mcp.example.com
heavenly access oauth apply --host health-mcp.example.com
```

The client requires the exact configured application ID, `self_hosted` type,
exact hostname, a readable application/AUD tag, and only exact-email `allow`
rules. It rejects bypass, Everyone, email-domain, or other broad rules. Apply
preserves the full application document returned by Cloudflare and reconciles
only `oauth_configuration`: enabled dynamic registration, localhost/loopback
callbacks, and default 15-minute access/14-day grant lifetimes when the account
has not selected values. A second apply performs no write.

The origin still validates the signed Access JWT and its own exact identity
allowlist. Cloudflare policy acceptance alone is not sufficient.

## Agent workflow

A user can tell a trusted agent:

```text
Add approved.person@example.com to the Heavenly MCP allowlist.
```

The agent should show the preview first and perform `--apply` only after the requested email and target policy have been confirmed. Access tokens and Cloudflare API tokens must never be pasted into chat.
