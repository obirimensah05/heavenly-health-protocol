# Security policy

## Supported version

Security fixes are applied to the latest commit on `main`. This project is
pre-1.0; pin deployments to a reviewed commit and read release notes before
updating.

## Report a vulnerability

Use GitHub's private vulnerability-reporting flow from the repository Security
tab. Do not open a public issue containing a vulnerability proof, private
hostname, account identifier, credential, OAuth code/token, provider payload, or
health record.

Include the affected commit, boundary, reproduction conditions, and expected
versus observed behavior. Redact all user and deployment data. If a real secret
was exposed, revoke or rotate it before continuing investigation.

## Security boundaries

- Native MCP binds to loopback unless an operator explicitly configures a
  reviewed public hostname.
- Remote access is expected to use Cloudflare Managed OAuth and an exact-identity
  policy. The origin independently verifies the Access assertion.
- Supabase credentials and OAuth material are loaded only from protected runtime
  inputs and never returned by tools.
- Health reads are relation-, metric-, time-, and row-bounded. Raw records and
  arbitrary SQL are not exposed.
- Health mutations require a separate local owner approval.
- Docker runtimes run non-root with a read-only root filesystem, dropped
  capabilities, no-new-privileges, and explicit resource/access grants.

## Public release controls

The public repository must be generated from a fresh tracked-file export, not by
changing visibility on a private repository. Release validation rejects local
environment files, credential artifacts, handover/state files, absolute user
home paths, secret-shaped content, injected private markers, symlinks, and paths
outside the manifest.

CI runs locked installation, linting, type checks, dependency audit, tests,
compilation, package build, Compose validation, and a safe setup preview.
Dependabot maintains Python, GitHub Actions, and Docker dependencies. Public
GitHub security features should keep secret scanning, dependency alerts, and
code scanning enabled.
