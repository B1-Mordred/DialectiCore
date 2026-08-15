# API

The public API is versioned under `/api/v1`.

Implemented in the current scaffold:

- `GET /api/v1/system/health`
- `GET /api/v1/system/live-provider-readiness`
- `POST /api/v1/system/b1-managed-media-smoke`
- `GET /api/v1/system/credential-provisioning`
- `GET /api/v1/system/workflow-orchestration`
- `GET /api/v1/system/workflow-retries`
- `GET /api/v1/system/auth-policy`
- `GET /api/v1/system/workers`
- `POST /api/v1/system/workers/heartbeat`
- `GET /api/v1/system/metrics`
- `GET /api/v1/system/events`
- `GET /api/v1/system/backups`
- `POST /api/v1/system/backups`
- `POST /api/v1/system/backups/restore`
- `GET /api/v1/projects`
- `POST /api/v1/projects`
- `GET /api/v1/projects/{project_id}`
- `PUT /api/v1/projects/{project_id}`
- `DELETE /api/v1/projects/{project_id}`
- `GET /api/v1/language-profiles`
- `POST /api/v1/language-profiles`
- `GET /api/v1/language-profiles/{profile_id}`
- `PUT /api/v1/language-profiles/{profile_id}`
- `DELETE /api/v1/language-profiles/{profile_id}`
- `POST /api/v1/episodes`
- `GET /api/v1/episodes`
- `GET /api/v1/episodes/summaries`
- `GET /api/v1/episodes/{episode_id}`
- `POST /api/v1/episodes/{episode_id}/produce`
- `POST /api/v1/episodes/{episode_id}/workflow/advance`
- `GET /api/v1/episodes/{episode_id}/status`
- `GET /api/v1/episodes/{episode_id}/pilot-readiness`
- `GET /api/v1/episodes/{episode_id}/discussion`
- `GET /api/v1/episodes/{episode_id}/transcripts`
- `POST /api/v1/episodes/{episode_id}/localize`
- `GET /api/v1/episodes/{episode_id}/assets`
- `POST /api/v1/episodes/{episode_id}/audio-assets/plan`
- `POST /api/v1/episodes/{episode_id}/audio-assets/generate`
- `POST /api/v1/episodes/{episode_id}/speech/produce`
- `POST /api/v1/episodes/{episode_id}/audio-assets/qc`
- `POST /api/v1/episodes/{episode_id}/audio-assets/sync`
- `POST /api/v1/episodes/{episode_id}/audio-assets/cancel`
- `POST /api/v1/episodes/{episode_id}/visual-assets/plan`
- `POST /api/v1/episodes/{episode_id}/visual-assets/generate`
- `POST /api/v1/episodes/{episode_id}/visuals/produce`
- `POST /api/v1/episodes/{episode_id}/visual-assets/sync`
- `POST /api/v1/episodes/{episode_id}/visual-assets/cancel`
- `POST /api/v1/episodes/{episode_id}/subtitles/generate`
- `GET /api/v1/episodes/{episode_id}/publish-jobs`
- `POST /api/v1/episodes/{episode_id}/publish`
- `POST /api/v1/episodes/{episode_id}/discussion/turns/{turn_id}/regenerate`
- `POST /api/v1/episodes/{episode_id}/discussion/turns/{turn_id}/exclude`
- `POST /api/v1/episodes/{episode_id}/research/source-review`
- `GET /api/v1/episodes/{episode_id}/research/sources`
- `GET /api/v1/episodes/{episode_id}/research/claims`
- `GET /api/v1/episodes/{episode_id}/quality`
- `GET /api/v1/episodes/{episode_id}/approvals`
- `POST /api/v1/episodes/{episode_id}/approvals/{approval_id}/decision`
- `GET /api/v1/episodes/{episode_id}/audit`
- `GET /api/v1/audit-events`
- `GET /api/v1/model-endpoints`
- `POST /api/v1/model-endpoints`
- `GET /api/v1/model-endpoints/{endpoint_id}`
- `PUT /api/v1/model-endpoints/{endpoint_id}`
- `POST /api/v1/model-endpoints/{endpoint_id}/health`
- `DELETE /api/v1/model-endpoints/{endpoint_id}`
- `GET /api/v1/voicebox-endpoints`
- `POST /api/v1/voicebox-endpoints`
- `GET /api/v1/voicebox-endpoints/{endpoint_id}`
- `PUT /api/v1/voicebox-endpoints/{endpoint_id}`
- `POST /api/v1/voicebox-endpoints/{endpoint_id}/health`
- `POST /api/v1/voicebox-endpoints/{endpoint_id}/ca-certificate/bootstrap`
- `POST /api/v1/voicebox-endpoints/{endpoint_id}/b1-german-voice-presets/provision`
- `DELETE /api/v1/voicebox-endpoints/{endpoint_id}`
- `GET /api/v1/voice-profiles`
- `POST /api/v1/voice-profiles`
- `GET /api/v1/voice-profiles/{profile_id}`
- `PUT /api/v1/voice-profiles/{profile_id}`
- `DELETE /api/v1/voice-profiles/{profile_id}`
- `GET /api/v1/comfyui-endpoints`
- `POST /api/v1/comfyui-endpoints`
- `GET /api/v1/comfyui-endpoints/{endpoint_id}`
- `PUT /api/v1/comfyui-endpoints/{endpoint_id}`
- `POST /api/v1/comfyui-endpoints/{endpoint_id}/health`
- `POST /api/v1/comfyui-endpoints/{endpoint_id}/ca-certificate/bootstrap`
- `DELETE /api/v1/comfyui-endpoints/{endpoint_id}`
- `GET /api/v1/comfyui-workflows`
- `POST /api/v1/comfyui-workflows`
- `GET /api/v1/comfyui-workflows/{workflow_id}`
- `PUT /api/v1/comfyui-workflows/{workflow_id}`
- `DELETE /api/v1/comfyui-workflows/{workflow_id}`
- `POST /api/v1/episodes/{episode_id}/visual-assets/qc`
- `GET /api/v1/visual-profiles`
- `POST /api/v1/visual-profiles`
- `GET /api/v1/visual-profiles/{profile_id}`
- `PUT /api/v1/visual-profiles/{profile_id}`
- `POST /api/v1/visual-profiles/{profile_id}/reference-image`
- `GET /api/v1/visual-profiles/{profile_id}/reference-images/{reference_type}/download`
- `DELETE /api/v1/visual-profiles/{profile_id}/reference-images/{reference_type}`
- `DELETE /api/v1/visual-profiles/{profile_id}`
- `POST /api/v1/show-media/scene-reference-image`
- `GET /api/v1/show-media/scene-reference-image/download`
- `GET /api/v1/publisher-targets`
- `POST /api/v1/publisher-targets`
- `GET /api/v1/publisher-targets/{target_id}`
- `PUT /api/v1/publisher-targets/{target_id}`
- `POST /api/v1/publisher-targets/{target_id}/health`
- `DELETE /api/v1/publisher-targets/{target_id}`

`GET /api/v1/system/health` returns a structured production health summary. It
checks repository/database reachability, local object-storage path readiness or
S3/MinIO endpoint and configured bucket reachability, credential-reference
coherence, FFmpeg/ffprobe availability, enabled model endpoints, Voicebox
endpoints, ComfyUI endpoints, publisher targets, and the configured Temporal
runtime mode.
The `temporal_runtime` component reports local, bridge, or external mode,
namespace/task queue, bridge configuration, native backend address readiness,
and native worker readiness where applicable. The response includes top-level
`status`, component entries, episode/configuration counts, pending audio/visual
queue counts, failed asset counts, completed render counts, and a safe settings
summary with drivers, limits, auth header names, provider-session claim names,
default role labels, and credential-reference presence booleans rather than
secret values or reference contents. Worker control signals are summarized in
the `worker_signals` object and corresponding
`recent_worker_signals`, `blocking_worker_signals`, and `failed_worker_signals`
counts.

`GET /api/v1/system/live-provider-readiness` returns
`live_provider_readiness.v1`, an operator-safe preflight report for live
production. It aggregates production posture, credential-reference readiness,
runtime path readiness, enabled model providers, remote Voicebox and ComfyUI
endpoints, object storage, backup storage, Redis, auth runtime, Temporal
runtime, worker registry, worker signal state, workflow orchestration, workflow
retry backlog, production-run state, media queues, publish jobs, and publisher
targets into pass, warning, and fail checks. Missing or unvalidated backups, missing worker
heartbeat coverage, scheduled workflow retries, pending media jobs, and
submitted publish jobs, paused active production runs, and already-running
production runs are surfaced as warnings, while hard runtime
dependencies, orchestration errors, blocked Temporal dispatches, exhausted
workflow retries, and failed media assets fail the preflight. Media queue
`pending_*_jobs` counts include submitted/running work; submitted and running
fields are exposed as sub-breakdowns rather than additional work to sum.
workflow retries, failed media assets, failed publish jobs, failed worker signal
delivery, active blocking worker signals, failed or cancelled active production
runs, and automated live publishing enabled without an enabled
`automated_live_publish` target remain blockers. The response lists blockers and
warnings by category without
returning raw credential values.

`POST /api/v1/system/b1-managed-media-smoke` submits a tiny B1 managed-media job
through `https://api.ai.b1.germering/v1/media/jobs` by default, polls
`/v1/media/jobs/{job_id}`, writes `b1_managed_media_smoke_evidence.v1`, records a
global `b1_managed_media.smoke_checked` audit event, and returns the evidence
summary plus an `exit_code`. The request supports `api_base`, `model`, prompt,
image size, sampler settings, poll bounds, `evidence_output`,
`requirements_output`, and `allow_runner_failure`. When the result does not pass
and `requirements_output` is set, the API appends a Codex-readable B1 appliance
fix note, normally `/home/mordred/media-requirements.md`, with model alias,
terminal state, failure category/message, native ComfyUI prompt ID, artifact
count, and acceptance criteria.
Production test reports include `provider_repair_handoff`
(`provider_repair_handoff.v1`), a sanitized summary of that requirements file:
configured path, existence/status, file size, modified time, section count,
latest section headings, and whether Voicebox and B1 managed-media requirement
sections are present. The report does not inline the markdown body or raw
provider traces. The full live episode smoke can append the same handoff file
when a production attempt records failed B1 speech generation, failed B1
managed-media execution/smoke, missing B1 media presets, or an unhealthy native
ComfyUI gateway, so the production report can point operators to the remote
B1-side repair task without exposing raw provider payloads.
They also include `publish_evidence_binding`
(`publish_evidence_binding.v1`), which compares the latest publish job against
the selected export package and current production manifest. It reports package
ID/checksum binding, whether the current manifest embeds the latest publish job
and status, whether the original publish payload manifest still equals the
current manifest, dry-run/live mode, and publish URL. A completed dry-run whose
current manifest was refreshed after publish is reported as warning evidence,
not as an acceptance blocker, when package and current-manifest bindings are
otherwise coherent.

`GET /api/v1/episodes/{episode_id}/pilot-readiness` returns
`episode_pilot_readiness.v1`, an episode-scoped preflight for the first real
talk-show pilot. It separates readiness into `discussion`, `speech`, `visuals`,
and `rendering` stages. Discussion requires one moderator, at least three
active selected participants, concrete model assignments, existing enabled
non-mock model endpoints, and non-unhealthy model endpoint health. Speech
requires every selected participant to have an enabled voice profile backed by
an existing enabled non-mock Voicebox endpoint. Visuals require enabled visual
profiles, portrait and full-body references, enabled primary ComfyUI workflows,
and usable ComfyUI endpoints; native ComfyUI endpoints with a failed `/prompt`
admission probe add `selected_native_comfyui_prompt_admission_ready=false`,
`prompt_admission_blocked_endpoints`, and a native-visual blocker with safe
endpoint/participant/workflow/profile IDs plus the redacted admission code and
policy detail. Mock ComfyUI use is a warning so an audio-first pilot can proceed
before final animation is live. Rendering checks `ffmpeg` and `ffprobe`
availability for preview/final video and QC. Each stage includes
`readiness_checks`, `failed_readiness_checks`, bounded participant ID lists for
missing assignments or references, and safe blockers/warnings without returning
secrets. The top-level `status`, `blockers`, and `warnings` describe the
episode's declared `production_target`; `pilot_modes`, `stages`,
`all_stage_blockers`, and `all_stage_warnings` retain diagnostics for modes that
are not currently selected. Passing `refresh_comfyui_health=true` refreshes
enabled non-mock or native ComfyUI endpoints with the same health checker used
by `POST /api/v1/comfyui-endpoints/{endpoint_id}/health` before computing
readiness, and adds `comfyui_health_refresh`
(`pilot_comfyui_health_refresh.v1`) with compact endpoint status and prompt
admission evidence.
are not currently selected.

`GET /api/v1/system/credential-provisioning` returns
`credential_provisioning_plan.v1`, a safe checklist of configured
credential-reference labels. By default `include_disabled=true` includes
disabled live targets such as the seeded YouTube resumable target so operators
can provision secrets before enabling them. The response groups required
environment variables, Docker secret names, and absolute secret-file paths,
reports resolved/unavailable status for each reference, and includes example
Compose environment and Docker secret labels without returning secret values.
When `DIALECTICORE_DATABASE_URL` is blank, the active
`DIALECTICORE_DATABASE_PASSWORD_REFERENCE` is included in this checklist and in
credential readiness so missing database secrets are visible without exposing
the password.

`GET /api/v1/system/auth-policy` returns the configured RBAC policy without
secret values. It reports whether auth is enabled, which request headers are
used for API key, role, and user identity, whether an API-key reference is
configured, configured authentication modes, trusted identity header names,
provider-session introspection configuration, group-map presence, and the
role-to-permission matrix. The `permissions` list is the route permission
vocabulary, including admin-only actions such as `configuration_write` and
`backup_restore` even when those are granted through the admin wildcard role.

`GET /api/v1/system/auth/provider-session/revocations` lists active
provider-session revocation records. `POST
/api/v1/system/auth/provider-session/revocations` records a revocation by
`token_sha256`, `jti`, or `subject`, plus optional reason, expiry, and operator
ID. The Web UI Security panel uses these endpoints to record and inspect active
revocations. The endpoint is protected as configuration-write/admin access; raw
bearer tokens are not accepted or stored.

`GET /api/v1/system/workers` returns the file-backed worker heartbeat registry,
including worker role, worker ID, status, last heartbeat age, stale flag, active
per-role worker leases, lease expiry, and role/count summaries. `POST
/api/v1/system/workers/heartbeat` records a heartbeat for worker supervisors or
external probes; the built-in Docker worker entrypoint writes the same heartbeat
records directly. Unsupported `DIALECTICORE_WORKER_ROLE` values record a failed
`unsupported_worker_role.v1` heartbeat with the supported-role list before the
worker exits non-zero. The worker summary reports `degraded` when active
heartbeats do not cover every configured worker role, even if the currently
running workers are fresh and idle. It also includes retained heartbeat and pruned
stale heartbeat counts; stale heartbeat files and expired lease files are
retained until `DIALECTICORE_WORKER_RUNTIME_STATE_RETENTION_SECONDS`, then
pruned by worker status reads. It also reports malformed worker-owned heartbeat
and lease JSON counts plus malformed files pruned after the same retention
window. `GET /api/v1/system/metrics` returns
Prometheus-style text metrics for system health, component health, episode
counts, media queue counts, publish job outcomes, backup archive status, remote
object-storage endpoint reachability, auth runtime/session-log readiness,
Temporal runtime mode/status, deployment readiness posture, workflow
orchestration/dispatch evidence, workflow stage retry backlog, production-run
state, Redis runtime reachability, worker counts, worker heartbeat ages, and
worker lease expiry.
Every boolean `readiness_checks` entry emitted by a system-health component is
also exported as
`dialecticore_component_readiness_check{component="...",check="...",status="pass|fail"} 1`,
so clients can alert on normalized component gates without parsing JSON or
waiting for a component-specific metric.
Production deployment readiness is exported as
`dialecticore_deployment_readiness_status`,
`dialecticore_deployment_readiness_issues`, and
`dialecticore_deployment_readiness_check` for production-safe database, CORS,
auth, remote object storage, Redis runtime, backup path, and runtime-state path
checks, including `database_url_resolved` for assembled database URLs that use a
password reference and `cors_origin_restricted` for non-wildcard browser
origins, `worker_heartbeat_ttl_covers_poll_interval` for worker timing that
will not flap heartbeat-backed Docker healthchecks between polling passes, and
`worker_lease_ttl_covers_poll_interval` for scaled-worker lease ownership that
survives the configured poll interval.
Runtime path readiness is exported as
`dialecticore_runtime_path_ready`,
`dialecticore_runtime_path_state`,
`dialecticore_runtime_path_free_bytes_sufficient`, and
`dialecticore_runtime_path_free_bytes` for the backup path, runtime-state path,
and local object-storage path when applicable. `dialecticore_runtime_path_state`
breaks readiness into path configuration, target/parent existence, directory
status, writability, and free-space sufficiency booleans. Database migration
readiness is exported as `dialecticore_database_migration_status`, with
sanitized current/head Alembic revision labels and an enforcement label that is
true in production. The generic readiness-check metric also exports
`database_migrations` checks for revision inspection availability, configured
migration heads, recorded database revision presence, and whether the schema is
at head. Local filesystem
object-storage readiness is also exported as
`dialecticore_object_storage_local_path_ready` plus
`dialecticore_object_storage_local_path_state` for checked-path existence,
directory status, parent availability, and writability. Active
credential-reference readiness is exported as
`dialecticore_credential_reference_count` for checked, resolved, unavailable,
owner-type, and reference-scheme counts; secret values are never included.
Credential provisioning readiness is exported as
`dialecticore_credential_provisioning_count` for active/all reference counts,
active/all unavailable counts, disabled-target unavailable counts, and grouped
`env:`, `docker-secret:`, `file:`, unsupported-scheme, and invalid-syntax counts
without including secret values or target names in metric labels.
Workflow orchestration observability is
exported as `dialecticore_workflow_orchestration_count` for attempts,
progressed/failed stages, errors, dispatch totals, dispatch status, worker,
policy, production-handoff status, and production-handoff blocker breakdowns.
Live-readiness uses the deduplicated `current_*` orchestration counts; cancelled
or failed terminal episodes remain in historical totals but are excluded from
current unresolved orchestration blockers.
Media queue readiness follows the same audit/current split: aggregate
`failed_*` and pending-job counters remain historical across all episodes, while
`current_failed_*` and `current_pending_*` counters drive live-readiness gates
and exclude terminal cancelled or failed episodes.
Workflow retry observability is exported as
`dialecticore_workflow_stage_retry_count` for active total retry backlog,
historical/resolved retry counts, active status breakdowns, active stage
breakdowns, schedule-status breakdowns for due, backoff, unknown, and
non-scheduled retry entries, plus resolved-history breakdowns by resolution
status and stage. Production-run state is exported as
`dialecticore_production_run_health_status` and
`dialecticore_production_run_count` for total, active, running-active,
paused-active, failed-active, cancelled-active, completion-blocked, and unique
attention-run counts, plus `kind="completion_failed_check"` samples labeled by
failed completion gate name.
Remote
S3/MinIO object-storage endpoint reachability is exported as
`dialecticore_object_storage_remote_reachable`; configured bucket availability
is exported as `dialecticore_object_storage_bucket_available`. Local
object-storage path readiness is exported as
`dialecticore_object_storage_local_path_ready`, with supporting boolean state in
`dialecticore_object_storage_local_path_state`, including the normalized
`writable_target_or_parent` state used by runtime-path readiness.
Redis observability is exported as `dialecticore_redis_runtime_enabled`,
`dialecticore_redis_runtime_reachable`, and
`dialecticore_redis_worker_signal_maxlen` for fan-out mode, worker-signal mode,
TCP reachability, and bounded stream retention. `/system/health` reports the
Redis component as degraded when any normalized Redis gate fails, even when TCP
reachability succeeds. Auth observability is exported as
`dialecticore_auth_mode_enabled` for API-key, trusted-identity, and
provider-session modes, plus `dialecticore_auth_provider_session_count` for
active/expired revocations and accepted/denied/error/retained decision-log
records. The normalized `auth_runtime` readiness checks include
`api_key_header_configured`, `role_header_configured`, `user_header_configured`,
`trusted_identity_header_configured`, `trusted_email_header_configured`,
`trusted_groups_header_configured`, `provider_session_token_header_configured`,
`provider_session_user_claim_configured`,
`provider_session_groups_claim_configured`, and `api_key_reference_resolves` so
blank header or provider claim names, or configured API-key references that
cannot be read, are visible through `/system/health`, live-provider readiness,
and generic component readiness metrics. Publisher target observability is
exported as
`dialecticore_publisher_target_health_status` and
`dialecticore_publisher_target_count` for target status, enabled, live-enabled,
automated-live-capable, mock, dry-run-only, healthy, unknown, unhealthy, and
issue counts. Publish job observability is exported as
`dialecticore_publish_job_count` for active total, submitted, completed, failed,
dry-run, and live persisted publish jobs, plus
`dialecticore_publish_package_manifest_count` for completed export packages,
completed production-manifest assets, packages missing or failing package QC,
packages missing checked thumbnail or generated subtitle/caption entries, and
packages missing linked production manifests. Live-provider readiness includes a
`publish_job_summary.v1` check using the same non-replaced job set, warning on
submitted jobs and completed export packages missing linked production
manifests, and blocking on failed jobs plus missing/failing package QC or missing
package thumbnail/subtitle evidence.
Backup
observability is exported as `dialecticore_backup_archive_count`,
`dialecticore_backup_archive_validation_count`,
`dialecticore_backup_latest_archive_info`,
`dialecticore_backup_latest_age_seconds`, and
`dialecticore_backup_latest_size_bytes`,
`dialecticore_backup_latest_restore_validated`, and
`dialecticore_backup_latest_restore_validation_age_seconds`, based on the
configured backup directory, archive validation coverage, latest readable archive
manifest, and latest matching dry-run restore-validation audit event. External
Temporal execution
evidence is also exported as `dialecticore_temporal_worker_execution_status`
and `dialecticore_temporal_worker_execution_count` gauges for execution status,
progressed stages, errors, and configured activity count.
`dialecticore_worker_runtime_seconds`
exports the configured worker heartbeat TTL, lease TTL, and runtime-state
retention window. `dialecticore_worker_count` includes malformed heartbeat and
lease counts, malformed files pruned after retention, and expired lease records
pruned after the same retention window.
`dialecticore_worker_signal_count`
exports recent worker signal
counters by status, signal type, target role, active blocking target role, and
delivery source.

`GET /api/v1/system/events` returns a server-sent event stream with
`system_status_event.v1` snapshots. Each `system.snapshot` event includes
health status, counts, queue counters, worker status/counts, active lease count,
stale worker count, retained/pruned heartbeat cleanup counts, worker TTL and
runtime-state retention settings, worker signal counters, a bounded set of
recent audit events, and `redis_fanout` delivery evidence. Query parameters include
`interval_seconds` from 1 to 60 seconds, `audit_limit` from 0 to 50, and
`once=true` for one-shot smoke checks. When
`DIALECTICORE_REDIS_EVENT_FANOUT_ENABLED=true`, the same snapshot is published
to the configured Redis Pub/Sub channel for multi-API Web UI fan-out. The Web UI
Live Status Stream panel shows the latest local `redis_fanout` status, channel,
and delivery count from the same SSE payload.

`POST /api/v1/system/workers/signals` records a `worker_signal_delivery.v1`
operator signal for a target worker role. Supported signal types are `drain`,
`resume`, `reload`, and `stop_after_current`. When
`DIALECTICORE_REDIS_WORKER_SIGNAL_ENABLED=true`, the signal is delivered to the
configured Redis stream and also retained in runtime state for audit/listing.
Redis stream writes use approximate `MAXLEN` trimming from
`DIALECTICORE_REDIS_WORKER_SIGNAL_MAXLEN` so long-running deployments have a
bounded stream by default. The same setting caps the local runtime-state signal
registry, recent signal list, summary counts, and worker gate lookups.
Signal listing and worker gates merge runtime-state records with recent Redis
stream entries, so workers on a host without the API's local runtime-state files
can still honor Redis-only signals. Workers inspect the latest applicable
role-specific or wildcard signal before starting a new polling pass. `drain` and
`stop_after_current` prevent new work and appear in heartbeat details as
`signal_skipped=true`; `resume` clears the block by becoming the latest signal.
Worker-signal summaries compute `blocking_count`,
`active_blocking_target_roles`, and `by_active_blocking_target_role` from the
latest applicable role-specific or wildcard signal. A wildcard `resume` clears
older target-specific blocks; recent signal type/status breakdowns still include
older drains for audit context.
If signal retention is configured very low, older blocking signals can expire
after enough newer records are written.
`GET /api/v1/system/workers/signals` lists recent signal records with
`delivery_sources` evidence; the Web UI Worker Status panel uses the same routes
for operator signal posting and recent signal review. Each posted signal also
emits a global `worker.signal.recorded` audit event with safe delivery metadata
and payload key names.

`POST /api/v1/system/backups` creates a tar.gz archive below
`DIALECTICORE_BACKUP_PATH`. The archive contains `manifest.json`,
`database.json`, local object-storage files when requested for the
local/filesystem backend, remote `object-storage-s3/` bucket objects when
requested for the S3-compatible backend, and runtime-state files when requested.
S3 endpoint metadata in the archive manifest is sanitized before it is written
or returned, removing URL userinfo, query strings, and fragments.
`GET /api/v1/system/backups` lists readable backup manifests, recomputes archive
size metadata, computes checksums for bounded-size archives, and attaches
archive-level `restore_validation` evidence from matching
`backup.restore_validated` audit events only when the recorded validation
checksum matches the current archive checksum. Large dashboard/list archives
report `checksum_status=skipped` and `checksum_not_evaluated` restore-validation
status instead of hashing the full tarball during routine UI refreshes; run the
restore validation route for full integrity evidence. Modified archives whose
checksum is evaluated report `checksum_mismatch` and are not considered
restore-validated. `POST
/api/v1/system/backups/restore` validates an archive by default; when
`apply=true`, it can replace the database tables from `database.json` and safely
extract local object-storage files, upload S3 bucket objects, and extract
optional runtime-state files into the currently configured targets. Validation
responses include `backup_restore_plan.v1` evidence with selected scopes,
database record counts, object/runtime file counts, included payload status, and
replace-existing policy. Backup create, dry-run restore validation, and applied
restore operations are audited. `scripts/backup_smoke.py` exercises the create
and dry-run validation routes end to end, writes compact
`backup_smoke_evidence.v1`, and checks that the live-provider backup readiness
section reports `pass` for the newly validated archive.

`scripts/b1_managed_media_smoke.py` exercises the external B1 managed media API
without creating a DialectiCore episode. It posts a small request to
`/v1/media/jobs`, polls `/v1/media/jobs/{job_id}`, and writes
`b1_managed_media_smoke_evidence.v1` with the accepted job ID, terminal state,
artifact summaries, and runner failure category/message when B1 accepts the job
but fails during execution.

- `GET /api/v1/participant-profiles`
- `POST /api/v1/participant-profiles`
- `GET /api/v1/participant-profiles/{profile_id}`
- `PUT /api/v1/participant-profiles/{profile_id}`
- `DELETE /api/v1/participant-profiles/{profile_id}`

The endpoint list will expand to match `goal_DialectiCore.md` as later
increments land.

Project records are persisted separately with name, description, default
language, and default show format. Episodes may reference `project_id`; deleting
a project is blocked while episodes still link to it. Create, update, and delete
operations are audited and project counts appear in system health.

Language profile administration is persisted in `language_profile_records`.
Records capture display name, BCP 47 language tag, native name, default
localization mode, subtitle direction, line-breaking policy, voice defaults, and
enabled state. The API and Web UI support create, update, and delete; deleting a
language profile is blocked while projects, episodes, or voice profiles still
reference its language tag. Operations are audited, included in backup/restore,
and counted in system health.

Episode records are persisted through SQLAlchemy. The current implementation
projects key production columns for querying and stores the validated aggregate
payload so discussion sessions, turns, transcripts, QC results, approvals, and
audit events survive API restarts. Media assets are also projected into
`asset_records` with asset type, language, source entity, storage, media
dimensions, checksum, status, and full payload fields. `GET
/api/v1/episodes/{episode_id}/assets` reads that projection when available, with
episode-aggregate fallback for older unsynced records.
`GET /api/v1/episodes` returns full episode aggregates for detail/debug clients.
The Web UI uses `GET /api/v1/episodes/summaries` for episode lists and
dashboard counts. That endpoint returns compact `EpisodeSummary` rows with IDs,
titles, status, duration bounds, source/output languages, discussion
turn/duration counts, artifact/result/publish counts, and pending approvals,
without heavy transcript, asset, discussion-session, or audit payloads. After an
operator selects an episode, the UI loads the full aggregate through
`GET /api/v1/episodes/{episode_id}`.

Recent audit events are also projected into `audit_event_records` and exposed
through `GET /api/v1/audit-events?limit=50`. Episode lifecycle, discussion turn,
transcript revision, approval, project, language profile, model endpoint, and
participant profile changes share the same stream. The endpoint is bounded to
1-200 rows and returns newest events first.

Model endpoint administration is persisted separately in
`model_endpoint_records`. Credential values are not accepted or returned; records
use `credential_reference` strings such as `env:OPENAI_API_KEY`. Create, update,
health-check, and delete operations add global audit events without storing
secret material. Health checks update `health_status` and discovered
`capabilities` for mock, OpenAI-compatible, Ollama, Anthropic-compatible,
Mistral-compatible, and generic HTTP providers. Remote checks use provider
defaults such as `/models`, `/api/tags`, or `/health`, and can be overridden
with `capabilities.health_path`. Provider-supplied capability metadata is
recursively redacted for token, secret, password, API-key, authorization, and
credential fields before it is persisted or returned. Endpoint `base_url`
values with URL username/password userinfo are rejected; credentials must be
provided through `credential_reference`. Top-level credential references must
use `scheme:target` syntax, and secret-shaped capability fields are redacted on
write while reference-shaped capability fields remain available.
`POST /api/v1/model-endpoints/openrouter/presets/provision` creates or refreshes
the OpenRouter OpenAI-compatible endpoint with `env:OPENROUTER_API_KEY`, a
curated `model_presets` range, and optional assignment of the six
frontier-model participant profiles to matching model IDs. The response reports
created/updated endpoint state, assigned participant IDs, and missing
participant IDs without returning raw credentials.

Participant profile administration is persisted in `participant_profile_records`.
Episode creation uses these stored profiles by default and preserves the
participant order declared in the episode definition. Create, update, and delete
operations are included in the global audit stream. The Web UI exposes the same
profile management surface for model endpoint/model assignment, sampling
settings, prompt template, perspective, expertise, speaking style, tool policy,
and optional Voicebox/visual profile bindings.

Voicebox endpoint administration is persisted in `voicebox_endpoint_records`.
Endpoint records include adapter type, base URL, credential reference, timeout,
concurrency, retry policy, enabled state, capabilities, and health status.
`POST /api/v1/voicebox-endpoints/{endpoint_id}/health` checks mock endpoints
locally or calls `/health` and `/capabilities` on a configured remote Voicebox
base URL, then stores the resulting status/capabilities. Credential values are
not accepted or returned; only references such as `env:VOICEBOX_TOKEN` are
stored. Provider-supplied capability metadata from health responses is
recursively redacted before persistence and API exposure. Endpoint `base_url`
values with URL username/password userinfo are rejected; credentials must be
provided through `credential_reference`. Top-level credential references must
use `scheme:target` syntax, and secret-shaped capability fields are redacted on
write while reference-shaped capability fields remain available.
`b1_voice_stream` endpoints use the native stream contract for health evidence:
the configured credential reference must resolve, the configured public CA
bootstrap URL is probed without endpoint authorization, and the saved
`tls_ca_cert_path` must point to an available certificate file before the
endpoint is marked healthy.
`POST /api/v1/voicebox-endpoints/{endpoint_id}/ca-certificate/bootstrap`
downloads a configured public CA bootstrap URL without endpoint authorization,
validates the expected SHA-256 when configured, stores the certificate under
runtime-state certificates using only the configured `tls_ca_cert_path`
filename, updates the endpoint to the stored runtime-state path, updates safe
bootstrap evidence, reruns endpoint health, and persists the updated endpoint.
Successful bootstrap records a
`voicebox_endpoint.ca_certificate_bootstrapped` audit event with safe endpoint
ID, health, stored, SHA-match, and path-configured flags only.
`POST /api/v1/voicebox-endpoints/{endpoint_id}/b1-german-voice-presets/provision`
requires a saved `b1_voice_stream` endpoint. It creates only missing supplied B1
German voice profile presets, preserves existing profiles with matching IDs, and
when `assign_participants=true` assigns available B1 voice profile IDs to the
matching frontier-model participant profiles: ChatGPT, Claude, DeepSeek, Grok,
Gemini, and Mistral. Existing participant `voice_profile_id` values are
preserved by default; pass `reassign_participants=true` with
`assign_participants=true` to deliberately move matching frontier participants
back to the supplied native B1 presets. The response returns created/existing
voice profile IDs, participant-to-voice assignments, preserved participant IDs,
reassigned participant IDs, and the requested reassignment mode. The audit event
`voice_profile.b1_presets_provisioned` stores safe counts and local IDs only,
not remote native profile IDs or credential references.

Voice profiles are persisted in `voice_profile_records` and reference a
Voicebox endpoint. Participant profiles may reference a `voice_profile_id`;
unknown voice profile references are rejected.

ComfyUI endpoint administration is persisted in `comfyui_endpoint_records`.
Endpoint records include adapter type, base URL, credential reference, timeout,
concurrency, retry policy, enabled state, capabilities, and health status.
`POST /api/v1/comfyui-endpoints/{endpoint_id}/health` checks mock endpoints
locally or calls `/system_stats` on a configured remote ComfyUI base URL. For
B1 native ComfyUI endpoints it calls `/object_info` and performs an empty
`/prompt` admission probe so health reflects both read access and scheduler/GPU
write admission. Credential values are not accepted or returned; only references
such as `env:COMFYUI_TOKEN` are stored. Provider device metadata and prompt
admission responses are recursively redacted before persistence and API
exposure. A B1 `hardware_resource_policy` admission failure means the remote
appliance is reachable but cannot currently accept prompt work; DialectiCore
surfaces the reason and leaves GPU cleanup to the appliance operator because
mutating compatibility routes such as `/free` may be denied by the gateway.
Endpoint `base_url` values with URL username/password userinfo
are rejected; credentials must be provided through `credential_reference`.
Top-level credential references must use `scheme:target` syntax, and
secret-shaped capability fields are redacted on write while reference-shaped
capability fields remain available.

ComfyUI workflows are persisted in `comfyui_workflow_records` and reference a
ComfyUI endpoint. Workflow records capture workflow type, version, output asset
type, API workflow JSON, prompt templates, and default parameters. Seeded
workflows include preset sampler settings, explicit node patch bindings,
computed frame-count bindings for video presets, and motion/camera/lighting or
B-roll composition metadata. Unknown endpoint references are rejected, and
workflows cannot be deleted while visual profiles still reference them.

Discussion prompt templates are persisted in
`discussion_prompt_template_records`. `GET /api/v1/discussion-prompt-templates`
lists managed templates; `POST`, `PUT /{template_id}`, `GET /{template_id}`,
and `DELETE /{template_id}` create, update, read, and delete template records.
Records include template ID, version, participant type, system/user content,
variables, creator, creation timestamp, enabled state, and change summary.
Participant profiles must reference a known enabled template ID whose participant
type matches the profile. A template cannot be deleted, disabled, or changed to a
different participant type while participant profiles still reference it. Each
generated model turn records the selected prompt-template ID and version in
generation metadata.

Visual profiles are persisted in `visual_profile_records` and reference primary
plus optional reaction/B-roll ComfyUI workflows. Participant profiles may
reference a `visual_profile_id`; unknown visual profile references are rejected.
`POST /api/v1/visual-profiles/{profile_id}/reference-image` accepts a JSON
upload payload with `filename`, `content_type`, base64 image bytes, and a
`reference_type` of `portrait`, `full_body`, or `wardrobe` for PNG, JPEG, or
WebP. The backend validates the declared type against image bytes, stores the
image through the configured object store, replaces the portrait or full-body
typed `reference_images` slot, appends wardrobe references so multiple optional
outfit/detail references can be carried for a character, keeps the legacy
`reference_image_uri` portrait value where applicable, and records a safe
`visual_profile.reference_image_uploaded` audit event with content type, size,
checksum, storage backend, and URI-presence evidence.
`GET
/api/v1/visual-profiles/{profile_id}/reference-images/{reference_type}/download`
streams the stored typed reference as a same-origin attachment for operators or
remote workflow clients. For wardrobe references, add `?uri={stored_object_uri}`
to download one specific wardrobe image; without `uri`, the latest wardrobe
reference is returned for compatibility with single-slot clients. `DELETE
/api/v1/visual-profiles/{profile_id}/reference-images/{reference_type}` removes
the typed association from the visual profile; for wardrobe references, add
`?uri={stored_object_uri}` to remove one specific wardrobe image while keeping
the others. It clears the legacy portrait URI if it pointed at the removed
portrait, records
`visual_profile.reference_image_removed`, and intentionally retains the stored
object for audit and immutable evidence. Future visual planning copies both
object-storage URIs and API download URLs into ComfyUI prompt inputs and
timeline scene/layer metadata so video scenes can animate the participant from
uploaded portrait, full-body, and wardrobe material.
`POST /api/v1/show-media/scene-reference-image` accepts the same PNG, JPEG, or
WebP base64 upload shape for show-level studio/set references before an episode
is created. The response returns a stored `scene_reference_image_uri` plus safe
content type, checksum, size, and object-key metadata; the Web UI writes that URI
into `media.scene_reference_image_uri` on the episode definition draft. Scene
references are show media properties, not character profile properties.
`GET /api/v1/show-media/scene-reference-image/download?uri=...` downloads stored
show-scene material and rejects URIs outside the
`show-media/scene-reference-images/` object prefix.
For already-created episodes, `PATCH
/api/v1/episodes/{episode_id}/production-settings` updates the duration bounds
and may also persist `scene_reference_image_uri` on
`definition.media.scene_reference_image_uri`. Omitting the field leaves the
stored scene reference unchanged; a non-empty string sets it, and `null` or an
empty string clears it. When the field is supplied, the
`episode.production_settings.updated` audit event includes `media_previous` and
`media_current` scene-reference values.

Turn-level review actions are allowed only while the episode is in transcript
review and the canonical transcript is not approved. Each regeneration or
exclusion creates a new broadcast transcript version, keeps source turn links,
sets each transcript turn's `transcript_version_id`, updates the canonical
transcript pointer, runs transcript semantic QC, and records audit events.
Transcript approval returns `422` when the canonical transcript has failing
semantic-fidelity QC.

Localization starts after canonical transcript approval. `POST
/api/v1/episodes/{episode_id}/localize` creates configured non-canonical
language transcript versions, sets transcript-turn version links, preserves
source discussion-turn links, adds pronunciation markup for each playable turn, records
`localized_transcript_semantic_fidelity` QC, creates one pending
`localized_transcript_review` approval targeted at each localized transcript
version, and emits audit events. The QC result includes the configured
semantic-fidelity threshold, new-claim policy, and transcript localization
metadata;
when `allow_new_claims=false`, any localized claim record that is not present in
the source turn fails with `localized_new_claim_detected`. The current localizer
is deterministic scaffold behavior for adapter integration; it is source-bound,
mode-preserving, and usable by downstream audio/subtitle/visual production, but
does not claim production-grade translation quality.

Audio planning starts from a canonical or localized transcript. `POST
/api/v1/episodes/{episode_id}/audio-assets/plan` creates one planned audio asset
per non-excluded transcript turn, links each asset to its transcript turn,
records Voicebox generation metadata plus transcript type/localization metadata,
and adds
`audio_asset_plan_completeness` QC. Remote Voicebox submission and retrieval are
handled by `POST /api/v1/episodes/{episode_id}/audio-assets/generate`. The
deterministic mock Voicebox adapter stores real WAV objects with checksums,
measured durations, and word timestamps. Remote Voicebox adapters submit
normalized `/tts` requests and persist returned job IDs, storage URIs,
durations, checksums, and timing metadata. B1 stream Voicebox adapters submit
the native `/generate/stream` payload, require non-empty audio content with an
audio content type, then store the returned WAV bytes through the same object
storage and QC path. `audio_generation_completeness` QC
verifies every playable turn has a completed audio asset with storage, duration,
and checksum metadata. `audio_media_integrity` QC verifies the playable speech
media itself before production completion can close.
When generation or remote-job sync completes audio, the source discussion turns
linked by the transcript receive per-language
`actual_audio_duration_seconds_by_language`, the latest
`actual_audio_duration_seconds`, and the discussion session's speaker balance is
recalculated with `actual_speaking_seconds` for the produced language.

`POST /api/v1/episodes/{episode_id}/speech/produce` is the producer-facing
shortcut for the same approved-transcript path. It creates missing planned
audio assets for the selected transcript and then submits/generates them through
the participants' configured voice profiles and Voicebox endpoints. It does not
approve transcripts or bypass review gates; a canonical or localized transcript
must already be approved. A localized transcript remains in `pending_review`
until its `localized_transcript_review` approval is accepted through the normal
approval-decision endpoint.

Completed mock Voicebox audio is now materialized as real WAV objects in the
configured object store and addressed with stable backend URIs:
`object://bucket/key` for the local backend or `s3://bucket/key` for the
S3-compatible backend. Remote adapters that return inline `audio_base64`
payloads are stored the same way. Same-origin HTTP(S) result URLs returned as
`storage_uri`,
`audio_url`, `result_url`, `media_url`, or `download_url` are downloaded,
stored, and rewritten to backend object URIs. External signed URLs require
the endpoint capability `allow_external_result_urls`; bearer credentials are
sent to external URLs only when `result_download_include_authorization` is true.
Stored audio is probed from the actual file or S3 probe cache when available,
using `ffprobe` for container metadata, FFmpeg `loudnorm` for integrated
loudness analysis, and a WAV parser fallback otherwise. Probe evidence is
persisted in asset `generation_metadata.media_probe`, including measured
duration, MIME type, sample rate, channel count, byte size, probe tool, and
warnings. Stored WAV assets also record waveform-derived peak dBFS, RMS dBFS,
silence ratio, and clipping detection. When FFmpeg is available, the probe
records integrated LUFS, loudness range, true peak, loudness target, true-peak
target, loudness-range target, normalization gain, target offset, and
normalization type; otherwise LUFS falls back to the stored-WAV RMS estimate.
The storage/probe-cache root is controlled by
`DIALECTICORE_OBJECT_STORAGE_LOCAL_PATH`.

Audio generation requests may target `asset_ids`, `transcript_turn_ids`,
`participant_ids`, or `failed_only` selections with `regenerate: true`, so a TTS
failure does not require regenerating the full episode. Generation records
attempt counts, emits `audio.assets.regenerated` audit events for selective or
explicit regeneration, and records `audio_media_integrity` QC. The same QC can
be rerun with `POST /api/v1/episodes/{episode_id}/audio-assets/qc`. Current QC
uses available stored-media probe values or Voicebox metadata to check storage,
duration plausibility, MIME format, sample rate, channel count, language marker,
voice-profile consistency, clipping, silence, loudness, true peak, and
word-timestamp bounds. Real object storage-backed audio and media probing are
used when the stored object or S3 probe cache is available; provider metadata
remains the fallback for external remote URIs. QC details include
`loudness_analyzed_audio_asset_count`,
`loudness_normalization_recommended_audio_asset_count`, and
`downloaded_remote_result_count` evidence.
Use `POST /api/v1/episodes/{episode_id}/workflow/start` to create a durable
worker-managed production run without re-running the discussion engine. After a
run exists, `POST /api/v1/episodes/{episode_id}/workflow/advance` executes the
ordered local worker stages and records orchestration evidence. The legacy
`POST /api/v1/episodes/{episode_id}/produce` endpoint remains available for
direct discussion execution, but the Web UI starts durable production through
`/workflow/start`.

The workflow-worker audio stage uses the same selective mechanism when it sees
planned, failed, or cancelled turn-level audio assets. Its per-stage summary
reports `targeted_audio_assets` and `repair_audio_assets`, allowing producer
UIs and orchestration logs to distinguish ordinary first-pass speech generation
from failed/cancelled turn repair. The same counts are persisted on
`workflow_control.worker_orchestration_log[].stage_attempts[]` for durable
selected-episode workflow evidence.

Asynchronous remote Voicebox jobs can be synchronized with `POST
/api/v1/episodes/{episode_id}/audio-assets/sync`. The endpoint polls assets with
stored `remote_job_id` metadata through the Voicebox endpoint's
`job_status_path_template` capability, defaulting to `/tts/jobs/{job_id}`. It
updates submitted/running assets with returned storage, duration, checksum,
sample-rate, language, loudness, clipping, silence, word, and phoneme timing
metadata, downloads completed HTTP(S) result URLs when allowed, then records
`audio_generation_completeness`, `audio_media_integrity`, and
`audio.jobs.synced` audit evidence. `include_completed: true` can re-check
completed jobs when a provider exposes mutable result metadata.

Completed audio assets also carry normalized timing tracks for downstream
lip-sync and styled subtitle stages. Provider `phoneme_timestamps` are
normalized into `normalized_phoneme_timestamps` and `viseme_timestamps` with
`phoneme_timing.source: "provider_phoneme_timestamps"`. When a provider does
not return phonemes but word timings are available, the service derives an
explicitly estimated phoneme and viseme track from the word timings with
`phoneme_timing.source: "estimated_from_word_timestamps"`. Audio QC validates
track structure, timing bounds, monotonic ordering, and viseme/phoneme count
alignment, and reports provider, estimated, ready, and missing timing counts.

Submitted or running remote Voicebox jobs can be cancelled with `POST
/api/v1/episodes/{episode_id}/audio-assets/cancel`. The request accepts the same
`asset_ids`, `transcript_turn_ids`, and `participant_ids` selectors used by
generation and sync. Remote adapters call the endpoint's
`job_cancel_path_template` or `cancellation_path_template` capability, defaulting
to `DELETE /tts/jobs/{job_id}`. `job_cancel_method` or `cancellation_method` may
override the method, including `POST` cancel endpoints. By default,
`reset_to_planned: true` clears the active remote job reference, records the
cancelled job ID in metadata, marks the asset ready for retry, and emits
`audio.jobs.cancelled`. Explicit regeneration of submitted or running assets is
also cancellation-aware and cancels the previous remote job before submitting the
replacement when the provider supports it.
Already-cancelled assets can also be selected with `reset_to_planned: true`;
this preserves the last `cancelled_remote_job_id`, clears active storage/job
state, marks the asset ready for retry, and lets the workflow worker pick it up
after provider health recovers.

Subtitle generation starts from the same approved target transcript selection
used by audio. `POST /api/v1/episodes/{episode_id}/subtitles/generate` creates a
completed subtitle asset for the approved canonical or latest approved
localized transcript. Explicit `transcript_version_id` requests do not bypass
the approval gate. The
current implementation emits WebVTT by default, accepts `format: "srt"` for
SubRip output, links the asset to the transcript version, stores the rendered
subtitle text plus cue provenance in generation metadata, calculates a checksum,
and records `subtitle_generation_completeness` QC. When completed audio assets
include `word_timestamps`, cues are segmented into shorter word-timed subtitle
lines. Otherwise cue timings use completed audio asset durations or
deterministic transcript-based estimates. Estimated timings produce warning QC.
Subtitle QC checks turn coverage, empty lines, source links, timing overlaps,
line length, and audio sync drift against `block_on_sync_error_ms`. The route
emits `subtitle.asset.generated` audit events.

Visual planning starts from the same target transcript selection used by audio.
`POST /api/v1/episodes/{episode_id}/visual-assets/plan` creates planned visual
asset placeholders for the canonical or latest localized transcript. It adds one
primary video asset per playable transcript turn, optional B-roll placeholders
when the episode media definition enables B-roll, one reusable studio scene per
episode/language, and one reusable reaction/listening loop per active
participant with an enabled reaction workflow. Primary turn assets record
`shot_plan` metadata that references reusable reaction/studio assets, optional
B-roll, transition type, subtitle style, and citation-overlay requirements. The
route also plans `citation_card` overlay assets for cited transcript turns when
the episode media policy enables citation cards. Those overlay assets retain
claim text, evidence refs, and resolved source metadata. The route stores
ComfyUI endpoint, workflow, visual profile, prompt input, and fallback-policy
metadata for remote assets and emits `visual.assets.planned` audit events. The
route records `visual_asset_plan_completeness` QC to verify participant visual
profiles, enabled workflows, primary visual coverage, reusable visual counts,
shot-plan coverage, and citation-overlay coverage.

Visual generation submits planned visual assets to the asset's configured
ComfyUI workflow. `POST
/api/v1/episodes/{episode_id}/visual-assets/generate` supports the same
selection controls as audio generation: `asset_ids`, `transcript_turn_ids`,
`participant_ids`, `failed_only`, `regenerate`, and `fallback_on_failure`. The
remote adapter submits a normalized `/prompt` request and stores the returned
`prompt_id` or `job_id` as `remote_job_id`. Workflow `api_workflow` JSON is
deep-copied and patched before submission from prompt inputs, visual profile
metadata, media dimensions, seed, and transcript text. The seeded workflow
registry includes concrete talking-head, reaction-loop, topic B-roll, and
studio-wide API workflow templates with explicit patch bindings. Workflow
records can provide explicit
`node_input_bindings`; otherwise common input names such as `text`, `prompt`,
`positive`, `negative`, `width`, `height`, `fps`, and `seed` are patched
conservatively. Immediate provider payloads with `video_base64`,
`image_base64`, `media_base64`, `result_base64`, or same-origin result URLs are
written to the configured object store. If submission fails and
`fallback_on_failure` is enabled, the service stores a deterministic SVG
citation card or fallback still so the turn remains render-suitable. The
deterministic mock ComfyUI adapter stores SVG still assets with
`deterministic_mock_visual`, `render_ready`, and
`requires_static_image_duration` metadata. These prove the storage, metadata,
retry, QC, timeline, and render handoff paths without requiring a local ComfyUI
server.
Planned citation overlay cards are generated locally as deterministic SVG
graphics and stored through the same object-storage, checksum, probe, and QC
path as remote visual outputs.

`POST /api/v1/episodes/{episode_id}/visuals/produce` is the producer-facing
shortcut for approved transcripts after speech exists. It plans missing visual
assets, submits/generates them through the participants' configured visual
profiles and ComfyUI workflows, and records visual media QC in the same call.
It preserves the lower-level plan/generate/sync/QC endpoints for selective
retry and remote-job recovery. The workflow-worker visuals stage targets
planned, failed, and cancelled visual assets by ID and reports
`targeted_visual_assets` plus `repair_visual_assets`, so failed character clips,
reaction loops, studio shots, B-roll, or citation overlays can be repaired
without restarting the discussion or completed media stages. These counts are
also journaled on per-stage workflow orchestration attempts and shown in the
Web UI Workflow Evidence panel.

Asynchronous remote ComfyUI jobs can be synchronized with `POST
/api/v1/episodes/{episode_id}/visual-assets/sync`. The endpoint polls assets
with stored `remote_job_id` metadata through `job_status_path_template` or
`history_path_template`, defaulting to `/history/{job_id}`, stores returned media
where possible, and records `visual.jobs.synced`. Submitted or running visual
jobs can be cancelled with `POST
/api/v1/episodes/{episode_id}/visual-assets/cancel`; by default cancellation
resets assets to `planned` so they can be retried. Both generation and sync
record `visual_generation_completeness` QC for completed, submitted, failed,
stored, probed, render-ready, render-suitable, and fallback counts. Stored
PNG/JPEG outputs are probed from file headers, stored SVG fallback cards are
probed from SVG dimensions, and stored video outputs are probed with `ffprobe`
when available. Probe evidence is persisted in `generation_metadata.media_probe`,
including video codec, pixel format, bit-rate, frame-count source, estimated
frame count, and critical video probe warnings when applicable.

Visual media QC can be rerun with `POST
/api/v1/episodes/{episode_id}/visual-assets/qc`. The check evaluates active
visual assets for completion, storage, checksum, probe evidence,
render-suitability, expected dimensions, PNG pixel evidence, SVG structural
evidence, video probe integrity, FPS, duration alignment with completed turn
audio, lip-sync readiness from audio phoneme timing, measured lip-sync offsets,
character identity/style consistency against plan-time profile snapshots,
configured workflows/endpoints, placeholder use, and fallback use. It records
`visual_media_integrity` QC, including pixel analyzed counts, pixel warning
counts, video probe warning/invalid counts, max/average lip-sync offsets, and
identity/style warning counts, and emits `visual.qc.completed` audit evidence.

Timeline assets can be built with `POST
/api/v1/episodes/{episode_id}/timeline/build`. The request accepts
`transcript_version_id`, `language`, `user_id`, and `regenerate`; the selected
transcript must be approved even when addressed explicitly by ID. The response
is the updated episode with a completed `timeline` asset, object-storage checksum,
`timeline_integrity` QC, `timeline.asset.built`, `timeline.qc.completed`, and
workflow stage audit evidence. Timeline segment JSON includes media asset
fingerprints for linked audio, visual, subtitle, fallback, and citation overlay
assets; `timeline_integrity` fails with `timeline_stale_media_fingerprint` when
a linked asset is replaced, missing, checksum-mutated, storage-mutated,
status-mutated, or no longer render-ready after timeline creation. Segments also
carry `source_discussion_turn_ids` copied from the transcript turn; when a turn
has canonical source links, `timeline_integrity` fails if an edited or imported
segment drops or changes those discussion-turn IDs.

`GET /api/v1/episodes/{episode_id}/timeline` returns the latest active timeline
asset, its raw `EpisodeTimeline` JSON payload, and a typed `timeline_entity`
view with ID, episode ID, language, version, status, duration, full
`timeline_json`, and creation time for a transcript or language.
`GET /api/v1/episodes/{episode_id}/assets/{asset_id}/download` streams a stored
episode asset through the API when the asset belongs to that episode, is not
`replaced`, has a configured object-storage URI, and the configured local or S3
probe-cache object is present. The response uses the asset MIME type and a
stable filename derived from asset type and ID, allowing operators to inspect
final renders, thumbnails, subtitles, export packages, and manifests without
opening object-storage paths manually.
The production test report uses the same resolver and only emits `download_url`
for artifacts whose stored files are currently available; unavailable required
delivery artifacts appear as `*_not_downloadable` blockers.
The report also includes `acceptance_summary`
(`production_acceptance_summary.v1`), a compact status rollup for real-life
test triage. It copies only comparison-stable fields: episode status,
production target, completion/report/package/download statuses, blockers,
failed checks, required deliverable IDs/checksums/file sizes, package inspection
counts, compact publish-evidence binding status, and
`workflow_run_until_blocked`
(`production_workflow_run_until_blocked_summary.v1`). That workflow field
reports the last bundled producer run status, stop reason, pass count,
progressed-stage count, pending review stages, completion status, bounded failed
checks, compact talk-show handoff blockers/readiness/asset IDs with
`next_handoff_action`, and bounded orchestration attempt IDs without raw worker
summaries. Use
the full
`deliverables`, `package_inspection`, `publish_evidence_binding`, and `quality`
sections when debugging a failing or warning summary. The report also includes
`media_readiness` (`production_media_readiness.v1`), which carries the selected
pilot media mode, audio-first readiness, native-visual readiness, native visual
configuration readiness, native ComfyUI prompt-admission readiness, selected
non-mock B1 managed media endpoints, required aliases, available aliases, and
missing aliases. `native_visual_ready` requires completed native primary visuals,
not only a healthy endpoint. The catalog and admission fields are configuration
and admission evidence; B1 GPU-runner completion is proven by completed visual
assets or the managed media smoke evidence. When
`DIALECTICORE_B1_MANAGED_MEDIA_SMOKE_EVIDENCE_PATH` points at the latest
`scripts/b1_managed_media_smoke.py` output, `media_readiness.managed_media_smoke`
adds a sanitized summary with model alias, operation, job ID, terminal state,
failure category/message, artifact count, and the next operator action.
The report keeps `operator_next_action` as the primary highest-priority action
and also emits `operator_next_actions`, an ordered list of parallel scoped
actions such as `workflow`, `speech`, `managed_media`, `delivery_artifacts`,
`export_package`, `publishing`, and `completion`, so one failing provider does
not hide secondary work needed for the same talkshow run. Provider repair
actions for Voicebox/B1 still take priority, but otherwise the latest
`workflow_run_until_blocked.handoff.next_handoff_action` is promoted before
generic package, publish, or completion suggestions. That keeps transcript,
preview-render, final-render, and media-handoff review gates visible after a
bundled run stops. When an audio-first real-life report passes but native
visuals are still missing or backed by local fallback assets,
`operator_next_actions` keeps the acceptance pass and adds nonblocking `speech`,
`managed_media`, and `native_visual` follow-up entries so operators can publish
the valid audio-first package while continuing Voicebox and character-animation
recovery work for the next talkshow run.
The Web UI consumes these same fields in **Workflow Evidence** -> **Real Test
Report** to show direct controls for approval navigation, dry-run publishing,
deliverable downloads, B1 cast/media checks, and native visual retries.
`GET /api/v1/episodes/{episode_id}/youtube-package/inspect` reads the latest
completed export package, or the package selected by `package_asset_id`, and
returns `youtube_package_inspection.v1` evidence. It verifies that the ZIP can
be opened, `youtube-package.json` can be parsed, the package includes a rendered
video entry, the manifest schema is `youtube_package.v1`, manifest-declared
thumbnail and subtitle entries are present in the ZIP, and the ZIP file list
matches the package asset metadata when that metadata is present. The production
test report embeds the same inspection and reports
`export_package_not_inspectable` when the package cannot be opened or fails core
inspection.
`POST /api/v1/episodes/{episode_id}/assets/{asset_id}/replace` registers a
manually corrected media or manifest asset without rerunning the upstream
generator. The original asset is marked `replaced`, the new asset preserves the
original source entity link and records `manual_asset_replacement.v1` metadata,
and active timeline JSON references to the old asset ID are rewritten to the
replacement asset with checksum-bound timeline storage and `asset.replaced`
audit evidence.
`PUT /api/v1/episodes/{episode_id}/timeline` persists an edited timeline as a
new active timeline asset and marks the previous one as `replaced`, allowing
timeline edits without regenerating the discussion transcript or media. Edited
scene `start_ms`/`end_ms` values are normalized into per-scene `duration_ms`,
and the response includes `timeline.asset.edited` plus `timeline_integrity`
evidence.

`GET /api/v1/render-presets` returns the initial output presets: YouTube 1080p,
YouTube 1440p, YouTube 4K, preview low-bitrate, audio-only, and short
promotional clip.

`POST /api/v1/episodes/{episode_id}/renders` renders an active timeline with
FFmpeg. The request accepts `render_type=preview` or `render_type=final`,
`preset_id`, optional `timeline_asset_id`, optional transcript/language filters,
`user_id`, `regenerate`, and `allow_unapproved_preview`. Preview jobs store a
deterministic scene-composite MP4 capped at 30 seconds and create a targeted
`preview_render_review` approval. Final jobs render the full timeline duration
after the matching preview render has been approved, unless the caller
explicitly sets `allow_unapproved_preview=true`. Both paths store a separate
render manifest that lists source
timeline/evidence/audio/video/subtitle assets, their source entity links,
status, storage URI, media metadata, checksums, render-readiness state,
normalization targets, and
`scene_composition.v1` evidence with segment counts, resolved media counts,
resolved visual plate counts, composited visual overlay layer counts, generated
visual fallback counts, timeline-ordered dialogue audio layer counts, silent
audio fallback counts, layout policy names, transition policy names, animated
scene counts, motion primitive names/counts, advanced layout counts,
split-screen/focus-shift/cross-scene counts, rendered xfade counts,
cross-scene renderer mode, rendered layer transform names/counts, rendered
scale/opacity keyframe counts, rendered easing curve names/counts, rendered
mask names/counts, non-rectangular mask counts, layer mask renderer mode,
layer motion renderer mode,
per-layer layout slots, per-layer animation cues, subtitle track counts,
burned-in caption cue counts,
planned/resolved/composited citation overlay counts, and the active
`timeline_scene_composite_*` mode. Resolved image/video visual assets are
normalized into timeline-ordered scene plates, composing studio, talking-head,
B-roll, and reaction layers simultaneously through deterministic role-aware
layout policies. Those policies now include advanced focus-with-context and
split-screen layouts with focus-role metadata and safe-area rules when those
assets are available before falling back to generated backgrounds. Non-cut
timeline camera transitions resolve to deterministic transition policies,
cross-scene flags, motion primitive evidence, and simple per-scene fade-in
effects. Adjacent scene plates with cross-scene policies are composed through
FFmpeg `xfade` boundaries while preserving the final timeline duration.
Animated B-roll and reaction/focus overlays use FFmpeg per-frame overlay
position, scale, and alpha fade expressions to move into their resolved layout
slots. Rendered position and scale keyframes honor the policy easing value;
the expression set supports deterministic cubic `ease_out`, `ease_in`, and
`ease_in_out` curves plus an `ease_out_back` spotlight curve. `source_reveal`
transitions render B-roll through an arc path with a diamond alpha mask, and
`speaker_spotlight` transitions render primary speaker overlays through a
bounce path with a circular alpha mask. Other non-full-frame visual layers
include deterministic rounded-rectangle alpha mask policies, and FFmpeg applies
those masks to the overlay stream before compositing.
Resolved per-segment dialogue audio objects are normalized
into the rendered 48 kHz audio track; missing clips or timing gaps are
represented as generated silence. Linked subtitle VTT/SRT text is parsed and
burned into the render by cue timing, with missing cue text reported through
render QC. When the timeline contains cited claims, the manifest
includes `evidence_lineage` with the evidence-pack asset/checksum, referenced
source IDs, source metadata, per-segment citation links, unresolved refs, and
retrieval-log summary. Render assets include FFprobe evidence in
`generation_metadata.media_probe` plus `render_preview_integrity` or
`render_final_integrity` QC. Final-render QC includes configured target,
minimum, and maximum runtime evidence and fails when measured final duration is
outside the episode duration bounds. Preview and final render approvals require
the matching render QC row, and approvals are rejected while the latest matching
render QC is missing or failing. `GET
/api/v1/episodes/{episode_id}/renders` lists render assets for the episode.

`POST /api/v1/episodes/{episode_id}/workflow/actions` records durable workflow
control actions. The request accepts `action` (`pause`, `resume`, `cancel`,
`stop_run`, `retry_failed_stage`, `approve_stage`, `reject_stage`,
`continue_after_manual_edit`, or `complete`), optional `user_id`, and optional `comment`. Pause
keeps the current production stage while setting `workflow_control.paused`;
`/produce` rejects paused episodes. Resume clears the paused flag. Cancel moves
the episode to `CANCELLED` and records the stage it was cancelled from.
`stop_run` stops the active production-run journal without cancelling the
episode, preserving the current editable stage so production can be started
again later. Retry is
available for `FAILED` episodes or episodes with failed assets; it clears
paused/cancelled flags, increments `workflow_control.retry_count`, restores the
failed stage when `workflow_control.failed_stage` is available, and reopens the
run projection as running. Stage approval appends `workflow_stage_decision.v1`
approval evidence without moving the episode. Stage rejection appends the same
decision evidence, records the rejected stage as `workflow_control.failed_stage`,
moves the episode to `FAILED`, and leaves the existing failed-stage retry path
available. `continue_after_manual_edit` records the operator continuation,
captures `manual_edit_evidence.v1` from relevant post-failure manual-edit audit
events (`asset.replaced`, `timeline.asset.edited`,
`transcript.turn.regenerated`, and `transcript.turn.excluded`), stores the
sanitized event counts/details plus `evidence_checksum` on
`workflow_control`, the run signal, the Temporal signal log, and the
continuation audit event, clears pause/cancel/failure flags, restores the
failed/rejected/paused stage when known, and reopens the run projection as
running. The Web UI Workflow Evidence panel summarizes the same manual-edit
bundle as sanitized event counts, edit categories, post-failure scope, and
checksum presence without rendering raw audit event IDs, actors, comments, or
asset IDs. `complete` validates the
same `production_completion_readiness.v1` gates exposed by
`/workflow/completion-readiness`, records the gate result on the run, appends a
durable completion signal, and moves the episode to `COMPLETED`; failed gates
return 422 and leave the run in progress. `/produce` creates
`workflow_control.run` with schema `production_workflow_run.v1`, a `run_id`,
`run_sequence`, current stage, stage plan, stage history, and signal history.
Discussion execution updates the run stage history through discussion prep,
discussion, transcript QC, and transcript review. Content approval decisions
also update the active run projection when they move the episode stage:
approved research review records `DRAFT`, and approved transcript review
records `READY`, both sourced as `approval.decision.recorded`. A required
research review that is still pending or was rejected is a hard workflow gate:
discussion workers will not start the talk show transcript, and completion
readiness reports `research_approval_missing` or `research_approval_rejected`
until the evidence pack is rebuilt/revised and approved. Workflow actions append
pause/resume/cancel/retry/approve/reject/continue signals to the active run
when one exists, append `workflow.*` audit evidence, and, where the stage
changes, a `workflow.stage.changed` event. Start and workflow-action requests
also append
`workflow_control.temporal_signal_log` entries with schema
`temporal_signal_transport_attempt.v1`; each entry records the local signal ID,
run ID, namespace, optional task queue, endpoint configuration, and sent,
skipped, failed, or disabled delivery status for the optional external Temporal
bridge. They also append `workflow_control.workflow_event_log` entries with
schema `workflow_event.v1` for local replay.

`POST /api/v1/episodes/{episode_id}/workflow/advance` runs one ordered
workflow-worker pass for the selected episode only. It reuses the same stage
chain as the background `workflow-worker`: research, discussion, localization,
claim QC, audio, Voicebox sync, subtitles, visuals, ComfyUI sync, timeline,
render, publishing, and completion. The response contains the updated `episode` and a
`workflow_worker_orchestration_summary.v1` `summary` with per-stage counts,
errors, progress totals, a stable `orchestration_attempt_id`, a
`workflow_run_start_summary.v1` entry showing whether
the selected episode's durable production run was started during the pass,
discussion-stage `model_configuration_blocked` counts with
`discussion_model_configuration.v1` details when active participants have empty
model IDs, unknown model endpoints, or disabled model endpoints, pause/cancel
`workflow_blocked` counts, and any orchestration records written into
`workflow_control.worker_orchestration_log`. The summary also includes
`production_handoffs` entries with schema `talkshow_production_handoff.v1`;
persisted orchestration logs use the attempt ID as `summary_id`. Each persisted
stage attempt embeds a `workflow_stage_manifest.v1` containing normalized
progress metrics, sanitized stage errors, the stage-summary checksum, and a
manifest checksum. Replaying the same attempt ID is idempotent so restart
recovery does not create duplicate stage retries.
each entry checks the selected episode's approved transcript, playable turns,
active speaker character configuration, required model/voice/visual assignments,
completed per-turn speech assets, completed per-turn primary character visuals,
shot-planned reusable reaction loops and studio-scene links, configured
localized output transcript approval/QC readiness, subtitle asset, timeline
segment links, render assets, delivery package, production manifest, and latest
claim-QC status. Handoff `status` is
`review_ready` once the talk show has a preview or final render with coherent
turn-to-media links and no blocking QC failures, and `delivery_ready` once the
final render is approved, the YouTube package has non-failing package QC, the
production manifest is valid and refreshed with the completed publish job plus
publish delivery QC, and a publish handoff job completed with publish delivery
QC. `character_configuration` reports the active playable speakers, their
model endpoint/model ID, voice profile, and visual profile readiness. Missing
participant profiles or incomplete model, voice, or visual assignments block the
handoff with `character_profile_missing`, `character_model_missing`,
`character_voice_missing`, or `character_visual_missing`, so a produced
talk-show run cannot look media-ready after a cast configuration was removed or
left incomplete. Handoff turn evidence also reports
`stale_model_turn_ids`, `stale_voice_asset_turn_ids`, and
`stale_visual_asset_turn_ids`; when a transcript source discussion turn records
an older model endpoint/model ID, or completed speech/primary character visuals
record an older profile ID than the speaker's current assignment, readiness
blocks with `character_model_turn_stale`, `character_voice_asset_stale`, or
`character_visual_asset_stale` until those turns are regenerated.
`localized_outputs` reports `localized_output_handoff.v1` evidence for
configured non-canonical output languages; missing, unapproved, missing-QC, or
failing-QC localized transcripts block handoff readiness with
`localized_output_missing`, `localized_output_not_approved`,
`localized_output_qc_missing`, or `localized_output_qc_failing`.
`character_animation`, `studio_scene`, and `timeline` handoff
details report
expected, linked, and missing shot-planned reusable reaction/studio segments;
`shot_planned_reaction_loop_missing` and
`shot_planned_studio_scene_missing` block readiness when a planned talk show
character animation or set scene is not completed and render-ready. Render
handoff details include preview/final approval state, latest render QC
status/id, delivery package QC status/id, production manifest validity, and
publish-job/QC status, so operators can distinguish a finished render asset from
an approval-ready or delivery-ready episode. Missing publish delivery QC or
blocking publish delivery QC failures, and stale production manifests that do
not embed the latest publish evidence, are reported as handoff blockers instead
of silently holding the episode below delivery readiness. The completion stage
runs the same `production_completion_readiness.v1` gate and closes the episode
only when the gate already reports `pass`; skipped readiness blockers remain
visible under `summary.stages.completion.readiness_blockers`. Each completion
attempt also records `workflow_completion_handoff.v1` with `completed` or
`blocked` status and failed gate names on the worker orchestration attempt and
active run projection. The matching selected-episode handoff is also stored
on each `workflow_control.worker_orchestration_log[].production_handoff` entry
and on `workflow_control.run.last_worker_orchestration.production_handoff`, so
operators can refresh the UI or inspect persisted workflow state without losing
the latest talk-show media readiness evidence.

This endpoint is intended for the producer UI's "Advance Workflow" action after
manual gates such as transcript approval, without scanning or mutating other
episodes. The selected episode must already have a running durable workflow run
created by `POST /api/v1/episodes/{episode_id}/workflow/start` or by the legacy
direct `/produce` path; otherwise stage execution is not admitted and the
response reports
`summary.workflow_admission.missing_run_episode_count>0`. With mock providers,
the selected episode path can advance from approved transcript to completed
speech, subtitles, render-ready character visuals, timeline, and preview render
in one pass after the run is active; a second pass creates the final render, and
a third pass after
`final_render_review` approval creates the thumbnail, YouTube export package,
production manifest, dry-run publish job, refreshed manifest publish evidence,
and automatic workflow completion when completion readiness reports `pass`.
Operators may still call `POST
/api/v1/episodes/{episode_id}/workflow/actions` with `{"action":"complete"}` for
manual closeout in deployments that are not running the workflow completion
stage.

`POST /api/v1/episodes/{episode_id}/workflow/run-until-blocked` is the
producer-facing bundled advance action. It accepts `start_if_needed`,
`max_passes` from 1 to 10, `user_id`, and an optional `comment`. When allowed, it
creates the durable workflow run, then repeatedly invokes the same selected
episode worker pass used by `/workflow/advance` until completion, a pending
human approval, stage errors, zero progress, cancellation, or the pass limit.
It never approves transcript, preview-render, final-render, research, or
localized-transcript gates. The response returns the final `episode`, `status`,
`stop_reason`, `pass_count`, accumulated `progressed_stage_count`, every worker
`summary`, current `pending_approvals`, and the current
`production_completion_readiness.v1` gate. The response also includes `handoff`,
a compact `workflow_run_until_blocked_handoff_summary.v1` derived from the
latest `talkshow_production_handoff.v1` for the episode. It carries the current
handoff status, bounded blocking reasons, compact character/turn readiness,
stage readiness flags, key render/package/publish asset IDs, and a
`next_handoff_action` such as `approve_broadcast_transcript`,
`produce_remaining_speech_assets`, `review_preview_render`, or
`review_final_render`. Each call also persists compact
`workflow_run_until_blocked_evidence.v1` under
`episode.workflow_control.last_run_until_blocked` and mirrors it on
`workflow_control.run.last_run_until_blocked` when a run is active. That durable
record keeps only stop status, pass/progress counts, pending review stages,
completion status/failed checks, the compact handoff summary, and orchestration
attempt IDs, not full worker payloads. The Web UI's **Run Until Review**
control uses this endpoint so a producer can move a show from topic definition
to the next required review without manually clicking every intermediate media
stage.

`GET /api/v1/system/workflow-orchestration` returns
`workflow_orchestration_evidence.v1`, a bounded cross-episode view of recent
`workflow_worker_orchestration_attempt.v1` and `temporal_stage_dispatch.v1`
journals. It includes the same aggregate summary as system health plus newest
attempts and dispatches for operator triage, including persisted
`production_handoff` payloads when an orchestration attempt recorded talk-show
media readiness evidence. The summary preserves historical aggregate error,
failed-stage, blocked-dispatch, and blocked-handoff counts, and also exposes
`current_error_count`, `current_failed_stage_count`,
`current_blocked_dispatch_count`, and
`current_blocked_production_handoff_count` based on each episode's newest
orchestration evidence. Live-provider readiness uses those `current_*` counts,
so resolved historical attempts stay auditable without blocking operator
preflight.
`GET /api/v1/system/workflow-retries`
returns `workflow_retry_backlog.v1`, a bounded cross-episode queue view built
from active `workflow_stage_retry.v1` entries. Retry or manual-edit recovery
marks matching scheduled or exhausted records as resolved in
`workflow_control`, preserving their audit trail while excluding them from this
active backlog. `POST
/api/v1/episodes/{episode_id}/workflow/retries/{retry_id}/resolve` is the
operator acknowledgement path for obsolete scheduled or exhausted retry records:
it marks only that retry `operator_acknowledged`, records
`workflow.stage_retry.resolved` and
`workflow.stage_retry.operator_acknowledged` journal events, writes a
`workflow.stage_retry.acknowledged` audit event, and does not reopen or rerun the
episode. Future retry attempts still use the persisted per-stage history for
cumulative attempt numbering, including resolved records, and the local workflow
journal records `workflow.stage_retry.resolved` evidence for the operator or
automatic action. Replayed worker orchestration attempts with the same
`summary_id` do not add another `workflow_stage_retry.v1` entry. `POST
/api/v1/episodes/{episode_id}/workflow/advance`
also consumes due scheduled retries for the selected active run before executing
stage workers: an elapsed `next_retry_not_before` reopens the target stage,
marks that specific entry `automatic_retried`, records
`workflow.stage_retry.automatic_retry_requested`, and reports
`automatic_stage_retries` in the worker summary. The summary carries active
backlog totals separately from historical and resolved retry counts. It includes the same aggregate retry
summary as system health plus entries sorted by schedule readiness: due now,
still in backoff, unknown schedule, then exhausted/non-scheduled entries.
Live-provider readiness includes the same retry summary so scheduled retries are
visible before live runs and exhausted retry budgets block operator preflight. `GET
/api/v1/episodes/{episode_id}/workflow/replay` returns
`workflow_replay_report.v1` with a replayed run projection, current run
projection, signal projection, event count, event-log checksum, and mismatch
issues. Signal replay includes safe manual-edit evidence fields such as schema,
event count, event-type counts, and evidence checksum, and reports a mismatch if
the replayed signal projection diverges from the active run projection. `GET
/api/v1/episodes/{episode_id}/workflow/completion-readiness` returns
`production_completion_readiness.v1` with the approved canonical broadcast
transcript, active speaker model/voice/visual configuration, per-playable-turn
speech and primary visual coverage, configured localized output transcript
approval/QC readiness, subtitle and timeline coverage,
expected/linked/missing shot-planned reusable reaction-loop and studio-scene
segments, matching subtitle synchronization and
timeline-integrity QC, passing discussion-structure QC with required topic
dimension coverage, approved preview render and preview QC for that transcript timeline,
final-render linkage to that transcript timeline, matching speech and visual
media-integrity QC, final-render QC, checked final-render thumbnail, optional
research evidence-pack and claim-QC gates, final-render, package,
production-manifest, preview/final-render approval, package thumbnail inclusion,
package subtitle/caption inclusion, package QC, production-manifest validity,
completed publish-job, publish delivery QC, unresolved failed-asset, and latest
QC gates that must pass before workflow control can record `COMPLETED`. The
payload includes the episode's
`quality_completion_policy.v1`, transcript status, research-required flag,
discussion-structure QC status and missing topic dimensions,
playable-turn count, completed speech/visual turn counts, missing media turn
IDs, `character_configuration_handoff.v1` evidence with
`character_profile_missing`, `character_model_missing`,
`character_voice_missing`, and `character_visual_missing` failed-check names
when the approved transcript's speakers are no longer fully configured,
`stale_model_turn_ids`, `stale_voice_asset_turn_ids`, and
`stale_visual_asset_turn_ids` plus `character_model_turn_stale`,
`character_voice_asset_stale`, and `character_visual_asset_stale` failed-check
names when transcript/media provenance records an older assignment,
`localized_output_readiness.v1` evidence with `localized_output_missing`,
`localized_output_not_approved`, `localized_output_qc_missing`, and
`localized_output_qc_failing` failed-check names when configured non-canonical
language outputs are not ready for production,
subtitle/timeline asset IDs, audio/visual media QC statuses,
subtitle/timeline QC statuses, preview-render asset/QC/approval status,
final-render timeline-link status, final-render QC, thumbnail asset/QC status,
package thumbnail/subtitle inclusion status, evidence-pack QC, claim/citation
QC, package-QC, manifest-validity, publish-job, and publish-delivery-QC evidence, and
separates blocking failures from nonblocking failed assets/QC rows allowed by
the production definition. It also includes `resolved_failed_assets` entries for
failed assets that were manually replaced, including replacement asset ID,
status, checksum/storage evidence, replacement reason, and whether the
replacement is completed and storage-backed. Dry-run publish delivery QC is
reported as nonblocking evidence, while live publish delivery failures block
completion. Workflow orchestration handoff evidence uses the same delivery
package checks, so `delivery_ready` is not reported unless the package and
embedded production manifest carry the required checked thumbnail and
subtitle/caption entries.
`GET
/api/v1/system/health` also includes `database_migrations`, a
`database_migrations_readiness.v1` component that compares the running
database's recorded Alembic revision with the current migration head. Missing or
behind revisions are informational during development but fail live-provider
readiness in production because self-hosted persistent databases must match the
app image before production work starts. `GET
/api/v1/episodes/{episode_id}/status` includes `workflow_paused`,
`workflow_cancelled`, `retry_available`, and the full `workflow_control` block.
When `DIALECTICORE_TEMPORAL_BACKEND_MODE=external`, workflow-worker
orchestration passes append `workflow_control.temporal_stage_dispatch_log`
entries with schema `temporal_stage_dispatch.v1`. Each dispatch record includes
run and episode IDs, dispatch sequence, source orchestration summary, stage,
target episode status, activity name, namespace, task queue, readiness status,
missing runtime settings if blocked, and an idempotency key for native Temporal
worker pickup. When the Docker `temporal-worker` role is running in external
mode, its heartbeat details include `temporal_worker_execution_summary.v1` with
one `temporal_stage_activity_execution.v1` activity summary for each production
stage, and running episodes receive normal worker orchestration, retry, replay,
and dispatch evidence from that pass.

`POST /api/v1/episodes/{episode_id}/thumbnails/generate` extracts a JPEG
thumbnail from a completed render. The request accepts optional `render_asset_id`,
`user_id`, and `regenerate`. The response is the updated episode with a completed
`thumbnail` asset, `thumbnail_integrity` QC, and thumbnail audit events.

`POST /api/v1/episodes/{episode_id}/youtube-package/export` exports a YouTube
delivery ZIP from the latest final render, or a supplied `render_asset_id`.
The request accepts optional `render_asset_id`, optional `thumbnail_asset_id`,
`user_id`, `regenerate`, and `allow_preview_render` for non-production test
exports. Production final-render exports require an approved
`final_render_review` approval targeted at the selected render asset; otherwise
the endpoint returns `422`. Preview exports are allowed only when
`allow_preview_render=true`. The ZIP contains `youtube-package.json`,
`video/render.mp4`, the matching thumbnail, and subtitle files for completed
subtitle assets when they are available from object storage or embedded subtitle
text metadata. Production completion blocks until the selected package records
subtitle/caption inclusion for transcripts with generated subtitles.
The manifest preserves the render evidence-lineage block and normalized timeline
chapters, including YouTube timecodes, for delivery audit. The response includes
a completed `export_package` asset plus `youtube_package_integrity` QC with
included-file, subtitle-file, chapter, and evidence-lineage counts. If the
rendered timeline has chapters but the package manifest omits or changes those
chapter entries, package QC fails.

`POST /api/v1/episodes/{episode_id}/production-manifest` writes a
machine-readable `production_manifest` asset for the latest completed
`export_package` asset, or a supplied `package_asset_id`. The request accepts
optional `package_asset_id`, optional `render_asset_id`, `user_id`, and
`regenerate`. Generation requires the latest `youtube_package_integrity` QC for
the selected package to be present and non-failing. The stored
`production_manifest.v1` JSON records the episode definition, participants,
model endpoints, workflow control and audit counts, transcript summaries,
including localization metadata and per-turn transcript-to-discussion lineage,
timeline segment lineage, normalized timeline chapters, embedded render
manifest, embedded YouTube package manifest, asset checksums/storage URIs, QC
results, approvals, package-linked publish jobs, and evidence lineage. Timeline
segment entries include
secondary, reaction-loop, and studio-scene asset IDs, and the top-level
`talkshow_visuals` block records expected, linked, and missing shot-planned
reusable reaction/studio segment counts for final cast/set continuity audit.
Manifests that contain reusable reaction/studio segment links but omit this
ready handoff block are treated as invalid by completion readiness, system
health, and live publishing.
The embedded `delivery_package.asset_id`
must be present and must match the selected export package for completion,
health, and live-publish readiness. For YouTube-ready delivery, the embedded
delivery-package manifest and included-file list must also carry the selected
thumbnail asset, `thumbnail/thumbnail.jpg` package entry, and non-empty
subtitle/caption manifest plus `subtitles/*` package entries when the transcript
has generated subtitles. When the manifest timeline has chapters, the embedded
delivery-package manifest must carry matching chapter entries; otherwise
completion readiness, System Health, and worker handoff report the production
manifest as invalid. Once publishing has
produced a completed job and publish-delivery QC, the publishing workflow
regenerates the package manifest so the active `production_manifest` also embeds
that final publish evidence. Existing manifests for the same export package are
preserved as `replaced` when regenerated.
Asset generation metadata, model endpoint capabilities, QC-result details, and
publish-job result and delivery snapshots embedded in the manifest are
recursively redacted for token, secret, password, API-key, authorization, and
credential fields.
Workflow completion readiness, publish readiness, and live publishing accept
only structurally valid package-linked `production_manifest.v1` assets. When the
manifest embeds package checksum, storage URI, or package ID evidence, those
values must match the selected completed export package, preventing an older
manifest from authorizing a replaced or mutated package. Invalid completed
manifest assets are reported in publish-job readiness and Prometheus
package-manifest counts. Completed export packages without
`youtube_package_integrity` QC, or with latest failing package QC, are also
reported as live readiness blockers and package-manifest metrics.

`GET /api/v1/publisher-targets` lists configured delivery targets. `POST`,
`PUT`, and `DELETE` manage target records through the same contract used by the
Web UI. Target records include platform, adapter type, base URL, channel ID,
privacy status, default language, tags, retry policy, enabled state, health
status, capabilities, and credential references only; raw secrets are not
accepted or returned. The default target is `mock-youtube`, which records a
dry-run YouTube delivery payload without uploading to a live account.
Provider-supplied target-health capability metadata is recursively redacted for
token, secret, password, API-key, authorization, and credential fields before it
is persisted or returned. Target `base_url` values with URL username/password
userinfo are rejected; credentials must be provided through
`credential_reference`. Top-level credential references must use `scheme:target`
syntax, and secret-shaped capability fields are redacted on write while
reference-shaped OAuth capability fields remain available.
Targets using `http`, `http_upload`, or `generic_http` can point at an external
delivery service with `base_url`, optional bearer-token `credential_reference`,
and capability overrides such as `delivery_path` or `health_path`. Targets using
`youtube_resumable` require either a credential reference that resolves to a
YouTube OAuth bearer token or refresh-token capabilities
(`oauth_refresh_token_reference`, `oauth_client_id_reference`, and
`oauth_client_secret_reference`) that resolve through the configured
`oauth_token_url`. The seeded disabled `youtube-resumable` target documents the
default Google API paths for OAuth refresh, resumable video, thumbnail, and
caption upload. The publishing worker creates dry-run publish jobs by default.
Publisher job metadata records request endpoint posture as scheme/path/query-key
evidence and OAuth credential availability by reference scheme/configuration
only; it does not persist raw delivery endpoints, resumable session URIs,
OAuth token URLs, or credential-reference targets in publish-job result
metadata. Actual outbound requests still use the configured target URLs and
credential references at execution time.
Live automated worker publish requires
`DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED=true` and target capability
`automated_live_publish=true`; explicit API/UI publish requests can still choose
`dry_run=false` for an enabled live target, but non-mock live delivery now also
requires a completed `production_manifest` asset linked to the selected export
package. Publishing-worker heartbeats include dry-run/live counts,
`production_manifests_created`, `production_manifests_refreshed`, plus
`package_qc_blocked_handoffs`, `production_manifest_blocked_handoffs`, and
per-error `error_kind` evidence when package QC or manifest gates block
handoff. The live-provider readiness
`publisher_targets` check reports enabled, live, and automated-live-capable
target counts and warns on enabled targets whose health is still `unknown`.
It fails if global automated live publishing is enabled without an enabled
`automated_live_publish` target.
`POST /api/v1/publisher-targets/{target_id}/health` marks mock targets healthy,
probes HTTP delivery targets, and verifies configured YouTube resumable targets
through the YouTube channels endpoint, merging returned capability metadata when
the health response is JSON.

`POST /api/v1/episodes/{episode_id}/publish` creates a publish job from the
latest completed `export_package` asset or a supplied `package_asset_id`. The
`mock` adapter records the exact title, description, tags, language, video URI,
thumbnail URI, subtitle count, chapters, package checksum, and evidence-lineage
block that would be submitted to YouTube. HTTP delivery targets send that same
payload to `base_url + delivery_path`; when the export package has a local
`object_storage_path`, the adapter sends multipart form data with the JSON
payload and package file, otherwise it sends JSON metadata only. Non-mock
targets support `dry_run=true` without network delivery. Non-dry-run delivery is
blocked until the selected package has non-failing `youtube_package_integrity`
QC and a completed production manifest; the delivery payload includes the
manifest asset ID, URI, checksum, and schema version as safe audit evidence. The
`youtube_resumable`
adapter resolves an OAuth access token directly or by refresh-token exchange,
records safe OAuth source/refresh metadata, reads `video/render.*` from the
local YouTube package ZIP, posts video metadata to the YouTube
resumable-upload endpoint, persists the returned session URI in job metadata,
uploads the video bytes with `PUT`, and records the returned YouTube video
ID/watch URL. After the video ID is available, it uploads `thumbnail/*` bytes to
the YouTube thumbnail endpoint and uploads each `subtitles/*` entry to the
YouTube captions endpoint with `snippet.videoId`, language, name, and draft
metadata. Upload status codes, response payloads, entry names, content types,
byte sizes, and aggregate caption counts are persisted in publish-job result
metadata and
`publish_delivery_integrity` QC. That QC row records the payload package asset
ID, package-checksum match state, production-manifest asset ID, manifest checksum
presence, and schema version carried in the delivery payload, and fails live
non-mock payloads that do not match the selected package or lack a valid
manifest handoff. Live HTTP and YouTube responses populate the publish job's
remote job ID, publish URL, status, and result metadata. Failing
`youtube_package_integrity` QC, whether represented by `status=fail` or
`severity=fail`, blocks publishing, and missing package QC or missing production
manifests block live non-mock publishing before
any external request is made.
`GET
/api/v1/episodes/{episode_id}/publish-jobs` returns the persisted publish jobs
stored with the episode. `GET /api/v1/system/health` counts non-replaced
publish jobs by submitted, completed, failed, dry-run, and live state, and the
same counts are available in system snapshots and Prometheus metrics.

`POST /api/v1/episodes/{episode_id}/research/build` creates the initial
`evidence_pack` asset for an episode. The request accepts
`user_id`, `regenerate`, optional `require_approval`, and optional supplied
`sources` and `retrieval_targets`. It also accepts `discover_sources` and
optional `discovery_queries` for operator-configured source discovery. Supplied
source entries include title, URI, source type, publication metadata,
confidence hint, summary, and source text. Retrieval targets include an
explicit HTTP(S) URL plus source metadata; the research service fetches the URL,
extracts text from plain text, JSON, or simple HTML payloads, records structured
`retrieval_tool_log` entries in asset metadata, and skips unsupported URI
schemes or failed fetches with logged errors. When discovery is enabled and a
discovery URL template is configured, the service sends topic or
producer-provided discovery queries to that endpoint, extracts result URLs from
JSON or simple HTML, records `discovery_tool_log`, and fetches discovered URLs
through the same retrieval path with query/rank provenance. The research service
de-duplicates sources, scores them, records content checksums, ranks them with
the `confidence_authority_recency_checksum_v1` policy, and extracts
source-linked claims/statistics plus structured verified facts and competing
interpretations from relevant source text. Structured extraction uses the
`deterministic_fact_patterns_v1` policy for definitions, mechanisms,
recommendations, and tradeoff/comparison statements. It also records
source-grounded relationship/quantity facets with
`deterministic_relation_quantity_facets_v1`, including normalized subject,
relation, object, optional quantity, source ID, and claim ID. It records
source-bound causal/scope context records with
`deterministic_causal_scope_context_v1`, preserving cause/effect connectors and
scope qualifiers with source ID and claim ID provenance. When configured, the
service calls a trusted advanced extraction gateway, accepts only
source-bound claims, and records `source_bound_external_extractor_v1` tool-log
counts for attempts, accepted claims, and rejected untrusted claims. The service
records deterministic cross-source agreement/conflict summaries using shared
topical terms, stance signals, and matching claim facets across distinct
external sources with `deterministic_claim_facet_relationships_v1`.
Evidence-pack metadata includes source-ranking count, strong-source count,
discovery/retrieval counts, advanced extraction counts, agreement/conflict
counts, facet agreement/conflict counts, cross-source cluster count,
claim-facet count, causal/scope context counts, claim support-group counts using
`deterministic_claim_support_groups_v1`, and the highest-ranked source ID. The
response includes the stored evidence pack, `evidence_pack_integrity` QC, audit
events, and a `research_review` approval when approval is required. Required
research review approval must be approved before discussion generation can
begin; rejected review decisions require rebuilding or revising the evidence
pack before the talk show can advance.
`GET
/api/v1/episodes/{episode_id}/research` returns the latest active evidence-pack
asset and JSON payload. `GET
/api/v1/episodes/{episode_id}/research/sources` returns the persisted
`ResearchSource` projections for the latest saved evidence pack, including
source ID, episode ID, URL, title, publisher, published date, retrieval time,
content hash, source type, credibility score, and metadata linking the row back
to the evidence-pack asset. `GET
/api/v1/episodes/{episode_id}/research/claims` returns persisted
`EvidenceClaim` projections with statement, claim type, confidence, status,
supporting and contradicting source IDs, notes, and extraction metadata. `POST
/api/v1/episodes/{episode_id}/research/source-review` records a human review
decision for one source (`approved`, `rejected`, or `needs_revision`), stores it
in the evidence-pack metadata, updates `human_source_review_v1` summary counts,
records `research_source_review_integrity` QC, and emits
`research.source_review.recorded` audit evidence. `POST
/api/v1/episodes/{episode_id}/research/claim-qc` checks transcript claims
against evidence source IDs and records `claim_citation_integrity` QC. The
automatic `qc-worker` waits for an approved canonical/broadcast transcript
before running this check; callers can still target a specific transcript
version explicitly for manual review.

Episode status responses include estimated discussion duration and speaker
balance. Full episode records include `discussion_minimum_structure` QC details
with required-dimension coverage state, missing dimensions, configured speaker
IDs, represented speaker IDs, and missing speaker IDs. `speaker_count` is the
number of non-excluded speakers who actually received a turn;
`configured_speaker_count` is the selected cast size. Full episode records also
include `discussion_duration_control` QC results that document target/min/max
runtime compliance and any hard duration controls applied to turns. Each
discussion turn's generation metadata also includes
`discussion_turn_coverage.v1`, so operators can audit which turn first satisfied
a topic dimension and coverage is rebuilt after regeneration or exclusion edits.

The Web UI uses the same `POST /api/v1/episodes` contract to create custom
episodes. The editor submits title, central question, duration, monologue cap,
discussion control settings, required dimensions, output languages, language
fidelity/new-claim policy, research policy, media dimensions and generation
flags, workflow retry settings, quality gates, one host profile, and distinct
panelist profiles. The default editor state creates a six-character
frontier-model panel. Episode creation applies the submitted host/panelist
assignment roles to the episode-local cast without changing the reusable
participant profile defaults. The dashboard can create localized transcripts,
plan audio assets, generate audio assets, generate subtitles, plan visual
assets, inspect and administer Voicebox endpoint/profile configuration, inspect
and administer ComfyUI endpoint/workflow and visual profile configuration,
generate visual assets, sync or cancel remote ComfyUI jobs, run Voicebox and
ComfyUI health checks, cancel or sync remote Voicebox jobs, build evidence
packs, approve research, run claim QC, and polls the global audit stream so
recent production and configuration changes are visible without opening an
episode.
