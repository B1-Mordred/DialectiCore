# Security

Increment 6 adds an API-level RBAC guard, optional trusted identity headers, and
provider-managed bearer session validation by token introspection for
self-hosted deployments behind an authenticating reverse proxy or
identity-aware gateway.

## Credential Handling

- Plaintext provider credentials are not stored in normal application tables.
- API responses return credential references such as `env:VOICEBOX_TOKEN`, not
  secret values.
- Provider endpoint and publisher target `base_url` fields reject URL
  username/password userinfo; credentials must be supplied through credential
  references. Request-validation error responses omit rejected input values so a
  malformed secret-bearing request is not echoed back to clients.
- Provider endpoint and publisher target `credential_reference` fields require
  `scheme:target` syntax so a pasted raw token is rejected instead of stored.
  Secret-shaped keys inside endpoint/target capability maps are redacted on
  write, while reference-shaped OAuth capability fields remain available.
- The RBAC API key is also configured by reference:
  `DIALECTICORE_AUTH_API_KEY_REFERENCE=env:DIALECTICORE_API_KEY`.
- Credential references support `env:NAME`, `file:/absolute/path`, and
  `docker-secret:name`; Docker-secret references read `/run/secrets/name`.
  Other prefixes are reported as unsupported by health, live readiness, and the
  credential-provisioning checklist rather than being treated as valid secret
  managers. Invalid/raw setting references are reported as `[invalid]` with a
  separate invalid count, unsupported reference targets are hidden, and both
  invalid syntax and unsupported schemes fail the credential-reference readiness
  gate without reflecting pasted token values.
- Backup, restore, configuration, approval, and production operations emit audit
  events without writing raw secret material into audit details.

## RBAC

RBAC is disabled by default for local development:

```env
DIALECTICORE_AUTH_ENABLED=false
```

When enabled, every `/api/v1/*` request must include:

- `x-dialecticore-api-key`: value matching the configured API-key reference.
- `x-dialecticore-role`: one of `viewer`, `reviewer`, `editor`, `producer`,
  or `admin`.
- `x-dialecticore-user`: optional stable user ID for audit/context.

Alternatively, when `DIALECTICORE_AUTH_TRUSTED_IDENTITY_ENABLED=true`, a trusted
upstream gateway can authenticate sessions and inject identity headers:

- `x-forwarded-user`: stable authenticated user ID.
- `x-forwarded-email`: optional user email, surfaced only as safe policy
  metadata.
- `x-forwarded-groups`: comma- or semicolon-separated upstream groups.

Map upstream groups to DialectiCore roles with
`DIALECTICORE_AUTH_TRUSTED_GROUP_ROLE_MAP`, for example:

```env
DIALECTICORE_AUTH_TRUSTED_IDENTITY_ENABLED=true
DIALECTICORE_AUTH_TRUSTED_GROUP_ROLE_MAP=dialecticore-producers=producer,dialecticore-admins=admin
```

Trusted identities without a mapped group receive
`DIALECTICORE_AUTH_TRUSTED_DEFAULT_ROLE`, which defaults to `viewer`. If no
trusted identity header is present, the API-key path remains available.
For production readiness, configure at least one admin-capable bootstrap path:
an API-key reference usable with the `admin` role, a trusted identity default
role of `admin`, or a trusted/provider group-role mapping that assigns an
upstream administrator group to `admin`. This check is reported as
`initial_admin_path_configured` in deployment readiness and live-provider
readiness without exposing any credential values.
For the Docker-secret-backed API-key path, run
`PYTHONPATH=backend .venv/bin/python -m app.bootstrap_admin --secrets-dir secrets`
before starting the production-secrets Compose stack. The helper creates
`secrets/dialecticore_api_key` with `0600` permissions, refuses accidental
overwrite unless `--force` is passed for rotation, and prints the required admin
role/user headers while redacting the generated key unless `--show-secret` is
explicitly requested.
When that bootstrap path uses an API-key reference, deployment readiness also
verifies the reference resolves and reports
`auth_api_key_reference_available=false` if the configured environment variable,
absolute file, or Docker secret is unavailable.
Deployment readiness also checks known placeholder/default secret material after
resolving configured environment, absolute file, and Docker-secret references,
then reports only safe setting labels when a sentinel value is still active.
The internal Docker API healthcheck follows the same policy order: it uses the
configured API-key reference when present, or trusted-identity headers when that
mode is enabled without an API key. Provider-managed bearer sessions still
require an external token source and are not used by the container healthcheck.
That helper also validates the health response body and only accepts configured
JSON `status` values, defaulting to `healthy,degraded`, so a malformed or
`unhealthy` payload fails even when the endpoint returns HTTP 200.
The `auth_runtime` health component resolves configured API-key references for
readiness and reports an unavailable reference as a failed
`api_key_reference_resolves` gate without returning the key value.
It also verifies that enabled API-key, trusted-identity, and provider-session
header names plus provider-session user/group claim names are non-empty before
an operator cuts over live traffic.
The API and worker Dockerfiles are contract-tested to keep the Python runtime
non-root at UID/GID `10001`, install FFmpeg for media probing/rendering, clean
apt package lists, avoid pip cache retention, and confine intended writes to
prepared `/data/*` mount points.
The Web UI container runs with a read-only root filesystem and Nginx temporary
paths under `/tmp`, which Compose mounts as a bounded tmpfs, so routine proxy
buffering does not require writable image layers. The Python and Web UI tmpfs
limits are controlled separately through `DIALECTICORE_DOCKER_PYTHON_TMPFS_SIZE`
and `DIALECTICORE_DOCKER_WEB_TMPFS_SIZE`, keeping scratch-space expansion an
explicit operator choice.
The Web UI image/proxy contract is also tested so the production path remains a
static unprivileged Nginx runtime with same-origin `/api/` proxying, forwarded
headers, disabled proxy buffering for status streams, and SPA route fallback.

Provider-managed sessions are available when
`DIALECTICORE_AUTH_PROVIDER_SESSION_ENABLED=true`. Requests send a bearer token
in `Authorization` by default. DialectiCore posts that token to
`DIALECTICORE_AUTH_PROVIDER_SESSION_INTROSPECTION_URL`, requires
`active=true`, rejects expired `exp` values, maps provider groups to RBAC roles,
checks the file-backed revocation registry, and never returns the bearer token
or raw client secret in API responses. `auth_runtime` preflights optional
provider-session HTTPS introspection URL policy plus optional client ID/secret
references, provider token header-name configuration, and provider user/group
claim-name configuration, and reports only scheme/reference-status metadata when
those checks fail.

```env
DIALECTICORE_AUTH_PROVIDER_SESSION_ENABLED=true
DIALECTICORE_AUTH_PROVIDER_SESSION_INTROSPECTION_URL=https://idp.example.test/oauth2/introspect
DIALECTICORE_AUTH_PROVIDER_SESSION_CLIENT_ID_REFERENCE=env:OIDC_CLIENT_ID
DIALECTICORE_AUTH_PROVIDER_SESSION_CLIENT_SECRET_REFERENCE=docker-secret:oidc_client_secret
DIALECTICORE_AUTH_PROVIDER_SESSION_GROUP_ROLE_MAP=dialecticore-producers=producer,dialecticore-admins=admin
```

Provider logout or revocation webhooks can write safe revocation records through
`POST /api/v1/system/auth/provider-session/revocations`. Operators can record
and list the same active revocations in the Web UI Security panel. Records may
target a `token_sha256`, `jti`, or `subject`; raw bearer tokens are not stored.
The registry defaults to `DIALECTICORE_RUNTIME_STATE_PATH/auth` and is listed
through `GET /api/v1/system/auth/provider-session/revocations`.

Every provider-session bearer validation also writes a bounded decision record
to `DIALECTICORE_RUNTIME_STATE_PATH/auth/provider-session-decisions.json` unless
`DIALECTICORE_AUTH_PROVIDER_SESSION_DECISION_LOG_PATH` points elsewhere. The log
retains `DIALECTICORE_AUTH_PROVIDER_SESSION_DECISION_LOG_LIMIT` records, default
`100`, and stores only safe operational evidence: token SHA-256 hash, subject,
JTI, mapped user/role/groups, permission, request method/path, accepted/denied
or error status, denial reason, and expiry. The API exposes recent records at
`GET /api/v1/system/auth/provider-session/decisions`; the Web UI Security panel
shows the same recent decisions next to revocations.

Roles:

- `viewer`: read-only API access.
- `reviewer`: read plus approval decisions.
- `editor`: read, episode creation, research, transcript edit, approvals, and
  media-generation controls.
- `producer`: editor permissions plus production start, workflow controls,
  backup creation, publishing, and worker heartbeats.
- `admin`: all permissions, including configuration changes and restore.

Protected permission categories include read, episode write, production run,
workflow control, transcript edit, approval decision, media generation,
configuration write, backup create, backup restore, publish, and worker
heartbeat.

`GET /api/v1/system/auth-policy` exposes the current role/permission matrix,
header names, configured authentication modes, provider session introspection
configuration status, provider revocation count, provider decision-log retention
status, the full route permission vocabulary, and group-map presence without
returning API keys, bearer tokens, or raw client secrets. The permission
vocabulary includes admin-only route permissions such as `configuration_write`
and `backup_restore` even though the admin role itself is represented as `*`.

## Deployment Notes

Do not ship a static API key to untrusted browsers as the final production
security boundary. For production exposure, place the Web UI behind an
authenticating reverse proxy or identity-aware gateway that injects trusted
role/user headers and keeps the API key server-side. Direct browser-provided
headers are acceptable only for private development, trusted internal operator
networks, or a private operator workstation.

Trusted identity headers are only safe when direct client access to the API is
blocked or the proxy strips and rewrites those headers. Provider sessions depend
on the identity provider's token lifecycle and introspection endpoint. The Web
UI Security panel can store a provider bearer token or API-key credentials in
browser localStorage, attach them to API requests using the configured safe
header names, and clear them with Logout.
