# Configuration

Non-secret configuration belongs in the database with versioning. Secret values
must not be stored in normal application tables or returned from normal API
responses. Increment 1 supports credential references in model endpoint records;
secret backends are added in the production hardening increment.

Increment 1 persists model endpoints and participant profiles. The Web UI uses
those records when creating an episode: producers select a host and panelists
from enabled participant profiles and set discussion controls,
language fidelity policy, research policy, media dimensions and generation
flags, workflow retry settings, and quality gates before submission. The
dashboard can create, update, enable, disable, delete, and health-check model
endpoints while preserving credential references instead of raw secrets. It can
also create, update, enable, disable, and delete participant profiles, including
model assignment, sampling settings, prompt template, perspective, expertise,
speaking style, tool policy, and optional Voicebox/visual profile bindings.
Default installations include the six frontier-model talk-show characters
`ChatGPT`, `Claude`, `DeepSeek`, `Grok`, `Gemini`, and `Mistral`, plus the
legacy deterministic mock cast used by synthetic tests. The example episode uses
ChatGPT as moderator and the other five as panelists. The frontier profiles keep
`voice_profile_id` blank until a producer assigns B1 voices, but deterministic
mock fallback profiles named `voice-{participant_id}` let local audio production
run before the remote voice appliance is configured.
Episode creation validates the selected cast against the model endpoints stored
on that episode: a participant that points at a missing or disabled model
endpoint is rejected before the discussion workflow can start. Accepted episodes
write `episode.configuration.readiness_checked` audit evidence with
`episode_configuration_readiness.v1`, including per-character model, voice,
visual, and tool-policy readiness. Missing voice or visual bindings are recorded
for operators because those can still be configured before audio or visual
production.
Increment 6 adds persisted discussion prompt templates with explicit template
ID, version, variables, creator, creation timestamp, enabled state, and change
summary. The Web UI Prompt Templates panel can create, update, enable, disable,
and delete unused templates; participant profiles select from the same managed
template IDs. Profiles can only use enabled templates with a matching participant
type, and in-use templates cannot be disabled or retyped. Generation metadata
records the active template ID/version.

Model endpoint and participant profile create, update, and delete operations
write non-secret audit events to `audit_event_records`. Audit details include
stable identifiers, display names, provider/profile types, and enabled state;
they do not include raw credentials.

Model endpoint health checks are available through the API and Web UI. They
resolve credential references only for outbound probe headers, persist
`health_status`, and merge discovered non-secret `capabilities`. OpenAI-compatible,
Anthropic-compatible, and Mistral-compatible endpoints default to `/models`,
Ollama defaults to `/api/tags`, and generic HTTP defaults to `/health`. Set
`capabilities.health_path` when a gateway exposes a different readiness route.

The OpenRouter setup action in the Web UI and
`POST /api/v1/model-endpoints/openrouter/presets/provision` create or refresh a
safe OpenAI-compatible endpoint using `env:OPENROUTER_API_KEY`. The action also
stores a curated `capabilities.model_presets` list and, by default, assigns the
six frontier-model characters to matching OpenRouter model IDs. The normal
participant editor still allows any other OpenRouter model ID through the
free-text field.

Primer narrator profiles also contain a pronunciation policy. Enabling it keeps
the approved editorial primer script unchanged and creates a separate spoken
script for Voicebox. The policy can use a narrator-specific dictionary,
deterministic acronym, number, unit, and symbol expansion, and optional
OpenAI-compatible AI suggestions. AI output is accepted only as explicit
source-to-spoken substitutions and punctuation changes; server-side validation
rejects added, removed, or reordered transformed words. A changed editorial
script or pronunciation profile invalidates the prior spoken-script approval.

The reviewed workflow is exposed through:

- `GET /api/v1/episodes/{episode_id}/primer` for current spoken-script status;
- `POST /api/v1/episodes/{episode_id}/primer/spoken-script/prepare` to build or
  refresh the derivative;
- `PUT /api/v1/episodes/{episode_id}/primer/spoken-script` to save reviewed
  substitutions and punctuation;
- `POST /api/v1/episodes/{episode_id}/primer/spoken-script/approve` to release
  the current derivative for narration timing and rendering.

When pronunciation preparation is disabled, primer narration continues to use
the canonical script and existing episodes require no migration.

Supported secret reference schemes:

- `env:NAME`: reads `NAME` from the process environment, falling back to an
  absolute file path in `NAME_FILE` when `NAME` is blank.
- `file:/absolute/path`: reads a UTF-8 secret file and strips the trailing line
  ending normally added by secret managers.
- `docker-secret:name`: reads `/run/secrets/name` and rejects path traversal.

Any other prefix, including future secret-manager names such as `vault:`, is
reported as an unsupported scheme by credential readiness until an adapter for
that scheme exists.

These schemes are used by model endpoints, Voicebox, ComfyUI, publisher targets,
auth settings, OAuth refresh settings, and S3/MinIO object-storage credentials.

Database settings:

- `DIALECTICORE_DATABASE_URL`: full SQLAlchemy database URL. When set, this
  takes precedence and keeps the existing development/simple-deployment path.
- `DIALECTICORE_DATABASE_DRIVER`: driver used when `DIALECTICORE_DATABASE_URL`
  is blank. Default: `postgresql+psycopg`.
- `DIALECTICORE_DATABASE_HOST`: database hostname used when assembling the URL.
- `DIALECTICORE_DATABASE_PORT`: database port used when assembling the URL.
- `DIALECTICORE_DATABASE_NAME`: database name used when assembling the URL.
- `DIALECTICORE_DATABASE_USER`: database username used when assembling the URL.
- `DIALECTICORE_DATABASE_PASSWORD_REFERENCE`: optional `env:`, `file:`, or
  `docker-secret:` reference used when assembling the URL without embedding the
  password in `DIALECTICORE_DATABASE_URL`.

API settings:

- `DIALECTICORE_ENV`: deployment environment label. Production readiness checks
  apply their blocking posture when this is `production`; development remains
  informational.
- `DIALECTICORE_API_HOST`: API bind host for non-container local runs. Default:
  `0.0.0.0`.
- `DIALECTICORE_API_PORT`: API bind port for non-container local runs. Default:
  `8000`.
- `DIALECTICORE_WEB_BIND_ADDRESS`: host interface used by Docker Compose for the
  Web UI published port. Default: `127.0.0.1`.
- `DIALECTICORE_API_BIND_ADDRESS`: host interface used by Docker Compose for the
  production API published port. Default: `127.0.0.1`.
- `DIALECTICORE_TEMPORAL_UI_BIND_ADDRESS`: host interface used by Docker Compose
  for the Temporal UI published port. Default: `127.0.0.1`.
- `DIALECTICORE_MINIO_BIND_ADDRESS`: host interface used by Docker Compose for
  the MinIO S3 API published port. Default: `127.0.0.1`.
- `DIALECTICORE_CORS_ALLOWED_ORIGINS`: comma-separated browser origins allowed
  by the API CORS middleware. Default: `*` for local development. Production
  deployments should set the exact Web UI or reverse-proxy origin instead of
  using wildcard CORS.

RBAC settings:

- `DIALECTICORE_AUTH_ENABLED`: enables the `/api/v1/*` RBAC middleware. Default:
  `false` for local development.
- `DIALECTICORE_AUTH_API_KEY_REFERENCE`: secret reference for the shared API key,
  for example `env:DIALECTICORE_API_KEY` or `docker-secret:dialecticore_api_key`.
  Any credential record that uses `env:NAME` resolves `NAME` first and then an
  absolute `NAME_FILE` path, so Docker-secret-backed deployments can keep saved
  provider records portable while mounting secret files.
- `DIALECTICORE_AUTH_API_KEY_HEADER`: request header carrying the API key.
  Default: `x-dialecticore-api-key`.
- `DIALECTICORE_AUTH_ROLE_HEADER`: request header carrying the role. Default:
  `x-dialecticore-role`.
- `DIALECTICORE_AUTH_USER_HEADER`: request header carrying the audit/user label.
  Default: `x-dialecticore-user`.
  When authentication is enabled, API-key, role, and user header names must be
  non-empty; `auth_runtime` reports `api_key_header_configured`,
  `role_header_configured`, and `user_header_configured` before live traffic.
- `DIALECTICORE_AUTH_TRUSTED_IDENTITY_ENABLED`: accepts a trusted
  reverse-proxy or identity-aware gateway user header as an authenticated
  session when RBAC is enabled. Default: `false`.
- `DIALECTICORE_AUTH_TRUSTED_IDENTITY_HEADER`: trusted upstream user ID header.
  Default: `x-forwarded-user`.
- `DIALECTICORE_AUTH_TRUSTED_EMAIL_HEADER`: trusted upstream email header
  reported in policy for gateway alignment. Default: `x-forwarded-email`.
- `DIALECTICORE_AUTH_TRUSTED_GROUPS_HEADER`: trusted upstream group-list header.
  Default: `x-forwarded-groups`; comma and semicolon separators are accepted.
  When trusted identity is enabled, the identity, email, and group header names
  must be non-empty; `auth_runtime` reports
  `trusted_identity_header_configured`, `trusted_email_header_configured`, and
  `trusted_groups_header_configured`.
- `DIALECTICORE_AUTH_TRUSTED_GROUP_ROLE_MAP`: comma-separated
  `external-group=role` mappings, for example
  `dialecticore-producers=producer,dialecticore-admins=admin`. The first
  matching mapped group selects the DialectiCore role.
- `DIALECTICORE_AUTH_TRUSTED_DEFAULT_ROLE`: role assigned to trusted identities
  without a mapped group. Default: `viewer`.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_ENABLED`: enables provider-managed bearer
  session validation by token introspection. Default: `false`.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_INTROSPECTION_URL`: HTTPS endpoint used to
  validate bearer tokens and fetch session claims. `auth_runtime` reports
  `provider_session_introspection_url_secure=false` when provider sessions are
  enabled with a non-HTTPS introspection URL.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_CLIENT_ID_REFERENCE`: optional secret
  reference for the introspection client ID.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_CLIENT_SECRET_REFERENCE`: optional secret
  reference for the introspection client secret. Client ID and secret references
  must both resolve when either one is configured; `auth_runtime` reports
  `provider_session_client_credentials_ready=false` before live traffic if the
  pair is mismatched or unavailable.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_TOKEN_HEADER`: request header carrying the
  bearer token. Default: `authorization`.
  When provider sessions are enabled, this header name must be non-empty;
  `auth_runtime` reports `provider_session_token_header_configured`.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_USER_CLAIM`: introspection payload claim
  used as the stable user ID. Default: `sub`.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_GROUPS_CLAIM`: introspection payload claim
  carrying groups. Default: `groups`; list, comma, and semicolon formats are
  accepted.
  When provider sessions are enabled, both claim names must be non-empty;
  `auth_runtime` reports `provider_session_user_claim_configured` and
  `provider_session_groups_claim_configured`.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_GROUP_ROLE_MAP`: comma-separated
  `external-group=role` mappings for provider sessions.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_DEFAULT_ROLE`: role assigned to active
  provider sessions without a mapped group. Default: `viewer`.
  Production readiness requires at least one viable auth mode and one
  admin-capable bootstrap path across those viable modes: an API-key reference
  with non-empty API-key/role/user headers, a trusted identity mode with
  non-empty trusted headers and an admin default/group mapping, or a provider
  session mode with HTTPS introspection, non-empty token/claim settings,
  ready optional client credentials, and an admin default/group mapping.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_TIMEOUT_SECONDS`: introspection request
  timeout. Default: `3`.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_REVOCATION_PATH`: optional path for the
  file-backed provider-session revocation registry. When omitted, the registry
  is stored below `DIALECTICORE_RUNTIME_STATE_PATH/auth`. The Web UI Security
  panel can list active records and add new records by token hash, JTI, or
  subject without storing raw bearer tokens.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_DECISION_LOG_PATH`: optional path for the
  bounded provider-session decision log. When omitted, accepted, denied, and
  error decisions are stored below `DIALECTICORE_RUNTIME_STATE_PATH/auth` with
  token SHA-256 hashes, safe session claims, mapped role, permission, request
  path, status, and reason.
- `DIALECTICORE_AUTH_PROVIDER_SESSION_DECISION_LOG_LIMIT`: maximum retained
  decision records. Default: `100`.

Production deployments must replace local Compose sentinel credentials before
setting `DIALECTICORE_ENV=production`. Deployment readiness fails when active
settings still expose the default API key, MinIO root password, Postgres
password, or default Postgres password embedded in `DIALECTICORE_DATABASE_URL`.
The health and live-readiness payloads report only setting labels, not secret
values.

Increment 2 adds persisted Voicebox endpoints and voice profiles. Voicebox
endpoints use credential references, not raw secrets, and carry health status
plus discovered capabilities. The dashboard can create, update, enable,
disable, delete, and health-check Voicebox endpoints, including timeout,
concurrency, retry policy, capabilities, base URL, and credential reference
fields. Voice profiles reference a Voicebox endpoint and can be assigned to
participant profiles through `voice_profile_id`; the dashboard can create,
update, enable, disable, and delete those profiles, including language, speaker
label, model ID, prosody, rate, pitch, and pronunciation dictionary fields.
Invalid voice assignments are rejected by the API.

Increment 3 adds persisted ComfyUI endpoints, workflow records, and visual
profiles. ComfyUI endpoints use credential references, not raw secrets, and
carry health status plus discovered capabilities. The dashboard can create,
update, enable, disable, delete, and health-check ComfyUI endpoints, including
timeout, concurrency, retry policy, capabilities, base URL, and credential
reference fields. Workflows reference ComfyUI endpoints, and the dashboard can
create, update, enable, disable, and delete workflow records with version,
workflow type, output asset type, API workflow JSON, prompt template JSON, and
default parameter JSON. Visual profiles reference workflows; the dashboard can
create, update, enable, disable, and delete visual profiles with character
names, primary/reaction/B-roll workflow assignments, reference image URI,
style/negative prompts, seed, and wardrobe JSON. Participant profiles can be
assigned through `visual_profile_id`; invalid references are rejected by the
API.

Research discovery settings:

- `DIALECTICORE_RESEARCH_RETRIEVAL_TIMEOUT_SECONDS`: HTTP timeout for
  producer-supplied URL retrieval and discovered-source fetches. Default: `8`.
- `DIALECTICORE_RESEARCH_RETRIEVAL_MAX_BYTES`: maximum response bytes retained
  from each retrieved source before deterministic extraction. Default:
  `1000000`.
- `DIALECTICORE_RESEARCH_DISCOVERY_ENABLED`: enables live discovery requests
  during `research/build` when the request sets `discover_sources=true`.
  Default: `false`.
- `DIALECTICORE_RESEARCH_DISCOVERY_URL_TEMPLATE`: HTTP(S) endpoint template for
  a trusted search/discovery gateway. Include `{query}` where the URL-encoded
  query should be inserted; when omitted, `?q=` or `&q=` is appended.
- `DIALECTICORE_RESEARCH_DISCOVERY_MAX_QUERIES`: maximum discovery queries per
  evidence-pack build. Default: `4`.
- `DIALECTICORE_RESEARCH_DISCOVERY_MAX_RESULTS_PER_QUERY`: maximum result URLs
  selected from each discovery response. Default: `5`.

Discovery responses may be JSON with `results`, `items`, `organic_results`, or
`webPages.value`, or simple HTML links. Discovered URLs are fetched through the
normal retrieval path, and discovery query/rank provenance is retained in
evidence metadata and QC.

Advanced research extraction settings:

- `DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_ENABLED`: enables a trusted
  external advanced-claim extraction gateway during `research/build`. Default:
  `false`.
- `DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_URL`: HTTP(S) endpoint that accepts
  `research_advanced_extraction_request.v1` JSON and returns source-bound
  `claims`.
- `DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_TIMEOUT_SECONDS`: request timeout.
  Default: `15`.
- `DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_MAX_SOURCES`: maximum sources sent
  to the extractor per evidence-pack build. Default: `8`.
- `DIALECTICORE_RESEARCH_ADVANCED_EXTRACTION_MAX_CLAIMS_PER_SOURCE`: maximum
  accepted source-bound claims per source. Default: `6`.

Returned advanced claims must cite the current source ID through
`evidence_refs` or `source_id`; unbound or unknown-source claims are rejected,
counted, and retained only as tool-log/QC evidence.

Publisher targets are persisted configuration records for delivery destinations
such as YouTube. They store platform, adapter type, channel ID, privacy status,
capabilities, retry policy, enabled state, health status, and credential
references only. The dashboard can create, update, enable, disable, delete, and
health-check publisher targets, including base URL, credential reference,
default language, default tags, retry policy, and capability fields. The default
`mock-youtube` target is a dry-run target for local verification and does not
upload to a live account. Targets using `http`,
`http_upload`, or `generic_http` with a `base_url` can deliver packages to a
configured HTTPS service; set `capabilities.delivery_path` and
`capabilities.health_path` to override the default `/publish` and `/health`
paths. Credential references such as `env:PUBLISHER_TOKEN` are resolved at
delivery time and sent as bearer tokens, without storing raw secrets. File-backed
and Docker-secret references are also supported.
The seeded disabled `youtube-resumable` target uses adapter type
`youtube_resumable`, platform `youtube`, and credential reference
`env:YOUTUBE_OAUTH_ACCESS_TOKEN`. When enabled, it uses that access-token
reference when available; otherwise it can exchange
`capabilities.oauth_refresh_token_reference`,
`capabilities.oauth_client_id_reference`, and
`capabilities.oauth_client_secret_reference` for a fresh access token through
`capabilities.oauth_token_url` without storing raw OAuth secrets. It then uses
the YouTube Data API resumable-upload flow for the package's `video/render.*`
entry and uploads the package thumbnail and caption files through
YouTube-native endpoints. Set
`capabilities.upload_path`, `capabilities.thumbnail_upload_path`,
`capabilities.caption_upload_path`, `capabilities.health_path`,
`capabilities.api_base_url`, `capabilities.upload_base_url`, or
`capabilities.oauth_token_url` to override the default Google API endpoints for
tests or gateway deployments. Publish-job result metadata records only safe
endpoint evidence such as scheme, request path, and query-key names, plus
credential-reference scheme/configuration posture; raw OAuth token URLs,
delivery hosts, resumable session URIs, and credential-reference targets are not
stored in job metadata. Set `capabilities.thumbnail_upload=false` or
`capabilities.caption_upload=false` to skip those native uploads for a target.

Worker loop settings:

- `DIALECTICORE_REDIS_URL`: Redis endpoint used for optional Web UI event
  fan-out and distributed worker signal delivery. Docker Compose sets this to
  `redis://redis:6379/0`.
- `DIALECTICORE_REDIS_EVENT_FANOUT_ENABLED`: when `true`,
  `/api/v1/system/events` publishes each `system_status_event.v1` snapshot to
  Redis Pub/Sub as well as returning it on the local SSE response. Default:
  `false`.
- `DIALECTICORE_REDIS_EVENT_CHANNEL`: Redis Pub/Sub channel for system events.
  Must be non-blank when Redis event fan-out is enabled. Default:
  `dialecticore:system-events`.
- `DIALECTICORE_REDIS_WORKER_SIGNAL_ENABLED`: when `true`, worker control
  signals posted through the API are delivered to a Redis stream and retained in
  the runtime-state registry. Default: `false`.
- `DIALECTICORE_REDIS_WORKER_SIGNAL_STREAM`: Redis stream for worker control
  signals. Must be non-blank when Redis worker signals are enabled. Default:
  `dialecticore:worker-signals`.
- `DIALECTICORE_REDIS_WORKER_SIGNAL_MAXLEN`: approximate maximum number of
  worker signal records retained in the Redis stream and local runtime-state
  signal registry. The API passes this as Redis `XADD MAXLEN ~` and also uses
  it to bound recent signal listings, summaries, and worker gate lookups.
  Default: `200`.
- `DIALECTICORE_REDIS_TIMEOUT_SECONDS`: Redis TCP/client timeout for health
  checks, event publishing, and worker signal delivery. Default: `1`.
- `DIALECTICORE_DOCKER_LOG_MAX_SIZE`: Docker Compose JSON log rotation size
  limit applied to every bundled service. Default: `10m`.
- `DIALECTICORE_DOCKER_LOG_MAX_FILE`: Docker Compose JSON log rotation file
  count applied to every bundled service. Default: `5`.
- `DIALECTICORE_DOCKER_STOP_GRACE_PERIOD`: Docker Compose graceful shutdown
  window applied to every bundled service. Default: `60s`.
- `DIALECTICORE_DOCKER_CPU_LIMIT`: Docker Compose CPU quota applied to every
  bundled service. Default: `2.0`.
- `DIALECTICORE_DOCKER_MEMORY_LIMIT`: Docker Compose memory limit applied to
  every bundled service. Default: `2g`.
- `DIALECTICORE_DOCKER_MEMORY_SWAP_LIMIT`: Docker Compose memory-plus-swap
  limit applied to every bundled service. Default: `2g`.
- `DIALECTICORE_DOCKER_PIDS_LIMIT`: Docker Compose process-count limit applied
  to every bundled service. Default: `512`.
- `DIALECTICORE_DOCKER_NOFILE_SOFT_LIMIT`: Docker Compose soft open-file limit
  applied to every bundled service. Default: `65536`.
- `DIALECTICORE_DOCKER_NOFILE_HARD_LIMIT`: Docker Compose hard open-file limit
  applied to every bundled service. Default: `65536`.
- `DIALECTICORE_RUNTIME_STATE_PATH`: shared local path for lightweight runtime
  files such as worker heartbeats. Docker Compose sets this to
  `/data/runtime-state`.
- `DIALECTICORE_BACKUP_PATH`: local path where API-created backup archives are
  written and listed. Docker Compose sets this to `/data/backups`.
- `DIALECTICORE_WORKER_POLL_INTERVAL_SECONDS`: seconds between adapter worker
  scans. Default: `15`.
- `DIALECTICORE_WORKER_SYNC_BATCH_LIMIT`: maximum episode aggregates scanned by
  a worker pass. Default: `50`.
- `DIALECTICORE_WORKER_HEARTBEAT_TTL_SECONDS`: seconds before a heartbeat is
  treated as stale in worker status and metrics. Default: `90`. Production
  readiness expects this value to be greater than
  `DIALECTICORE_WORKER_POLL_INTERVAL_SECONDS` so Docker worker healthchecks do
  not mark an idle worker stale between polling passes.
- `DIALECTICORE_WORKER_LEASE_TTL_SECONDS`: seconds a per-role worker lease
  remains active without renewal. Adapter workers renew leases around each
  polling pass so scaled containers do not duplicate the same remote-job sync
  work. Default: `45`. Production readiness expects this value to be greater
  than `DIALECTICORE_WORKER_POLL_INTERVAL_SECONDS` so the current worker keeps
  ownership through its sleep interval before the next poll.
- `DIALECTICORE_WORKER_RUNTIME_STATE_RETENTION_SECONDS`: seconds to retain
  stale worker heartbeat records and expired worker leases after their last
  operational timestamp. The effective retention is never shorter than the
  corresponding heartbeat or lease TTL. Default: `86400`.
- `DIALECTICORE_WORKER_AUTO_START_PRODUCTION_RUNS_ENABLED`: when `true`, the
  workflow and discussion workers may create production-run journals for
  eligible episodes they discover while polling. Default: `false`; normal
  operation expects an operator or API client to start production explicitly via
  `POST /api/v1/episodes/{episode_id}/produce`.
- `DIALECTICORE_B1_MANAGED_MEDIA_SMOKE_EVIDENCE_PATH`: local JSON evidence file
  written by `scripts/b1_managed_media_smoke.py`. Production-test reports read
  this file when present and expose a sanitized
  `media_readiness.managed_media_smoke` summary so operators can distinguish a
  healthy B1 model catalog from a failing GPU runner. Default:
  `output/smoke/b1-managed-media-smoke-image-default-latest.json`.
- `DIALECTICORE_RUNTIME_PATH_MIN_FREE_BYTES`: optional minimum free-space floor
  for required runtime paths in health and live-readiness checks. A value of `0`
  disables the capacity gate. Default: `0`.
- `DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES`: comma-separated `/system/health`
  JSON `status` values that the API container healthcheck accepts after the
  endpoint returns HTTP 200. Default: `healthy,degraded`; set to `healthy` when
  an orchestrator should restart or hold back the API during degraded readiness.
- `DIALECTICORE_TEMPORAL_BACKEND_MODE`: workflow runtime contract surfaced in
  `/api/v1/system/health`, `/api/v1/system/metrics`, and the Web UI. Supported
  values are `local`, `bridge`, and `external`. Default: `local`.
- `DIALECTICORE_TEMPORAL_BACKEND_ADDRESS`: native Temporal frontend address used
  for transport-level readiness checks when backend mode is `external`.
  Docker Compose defaults this to `temporal:7233`.
- `DIALECTICORE_TEMPORAL_BACKEND_TLS_ENABLED`: metadata flag for native Temporal
  TLS expectations. Default: `false`.
- `DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED`: set to `true` only when a
  native Temporal worker execution path is deployed for production stages. In
  Docker Compose, start the `temporal-worker` service with the `temporal-external`
  profile when this is enabled. External backend mode is reported as degraded
  while this remains `false`, and health also reports whether a non-stale
  `temporal-worker` heartbeat is active.
- `DIALECTICORE_TEMPORAL_BACKEND_CONNECT_TIMEOUT_SECONDS`: TCP readiness timeout
  for the native Temporal frontend check. Default: `1`.
- `DIALECTICORE_TEMPORAL_SIGNAL_TRANSPORT_ENABLED`: enables best-effort outbound
  workflow signal delivery to an external Temporal bridge. Default: `false`;
  local workflow state and audit evidence are still written when disabled.
- `DIALECTICORE_TEMPORAL_SIGNAL_ENDPOINT`: HTTPS endpoint that accepts
  `temporal_signal_request.v1` JSON for workflow `start`, `pause`, `resume`,
  `cancel`, and `retry_failed_stage` signals.
- `DIALECTICORE_TEMPORAL_SIGNAL_TIMEOUT_SECONDS`: request timeout for the
  external signal bridge. Default: `3`.
- `DIALECTICORE_TEMPORAL_NAMESPACE`: namespace label included in outbound signal
  payloads and persisted signal-log evidence. Default: `default`.
- `DIALECTICORE_TEMPORAL_TASK_QUEUE`: optional task queue label included in
  outbound signal payloads and persisted signal-log evidence.
- `DIALECTICORE_WORKFLOW_STAGE_RETRY_MAX_ATTEMPTS`: maximum recorded attempts for
  failed stage retry queue entries before an item is marked exhausted. Default:
  `3`.
- `DIALECTICORE_WORKFLOW_STAGE_RETRY_BACKOFF_SECONDS`: backoff delay recorded
  for scheduled failed-stage retry entries. Default: `60`.
- `DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED`: allows the publishing worker
  to submit non-dry-run publish jobs. Default: `false`. A publisher target must
  also be enabled and set `capabilities.automated_live_publish=true`; otherwise
  the worker continues to create dry-run publish jobs only.
- `DIALECTICORE_WORDS_PER_SECOND`: deterministic timing fallback used when
  estimating discussion turn duration from generated text. Default: `2.45`.

Workflow starts, stage changes, and workflow actions always write a local
`workflow_event.v1` journal under `workflow_control.workflow_event_log`; this is
independent of external signal transport and powers the workflow replay report.
The `temporal_runtime` health component reports the selected mode, namespace,
task queue, bridge settings, native backend address configuration, and native
TCP reachability where applicable. Local mode is considered healthy when the
local workflow control/replay path is available; bridge mode requires signal
transport and an endpoint; external mode requires a backend address, task queue,
transport reachability, and native worker readiness. In production,
`deployment_readiness` also reports a
`temporal_runtime_contract_configured` check so incomplete `bridge` or
`external` settings are visible as deployment posture issues before a live run.

Voicebox endpoints can set `capabilities.job_status_path_template` to customize
remote async result polling. The default path is `/tts/jobs/{job_id}`.

ComfyUI endpoints can set `capabilities.job_status_path_template` or
`capabilities.history_path_template` to customize remote visual result polling.
The default path is `/history/{job_id}`. They can also set
`capabilities.job_cancel_path_template` or
`capabilities.cancellation_path_template`, plus optional
`job_cancel_method`/`cancellation_method`, to customize remote visual
cancellation. The default cancel call is `DELETE /queue/{job_id}`.

ComfyUI workflow records can set `prompt_template.node_input_bindings` or
`default_parameters.node_input_bindings` to patch workflow API JSON before
submission. Bindings map workflow paths to logical values, for example
`6.inputs.text: positive_prompt`, `7.inputs.text: negative_prompt`,
`8.inputs.width: width`, and `9.inputs.seed: seed`. When explicit bindings are
not configured, the adapter patches common node input names conservatively. The
seeded ComfyUI workflows include concrete API workflow templates and explicit
bindings for talking-head video, reaction loops, topic B-roll images, and
studio-wide scenes, including sampler settings, computed frame counts, and
motion/camera/lighting or B-roll composition metadata; operators can replace
the node graph while keeping the same binding contract.

Object storage settings:

- `DIALECTICORE_OBJECT_STORAGE_BACKEND`: `local`, `filesystem`, `s3`, or
  `minio`. Default: `local`.
- `DIALECTICORE_OBJECT_STORAGE_ENDPOINT`: S3-compatible endpoint URL. Docker
  Compose sets this to `http://minio:9000`.
- `DIALECTICORE_OBJECT_STORAGE_BUCKET`: target object bucket. Default:
  `dialecticore`.
- `DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH`: local object root for the local
  backend and local probe-cache root for the S3 backend.
- `DIALECTICORE_OBJECT_STORAGE_REGION`: S3 client region. Default:
  `us-east-1`.
- `DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE` and
  `DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE`: optional credential
  references resolved with the same `env:`, `file:`, and `docker-secret:`
  schemes used by model and Voicebox endpoints.
- `DIALECTICORE_OBJECT_STORAGE_FORCE_PATH_STYLE`: enables path-style S3
  addressing for MinIO-compatible deployments. Default: `true`.
- `DIALECTICORE_OBJECT_STORAGE_AUTO_CREATE_BUCKET`: creates the configured S3
  bucket on first write when missing. Default: `true`.

Audio loudness settings:

- `DIALECTICORE_AUDIO_LOUDNESS_TARGET_LUFS`: integrated loudness target used
  for FFmpeg loudness-normalization analysis. Default: `-16.0`.
- `DIALECTICORE_AUDIO_LOUDNESS_TRUE_PEAK_LIMIT_DBTP`: true-peak ceiling used by
  the loudness analysis. Default: `-1.5`.
- `DIALECTICORE_AUDIO_LOUDNESS_RANGE_TARGET_LU`: loudness-range target used by
  the loudness analysis. Default: `11.0`.
