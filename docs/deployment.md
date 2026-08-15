# Deployment

The repository includes a Docker Compose skeleton for local development and
initial self-hosting.

```bash
cp .env.example .env
docker compose up --build
```

Compose healthchecks gate startup order for the self-hosted stack. PostgreSQL
uses `pg_isready`, Redis uses `redis-cli ping`, and MinIO uses the local
`/minio/health/live` HTTP endpoint. The bundled Temporal service is also probed
with `temporal operator cluster health` against its local frontend before
Temporal-facing optional services start. PostgreSQL must become healthy before
Temporal starts, and PostgreSQL, Redis, and MinIO must become healthy before
`production-api` starts. The API runs migrations, serves `/api/v1/system/health`,
and only then do the Web UI and worker roles begin.
This prevents workers from polling before migrations and shared storage checks
are available. The API container healthcheck runs `python -m app.healthcheck`;
the Web UI healthcheck probes Nginx on `127.0.0.1:8080`, and worker services run
the shared `python -m app.worker_healthcheck` heartbeat helper. The Compose
startup contract test enforces those healthchecks plus the dependency graph:
Temporal after Postgres health, API after Postgres/Redis/MinIO health, Web UI
and normal workers after API health, and the optional external
`temporal-worker` plus opt-in Temporal UI after Temporal is healthy.
The API, Web UI, and worker healthcheck interval, timeout, retry, and start
period values are operator-tunable through the matching
`DIALECTICORE_API_HEALTHCHECK_*`, `DIALECTICORE_WEB_HEALTHCHECK_*`, and
`DIALECTICORE_WORKER_HEALTHCHECK_*` settings, so slower self-hosted hosts can
avoid false unhealthy states without editing Compose. PostgreSQL, Redis, MinIO,
and Temporal healthcheck cadence is tunable the same way through
`DIALECTICORE_POSTGRES_HEALTHCHECK_*`, `DIALECTICORE_REDIS_HEALTHCHECK_*`,
`DIALECTICORE_MINIO_HEALTHCHECK_*`, and
`DIALECTICORE_TEMPORAL_HEALTHCHECK_*` settings, while preserving the same probe
commands.
The API helper also validates the `/system/health` JSON payload instead of
treating any HTTP 200 as healthy. By default it accepts `healthy` and
`degraded`; set `DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES=healthy` to make a
degraded API payload fail the container healthcheck. The helper probe target
and internal HTTP request timeout are also operator-tunable through
`DIALECTICORE_HEALTHCHECK_URL` and
`DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS`; keep the helper timeout below the
Compose `DIALECTICORE_API_HEALTHCHECK_TIMEOUT` value so Docker can report the
helper's failure message instead of killing the probe first. Invalid helper
timeout values fail the container healthcheck with a setting-specific error
instead of a Python traceback.
When RBAC is enabled, that helper resolves `DIALECTICORE_AUTH_API_KEY_REFERENCE`
and sends the configured API key and admin role headers to the internal health
endpoint, including Docker-secret-backed keys in the production-secrets
override. If no API-key reference is configured but trusted-identity mode is
enabled, the helper sends the configured trusted identity/email headers instead,
which is sufficient for the read-only health endpoint. Authenticated
healthchecks fail fast with a setting-specific error when any required API-key,
role, user, trusted-identity, or trusted-email header name is blank.

The `web-ui` image is a production static build served by Nginx, not the Vite
development server. By default the built UI calls `/api` on the same origin, and
Nginx proxies that path to `production-api:8000`; set
`DIALECTICORE_WEB_API_BASE_URL` at build time only when the browser should call a
different API origin directly. `DIALECTICORE_WEB_ROLE` and
`DIALECTICORE_WEB_USER` are also build-time defaults for browser-originated API
requests, but API keys are deliberately not accepted as Web UI Docker build
arguments or image environment because static frontend artifacts are inspectable
by users. Use browser login/session storage, an operator reverse proxy, or the
same-origin API proxy with backend-managed auth instead of baking a shared API
key into the image. The host port remains `5173` for local compatibility, mapped
to port `8080` inside the unprivileged Nginx web container. The Web UI container
also runs with a read-only root filesystem, dropped Linux capabilities,
`no-new-privileges`, and an explicit `/tmp` tmpfs. The Nginx config routes
request/proxy temporary files to `/tmp/*` paths so the read-only root filesystem
remains compatible with uploads, proxied API responses, and other buffered
request handling.
The frontend Dockerfile and Nginx proxy contract are tested: the image must
build with `npm ci`, expose only the non-secret `VITE_API_BASE_URL`,
`VITE_DIALECTICORE_ROLE`, and `VITE_DIALECTICORE_USER` build-time defaults,
serve `/app/dist` from `nginxinc/nginx-unprivileged`, listen on container port
`8080`, keep the Compose `5173:8080` mapping, proxy same-origin `/api/` traffic
to `production-api:8000`, preserve host/forwarded headers, disable proxy
buffering for live status streams, and fall back unknown routes to `index.html`.

Docker build contexts intentionally exclude local development state, generated
artifacts, backend tests, generated package metadata, coverage output, caches,
SQLite databases, runtime storage, `.env` files, and common secret material.
The repository ignore rules mirror that local artifact and secret boundary so
coverage files, local databases, generated package metadata, API-key scratch
files, package-manager credential files, cloud or cluster credential
directories, secret directories, and private key/certificate material are not
staged by accident during self-hosted operations.
Provide production credentials through explicit environment variables, mounted
files, Docker secrets, or configured runtime volumes instead of placing secrets
or local data in the repository tree before building images.
Before setting `DIALECTICORE_ENV=production`, replace the Compose sentinel
credentials such as `DIALECTICORE_API_KEY=change-me-before-enabling-auth`,
`MINIO_ROOT_USER=dialecticore`, `MINIO_ROOT_PASSWORD=change-me-in-production`, and
`POSTGRES_PASSWORD=dialecticore`. The deployment-readiness health component
reports only safe setting labels when these placeholder/default secrets are
still active, including when the sentinel value is resolved from a configured
file or Docker secret reference; it never returns the secret values.
For auth-enabled stacks, deployment readiness also verifies that the configured
API-key reference resolves when it is used as an admin bootstrap path, reporting
`auth_api_key_reference_available=false` without returning the key value.
Production deployments should also set `DIALECTICORE_CORS_ALLOWED_ORIGINS` to
the exact browser origin served by the Web UI or reverse proxy. Wildcard CORS is
kept for local development and is flagged by deployment readiness in production.
The default Compose file passes the setting through with `*` for local
development. The production-secrets override changes the default to the local
Web UI origin `http://localhost:5173`; replace it with your HTTPS origin when
serving through a public or private reverse proxy.
The `production-api` and worker images run as the non-root `dialecticore` user
with UID/GID `10001`. The image prepares `/data/object-storage`,
`/data/runtime-state`, and `/data/backups` for that user so Compose named
volumes are writable on first use; when replacing them with host bind mounts,
pre-create those directories with ownership or ACLs that allow UID `10001` to
write.
The Dockerfile contract test enforces that the Python images keep this
non-root UID/GID, install FFmpeg without retaining apt package lists, use
`pip --no-cache-dir`, prepare the writable `/data/*` directories, and keep the
API migration/start command plus worker entrypoint stable.
Compose also applies runtime isolation to those Python services: the container
root filesystem is read-only, Linux capabilities are dropped, privilege
escalation is disabled, and `/tmp` is provided as an in-memory tmpfs for
temporary FFmpeg, backup, and atomic-write work. Tune the Python `/tmp` size
with `DIALECTICORE_DOCKER_PYTHON_TMPFS_SIZE` and the Web UI `/tmp` size with
`DIALECTICORE_DOCKER_WEB_TMPFS_SIZE` when larger media probes, renders, or proxy
requests need more scratch space. Durable writes should remain limited to the
configured `/data/*` volumes or external services.
Bundled infrastructure services also run with `no-new-privileges` alongside the
shared lifecycle, logging, and resource controls. They intentionally do not use
the read-only-root or dropped-capability profile because official PostgreSQL,
Redis, MinIO, and Temporal images perform entrypoint and data-directory setup
that may require their default filesystem and capability behavior.
Infrastructure images use `pull_policy: missing`, so routine `docker compose up`
reuses already-present image tags instead of implicitly refreshing them. Pull
new infrastructure images explicitly during planned maintenance with
`docker compose pull postgres redis minio temporal temporal-ui`, then rerun
deployment readiness checks before production work. The bundled image references
are also operator-tunable through `DIALECTICORE_POSTGRES_IMAGE`,
`DIALECTICORE_REDIS_IMAGE`, `DIALECTICORE_MINIO_IMAGE`,
`DIALECTICORE_TEMPORAL_IMAGE`, and `DIALECTICORE_TEMPORAL_UI_IMAGE`, which lets
self-hosted deployments pin patch tags or route pulls through an internal
registry without editing Compose.
All Compose services use `restart: unless-stopped` so routine host reboots or
container exits bring the self-hosted stack back without relying on a separate
supervisor. The Compose contract test covers the restart policy for Web UI,
API, worker, database, Redis, MinIO, and Temporal services.
Compose also bounds Docker JSON logs for every service with
`DIALECTICORE_DOCKER_LOG_MAX_SIZE` and
`DIALECTICORE_DOCKER_LOG_MAX_FILE` defaults. This prevents long-running
self-hosted workers, API healthchecks, and provider adapters from filling the
host Docker log directory during unattended operation.
Every Compose service also enables Docker's init process and uses
`DIALECTICORE_DOCKER_STOP_GRACE_PERIOD` for graceful shutdown. This lets Python
workers, FFmpeg subprocesses, database services, MinIO, Redis, and Temporal
receive a bounded termination window while avoiding orphaned child processes.
Compose also applies a bounded CPU/memory/process/file-descriptor posture with
`DIALECTICORE_DOCKER_CPU_LIMIT`, `DIALECTICORE_DOCKER_MEMORY_LIMIT`,
`DIALECTICORE_DOCKER_MEMORY_SWAP_LIMIT`, `DIALECTICORE_DOCKER_PIDS_LIMIT`,
`DIALECTICORE_DOCKER_NOFILE_SOFT_LIMIT`, and
`DIALECTICORE_DOCKER_NOFILE_HARD_LIMIT` across every service. These defaults are
intended to prevent runaway CPU, memory, subprocess, or descriptor leaks from
exhausting the host while leaving enough headroom for FFmpeg, HTTP clients,
Redis, MinIO, and PostgreSQL. The swap limit defaults to the same value as the
memory limit so a constrained container cannot silently push the host into heavy
swap. Increase the CPU, memory, or swap limits on hosts where multiple large
renders, database workloads, or object-storage transfers are expected.
Compose uses two explicit networks. `edge` carries browser-facing Web UI,
production API, and Temporal UI traffic. `backend` carries API-to-database,
Redis, MinIO, Temporal, and worker traffic. Data-plane services and workers are
not attached to `edge`, so adding the Web UI or Temporal UI does not place them
on the same network as PostgreSQL, Redis, and worker internals by default.
In the default Compose file, published ports bind to loopback by default:
`DIALECTICORE_WEB_BIND_ADDRESS`, `DIALECTICORE_API_BIND_ADDRESS`,
`DIALECTICORE_TEMPORAL_UI_BIND_ADDRESS`, and
`DIALECTICORE_MINIO_BIND_ADDRESS` default to `127.0.0.1`. The default MinIO
publication is limited to the S3 API on host port `9000`; the browser console
inside the MinIO container listens on `9001` but is not published by the default
Compose file. Add an operator-local Compose override for `9001:9001` only while
you need console access. Set the relevant bind address to `0.0.0.0` or a
specific host interface only when exposing the service through a trusted LAN or
reverse proxy.
When layering `docker-compose.production-secrets.yml`, direct host publication
for `production-api` and bundled MinIO is reset completely. In that production
path, expose only the Web UI or an operator-managed reverse proxy by default;
add a local override for API or S3 host ports only when that exposure is an
intentional part of the deployment.
Temporal UI is an operator surface and is not part of the default Compose
profile. Start it only when needed with
`docker compose --profile ops-ui up temporal-ui` or include `--profile ops-ui`
with a broader `up`.
Compose passes through the operator-tunable runtime and production settings used
by readiness checks and worker loops, including runtime-path free-space floors,
Temporal signal timeout, workflow retry attempts/backoff, research retrieval and
discovery limits, deterministic words-per-second timing, audio loudness analysis
targets, non-secret S3/MinIO object-storage endpoint/bucket/client options, and
the Redis URL and automated live publishing gate.
Production readiness also consumes persisted model-provider, Voicebox, and
ComfyUI endpoint records. A production stack needs at least one enabled non-mock
endpoint for model routing, audio synthesis, and visual generation; remote
endpoints that require a base URL must have one configured, and enabled
endpoints cannot be unhealthy or have unknown health. The default mock providers
remain useful for local smoke tests, but they are not enough for a
production-ready multi-AI deployment.
Production deployment readiness checks both Redis runtime mode booleans and the
Redis URL: `DIALECTICORE_REDIS_EVENT_FANOUT_ENABLED=true` and
`DIALECTICORE_REDIS_WORKER_SIGNAL_ENABLED=true` are not enough if
`DIALECTICORE_REDIS_URL` is blank. It also requires nonblank
`DIALECTICORE_REDIS_EVENT_CHANNEL` and
`DIALECTICORE_REDIS_WORKER_SIGNAL_STREAM` names when those modes are enabled.
The detailed Redis health component still performs the endpoint reachability
probe and reports the same channel/stream configuration gates.

Remote ComfyUI and Voicebox are intentionally not bundled as required local
containers. Their endpoints are configuration records managed by the API/UI in
later increments.

## Database

The Docker stack points the Python services at the bundled PostgreSQL container
by default. Local development without Docker uses `sqlite:///./dialecticore-dev.db`.

Run migrations with:

```bash
alembic upgrade head
```

The current API also creates the Increment 1 tables at startup for local smoke
runs, but migrations are the authoritative schema path.

The `production-api` image runs `alembic upgrade head` before Uvicorn starts.
System health reports the running database's recorded Alembic revision through
the `database_migrations` component. In production, deployment readiness also
includes a `database_schema_at_head` gate when the revision can be inspected, so
a persistent database that has no recorded revision or is behind the app image
must be migrated before live production work starts.

## API Access

RBAC is disabled by default in Compose for local development. Set
`DIALECTICORE_AUTH_ENABLED=true` and provide `DIALECTICORE_API_KEY` to require
`x-dialecticore-api-key` plus `x-dialecticore-role` on every `/api/v1/*`
request. For production browser access, prefer an authenticating reverse proxy
that keeps the API key server-side and injects trusted role/user headers for the
Web UI.
The Compose app environment passes through `DIALECTICORE_AUTH_API_KEY_REFERENCE`
plus the API-key, role, and user header-name settings, so deployments with
different secret locations or gateway header conventions can tune those values
without editing Compose.
The production readiness check also requires an initial admin-capable path:
either an API-key reference that can be sent with the `admin` role, a trusted
identity default role of `admin`, or a trusted/provider group-role mapping that
assigns at least one upstream group to `admin`. A valid non-admin login path is
not enough for a production deployment to be considered ready. The counted path
must also satisfy the same runtime prerequisites as `auth_runtime`: non-empty
required headers, HTTPS provider introspection, non-empty provider claim names,
and complete optional provider client credentials.

For identity-aware gateways, set
`DIALECTICORE_AUTH_TRUSTED_IDENTITY_ENABLED=true`, configure the trusted
identity/group headers, and map upstream groups with
`DIALECTICORE_AUTH_TRUSTED_GROUP_ROLE_MAP`. Only enable this mode when the API is
reachable through a proxy that strips client-supplied identity headers and
rewrites them after successful authentication.

For provider-managed bearer sessions, set
`DIALECTICORE_AUTH_PROVIDER_SESSION_ENABLED=true`, configure the introspection
URL, and map provider groups with
`DIALECTICORE_AUTH_PROVIDER_SESSION_GROUP_ROLE_MAP`. Use client ID/secret
references when the provider requires authenticated introspection. When no
bearer token is present, trusted identity and API-key modes remain available if
configured. The provider-session revocation registry is file-backed below
`DIALECTICORE_RUNTIME_STATE_PATH/auth` unless
`DIALECTICORE_AUTH_PROVIDER_SESSION_REVOCATION_PATH` points elsewhere; mount
that path on durable storage when logout/revocation webhooks must survive API
container replacement. Provider-session decisions are also retained in a
bounded file-backed log below the same runtime-state auth directory unless
`DIALECTICORE_AUTH_PROVIDER_SESSION_DECISION_LOG_PATH` points elsewhere; keep
that location durable when accepted/denied session evidence is operationally
important. Operators can list recent decisions, list active revocations, and
record new token-hash, JTI, or subject revocations from the Web UI Security
panel. For private operator workstations, the Web UI Security panel can store
either a provider bearer token or API-key credentials in browser
localStorage and clear them with Logout; production Internet exposure should
still prefer a proxy or identity-aware gateway that owns browser login.

Secret references can point at environment variables (`env:NAME`), absolute
files (`file:/absolute/path`), or Docker secrets (`docker-secret:name`, resolved
from `/run/secrets/name`). API responses and audit events preserve the reference
string and do not return the secret value. In Compose or Swarm deployments, mount
Docker secrets into services that need them, then set the matching
`DIALECTICORE_*_REFERENCE` or endpoint `credential_reference` to
`docker-secret:<name>`.
The Compose app environment also passes through `DIALECTICORE_MODEL_PROVIDER`
with a `mock` default for local reproducibility, so operators can select a
different configured model-provider family without editing Compose.

For a local self-hosted production stack, create secret files outside version
control and layer the production override on top of the default Compose file:

```bash
mkdir -p secrets
PYTHONPATH=backend .venv/bin/python -m app.bootstrap_admin --secrets-dir secrets
openssl rand -base64 48 > secrets/postgres_password
openssl rand -base64 32 > secrets/minio_root_user
openssl rand -base64 48 > secrets/minio_root_password
docker compose -f docker-compose.yml -f docker-compose.production-secrets.yml up --build
```

`PYTHONPATH=backend .venv/bin/python -m app.bootstrap_admin` creates the
initial admin API-key Docker secret at `secrets/dialecticore_api_key` with
`0600` permissions, refuses to overwrite an existing key unless `--force` is
passed, and prints the configured `docker-secret:dialecticore_api_key` reference
plus the admin role/user headers. It redacts the generated token by default;
pass `--show-secret` only when an operator needs to copy the key into a private
browser or curl session once.

The override sets `DIALECTICORE_ENV=production`, enables RBAC and Redis runtime
channels, mounts the four secret files into API and worker containers, points
the API key, database password, MinIO access-key, and MinIO secret-key references
at `docker-secret:*`, and configures PostgreSQL and MinIO to read their root
credentials from `/run/secrets`. Secret mounts use explicit long-form Compose entries with
stable `/run/secrets/<name>` targets and read-only `0444` mode, rather than
relying on short-form defaults. The override also sets
`DIALECTICORE_CORS_ALLOWED_ORIGINS` to the local Web UI origin by default so
production readiness does not allow wildcard
CORS. `DIALECTICORE_DATABASE_URL` is left blank in that override so the API
assembles a PostgreSQL URL from operator-tunable `DIALECTICORE_DATABASE_*`
settings plus
`DIALECTICORE_DATABASE_PASSWORD_REFERENCE=docker-secret:postgres_password`
by default, instead of embedding the password in an environment variable.
The production API-key reference defaults to
`docker-secret:dialecticore_api_key`, but the same
`DIALECTICORE_AUTH_API_KEY_REFERENCE` setting can point at a differently named
Docker secret or another supported secret-reference scheme.
The object-storage access-key and secret-key references default to the bundled
MinIO Docker secrets in the production override, but
`DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE` and
`DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE` can point at differently
named Docker secrets, absolute secret files, or environment-backed credentials
for an external S3-compatible service.
The live B1 and OpenRouter provider presets keep their endpoint credential
references as `env:B1_API_KEY` and `env:OPENROUTER_API_KEY` so local dev, UI
provisioning, and backups do not need environment-specific endpoint rewrites.
In the production-secrets override those environment variables are intentionally
blank and the corresponding `B1_API_KEY_FILE` and `OPENROUTER_API_KEY_FILE`
variables point at `/run/secrets/b1_api_key` and
`/run/secrets/openrouter_api_key`. The shared credential resolver treats
`env:NAME` as `NAME` first and then `NAME_FILE`, which lets the same saved
provider records work in both `.env`-based development and Docker-secret-backed
production. Create those two secret files before using the production overlay
for live talkshow production.
The production-secrets override also clears direct production API and MinIO host
port publication. Browser API traffic should reach `production-api` through the
Web UI Nginx same-origin `/api/` proxy, and the API reaches bundled MinIO over
the private backend network. Add an operator-local override only if a managed
reverse proxy needs direct API access or host-side S3 tooling needs direct
access to the bundled MinIO API.
Render both deployment models before applying Compose changes:

```bash
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.production-secrets.yml config --quiet
docker compose -f docker-compose.yml --profile ops-ui config --quiet
docker compose -f docker-compose.yml -f docker-compose.production-secrets.yml --profile ops-ui --profile temporal-external config --quiet
```

The Compose contract tests enforce this rendered override boundary: API and
worker services must use the shared Docker-secret environment, raw API-key and
MinIO sentinel variables must be blank in production, PostgreSQL and MinIO must
use their `*_FILE` secret paths, Temporal must read the Postgres password from
the mounted Docker secret before starting, direct production API/MinIO host ports
must be removed, and worker-role assignments plus healthcheck commands must
survive Compose's actual merge model. The optional `ops-ui` render is also
checked so Temporal UI remains explicitly profile-gated, loopback-bound, attached
only to the edge/backend networks, and dependent on a healthy Temporal service.

## Object Storage

The current Increment 2 implementation supports two object storage backends:

- `local`: writes generated audio objects below
  `DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH` and records stable
  `object://bucket/key` URIs. This is the default for non-Docker development.
- `s3`: writes generated audio objects to an S3-compatible endpoint and records
  stable `s3://bucket/key` URIs. Docker Compose uses this backend with the
  bundled MinIO service.

Production deployment readiness checks that an S3-compatible backend has both a
nonblank `DIALECTICORE_OBJECT_STORAGE_ENDPOINT` and
`DIALECTICORE_OBJECT_STORAGE_BUCKET`. If either
`DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE` or
`DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE` is set, set both; deployment
readiness reports a failed object-storage credential-pair gate when only one is
configured. The detailed object-storage health component then probes TCP
reachability, credential-reference pairing, and bucket availability with the
configured endpoint and credentials.

The Python services also keep a local probe cache under
`DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH` so stored audio can be inspected with
`ffprobe`, FFmpeg `loudnorm`, or the WAV fallback after upload. The production
API and worker Docker images install FFmpeg so the Compose path records
container metadata plus integrated loudness/true-peak normalization analysis by
default. System health probes S3-compatible deployments with both a TCP endpoint
check and a non-mutating configured-bucket `head_bucket` check, and metrics
export both endpoint and bucket availability. Local filesystem deployments
export checked-path readiness and boolean path state through
`dialecticore_object_storage_local_path_ready` and
`dialecticore_object_storage_local_path_state`, including the
`writable_target_or_parent` state shared with runtime-path readiness. In Docker Compose, the
probe-cache path is backed by the
`object-storage` volume shared by `production-api`, `workflow-worker`,
`temporal-worker`, `research-worker`, media adapters, `qc-worker`,
`timeline-worker`, `render-worker`, and `publishing-worker`. The volume contract
test keeps media-writing roles mounted to object storage while keeping
discussion and localization workers runtime-state-only.

## Runtime State

`DIALECTICORE_RUNTIME_STATE_PATH` stores lightweight operational state that is
not part of the episode aggregate. Docker Compose mounts this path from the
shared `runtime-state` volume into `production-api` and every worker role. The
current Increment 6 hardening slice uses it for JSON worker heartbeats that
power `/api/v1/system/workers`, `/api/v1/system/health` worker counts, and
`/api/v1/system/metrics`. The `workflow-worker` role uses the same runtime
state path to publish aggregate local orchestration heartbeats for the ordered
research, discussion, localization, QC, provider-sync, timeline, render, and
publishing poller pass.
Because that coordinator can run the same media-writing stages locally, Compose
also mounts `object-storage` into `workflow-worker`.
If `DIALECTICORE_WORKER_ROLE` is not one of the supported production roles, the
worker image records a failed `unsupported_worker_role.v1` heartbeat and exits
non-zero instead of running an idle placeholder loop.
Each worker service also runs `python -m app.worker_healthcheck` as its Docker
healthcheck. The helper verifies a fresh non-failed heartbeat for the configured
`DIALECTICORE_WORKER_ROLE` from the current container hostname, so Docker
health reflects the same runtime-state evidence shown by the API and Web UI.
The Compose contract test compares every worker service name and
`DIALECTICORE_WORKER_ROLE` value with the runtime worker status registry and the
worker entrypoint's supported-role list, so a new worker role must be added to
all three surfaces before the deployment contract passes.
Stale heartbeat files and expired worker lease files are retained for
`DIALECTICORE_WORKER_RUNTIME_STATE_RETENTION_SECONDS`, then pruned on worker
status reads. Worker-owned malformed heartbeat and lease JSON files are counted
in worker status and pruned after the same retention window; generic signal
registries and other diagnostic files are not pruned by that cleanup path.
The publishing worker creates dry-run publish jobs unless
`DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED=true` and the chosen enabled
publisher target has `capabilities.automated_live_publish=true`. Keep the global
flag false for staging and local Compose unless live package delivery is
intentionally desired.
When that global flag is enabled in production, deployment readiness now also
requires at least one enabled non-mock, non-dry-run publisher target with
`automated_live_publish=true`; the detailed publisher-target component still
reports target health, mock/dry-run status, and platform breakdowns.
Even when a live target is explicitly selected, non-mock non-dry-run publishing
requires a completed `production_manifest` asset linked to the selected export
package before the API opens any external delivery request.
For episodes with active `workflow_control.run` records, the coordinator also
persists pass evidence directly on the episode aggregate under
`workflow_control.worker_orchestration_log` and stage failure retry entries under
`workflow_control.stage_retry_queue`. `DIALECTICORE_WORKFLOW_STAGE_RETRY_MAX_ATTEMPTS`
and `DIALECTICORE_WORKFLOW_STAGE_RETRY_BACKOFF_SECONDS` control the local retry
queue metadata used by this journal. The workflow worker consumes scheduled
entries whose `next_retry_not_before` has elapsed, marks the selected entry
`automatic_retried`, and reopens the recorded target stage before running the
normal stage chain. If an episode is paused or cancelled, due retries remain
idle and each stage worker reports `workflow_blocked` instead of creating or
syncing talk-show production outputs.

## Temporal Bridge

The API records local workflow state for every production start and workflow
control action. `DIALECTICORE_TEMPORAL_BACKEND_MODE` declares which runtime
contract the deployment expects:

- `local`: durable API workflow state, replay journals, and local stage pollers.
- `bridge`: local control state plus best-effort outbound signal mirroring.
- `external`: native Temporal backend execution is expected.

The selected contract is surfaced as the `temporal_runtime` component in
`/api/v1/system/health`, as `dialecticore_temporal_runtime_status` in metrics,
as `dialecticore_temporal_worker_execution_status` /
`dialecticore_temporal_worker_execution_count` execution-evidence gauges, and in
the Web UI system-health panel. External mode performs a TCP readiness check
against `DIALECTICORE_TEMPORAL_BACKEND_ADDRESS` and stays degraded until a task
queue is configured, `DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED=true`, and a
non-stale `temporal-worker` heartbeat contains
`temporal_worker_execution_summary.v1` evidence.

To mirror local signals to an external Temporal bridge, set
`DIALECTICORE_TEMPORAL_BACKEND_MODE=bridge`,
`DIALECTICORE_TEMPORAL_SIGNAL_TRANSPORT_ENABLED=true`, and configure
`DIALECTICORE_TEMPORAL_SIGNAL_ENDPOINT` to a trusted internal HTTPS endpoint.
The bridge receives `temporal_signal_request.v1` payloads; the episode aggregate
keeps `workflow_control.temporal_signal_log` evidence for sent, skipped, failed,
or disabled attempts. The same aggregate keeps
`workflow_control.workflow_event_log` entries that can be replayed with
`GET /api/v1/episodes/{episode_id}/workflow/replay` to verify stored run state
against the durable event journal. The Web UI surfaces the replay status,
event-log checksum, replay issues, recent bridge attempts, and recent replay
events for the selected episode. The bridge is best-effort and does not replace
local production-start guards, audit events, replay checks, or workflow status.
The local `workflow-worker` coordinator keeps using the persisted API control
state and stage gates while recording durable local activity-attempt and retry
queue evidence. In `external` Temporal mode, those same passes also write
`temporal_stage_dispatch.v1` envelopes under
`workflow_control.temporal_stage_dispatch_log`, one per ordered production
stage. The dispatch envelope is the durable handoff contract for native workers:
it includes run ID, stage, target episode status, activity name, namespace, task
queue, readiness or missing-setting evidence, and a stable idempotency key.

The Docker worker image also exposes `DIALECTICORE_WORKER_ROLE=temporal-worker`.
Enable it with:

```bash
DIALECTICORE_TEMPORAL_BACKEND_MODE=external \
DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED=true \
docker compose --profile temporal-external up temporal-worker
```

`temporal-worker` executes the same ordered production activity contract as the
dispatch envelopes (`dialecticore.production.research`, `discussion`,
`localization`, `qc`, `voicebox`, `comfyui`, `timeline`, `render`, and
`publishing`) and records `temporal_worker_execution_summary.v1`,
`temporal_stage_activity_execution.v1`, normal orchestration attempts, retry
queue evidence, and ready/blocked dispatch entries on running episodes. The
service remains blocked until backend address, task queue, and worker-enabled
settings are present.

## Backups

`DIALECTICORE_BACKUP_PATH` stores API-created backup archives. Docker Compose
mounts this path from the shared `backups` volume at `/data/backups` in
`production-api`; worker services do not mount that archive volume. The Compose
volume contract test enforces this backup boundary alongside the runtime-state
and object-storage mounts. The `backup_storage` health component checks actual
writability of the configured backup path or its checked parent before reporting
the archive location ready. The backup/restore control plane exports database rows,
local object-storage files for filesystem deployments, authoritative S3/MinIO
bucket objects for S3-compatible deployments, and runtime-state files when
requested. Keep PostgreSQL-native backups alongside DialectiCore archives for
large production databases and point-in-time recovery.
