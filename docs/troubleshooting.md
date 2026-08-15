# Troubleshooting

Use `/api/v1/system/health` to inspect API readiness, repository/database
reachability, database migration revision drift, local object-storage path state
or S3/MinIO endpoint reachability, bucket configuration, credential-reference coherence, FFmpeg/ffprobe
availability, configured provider endpoint counts, paused/cancelled/failed
episode counts, pending audio/visual jobs, failed assets, backup archive
readiness, deployment readiness posture, workflow orchestration/dispatch
evidence, workflow retry backlog counts, production-run attention counts, and
worker heartbeat counts. Remote
provider health is also available through the Voicebox and ComfyUI endpoint
health routes. A degraded `deployment_readiness` component in production means
one or more production posture checks failed: persistent database driver,
database URL/password-reference resolution, inspected schema-at-head status,
enabled/configured auth mode,
restricted API CORS origin, S3-compatible object storage, Redis fan-out and
worker-signal runtime, backup path, or runtime-state path. If
`database_url_resolved` is false, inspect
`database_resolution_error` and the credential provisioning entry for
`database_password_reference`; the API reports the failing reference label
without returning the password. Check the component's `issues` list before
treating lower-level components as independent incidents.
A degraded `database_migrations` component means the API could not inspect the
Alembic revision or the database revision does not match the current migration
head. In production, an inspectable missing or behind revision also fails the
`database_schema_at_head` deployment-readiness gate and blocks live-provider
readiness. Run `alembic upgrade head` against the configured database, then
recheck the component's `current_revisions`, `head_revisions`, and
`failed_readiness_checks`.
A degraded `runtime_paths` component means the backup directory,
runtime-state directory, or active local object-storage directory is not
configured, has no existing parent, has no writable target or parent, or is
below the configured `DIALECTICORE_RUNTIME_PATH_MIN_FREE_BYTES` floor. Compare
its per-path `checked_path`, `parent_exists`, `writable_target_or_parent`, and
`free_bytes` / `free_bytes_sufficient` fields before restarting workers; missing
runtime-state volume writes or exhausted capacity can look like worker liveness
or signal-delivery failures.
A degraded `credential_references` component means at least one active
`env:`, absolute `file:`, or `docker-secret:` reference used by enabled
integrations or active settings could not be resolved. Inspect the failing
reference labels and owner fields, then fix the container environment, mounted
secret file, or Docker secret before re-running endpoint health checks or media
jobs.
Use `/api/v1/system/live-provider-readiness` or the Web UI Live Provider
Readiness panel before a live production run. The report separates hard blockers
from warnings across model providers, remote Voicebox, remote ComfyUI,
publisher targets, object storage, backup storage, Redis, auth, Temporal
runtime, worker registry, worker signal state, workflow orchestration, workflow
retry backlog, production-run state, media queues, publish jobs, production
posture, runtime paths, and credential references without exposing raw secrets.
Runtime path failures in this report should be fixed before retrying workers or
remote media jobs, because they affect backup creation, runtime-state
coordination, and local object-storage writes.
Backup-storage warnings in the same report mean operators should create and
dry-run validate a backup before a live run, but they are not treated as missing
runtime dependency blockers.
Use `scripts/live_provider_smoke.py` when the question is whether a real-life
audio/model pilot can run now. It exercises OpenRouter and B1 Voicebox through
the configured records, writes a WAV under `output/smoke/`, and reports ComfyUI
admission separately so a blocked visual provider does not hide a working
audio-first path. Add `--all-participant-models --all-participant-voices` plus
`--frontier-cast` before a real frontier-model talkshow run to prove
every intended character model can answer and every intended character voice
can produce a non-empty RIFF/WAVE response. Use `--participant-ids` only for
custom casts. Per-participant failures are written to `model_participants` and
`voicebox_participants` evidence; Voicebox failures can be appended to
`/home/mordred/media-requirements.md`.
The Dashboard `Live Provider Readiness -> Check Cast` action calls
`POST /api/v1/system/live-provider-preflight` for the same frontier cast. It
returns `model_summary`, `voicebox_summary`, and `blocking_sections` without
creating an episode. If the UI shows `Models 6/6 passed` and `Voices 0/6
passed`, the model discussion path is available and the remaining blocker is
B1 Voicebox generation. In local Vite development, this long action connects
directly to `http://127.0.0.1:8000` when no `VITE_API_BASE_URL` is configured
so the dashboard's polling and SSE requests cannot starve the preflight POST
behind the Vite proxy.
The same panel's **B1 Smoke** action calls
`POST /api/v1/system/b1-managed-media-smoke`. Use it after B1 media fixes to
submit a small `image-default` managed-media job, write fresh
`b1_managed_media_smoke_evidence.v1`, append
`/home/mordred/media-requirements.md` when the appliance still fails, and refresh
live-provider readiness plus audit evidence from the browser.
Use `scripts/live_episode_smoke.py` when the question is whether the whole
episode workflow can run through discussion, transcript approval, speech,
visual fallback, subtitles, timeline, renders, packaging, production manifest,
and dry-run publish. It writes a stable JSON evidence file under
`output/smoke/`. Add `--provider-smoke-preflight` when you want the harness to
stop before workflow start unless every selected cast model and voice passes
the same real provider checks.
`output/smoke/` by default and prints that file's SHA-256 checksum. Check
`pilot_readiness.production_target`, `status`, `target_status`, and
`selected_pilot_mode` for the episode's declared target. `audio_first` can pass
or warn when real discussion, speech, and rendering are runnable, while
`native_visual` fails until ComfyUI-backed character animation is available.
Use `--production-target native_visual` for a full native visual acceptance
attempt, and use the default `audio_first` target only when fallback-backed
video is an acceptable pilot result. For transient B1 GPU admission failures,
add `--wait-native-visual-admission-seconds <seconds>` so the smoke keeps
refreshing ComfyUI endpoint health and pilot readiness before failing preflight;
the evidence file records the bounded wait under
`native_visual_admission_wait`.
When troubleshooting a completed audio-first smoke while native visuals remain
blocked, inspect `pilot_readiness.all_stage_blockers` and `pilot_modes` for the
native visual blockers. Use `completion_readiness.visual_source_summary` to
confirm how many primary turn visuals were native, fallback, or missing in the
completed handoff. If `workflow.production_target=native_visual`, fallback
primary visuals produce `native_primary_visuals_missing`; switch to
`audio_first` only when a fallback-backed pilot is the intended target.
For B1 native ComfyUI admission failures, inspect the `visuals` stage
`details.prompt_admission_blocked_endpoints` in the episode pilot-readiness
response. It maps the blocked endpoint to affected participant IDs, workflow
IDs, visual profile IDs, and the redacted admission code/detail so operators can
distinguish a character-configuration issue from B1 scheduler/GPU admission.
Worker-registry warnings mean active heartbeats are missing, stale, degraded, or
do not cover all configured worker roles. The raw worker status summary also
reports `degraded` for partial role coverage; compare
`/api/v1/system/workers`, container status, and runtime-state volume mounts
before starting a production run.
Failed worker heartbeats with `schema_version=unsupported_worker_role.v1` mean a
container was started with an unsupported `DIALECTICORE_WORKER_ROLE`. Fix the
role value to one of the listed supported roles; the worker exits non-zero after
recording this heartbeat so supervisors can surface the configuration error.
If `malformed_heartbeats` or `malformed_leases` is non-zero in the worker
summary, inspect the shared `DIALECTICORE_RUNTIME_STATE_PATH` worker heartbeat
or worker-lease JSON files for interrupted writes or manual edits. Worker-owned
malformed files are counted and pruned after
`DIALECTICORE_WORKER_RUNTIME_STATE_RETENTION_SECONDS`; signal registries and
other diagnostic files are left untouched.
If `pruned_expired_leases` increases without matching malformed counts, normal
lease-retention cleanup is working; investigate only if active workers are also
missing leases or duplicate remote-job sync passes are occurring.
Worker-signal blockers mean a failed signal delivery is recorded or an active
`drain`/`stop_after_current` control is still the latest applicable signal.
Post a `resume` signal or resolve the failed delivery evidence before starting
a live production run.
Workflow-retry warnings in live readiness mean scheduled retry work is still due
or waiting in backoff; inspect `/api/v1/system/workflow-retries` before starting
a live run. Workflow-retry blockers mean one or more retry entries exhausted the
configured attempt budget and need operator triage or an explicit failed-stage
retry before production can be trusted.
Workflow-orchestration blockers in live readiness mean recent
`workflow_worker_orchestration_attempt.v1` or `temporal_stage_dispatch.v1`
journals contain stage errors or blocked handoffs; inspect
`/api/v1/system/workflow-orchestration`, worker logs, and Temporal backend
settings before starting or resuming a live run.
Production-run warnings in live readiness mean at least one active run is
paused or another run is already active; inspect the episode status
`workflow_control.run`, resolve pause/resume intent, and let the active run
finish before starting a new live production. Production-run blockers mean a
failed or cancelled active run still needs operator triage.
Publish-job warnings in live readiness mean one or more publish jobs remain
submitted; publish-job blockers mean at least one non-replaced publish job
failed. Inspect the episode-level publish jobs before starting or resuming a
live run.
Publisher-target blockers in live readiness can also mean automated live
publishing is globally enabled while no enabled target declares
`automated_live_publish=true`. Disable
`DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED` or enable and health-check the
intended live-capable target before running the publishing worker.
For headless checks, compare the `publisher_targets` health component with
`dialecticore_publisher_target_health_status` and
`dialecticore_publisher_target_count{kind="automated_live_capable_enabled"}`.
An issue count above zero means publisher target configuration should be fixed
before relying on automated live delivery. Enabled targets with
`health_status=unknown` also increment issues and should be health-checked
before relying on live delivery.
Media-queue warnings mean audio, visual, subtitle, or remote audio work is still
pending or running. Media-queue blockers mean one or more assets are failed;
inspect the affected episode assets and QC evidence before starting or resuming
a live run. Historical `failed_*` and pending counters include terminal
cancelled or failed episodes for audit, while `current_failed_*` and
`current_pending_*` counters drive live readiness.
For B1 native ComfyUI, a healthy `/object_info` read does not prove render
admission. Inspect `prompt_admission_ready` and `prompt_admission_probe` on the
endpoint health record; a `hardware_resource_policy` 503 means the appliance is
reachable but the GPU scheduler is refusing `/prompt` work, for example because
free VRAM is below the configured reserve. DialectiCore does not treat this as
a local configuration failure and does not attempt to clear the appliance GPU:
the B1 gateway can deny mutating compatibility routes such as `/free` with
`comfyui_route_denied`. Clear or restart the conflicting runtime on the B1
appliance side, wait for enough free VRAM to exceed the configured reserve, then
rerun the ComfyUI endpoint health check before starting a real visual smoke. The
Live Provider Readiness panel shows a compact `Refresh <endpoint>` action for
unhealthy or unknown ComfyUI endpoints so operators can recheck admission from
the dashboard after B1 resources are freed.
Use `/api/v1/system/credential-provisioning` or the Web UI Credential
Provisioning panel when a blocker is a missing secret. The plan lists the exact
`env:`, `docker-secret:`, and `file:` targets currently referenced by settings
and integration records, including disabled live targets unless
`include_disabled=false` is supplied. For headless monitoring, scrape
`dialecticore_credential_provisioning_count`; active unavailable references are
runtime blockers, while inactive unavailable references identify secrets to
provision before enabling disabled live targets. Unsupported and invalid counts
separate unimplemented secret-manager prefixes from malformed raw pasted values.
A degraded
`workflow_orchestration` component means the newest coordination evidence for at
least one episode has unresolved errors, failed stages, blocked external
Temporal dispatches, or blocked production handoffs. Historical aggregate
counts remain in the payload for audit, but live readiness is driven by the
`current_*` counts. Terminal cancelled or failed episodes are excluded from
those current counts, so stale abandoned attempts should be cancelled rather
than allowed to masquerade as active blockers. Inspect `latest_attempt`,
`latest_dispatch`, `by_worker`, `by_dispatch_status`, and
`GET /api/v1/system/workflow-orchestration` before restarting workers. A
degraded `workflow_retries` component means one or more
episodes has scheduled or exhausted `workflow_stage_retry.v1` entries; inspect
`due_retry_entries`, `backoff_retry_entries`, `by_schedule_status`,
`next_retry`, and `GET /api/v1/system/workflow-retries` before restarting
workers. Due entries indicate retry work is ready for a worker pass; backoff
entries should normally wait until `next_retry_not_before`; exhausted entries
also appear as live-provider readiness blockers. Current unresolved orchestration
errors and blocked dispatches also appear as live-provider readiness blockers. A degraded
`production_runs` component means active run control state needs operator
attention; inspect `active_production_runs`, `paused_active_production_runs`,
`failed_active_production_runs`, `latest_run`, and the selected episode's
`workflow_control.run` before starting another live run. `attention_count` is a
deduplicated run count; a single run can still appear under multiple
`by_attention_reason` entries, such as paused and failed. A degraded
`object_storage` component for a local backend means the checked path or parent
does not exist, is not a directory, or is not writable. For an S3-compatible
backend, it means the endpoint TCP probe failed, the bucket is not configured,
the access/secret credential references are inconsistent, or the configured
bucket failed the safe `head_bucket` probe. A degraded `backup_storage`
component means
the backup path or parent is unavailable, no backup archive exists yet, or the
latest archive manifest cannot be read. Use `/api/v1/system/backups` to list
available archives and `/api/v1/system/backups/restore` with `apply=false` to
validate an archive without changing state. The validation response includes a
`backup_restore_plan.v1` block and writes `backup.restore_validated` audit
evidence, so compare target scope, record count, and file count before setting
`apply=true`. If restore validation reports `checksum_mismatch`, the archive was
modified or replaced after its dry-run validation; validate the current archive
again before relying on it. The Web UI Backups panel shows the same
`backup_storage` reason,
archive count, latest manifest readability, latest archive age, latest
restore-validation health status, and latest restore-validation summary for
operators who are not scraping the API directly.

Use `/api/v1/system/workers` when a worker container appears stuck. A stale
heartbeat means the API can still read the registry, but that role has not
updated its JSON heartbeat within `DIALECTICORE_WORKER_HEARTBEAT_TTL_SECONDS`.
An active lease means one worker owns the current polling pass for that role; a
second scaled adapter should show heartbeats with `lease_skipped: true` until
the owner renews or the lease expires. An expired lease means the previous owner
stopped renewing within `DIALECTICORE_WORKER_LEASE_TTL_SECONDS`, and another
worker can take over the next pass. Very old stale heartbeat records and expired
leases are pruned after
`DIALECTICORE_WORKER_RUNTIME_STATE_RETENTION_SECONDS`, so use backups or older
logs for long-term incident reconstruction.
If all workers are missing, verify that the `runtime-state` volume is mounted in
both `production-api` and worker containers and that `DIALECTICORE_RUNTIME_STATE_PATH`
matches across services. `/api/v1/system/metrics` exposes the same counters as
plain text for external monitoring. Use
`dialecticore_component_readiness_check` to alert on normalized component gates
by component name, check name, and pass/fail status when the JSON health payload
is too heavy for the monitoring path. For S3/MinIO deployments,
`dialecticore_object_storage_remote_reachable` exposes endpoint TCP reachability.
For Redis-backed fan-out or worker-signal incidents, inspect the `redis` health
component and compare `dialecticore_redis_runtime_reachable` with
`dialecticore_redis_runtime_enabled`; enabled modes with reachability `0` mean
the API cannot open the configured Redis TCP endpoint. Reachability `1` with a
degraded Redis component means the transport is open but a normalized Redis gate
still failed, such as a blank event channel or worker-signal stream name.
Backup metrics
`dialecticore_backup_archive_count`,
`dialecticore_backup_archive_validation_count`,
`dialecticore_backup_latest_archive_info`,
`dialecticore_backup_latest_age_seconds`, and
`dialecticore_backup_latest_size_bytes`,
`dialecticore_backup_latest_restore_validated`, and
`dialecticore_backup_latest_restore_validation_age_seconds`, plus
`dialecticore_backup_latest_content_validation` for object-storage/runtime-state
archive-member validation, should be present when scraping the API.
Deployment readiness metrics
`dialecticore_deployment_readiness_status`,
`dialecticore_deployment_readiness_issues`, and
`dialecticore_deployment_readiness_check` should be present when scraping the
API; in production, any failed readiness check should be resolved before
debugging symptoms from scaled workers or remote media services.
`dialecticore_deployment_readiness_check{check="database_url_resolved",status="fail"}`
means the API could not assemble the configured database URL, usually because a
database password reference is missing or unreadable.
`dialecticore_deployment_readiness_check{check="cors_origin_restricted",status="fail"}`
means `DIALECTICORE_CORS_ALLOWED_ORIGINS` still allows `*`; set the exact Web UI
or reverse-proxy origin before exposing production browsers.
`dialecticore_deployment_readiness_check{check="worker_heartbeat_ttl_covers_poll_interval",status="fail"}`
means `DIALECTICORE_WORKER_HEARTBEAT_TTL_SECONDS` is not greater than
`DIALECTICORE_WORKER_POLL_INTERVAL_SECONDS`; raise the heartbeat TTL or lower the
poll interval before relying on Docker worker healthchecks.
`dialecticore_deployment_readiness_check{check="worker_lease_ttl_covers_poll_interval",status="fail"}`
means `DIALECTICORE_WORKER_LEASE_TTL_SECONDS` is not greater than
`DIALECTICORE_WORKER_POLL_INTERVAL_SECONDS`; raise the lease TTL or lower the
poll interval before scaling duplicate worker roles.
Runtime path metrics `dialecticore_runtime_path_ready` and
`dialecticore_runtime_path_state` should also be present; a required path with
readiness `0` means the API cannot rely on that filesystem location for
backups, worker coordination state, or local media storage. Use
`dialecticore_runtime_path_state` to distinguish an unconfigured path, missing
parent, non-directory checked path, unwritable checked path, or insufficient
free space; `dialecticore_runtime_path_free_bytes` reports the available bytes
when a checked path exists.
For local filesystem object storage, compare
`dialecticore_object_storage_local_path_ready` with
`dialecticore_object_storage_local_path_state`; readiness `0` with
`state="writable_target_or_parent"` at `0` means the configured object-storage
path or its checked parent is not writable by the API process.
Credential metrics
`dialecticore_credential_reference_count{dimension="status",value="unavailable"}`
should remain `0` for active integrations. Owner-type and scheme breakdowns help
distinguish missing service-specific tokens from broader environment or secret
mount problems. A nonzero
`dialecticore_credential_reference_count{dimension="scheme",value="unsupported"}`
means a configured reference uses a prefix outside `env:`, `file:`, or
`docker-secret:` and must be changed or implemented before readiness can pass.
For provider-session incidents, inspect the `auth_runtime` health component and
compare `dialecticore_auth_provider_session_count{kind="active_revocations"}`
with the Security panel revocation list and
`dialecticore_auth_provider_session_count{kind="denied_decisions"}` before
changing identity-provider or reverse-proxy settings.
For publish incidents, compare `/api/v1/system/health` counts with
`dialecticore_publish_job_count{kind="failed"}` and the episode-level
`/api/v1/episodes/{episode_id}/publish-jobs` response before retrying or
replacing a job. Failed publish jobs also appear as live-provider readiness
blockers; submitted publish jobs appear as warnings.
In external Temporal mode,
`dialecticore_temporal_worker_execution_status` and
`dialecticore_temporal_worker_execution_count` show whether native activity
execution evidence is missing, blocked, running, or producing progress/errors.

If a workflow appears inconsistent, call
`GET /api/v1/episodes/{episode_id}/workflow/replay` and compare the replay
report issues with the episode's `workflow_control.run`. Check the
`temporal_runtime` component in `/api/v1/system/health` when Temporal behavior
does not match expectations:

- `mode=local` means the API is intentionally using local durable workflow
  control, replay journals, and stage pollers.
- `mode=bridge` should show signal transport enabled and an endpoint configured;
  otherwise bridge attempts will remain skipped or disabled.
- `mode=external` should show a backend address, task queue, reachable TCP
  probe, native worker readiness, and a non-stale `temporal-worker` heartbeat
  with `temporal_worker_execution_summary.v1` evidence when worker status is
  available. A degraded external mode means the deployment has not yet proven
  external Temporal stage execution, and the `temporal_worker_execution.reason`
  field explains whether execution evidence is missing, blocked, disabled, or
  signal/lease skipped.

For episodes in external mode, inspect
`workflow_control.temporal_stage_dispatch_log` on the episode status payload.
Missing dispatches mean no workflow-worker orchestration pass has recorded an
external handoff yet. Dispatches with `status=blocked` list missing runtime
settings; the same blocked count is aggregated in the `workflow_orchestration`
health component and `dialecticore_workflow_orchestration_count`. Dispatches
with `status=ready` include the activity name, task queue,
and idempotency key the `temporal-worker` activity pass uses for pickup. Inspect
the `temporal-worker` heartbeat details for `temporal_worker_execution_summary.v1`
and per-stage `temporal_stage_activity_execution.v1` records.

For worker drains, inspect `/api/v1/system/workers/signals` and worker heartbeat
details. The Web UI Worker Status panel posts the same signals and lists recent
records. Signal records include `delivery_sources`; `redis_stream` means the
worker can see the signal even when it does not share the API container's
runtime-state files. A latest `drain` or `stop_after_current` signal for the
role, `*`, or `all` causes the worker to skip new polling work with
`signal_skipped=true`; post a later `resume` signal to clear the block. If an
operator posts a signal but a remote worker does not react, verify
`DIALECTICORE_REDIS_WORKER_SIGNAL_ENABLED=true`,
`DIALECTICORE_REDIS_WORKER_SIGNAL_STREAM`, and Redis connectivity from the
worker container. Also check `/api/v1/audit-events` for
`worker.signal.recorded`; its details show whether the API recorded the request,
which delivery sources were present, the configured `redis_stream_maxlen`, and
whether Redis returned a stream ID. Older Redis stream entries may be trimmed by
`DIALECTICORE_REDIS_WORKER_SIGNAL_MAXLEN`; the same cap applies to local
runtime-state signal records, API signal lists, worker gate lookups, and summary
counts. Use the audit log for older operator evidence.
`/api/v1/system/health`, `/api/v1/system/events`, and
`dialecticore_worker_signal_count` in `/api/v1/system/metrics` expose bounded
recent counts by signal status, type, target role, active blocking target role,
delivery source, and malformed record count. A drain can remain visible in
recent breakdowns after a resume; live-readiness blocking is based on the latest
valid applicable role-specific or wildcard signal, while unsupported signal
types are kept as diagnostics only. Check `active_blocking_target_roles` or the
`active_blocking_target_role` metric dimension to find only the roles currently
blocking new work.
`dialecticore_worker_runtime_seconds` exports the heartbeat TTL, lease TTL, and
runtime-state retention window used for stale-worker and cleanup decisions.
