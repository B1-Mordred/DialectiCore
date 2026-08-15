# DialectiCore Implementation Plan

## Objective

Build the product described in `goal_DialectiCore.md` without narrowing the
final acceptance criteria. Progress is tracked by implementation increments and
verified with runnable tests, APIs, UI flows, manifests, and documentation.

## Increment 1: Functional Core

Deliver a runnable, inspectable system where a user can define a topic, select a
host and independently configured participants, run a real turn-by-turn
discussion, and review the resulting transcript.

Initial engineering slices:

1. Repository scaffold: backend, frontend, docs, Docker Compose, example episode
   definition, prompt templates, and synthetic no-paid-API test environment.
2. Strict episode definition schema and domain identifiers/statuses.
3. Provider-neutral model gateway contracts with executable mock,
   OpenAI-compatible, Ollama, Anthropic-compatible, Mistral-compatible, and
   generic HTTP adapters, plus persisted health/capability checks surfaced in
   the API and Web UI.
4. Discussion controller that generates only the active participant turn,
   isolates private memory, tracks duration and speaker balance, and persists
   every turn with discussion-session links plus per-participant memory IDs.
5. Broadcast transcript generation with transcript-turn version links, source
   turn links, and initial semantic fidelity checks.
6. FastAPI endpoints for episode creation, production start/status, transcript
   review, and audit access.
7. Basic Web UI for dashboard, episode definition, production status, and
   transcript review.
8. Persisted project administration with API/Web UI create, update, delete,
   episode `project_id` linking, system-health counts, and audit evidence.
9. Persisted model endpoint administration with API/Web UI create, update,
   delete, health, and capability management, plus transcript approval
   decisions.
10. Persisted participant profile administration with API/Web UI create, update,
    delete, model, sampling, voice, and visual assignment controls, plus
    assignment-ordered episode creation.
11. Turn-level transcript review actions for regeneration and exclusion before
    approval.
12. Episode editor form for project, title, central question, duration, discussion
    controls, language fidelity policy, research policy, media dimensions and
    generation flags, workflow retry settings, quality gates, host, and
    distinct panelists. The default editor cast uses the six frontier-model
    characters: ChatGPT as moderator, with Claude, DeepSeek, Grok, Gemini, and
    Mistral as panelists.
13. Global audit event projection for episode lifecycle, transcript review,
    approval, project, model endpoint, and participant profile changes.

## Increment 2: Audio And Localization

Increment 2 adds Voicebox, localization modes, subtitles, per-turn audio assets,
and audio QC.

Implemented groundwork:

1. Full production state vocabulary for localization and downstream media stages.
2. Source-linked localized transcript versions for configured non-canonical
   output languages after canonical transcript approval.
3. Localized transcript QC for source-link preservation, turn coverage, speaker
   attribution, pronunciation markup, configured semantic-fidelity/new-claim
   policy evidence, explicit added-claim detection, and empty-turn failures.
4. Episode asset model and planned per-turn audio assets linked to transcript
   turns.
5. Audio plan completeness QC and audit events for localization/audio planning.
6. Persisted language profile administration with default language catalog, BCP
   47 tags, default localization mode, subtitle direction, line-breaking and
   voice defaults, API/Web UI create/update/delete controls, audit events,
   backup/restore coverage, and System Health counts, plus Web UI controls for
   output language configuration, localization, and audio planning.
7. Persisted Voicebox endpoint and voice profile administration with mock
   defaults, health checks, capability discovery, participant voice assignment
   validation, Web UI create/update/delete controls, and audit events.
8. Executable Voicebox audio generation path for planned audio assets, including
   deterministic mock completion, normalized remote `/tts` submission, B1
   native `/generate/stream` WAV submission, storage/timing metadata
   persistence, audio generation QC, Web UI controls, and audit events.
   B1 CA bootstrap downloads the public certificate without endpoint
   authorization, verifies the configured SHA-256, and constrains
   operator-triggered certificate writes to runtime-state certificate storage.
   Persisted Voicebox provider-response and remote-cancel
   metadata is recursively redacted for token, secret, password, API-key,
   authorization, and credential fields before it reaches audio asset metadata.
9. Subtitle asset generation for canonical or localized transcripts, including
   WebVTT/SRT rendering, transcript-turn provenance, checksum metadata,
   subtitle completeness QC, Web UI controls, and audit events.
10. Metadata-based audio media QC and selective audio regeneration for chosen
    assets, transcript turns, participants, languages, or failed assets without
    full episode regeneration.
11. Manual asynchronous Voicebox job synchronization for submitted/running remote
    jobs, including configurable status paths, asset metadata updates,
    generation/media QC reruns, Web UI control, and audit events.
12. `voicebox-adapter` worker polling loop that scans persisted submitted or
    running audio jobs, syncs remote Voicebox results by language, saves updated
    episode aggregates, and records QC/audit evidence.
13. Word-timestamp subtitle cue segmentation and subtitle sync QC, including
    turn coverage, overlap, line-length, and audio drift checks.
14. Remote TTS cancellation and retry recovery for asynchronous Voicebox jobs,
    including selector-based cancellation, provider cancel path/method
    capabilities, planned-state reset for retry, cancellation-aware regeneration,
    Web UI control, and audit events.
15. Audio object storage writes and media probing for generated audio, including
    real WAV materialization for the mock Voicebox adapter, inline remote
    `audio_base64` storage, stable `object://bucket/key` asset URIs, measured
    duration/sample-rate/channel probe metadata, Docker volume wiring, Web UI QC
    probe counts, and persistence tests.
16. Waveform-derived audio media QC for stored WAV assets, including measured
    peak dBFS, RMS dBFS, silence ratio, clipping detection,
    provider-metadata fallback for external assets, negative defect tests, and
    Web UI waveform-analysis counts.
17. Remote audio result URL download/upload for Voicebox providers, including
    same-origin HTTP(S) media retrieval for immediate and async results,
    `storage_uri`/`audio_url`/`result_url`/`media_url`/`download_url` aliases,
    explicit external URL capability gates, stored-object rewrite/probing,
    downloaded-result QC counts, and Web UI evidence.
18. Phoneme timing tracks for later lip-sync and styled subtitle workflows,
    including provider phoneme normalization, estimated fallback tracks from word
    timing, viseme mapping, timing bounds/count QC, persistence tests, and Web UI
    readiness counts.
19. S3-compatible object storage backend for generated audio, including
    local/filesystem and S3 backend selection, MinIO-oriented Docker Compose
    wiring, `s3://bucket/key` asset URIs, first-write bucket creation, local
    probe-cache files for media validation after upload, and focused adapter
    tests.
20. Full loudness normalization analysis for generated audio, including
    configurable LUFS/true-peak/loudness-range targets, FFmpeg `loudnorm`
    integrated loudness and true-peak probing, normalization gain/offset
    metadata, S3/local probe-cache QC, Web UI loudness counts, and deterministic
    parser tests.

Remaining Increment 2 work:

- No known implementation gaps remain in the current Increment 2 plan.
- Next work starts Increment 3 visual generation.

## Increment 3: Visual Generation

Increment 3 adds ComfyUI endpoint administration, workflow registry, visual
profiles, shot planning, fallbacks, and visual QC.

Implemented groundwork:

1. Persisted ComfyUI endpoint administration with mock defaults, credential
   references, health status/capabilities, API and Web UI CRUD, health checks,
   and audit events.
2. Persisted ComfyUI workflow registry with versioned workflow metadata, output
   asset type, prompt templates, default parameters, endpoint validation, CRUD
   API routes, Web UI CRUD for structured workflow JSON, and delete guards.
3. Persisted visual profiles with character styling, workflow references,
   participant `visual_profile_id` validation/backfill, API and Web UI CRUD,
   and audit events.
4. Visual asset planning for canonical or localized transcripts, including one
   planned primary video asset per playable transcript turn, optional B-roll
   placeholders, reusable participant reaction/listening loops, reusable studio
   scene assets, ComfyUI workflow/profile prompt metadata, fallback policy
   metadata, `visual_asset_plan_completeness` QC, Web UI control, and audit
   events.
5. Visual generation submission/sync/cancellation scaffold, including
   normalized remote ComfyUI `/prompt` requests, `/history/{job_id}` sync,
   configurable status/cancel path capabilities, object-storage writes for
   returned media bytes or same-origin result URLs, deterministic mock visual
   placeholder objects, `visual_generation_completeness` QC, Web UI controls,
   `comfyui-adapter` worker sync, and audit events. Persisted ComfyUI
   provider-response metadata is recursively redacted for token, secret,
   password, API-key, authorization, and credential fields before it reaches
   asset, fallback, or remote-cancellation metadata.
6. ComfyUI workflow input patching and visual media probe evidence, including
   explicit `node_input_bindings`, conservative common-input patching,
   resolved prompt input audit metadata, PNG/JPEG header dimension probing,
   FFprobe-backed video metadata when available, Web UI probe counts, and
   visual QC warnings for probe defects.
7. Deterministic visual fallback materialization for failed remote ComfyUI
   submissions or failed job statuses, including stored SVG citation
   cards/fallback stills, SVG dimension probing, provider failure metadata,
   render-suitability QC counts, Web UI fallback counts, and audit evidence.
8. Deterministic shot-plan metadata for each playable transcript turn,
   including primary visual, reusable reaction loop, reusable studio scene,
   optional B-roll, transition, subtitle style, citation-overlay requirement,
   plan QC coverage counts, Web UI reaction/studio counts, and API tests.
9. Preset-specific default ComfyUI API workflow templates for talking-head
   video, reaction/listening loops, topic B-roll images, and studio-wide scenes,
   including explicit patch bindings, persisted default workflow evidence, API
   exposure tests, remote prompt patching tests, preset sampler settings,
   frame-count patching, and motion/camera/lighting control metadata.
10. Rerunnable visual media integrity QC, including completion/storage/checksum
    checks, probe evidence, render suitability, dimensions, FPS, duration
    alignment with audio, PNG pixel evidence, SVG structural evidence, video
    probe integrity for codec/pixel format/FPS/frame-count evidence, lip-sync
    readiness from audio phoneme timing, measured lip-sync offset, character
    identity/style consistency scoring from plan-time profile snapshots,
    workflow/endpoint metadata checks, Web UI control, and audit evidence.

Remaining Increment 3 work:

- No known Increment 3 implementation gaps remain in the current plan.
- Next work continues Increment 4 rendering, layout, thumbnail, publishing, and
  production hardening.

## Later Increments

Increment 4 adds `EpisodeTimeline`, scene editing, FFmpeg rendering, subtitles,
chapters, thumbnail generation, and render QC. Initial Increment 4 work now
includes a stored, checksummed `EpisodeTimeline` JSON asset with build/read/edit
API routes, a typed goal-field `timeline_entity` projection,
transcript/audio/visual/subtitle segment linking, chapters, timeline QC, Web UI
build control, scene-based Web UI timing/transition edits, and audit evidence.
It also includes generic manual asset replacement for corrected operator media,
including original-asset replacement metadata, active timeline reference
rewrites, checksum-bound updated timeline storage, API route coverage, and audit
evidence. It also includes render presets,
FFmpeg-backed preview and final slate render artifacts, render manifest storage
with normalization targets and source asset links, timeline scene composition
with normalized multi-layer visual scene plates for studio, talking-head,
B-roll, and reaction media, role-aware visual layout policy metadata,
deterministic advanced split-screen/focus layout policies, cross-scene
transition flags, FFmpeg `xfade` boundaries between adjacent scene plates,
FFmpeg per-frame eased overlay position transforms plus scale/opacity keyframes
and rounded-rectangle alpha masks for B-roll and reaction/focus motion,
source-reveal arc motion with `ease_in_out` curves and diamond alpha masks,
speaker-spotlight bounce motion with `ease_out_back` curves and circular alpha
masks, non-rectangular mask QC evidence, motion primitive evidence,
timeline-ordered dialogue audio assembly, citation overlay compositing,
subtitle cue burn-in, render QC,
targeted final-render review approvals before delivery packaging, thumbnail
generation, YouTube delivery ZIP exports, Web UI render/delivery controls,
initial YouTube Data API resumable video upload with credential reference OAuth
tokens, OAuth refresh-token exchange using secret references, YouTube-native
thumbnail/caption upload from package entries, production-manifest creation
before live publishing, System Health package/manifest coverage counters, and
audit evidence.

Remaining Increment 4 work:

- No known Increment 4 implementation gaps remain beyond live-provider
  hardening and operator credential provisioning.

Increment 5 adds research/evidence packs, claim extraction, source attribution,
advanced fact QC, citation overlays, and production manifests. Initial Increment
5 work now includes deterministic evidence-pack assets, configuration-derived
and supplied-source metadata, producer-provided URL retrieval with structured
tool logs, operator-configured live discovery endpoints with topic/query-derived
searches and result provenance, source de-duplication/scoring, deterministic
source-text claim/statistic extraction, deterministic fact-pattern extraction
for definitions, mechanisms, recommendations, and competing interpretations,
deterministic source-grounded relationship/quantity facets, optional trusted
source-bound external advanced extraction with rejected-claim QC evidence,
deterministic causal/scope context extraction for source-bound cause/effect
records and applicability qualifiers,
source ranking/freshness policy summaries, deterministic cross-source
agreement/conflict summaries with shared-term, stance, and claim-facet
relationship evidence, deterministic claim support groups for corroborated,
disputed, and single-source source claims, evidence-pack QC, human per-source
review decisions with review QC, research-review approval, participant prompt
grounding from evidence summaries, first-class persisted `ResearchSource` and
`EvidenceClaim` projections with read APIs and backup coverage, claim citation
QC, API routes, Web UI controls, source-linked citation-card overlay assets,
timeline overlay links, evidence-linked render and delivery manifests, first-class
`production_manifest.v1` assets/API/Web UI controls tying final packages back to
timeline segments, render/package manifests, QC, approvals, publish jobs, and
audit evidence.

Remaining Increment 5 work:

- No known Increment 5 implementation gaps remain beyond stronger future
  semantic/embedding-assisted analysis and live source/tool ecosystem
  hardening.

Increment 6 hardens Temporal workflows, pause/resume, retry controls, worker
scaling, RBAC, secrets, metrics, health dashboards, backup/restore, and
publisher operations. Initial Increment 6 groundwork now includes API-level
workflow pause, resume, cancel, stage approve/reject, and failed-stage retry
plus continue-after-manual-edit actions with persisted `workflow_control`
metadata, a versioned production-run record with run sequence, stage plan, stage
history, pause/resume/cancel/approve/reject/retry/continue signal history,
production-start guards, status flags, Web UI controls, and audit events. It
also includes a structured system health summary
with repository/database, object-storage, FFmpeg/ffprobe, endpoint
configuration, episode state, queue, failed-asset, and render counts surfaced in
the Web UI, including normalized database reachability, FFmpeg/ffprobe tool
availability, and model/Voicebox/ComfyUI endpoint collection gates. Endpoint
collection health now degrades when enabled endpoints are unhealthy or still
have unknown health, with failed gates distinguishing unavailable, unhealthy,
and unknown-health collections. Metrics now export
`dialecticore_component_readiness_check` for every boolean system-health
readiness gate, giving headless monitoring the same component/check/pass-fail
evidence as the API payload. A first worker coordination and observability slice now adds a
shared-runtime-state heartbeat registry, API/UI worker liveness summary, stale
heartbeat detection, Prometheus-style health/queue/worker metrics, server-sent
live status snapshots for health, queues, workers, leases, and audit evidence,
Docker volume wiring for API/worker visibility, and health-gated Docker Compose
startup so PostgreSQL, Redis, and MinIO are healthy before API migrations/health
checks and workers wait for the API health endpoint before polling. The Web UI
Compose image now serves the built React app through Nginx and proxies
same-origin `/api` requests to `production-api`, avoiding a Vite development
server in the self-hosted path. Worker registry readiness
now also names configured, active, missing, stale, failed, and degraded worker
roles and includes worker status/role breakdowns in health and live-provider
preflight evidence. The worker status summary itself now degrades on partial
configured-role coverage so direct worker status reads cannot report healthy
while production preflight is missing expected workers. Adapter scale safety now adds
file-backed per-role worker leases in the shared runtime-state volume so scaled
Voicebox and ComfyUI adapters skip duplicate remote-job sync passes while an
owner lease is active; lease ownership and expiry are surfaced in API health,
worker status, metrics, and the Web UI. Research, discussion, localization, QC,
speech, subtitle, visual, timeline, render, and publishing worker stages now
perform conservative idempotent polling for evidence-pack creation,
research-gated discussion runs, post-approval localization, source-bound claim
QC, per-turn speech generation, synchronized subtitle generation, character
visual production, approved media-ready transcripts, completed timelines,
approved final-render delivery preparation, production-manifest creation, and
automated dry-run publish jobs. A final completion worker stage now closes
running episodes automatically once the production completion-readiness gate
already reports `pass`, preserving the same completion signal, audit, replay,
and gate evidence as the manual complete action.
These stages use the same lease and heartbeat evidence.
`workflow-worker` now runs a local ordered coordination pass across those same
gated pollers and records aggregate per-stage orchestration evidence without
replacing the external Temporal execution target. Active production runs now also
receive durable `workflow_worker_orchestration_attempt.v1` journals, per-stage
activity checksums, run-level last-orchestration pointers, and
`workflow_stage_retry.v1` queue entries for stage failures with attempt counts,
backoff, next retry time, and exhausted/scheduled state. Worker-generated
summaries now carry `orchestration_attempt_id` values that become persisted
`summary_id` values, making replay of the same worker pass idempotent so restart
recovery cannot duplicate retry attempts.
Each persisted stage attempt now embeds a `workflow_stage_manifest.v1` with
normalized progress metrics, sanitized stage errors, the source stage-summary
checksum, and its own manifest checksum, giving restartable stage work a durable
audit handoff beyond aggregate counts.
Backup/restore now adds API-created
tar.gz backup archives, database table export/replace-restore hooks, local
object-storage file inclusion for filesystem deployments, authoritative remote
S3/MinIO bucket object inclusion and upload restore for S3-compatible
deployments, runtime-state file inclusion, restore validation before apply,
versioned restore-plan evidence, checksum-bound dry-run restore validation audit
events, Web UI
backup creation/listing with per-archive validation evidence, latest-archive
validation health, audited backup events, and Docker backup volume wiring. Asset
persistence now includes searchable `asset_records` projections
for generated/planned media and manifest assets with backup/restore coverage.
A first RBAC slice now adds an API-key-reference guard for
`/api/v1/*`, viewer/reviewer/editor/producer/admin roles, route-level
permission classification, safe auth policy/status visibility, and Web UI
security policy display. The auth policy now exposes the full route permission
vocabulary, including admin-only permissions granted through the admin wildcard,
so operators can audit protected API categories without inferring them from
role grants. A Docker-secret-backed admin bootstrap helper now creates the
initial `dialecticore_api_key` secret with restrictive file permissions,
overwrite protection, and redacted-by-default operator output for the
production-secrets stack. Auth tests now also enforce that role grants and
representative protected API routes stay covered by that route permission
vocabulary. Secrets hardening now centralizes `env:`, absolute
`file:`, and `docker-secret:` credential-reference resolution for model,
Voicebox, ComfyUI, database password assembly, auth, publishing, OAuth refresh,
and object-storage integrations without returning raw values through APIs or
audit evidence.
Publisher operations now include persisted publisher
targets, a default dry-run YouTube target, package-linked publish jobs, delivery
payload capture, generic HTTP package delivery, initial YouTube Data API
resumable video upload, YouTube-native thumbnail/caption upload from package
entries, OAuth refresh-token exchange using secret references, publish QC, audit
events, system-health visibility, Web UI publisher target CRUD/health controls,
publish-job evidence, and opt-in live automated publishing from the
publishing-worker behind both a deployment setting and per-target
`automated_live_publish` capability gate with dry-run/live heartbeat counts
plus explicit package-QC and production-manifest blocked-handoff counters.
Publisher response metadata now recursively redacts token, secret, password,
API-key, authorization, and credential fields before persistence, and YouTube
resumable upload session URIs are recorded only as presence evidence rather
than raw session URLs. The same redaction recognizes common camelCase and
PascalCase variants such as `accessToken`, `clientSecret`, and `apiKey` across
publisher, Voicebox, ComfyUI, and production-manifest evidence. The sensitive-key
rule now lives in a shared helper used by provider, media, publisher, and
manifest persistence paths, preventing future drift between evidence surfaces.
Remote endpoint health discovery now also runs provider-supplied model
capabilities, Voicebox capabilities, ComfyUI device metadata, and publisher
target capabilities through that helper before endpoint/target records are
persisted, returned by APIs, exported in backups, or surfaced in operator
dashboards. Endpoint and publisher target `base_url` inputs now reject URL
userinfo so credentials cannot be stored in API-visible base URLs, and
request-validation error responses strip rejected `input` values before
returning 422 payloads. Top-level endpoint/target `credential_reference` inputs
now require `scheme:target` syntax to reject raw pasted tokens, while
secret-shaped capability keys are redacted on write without blocking safe
reference-shaped OAuth capability fields. Endpoint and publisher-target upsert
audit events now record only safe credential-reference posture metadata,
including whether a reference is configured and which scheme it uses, without
storing reference targets or values. Runtime settings, health, live readiness,
and credential-provisioning paths now use the same credential
reference parser, sanitize invalid/raw references plus unsupported reference
targets before returning operator-visible evidence, and expose separate invalid
counts in health, live readiness, credential-provisioning summaries, metrics,
and Web UI summaries. A
Temporal bridge signal transport slice now persists sent/skipped/failed/disabled
attempt evidence for production start, pause, resume, cancel, and failed-stage
retry and can post `temporal_signal_request.v1` payloads to a trusted external
bridge without replacing local control state. A Temporal replay slice now writes
`workflow_event.v1` journal entries for production start, stage transitions,
workflow signals, and run completion, and exposes a replay report that
reconstructs run state, checks it against `workflow_control.run`, and returns a
stable event-log checksum. Operator retry and continue-after-manual-edit
reopens now journal stage re-entry events when the reopened checkpoint differs
from the last stage-history entry, so replay remains consistent after manual
recovery, and they mark matching scheduled or exhausted stage-retry queue
entries as resolved so operator-handled retries remain auditable without
continuing to block or warn as active retry backlog. Later automatic retry
entries keep cumulative per-stage attempt numbers across those resolved records
so operator recovery does not reset retry-budget enforcement, and retry
resolution itself is journaled as `workflow.stage_retry.resolved` replay/audit
evidence. Workflow completion is now guarded by
`production_completion_readiness.v1`: `COMPLETED` cannot be recorded until the
latest final render is approved, its completed export package exists, that
package has a completed production manifest, required discussion dimensions are
covered by passing `discussion_minimum_structure` QC, active assets have no
unresolved failed/corrupt/missing statuses, linked timeline media fingerprints
still match current audio/visual/subtitle assets, render-manifest source asset
snapshots still match current timeline/media assets, and the latest QC result for each target/check
is not failing when that QC row blocks under the episode's
`quality_completion_policy.v1`. The gate now preserves nonblocking failed
assets/QC rows allowed by the production definition as audit evidence without
blocking completion, and reports manually replaced failed assets under
`resolved_failed_assets` with replacement status, checksum/storage, and reason.
System Health, Live Provider Readiness, and Prometheus metrics now also count
production runs that are attempting or reporting `COMPLETED` without a passing
completion gate, exposing delivery blockers at fleet level.
Temporal runtime mode hardening now exposes an
operator-facing `temporal_runtime` health component and dedicated metrics for
local control, bridge signaling, or external native-backend mode, including
namespace/task-queue evidence, signal bridge configuration, native backend
address configuration, TCP reachability checks, and native worker readiness
degradation reasons. It also emits normalized readiness/failed-gate breakdowns
for Temporal mode validity, bridge signal transport/endpoint configuration, and
external backend address, task queue, reachability, native-worker enablement,
worker heartbeat, and execution evidence. External Temporal dispatch hardening now records
`temporal_stage_dispatch.v1` envelopes during workflow-worker orchestration when
external mode is selected, with per-stage activity names, run/episode IDs,
target stages, namespace/task-queue metadata, readiness or blocking reasons,
dispatch sequences, and stable idempotency keys for native worker pickup.
External Temporal worker execution now adds a Docker `temporal-worker` role that
runs in external mode, blocks on missing backend/task-queue/worker-enabled
settings, executes ordered `dialecticore.production.*` stage activities for
research, discussion, localization, QC, audio, Voicebox, subtitles, visuals,
ComfyUI, timeline, render, and publishing, emits
`temporal_worker_execution_summary.v1` and
`temporal_stage_activity_execution.v1` heartbeat evidence, and records the same
orchestration, retry, replay, and dispatch evidence on running episodes.
External Temporal health now requires that native-worker heartbeat execution
evidence, not just an active process heartbeat, before reporting the runtime
healthy, and Prometheus metrics now export that execution status plus progressed
stage, error, and activity counts for headless monitoring.
External identity/session hardening now accepts
trusted reverse-proxy or identity-aware gateway headers, validates
provider-managed bearer sessions through token introspection, rejects inactive or
expired or revoked sessions, maps upstream groups to DialectiCore RBAC roles,
preserves API-key fallback, exposes safe auth-mode policy metadata in the
API/UI, provides file-backed provider-session revocation hooks, adds Web UI
revocation recording/listing plus browser session login/logout for provider
bearer and API-key credentials, records bounded provider-session decision logs
with token hashes, safe claims, mapped role, permission, status, and denial
reason, exposes those recent decisions through the API/Web UI, and documents
deployment constraints for stripped/rewritten identity headers, provider
introspection, revocation persistence, and decision-log retention. Redis event
fan-out and distributed worker signal delivery now publish system snapshots,
deliver operator worker signals through Redis with local runtime-state evidence,
merge Redis stream records with local signal registries for cross-host worker
pickup, deduplicate delivery evidence by signal ID, and have workers honor
role-specific or wildcard `drain` and `stop_after_current` signals before
starting new polling passes; the Web UI Live Status Stream panel surfaces Redis
fan-out delivery evidence, and the Worker Status panel now posts those
signals and lists recent delivery evidence for operator review, while each
operator signal writes a global `worker.signal.recorded` audit event with safe
delivery metadata, and worker-signal status/type/target-role/delivery-source
counters are surfaced through system health, live-provider readiness, SSE
snapshots, and Prometheus metrics. SSE system snapshots now also include a
stable `id: system.snapshot:<checked_at>` cursor so reconnecting browser or
operator clients can identify the last observed snapshot without parsing the
payload body. Redis
worker-signal retention is now configurable and bounded with approximate
`XADD MAXLEN` trimming in Docker deployments, and the same cap applies to local
runtime-state signal records, API summaries, and worker gate lookups. Worker
signal readiness now counts and lists active blocking controls from the latest
applicable role-specific or wildcard signal, so a later role-specific `resume`
clears an older role-specific `drain` and a later wildcard `resume` clears older
target-specific blocks while preserving the historical signals in bounded audit
breakdowns. Malformed or unsupported worker-signal records are surfaced in
recent evidence and Prometheus counts, but ignored for latest-actionable-signal
selection and active blocking decisions. Worker signal, heartbeat-age,
lease-expiry, TTL, and runtime-state-retention Prometheus output now has direct
metrics regression coverage for recent/blocking/failed/malformed counters and
status/type/target-role/active-blocking/delivery-source breakdowns. Worker
heartbeat and lease runtime-state retention is also bounded, pruning stale
heartbeat files, expired lease files, and worker-owned malformed heartbeat/lease
JSON after the configured retention window, with retained, malformed, and pruned
cleanup evidence, including expired-lease prune counts distinct from malformed
file cleanup, shown in the Web UI Worker Status panel and live status
stream, and the retention/TTL plus cleanup values exported as Prometheus
metrics. Worker entrypoint role validation now records a failed
`unsupported_worker_role.v1` heartbeat and exits non-zero when
`DIALECTICORE_WORKER_ROLE` is not a supported production role, preventing
misconfigured containers from appearing as healthy idle placeholders. The Docker
Compose production contract now also compares worker service names and
`DIALECTICORE_WORKER_ROLE` values with both runtime worker registries, forcing
new production worker roles to stay aligned across deployment, status readiness,
and the worker entrypoint. Backup observability now also exposes a `backup_storage` health
component, normalized readiness/failed-gate breakdowns for backup path
writability using the checked target or parent path, archive
availability/readability, latest manifest readability, and
restore-validation currency, plus Prometheus gauges for backup archive count,
latest archive manifest readability, latest backup age, latest backup size,
archive validation coverage counts, latest dry-run restore-validation status, and validation age so
operators can monitor disaster-recovery readiness without opening archive files
manually; the Web UI Backups panel now surfaces the same health status, reason,
archive count, validation coverage, latest manifest readability, latest
restore-validation status, and latest archive age next to backup creation/listing
controls. Publisher job observability now adds non-replaced
publish job totals, completed/failed/submitted counts, dry-run/live breakdowns,
dedicated `dialecticore_publish_job_count` metrics, and System Health dashboard
cards for global publish volume and failures, plus normalized live-readiness
failed/submitted publish-job gates. Publisher-target observability now
adds a `publisher_target_health.v1` health component, dashboard evidence, and
Prometheus gauges for enabled, live-enabled, automated-live-capable, mock,
dry-run-only, health, and issue counts, including degradation when automated
live publishing is globally enabled without a capable target. The System Health
dashboard summary includes the failed publisher-target gates and boolean
checklist alongside those capability counts. Publisher readiness now also
carries normalized readiness/failed-gate breakdowns plus
enabled-target adapter, health-status, and platform breakdowns so operators can
identify whether the publishing pool is mock, generic HTTP, YouTube, healthy,
unknown, or mixed before enabling live delivery. Unknown enabled publisher
target health now degrades system health and remains a live-readiness warning
until the target health check records a known status.
Live-provider
readiness now also includes the same non-replaced publish-job summary, surfacing
submitted jobs as warnings and failed jobs as blockers before live runs.
Production-run observability now aggregates durable `workflow_control.run`
records plus active paused/cancelled/failed episode control state into a
`production_runs` health component, live-provider readiness category, Web UI
summary, and `dialecticore_production_run_count` metrics for total, active,
running-active, paused-active, failed-active, cancelled-active,
completion-blocked, and attention counts deduplicated by run, plus
attention-reason and completion-failed-gate breakdowns and a bounded
attention-run list for
operator triage. Production-run metric regression coverage now directly locks
the total, active, running, paused, failed, cancelled, completion-blocked, and
attention counter kinds. Paused or already-running active runs warn before live production, while
failed or cancelled active runs block live readiness. Production-run readiness
also emits normalized readiness/failed-gate breakdowns for active failed,
cancelled, paused, and already-running production runs. Auth runtime observability now
adds an `auth_runtime` health component for mode readiness, provider-session
revocation registry health/counts, decision-log health/counts, and normalized
readiness/failed-gate breakdowns for auth-mode viability, API-key reference
resolution, auth header-name and provider-session claim-name configuration,
default-role validity, provider introspection configuration and HTTPS policy,
provider-session client credential pair readiness, and runtime-file readability, plus
`dialecticore_auth_mode_enabled` and
`dialecticore_auth_provider_session_count` metrics for headless monitoring of
identity/session operations without exposing raw tokens or claims. Production
deployment readiness now also blocks auth-enabled deployments that have only
non-admin login paths, requiring an API-key admin path or trusted/provider
admin default/group mapping before the stack is considered bootstrap-ready. The
deployment auth-mode and admin-bootstrap gates now only count auth modes that
satisfy the same runtime prerequisites as `auth_runtime`, and deployment
readiness treats an unavailable configured API-key reference as a failure with
safe reference-status evidence. The System Health dashboard now also renders the
safe auth setting summary for API-key headers, trusted identity headers and
default role, provider-session token/claim labels, client credential reference
presence, and group-role map presence so operators can connect auth readiness
gates to the deployed identity configuration without exposing secret values.
The frontend now has a Vitest harness covering the System Health auth evidence
formatter, including unloaded state, blank header/claim labels, and proof that
raw credential-reference strings are not rendered in the operator summary. The
formatter also suppresses the auth evidence block when a partial health settings
payload contains no auth keys, avoiding misleading blank-auth cards during
schema evolution or degraded payload reads. Browser authentication header
construction and local session persistence are now covered by a pure frontend
test harness, proving provider bearer formatting, custom token headers, API-key
session override behavior, malformed stored-session fallback, and logout storage
removal without loading the full React app. The same helper now trims stored
browser credentials, ignores blank bearer/API-key values, and falls back to safe
default API-key, role, user, and provider-session token header names when a
stored browser session contains blank header labels.
Remote
object-storage observability now probes S3/MinIO endpoint reachability,
configured-bucket `head_bucket` availability, bucket configuration, and
credential-reference coherence through system health, Prometheus metrics, and
the Web UI. The System Health dashboard summary shows failed object-storage
readiness gate names and the boolean storage checklist for local and
S3-compatible backends. Redis runtime observability now exports
fan-out and worker-signal mode gauges, endpoint reachability, and bounded stream
retention metrics from the same health evidence, plus normalized Redis
readiness gates and failed gate names for mode enablement, URL configuration,
channel/stream naming, signal retention, and TCP reachability. The System Health
dashboard summary also includes safe Redis channel/stream names, stream max
length, failed gate names, and the boolean Redis checklist, and the Redis
component only reports healthy when all normalized Redis gates pass. Deployment readiness
observability now adds a `deployment_readiness` health component, dashboard
card, and Prometheus gauges for production posture checks covering persistent
database selection, configured authentication, S3-compatible media storage,
Redis runtime channels, backup path, runtime-state path, and normalized
readiness/failed-gate breakdowns for the boolean deployment posture. Runtime path
readiness now adds a `runtime_paths.v1` health component, dashboard evidence,
and Prometheus gauges for backup, runtime-state, and local object-storage path
readiness, state breakdowns for configuration/parent/writability/free-space
sufficiency, free bytes, configurable minimum-free-space sufficiency, and
normalized readiness/failed-gate breakdowns for configured required paths,
available/writable required paths, and required-path free-space sufficiency.
Credential
reference readiness now resolves active `env:`, absolute `file:`, and
`docker-secret:` references for enabled provider/media/publisher integrations
and active auth/object-storage settings, reporting safe owner/status metrics and
dashboard evidence plus normalized readiness/failed-gate breakdowns for active
credential resolution and supported reference schemes, explicitly counting
unsupported prefixes instead of treating them as provisionable secret targets,
without exposing secret values. Workflow orchestration
observability now aggregates `workflow_worker_orchestration_attempt.v1` and
`temporal_stage_dispatch.v1` journals into system health, Prometheus metrics,
and System Health dashboard cards for attempts, errors, progressed/failed
stages, dispatches, blocked handoffs, failed/progressed stage breakdowns, and
blocked/ready dispatch-stage breakdowns, plus normalized readiness/failed-gate
breakdowns for orchestration errors, failed workflow stages, and blocked
Temporal dispatches. The Prometheus renderer now emits those failed/progressed
stage and blocked/ready dispatch-stage dimensions directly from the health
component and has focused regression coverage for the full breakdown set.
Operators can inspect the bounded
cross-episode orchestration journal through
`GET /api/v1/system/workflow-orchestration` and the Web UI Workflow
Orchestration Evidence panel. Live-provider readiness now exposes a bounded
`live_provider_readiness.v1` preflight report through
`GET /api/v1/system/live-provider-readiness` and the Web UI Live Provider
Readiness panel, aggregating production posture, credential references, enabled
model providers, remote Voicebox/ComfyUI endpoints, object storage, runtime
paths, backup storage, Redis, auth, Temporal runtime, worker registry, worker
signal state, workflow orchestration, workflow retry backlog, media queues,
production-run state, publish jobs, and publisher targets into pass/warning/fail
operator evidence without returning raw secrets. Publish-job readiness now also
warns on completed export packages that do not yet have linked production
manifests, with top-level health counts, Prometheus package-manifest counters,
and bounded latest-package evidence.
Episode-scoped pilot readiness now adds
`GET /api/v1/episodes/{episode_id}/pilot-readiness` and a Web UI Pilot Run
Readiness panel. It gives operators a direct first-test verdict for the selected
episode across real model-backed discussion, B1/remote speech, character visual
animation prerequisites, and render tooling. The report requires non-mock model
and Voicebox bindings for every selected character, portrait plus full-body
visual references for animation, enabled ComfyUI workflows/endpoints, and
FFmpeg/ffprobe availability, while keeping mock ComfyUI as a warning for
audio-first pilots.
Worker registry readiness now includes normalized
readiness/failed-gate breakdowns for supplied status evidence, active
heartbeats, configured-role coverage, heartbeat freshness/status, and parseable
runtime-state files. Provider, Voicebox, and ComfyUI readiness now
also include normalized readiness/failed-gate breakdowns and bounded safe
endpoint issue entries for records missing base URLs, unhealthy enabled records,
and unknown-health enabled records, using endpoint IDs/names/types/health only.
Runtime path failures, orchestration errors, blocked Temporal dispatches,
exhausted workflow retries, failed media assets, failed publish jobs, failed
worker signal delivery, active blocking worker signals, failed/cancelled active
production runs, and automated live publishing enabled without an enabled
`automated_live_publish` target are
treated as live-run blockers in the same preflight. Worker signal readiness now
includes normalized readiness/failed-gate breakdowns for supplied signal-summary
evidence, failed delivery, and active blocking control signals, while the
`worker_signals` system-health component and dashboard summary expose the same
gate names and attention counts for ordinary operations. Missing or
unvalidated backups, missing/incomplete worker heartbeat coverage, scheduled
workflow retries, paused or already-running active production runs, pending
media jobs, and submitted publish jobs are surfaced as live-readiness warnings.
Media queue evidence now reports submitted/running audio, visual, and subtitle
work separately, plus failed subtitle assets alongside failed audio/visual
assets, through system health, Prometheus queue gauges, SSE snapshots, and the
Web UI readiness summaries, with normalized live-readiness gates for failed
media assets and pending audio, visual, and subtitle work. The aggregate pending
job counts include submitted/running work exactly once, while submitted/running
fields remain sub-breakdowns for routing backlog to the right worker. Headless
metrics regression coverage now locks the pending/submitted/running audio,
visual, and subtitle queue gauges plus failed audio/visual/subtitle asset
breakdowns emitted as `dialecticore_queue_count`.
Credential
provisioning now adds
`credential_provisioning_plan.v1` through
`GET /api/v1/system/credential-provisioning` and the Web UI Credential
Provisioning panel, grouping configured `env:`, `docker-secret:`, and `file:`
targets, including disabled live targets by default, and reporting
resolved/unavailable status without returning secret values. The same evidence
is now summarized in the `credential_provisioning` health component and
`dialecticore_credential_provisioning_count` metrics, with missing active
references treated separately from missing disabled-target credentials, plus
normalized readiness/failed-gate breakdowns and bounded sanitized missing
reference samples for operator triage.
Workflow retry
backlog observability
now aggregates active scheduled and exhausted `workflow_stage_retry.v1` entries
across episodes into system health, Prometheus metrics, and System Health
dashboard cards, including due-now, still-in-backoff, unknown-schedule, and
non-scheduled retry schedule states plus due/backoff/unknown/exhausted stage
breakdowns for live-readiness triage. It also carries historical and resolved
retry counts by resolution status/stage separately from active backlog totals,
plus normalized readiness/failed-gate breakdowns for exhausted, scheduled, due,
backoff-delayed, and unknown-schedule retry states. The Prometheus renderer now
exports due/backoff/unknown/exhausted stage dimensions from the retry health
summary and has focused regression coverage for those schedule-stage counters.
Operators can also inspect a bounded cross-episode retry
queue through `GET /api/v1/system/workflow-retries` and the Web UI Workflow
Retry Backlog panel, sorted by due, backoff, unknown, and exhausted/non-scheduled
state. Live-provider readiness now includes the same workflow orchestration and
retry summaries, surfacing scheduled retry work as warnings and orchestration
errors, blocked Temporal handoffs, or exhausted retry budgets as operator
blockers before live runs. Production deployment readiness now also validates
the selected Temporal runtime contract and restricted API CORS origins at the
settings level, reporting invalid backend modes, wildcard CORS, plus incomplete
bridge signal transport or external native-backend address/task-queue/worker
enablement through health, metrics, live readiness, and the Web UI deployment
summaries. The Docker Compose production-secrets override now carries a
non-wildcard `DIALECTICORE_CORS_ALLOWED_ORIGINS` default for the bundled Web UI
origin while keeping the local-development Compose default overrideable.
Deployment readiness now also resolves configured API-key, object-storage
secret, and database password references before comparing them with known
placeholder/default sentinel values, so file-backed and Docker-secret-backed
production credentials are checked without exposing secret contents.
The production API container healthcheck now uses an auth-aware backend helper
that resolves the configured API-key reference, including Docker secrets, before
calling the internal system-health endpoint, and falls back to trusted-identity
headers when that production auth mode is enabled without a shared API key, so
RBAC-enabled production stacks can still become healthy. The same helper now
validates required authenticated healthcheck header names before opening the
probe request, returning a setting-specific failure for blank API-key, role,
user, trusted-identity, or trusted-email header configuration.
Docker worker services now also have heartbeat-backed container healthchecks
using `app.worker_healthcheck`, aligning Docker health with the same
runtime-state worker evidence exposed through system health, metrics, SSE, and
the Web UI. Deployment readiness now flags worker heartbeat TTL settings that
do not exceed the configured worker poll interval, preventing self-hosted Docker
healthchecks from flapping for otherwise idle workers, and flags worker lease
TTLs that do not exceed the poll interval so scaled worker duplicates do not
take ownership before the current worker's next pass.
The Web UI Nginx config now pins request/proxy temporary files to `/tmp/*`
paths, matching the read-only Compose container and bounded tmpfs hardening.
Production Docker build contexts now explicitly exclude backend tests, generated
package metadata, coverage artifacts, local databases, runtime storage, caches,
and common secret material, with the Web UI build context carrying the same
local token, package-manager credential file, cloud/cluster credential
directory, secret directory, and private key/certificate exclusions, so
self-hosted images carry runtime code and migration assets without development
residue.
The version-control ignore boundary now mirrors that deployment hygiene for
coverage output, local databases, generated package metadata, operator scratch
API-key files, package-manager credential files, cloud/cluster credential
directories, secret directories, and private key/certificate material.
Operator configuration coverage now has a contract test requiring every
non-raw-secret backend `Settings` field to appear in `.env.example`, and Docker
Compose now passes through the runtime free-space floor, Temporal signal
timeout, workflow retry, deterministic timing, publishing, and research
retrieval/discovery/advanced extraction settings used by self-hosted workers and
readiness checks.
The production-secrets Compose override now also has contract coverage ensuring
API and worker services use Docker-secret credential references, raw sentinel
API/MinIO values are blanked, PostgreSQL and MinIO use `*_FILE` secret paths,
Temporal reads the Postgres secret at startup, the MinIO root user and password
are both supplied through Docker secrets, and every mounted production secret
uses explicit long-form Compose source/target/mode entries instead of short-form
defaults.
The production-secrets Compose override now also resets direct production API
and MinIO host port publication, keeping browser API traffic routed through the
Web UI proxy and bundled object storage reachable to API/workers over the
backend network without exposing either service on the production host by
default.
Python runtime image hardening now has contract coverage for the API and worker
Dockerfiles: non-root UID/GID `10001`, FFmpeg availability, apt/pip cache
hygiene, prepared `/data/*` writable mount points, and stable API/worker
entrypoints.
The Web UI production image/proxy path now has contract coverage requiring the
React app to be built with `npm ci`, served by unprivileged Nginx on port `8080`,
mapped to host port `5173`, proxy same-origin `/api/` traffic to the API with
forwarded headers and buffering disabled, and preserve SPA route fallback.
Backup archive validation now normalizes missing required `manifest.json` or
`database.json` members into explicit restore/listing errors, so incomplete or
corrupt archives are skipped during backup listing and rejected before dry-run or
applied restore. Required root metadata members must now be unique regular files,
so duplicate or link-style `manifest.json`/`database.json` entries cannot
ambiguously drive restore planning.
S3/MinIO object restore now validates manifest-listed archive members before
upload: missing objects, size mismatches, and SHA-256 checksum mismatches produce
explicit restore errors, while recorded content types are preserved on
`put_object`.
Dry-run restore validation now performs the same S3/MinIO archive-member
integrity checks without uploading objects and records
`s3_object_storage_restore_validation.v1` evidence inside the audited restore
plan, so checksum-bound backup validation cannot bless a corrupted media archive.
The S3/MinIO restore manifest is now also an allowlist: unexpected archive
members under `object-storage-s3/` are rejected during dry-run or applied
restore before they can be uploaded to the configured bucket.
Local filesystem object-storage and runtime-state backup sections now record
relative file path, byte-size, and SHA-256 checksum manifests. Dry-run and
applied restore validate those file manifests, reject missing or unexpected
archive members, and surface `file_storage_restore_validation.v1` evidence in
the restore plan for new archives.
S3/MinIO backup manifests now also sanitize recorded endpoint metadata by
removing URL userinfo, query strings, and fragments before archive creation,
listing, health, or restore-validation responses expose the manifest.
Backup observability now carries safe latest-archive content-validation summaries
for object-storage and runtime-state through system health, live-provider
readiness, and `dialecticore_backup_latest_content_validation` metrics, so
operators can distinguish archive-level validation from media/runtime member
validation. The Prometheus backup metrics now have focused renderer regression
coverage for archive counts, latest-archive labels, restore validation age, and
object-storage/runtime-state content-validation status gauges.
The Web UI Backups panel, latest validation result, and Live Provider Readiness
backup summary now render those safe content-validation summaries without
showing raw archive paths or object keys.
Compose volume topology now has contract coverage for runtime-state visibility
across every worker, object-storage mounts only on API/media-writing workers,
and backup archive volume isolation to `production-api`.
Compose startup ordering now has contract coverage for data-service
healthchecks, API dependency on healthy Postgres/Redis/MinIO, Web UI and normal
worker dependency on healthy API, optional external Temporal worker dependency
on started Temporal plus healthy API, and the concrete API/Web/worker
healthcheck commands. Compose app healthcheck cadence hardening now makes API,
Web UI, and worker interval, timeout, retry, and start-period values
operator-tunable through documented `DIALECTICORE_*_HEALTHCHECK_*` settings,
with rendered Compose defaults under contract. The API container healthcheck now
validates the `/system/health` JSON status payload, accepts only
`healthy,degraded` by default, and exposes
`DIALECTICORE_HEALTHCHECK_ALLOWED_STATUSES` through the default and
production-secrets Compose app environments for stricter degraded-state policy.
Compose infrastructure
healthcheck cadence hardening extends the same operator-tunable pattern to
PostgreSQL, Redis, MinIO, and Temporal without changing the probe commands, so
slower self-hosted data-plane startup can be tuned without Compose edits. Compose restart-policy hardening now requires
`restart: unless-stopped` across Web UI, API, worker, database, Redis, MinIO,
and Temporal services so the self-hosted stack restarts after host reboots or
container exits without an extra supervisor. Compose log-retention hardening now
applies bounded Docker JSON log rotation to every bundled service through
operator-tunable `DIALECTICORE_DOCKER_LOG_MAX_SIZE` and
`DIALECTICORE_DOCKER_LOG_MAX_FILE` defaults, preventing unattended self-hosted
workers and provider adapters from filling the host Docker log directory.
Compose process-lifecycle hardening now enables Docker init and applies a
shared `DIALECTICORE_DOCKER_STOP_GRACE_PERIOD` to every bundled service, giving
Python workers, FFmpeg subprocesses, databases, Redis, MinIO, and Temporal a
bounded graceful shutdown window while avoiding orphaned child processes.
Compose resource-limit hardening now applies shared CPU, memory, swap,
process-count, and open-file soft/hard limits to every bundled service through
operator-tunable `DIALECTICORE_DOCKER_CPU_LIMIT`,
`DIALECTICORE_DOCKER_MEMORY_LIMIT`, `DIALECTICORE_DOCKER_MEMORY_SWAP_LIMIT`,
`DIALECTICORE_DOCKER_PIDS_LIMIT`, `DIALECTICORE_DOCKER_NOFILE_SOFT_LIMIT`, and
`DIALECTICORE_DOCKER_NOFILE_HARD_LIMIT` defaults, reducing the blast radius of
runaway CPU, memory, swap, subprocess, or descriptor leaks during self-hosted
operation. Compose tmpfs hardening now also makes Python and Web UI `/tmp`
limits operator-tunable through `DIALECTICORE_DOCKER_PYTHON_TMPFS_SIZE` and
`DIALECTICORE_DOCKER_WEB_TMPFS_SIZE`, preserving read-only image layers while
allowing deliberate scratch-space sizing for media work and proxy buffering.
Compose infrastructure hardening now applies `no-new-privileges` to Temporal,
Temporal UI, PostgreSQL, Redis, and MinIO through a shared infrastructure
runtime anchor while leaving read-only roots and dropped capabilities reserved
for the app/web images that are built for that stricter posture.
Compose infrastructure image hardening now also sets `pull_policy: missing` on
externally sourced Temporal, PostgreSQL, Redis, and MinIO services, avoiding
implicit image refreshes during routine self-hosted `docker compose up` runs.
Those infrastructure image references are now operator-tunable through
documented `DIALECTICORE_*_IMAGE` settings for PostgreSQL, Redis, MinIO,
Temporal, and Temporal UI, preserving current defaults while supporting patch
pinning or internal registry mirrors without Compose edits.
Production Web UI build-argument hardening now has contract coverage for the
static image's non-secret browser defaults: `VITE_API_BASE_URL`,
`VITE_DIALECTICORE_ROLE`, and `VITE_DIALECTICORE_USER` are accepted from
documented Compose settings, while `VITE_DIALECTICORE_API_KEY` is intentionally
not accepted as a Docker build argument or image environment variable so shared
API keys are not baked into inspectable frontend artifacts.
Compose media-QC configuration hardening now passes the documented audio
loudness target, true-peak ceiling, and loudness-range target settings through
the shared app environment, with rendered default and production-secrets
Compose contract coverage, so self-hosted operators can tune loudness analysis
policy from `.env` instead of editing Compose.
Compose object-storage configuration hardening now passes the documented
non-secret S3/MinIO backend, endpoint, bucket, local probe-cache path, region,
path-style, and auto-create-bucket settings through the shared app environment,
with rendered default, operator-override, and production-secrets Compose
contract coverage, while credential references remain separately secret-safe.
Compose object-storage credential-reference hardening now also makes the S3
access-key and secret-key references operator-tunable in both the base stack and
production-secrets override, preserving the bundled MinIO defaults while
supporting external S3 credentials or differently named Docker secrets without
Compose edits.
Compose database configuration hardening now makes the base stack accept a full
operator-provided `DIALECTICORE_DATABASE_URL` or a blank URL plus component
database settings and a password reference, and makes the production-secrets
assembled database settings operator-tunable while keeping the default password
reference Docker-secret-backed.
Compose Redis configuration hardening now passes `DIALECTICORE_REDIS_URL`
through the shared app environment, with rendered default, operator-override,
and production-secrets contract coverage, so self-hosted deployments can point
the app containers at an external Redis-compatible service without editing
Compose.
Compose API-key auth configuration hardening now passes the documented API-key
credential reference and API-key/role/user header-name settings through the
shared app environment, while keeping the production-secrets default
Docker-secret-backed and contract-testing operator override rendering.
Compose API healthcheck probe configuration hardening now exposes the helper's
target URL and internal HTTP request timeout as documented
`DIALECTICORE_HEALTHCHECK_URL` and
`DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS` settings, with rendered default,
operator-override, and production-secrets contract coverage.
The API healthcheck helper now also validates that
`DIALECTICORE_HEALTHCHECK_TIMEOUT_SECONDS` is a positive number inside the
controlled probe failure path, so invalid operator input produces a concise
setting-specific Docker healthcheck failure instead of a traceback.
Compose model-provider configuration hardening now passes documented
`DIALECTICORE_MODEL_PROVIDER` values through the shared app environment instead
of hardcoding the container stack to `mock`, while preserving the mock default
for reproducible local self-hosted runs.
Compose network-topology hardening now separates browser-facing `edge` traffic
from backend data/worker traffic: Web UI, production API, and Temporal UI are on
`edge`, while API, workers, PostgreSQL, Redis, MinIO, and Temporal share
`backend`; data-plane services and workers are not attached to the edge network.
Compose published-port hardening now binds default-stack Web UI, production
API, Temporal UI, and MinIO S3 API host ports to loopback by default through
operator-tunable `DIALECTICORE_WEB_BIND_ADDRESS`,
`DIALECTICORE_API_BIND_ADDRESS`, `DIALECTICORE_TEMPORAL_UI_BIND_ADDRESS`, and
`DIALECTICORE_MINIO_BIND_ADDRESS` settings, requiring an explicit operator
choice before those development or operator surfaces listen on LAN or public
host interfaces. The production-secrets override then removes direct
production API and MinIO host publication entirely, leaving browser API traffic
to the Web UI proxy or an operator-managed reverse proxy by default.
Compose admin-surface hardening now also leaves the MinIO browser console
unpublished by default, keeping the S3 API available for local object-storage
compatibility while requiring an operator-local override for console port
`9001`.
Compose admin-surface hardening now gates Temporal UI behind the `ops-ui`
profile, keeping the default stack focused on runtime services while preserving
explicit operator access with `docker compose --profile ops-ui up temporal-ui`.
Compose Temporal startup hardening now probes the bundled Temporal frontend with
`temporal operator cluster health`, starts Temporal only after PostgreSQL is
healthy, and gates the optional native `temporal-worker` plus opt-in Temporal UI
on `service_healthy`, so a started container alone no longer satisfies
Temporal-facing services. Compose parser contract coverage now also renders the
default and production-secrets stacks with `docker compose config`, proving the
merged service graph, loopback default host ports, production API/MinIO port
resets, Docker-secret-backed app environment, worker-role assignments, and
healthcheck commands against Compose's actual model rather than text matching
alone. The same rendered-config coverage now includes the optional `ops-ui`
Temporal UI profile and the all-profile production render, proving the operator
UI stays loopback-bound, depends on healthy Temporal, remains on edge/backend
networks only, and does not reintroduce direct production API or MinIO host
ports.
Live-provider readiness now also carries a
dedicated credential-provisioning category so missing active credential
references block live runs while missing credentials for disabled future live
targets warn without blocking the current run. Local object-storage health now
checks actual target-or-parent writability, includes checked-path and reason
evidence, exports object-storage-specific local path readiness/state metrics,
uses the normalized `writable_target_or_parent` key shared with runtime-path
readiness, and feeds the same failure into live-provider readiness instead of
treating a merely existing directory as healthy. Object-storage health and live readiness
now also expose normalized readiness checks and failed gate names for local path
existence/directory/writability and S3 endpoint/credential/bucket readiness.
Credential-provisioning contract coverage now explicitly proves Docker-secret
backed MinIO access-key and secret-key references through the active-only
provisioning plan, credential-reference health, live-provider readiness, and
Prometheus credential provisioning metrics.
The Web UI Credential Provisioning panel now also renders the backend's
sanitized Compose environment and Docker secret examples next to target and
missing-reference evidence, so operators can act on the provisioning checklist
without inspecting raw API JSON. It also surfaces invalid-syntax and
unsupported-scheme counts from the typed provisioning summary.
The System Health credential-provisioning summary now mirrors the detailed
target-kind breakdown by showing environment variable, Docker secret, and file
counts in the compact component evidence.
The System Health safe settings summary now also exposes non-secret auth
operator context, including configured header names, provider-session claim
names, default role labels, and credential-reference presence booleans, so the
Web UI can explain auth/runtime readiness gates without returning secret values
or credential reference contents.
The System Health safe settings summary now also exposes non-secret
object-storage posture booleans for backend mode, bucket/endpoint/region
configuration, credential-reference pairing, path-style mode, and auto-create
policy, with Web UI rendering that omits raw endpoint URLs, bucket names, and
credential-reference targets.
The top-level safe settings payload now also omits raw object-storage bucket
names and runtime/backup filesystem paths, replacing the latter with configured
booleans while leaving detailed path evidence in the dedicated runtime-path and
object-storage health components.
Object-storage credential-reference readiness now treats blank or whitespace-only
S3 access-key and secret-key references as unconfigured everywhere the pair is
evaluated, so System Health, deployment-readiness gates, live-provider
readiness, and Prometheus deployment metrics cannot be satisfied by placeholder
whitespace.
Temporal runtime readiness now applies the same stripped configured-state
handling to bridge signal endpoints and external backend address/task queue
settings, so whitespace placeholders cannot trigger TCP probing or satisfy
System Health, live-readiness, or safe-settings configured flags.
Auth runtime readiness now also treats whitespace-only API-key references,
provider-session introspection URLs, provider-session client credential
references, and provider-session runtime file paths as unconfigured across auth
mode selection, safe settings, credential provisioning, and live-readiness
evidence, while still surfacing malformed nonblank references as invalid.
Safe Redis settings now use the same stripped configured-state contract as
deployment readiness, so whitespace-only Redis URLs cannot appear configured in
operator evidence while the Redis runtime checks reject them.
Voicebox generation now supports a configurable B1 stream adapter that posts the
native `profile_id`/`text`/`language`/`engine=chatterbox` request to
`/generate/stream`, stores returned WAV bytes through object storage and audio
QC, exposes the five supplied German native profile UUIDs as Web UI presets
without storing the bearer token, preserves normal add/edit/delete controls,
adds missing supplied B1 voice presets in one UI action without overwriting
existing profiles, assigns saved B1 voices to matching frontier-model
characters without overwriting existing participant voice choices, now backs
that setup with one backend provisioning route and safe audit event, and locks the B1
endpoint/profile preset, missing-only bulk-add, participant-assignment, and API
provisioning contracts with tests. B1 CA
bootstrap can be triggered from the Voicebox endpoint UI and stores the public
root certificate under runtime state for read-only Docker app roots; the
Voicebox endpoint list now summarizes B1 CA file/storage/SHA status without
rendering raw bootstrap URLs or certificate hashes, and successful bootstrap
emits a dedicated safe audit event.
The Web UI deployment-readiness summaries now also surface safe AI/media
provider posture counts for model, Voicebox, and ComfyUI endpoints plus
publisher-target counts, so production failures for missing remote endpoints,
missing base URLs, missing live publisher targets, unhealthy endpoints or
targets, or unknown health are visible without expanding raw JSON.
Rendered Compose contract coverage now also proves the effective merged service
model preserves runtime hardening defaults across the default, `ops-ui`, and
`temporal-external` profiles: `init`, restart policy, stop grace period,
bounded JSON log rotation, CPU/memory/swap/process/file-descriptor limits,
app/Web read-only roots with dropped capabilities and bounded `/tmp` tmpfs,
infrastructure `no-new-privileges`, and explicit infrastructure pull policy.
Compose operator-configuration coverage now also parses the default and
production-secrets Compose files for every interpolated `DIALECTICORE_*`
variable and requires each one to appear in `.env.example`, including
Compose-only bind, image, healthcheck, log-retention, tmpfs, and resource-limit
knobs that are not backend `Settings` fields. Rendered Compose contract
coverage now also proves operator-provided research retrieval, discovery, and
advanced-extraction settings reach the production API container environment.
Production deployment readiness now also includes a dedicated
`redis_url_configured` gate, so a stack cannot report the Redis runtime posture
as configured from fan-out/signal booleans alone when `DIALECTICORE_REDIS_URL`
is blank; the same failed gate appears in metrics, live-provider readiness, and
the Web UI deployment checklist.
Production deployment readiness now also consumes model-provider endpoint
evidence and requires at least one enabled non-mock model endpoint with required
remote base URL configuration and known non-unhealthy health before production
posture can pass; the failed gate appears in `/system/health`, Prometheus
readiness metrics, and live-provider readiness.
Production deployment readiness now applies the same persisted-endpoint posture
to Voicebox and ComfyUI, requiring at least one enabled non-mock audio and
visual generation endpoint with required base URL configuration and known
non-unhealthy health before production can report ready.
It also includes a `redis_runtime_channels_configured` gate for blank Redis
event-channel or worker-signal stream names, keeping production posture aligned
with the detailed Redis health component before workers rely on cross-host
fan-out or signal delivery.
Production deployment readiness now also includes S3-compatible object-storage
endpoint and bucket configuration gates, so choosing an S3 backend is not enough
when `DIALECTICORE_OBJECT_STORAGE_ENDPOINT` or
`DIALECTICORE_OBJECT_STORAGE_BUCKET` is blank; the detailed object-storage
component still reports endpoint reachability, credential pairing, and bucket
probe evidence.
It also exposes an object-storage credential-pair gate when exactly one of the
S3 access-key or secret-key references is configured, failing production posture
before media workers discover the mismatch during writes.
Production deployment readiness now also consumes publisher-target evidence,
requiring an enabled publisher target, at least one enabled non-mock/non-dry-run
live target, and known non-unhealthy publisher health for production delivery
posture. It separately keeps the `publisher_automated_live_target_available`
gate when automated live publishing is enabled, so live automation still
requires an enabled target that declares `automated_live_publish`.
Live non-mock publishing now also requires a completed `production_manifest`
asset linked to the selected export package before the publisher service opens
external HTTP or YouTube delivery, and the safe manifest asset ID, URI, checksum,
and schema version travel in the publish delivery payload for audit handoff.
Database migration readiness now adds a `database_migrations` health component
that compares the running database's recorded Alembic revision with the current
migration head, exposes sanitized current/head revision evidence and normalized
readiness gates, exports `dialecticore_database_migration_status` metrics, feeds
the production deployment-readiness `database_schema_at_head` gate when revision
inspection succeeds, and fails live-provider readiness in production when
persistent storage has no recorded revision or is behind the app image. The
behind-head case is now covered through API, deployment-readiness, live-readiness,
and Prometheus regression assertions using a real older Alembic revision.
Discussion prompt versioning now has a persisted administration slice:
`examples/prompt-templates.json` seeds `discussion_prompt_template_records`
with template ID, version, variable set, creator, creation timestamp, enabled
flag, and change summary for default moderator and panelist prompts; API and Web
UI controls can create, update, enable, disable, and delete unused templates;
participant profiles validate references to managed template IDs; and the model
gateway renders discussion prompts from repository-backed templates for every
provider adapter while recording normalized
`prompt_template_id`/`prompt_template_version` audit metadata on every generated
turn. Prompt-template assignment is now guarded by enabled state and participant
type, and templates referenced by profiles cannot be disabled or retyped, so
future generation cannot silently use inactive or role-incompatible prompt text.
Structured API request logging now adds per-request correlation IDs, propagates
`x-correlation-id` responses from supplied `x-correlation-id` or `x-request-id`
headers, emits JSON `dialecticore.api_request_log.v1` events with method, path,
status, duration, client host, and path-derived episode/asset/approval/turn and
ComfyUI workflow IDs where present, keeps generic job-ID support for concrete
job routes without inventing IDs on publish-job list routes, and exposes
`DIALECTICORE_LOG_LEVEL` for self-hosted logger verbosity. Log-level setup now
rejects invalid operator values with a concise setting-specific startup error
and has regression coverage for applying updated levels without adding
duplicate structured handlers.
Model-generation observability now records normalized latency and token-usage
metadata for every model gateway adapter, persists the evidence on discussion
turns, aggregates it into a `model_generation_observability` system-health
component, and exports Prometheus metrics for total/provider turn counts,
latency sums/counts, and token counts where providers return usage. Persisted
raw model-provider responses, including regenerated-turn history snapshots, are
recursively sanitized before storage so differently cased token/secret fields
cannot leak through discussion audit evidence.
Asset-production observability now aggregates persisted production assets into
an `asset_production_observability` system-health component and Prometheus
metrics for asset counts, failure rates, duration sums/counts, and storage byte
totals across all assets plus asset-type and language breakdowns.
Workflow-duration observability now aggregates durable run timestamps, stage
history, and output-language asset spans into a
`workflow_duration_observability` health component and Prometheus metrics for
episode production duration, stage duration, and per-language production
duration.
Queue-wait observability now aggregates persisted submitted/running asset age,
completed submitted-to-completed asset spans, and publish-job requested-to-
completed spans into a `queue_wait_observability` health component and
Prometheus queue-wait duration metrics. The Web UI System Health component list
now renders compact, sanitized summaries for model-generation, asset-production,
workflow-duration, and queue-wait observability so operators can see counts,
coverage, rates, and breakdowns without expanding raw health JSON.
Production-manifest asset entries now expose normalized per-item audit fields
for creation/update time, source turn/evidence references, reproducibility
metadata, retry history, manual edits, and latest approval state, so auditors do
not have to infer those goal-required fields solely from generation metadata.
Continue-after-manual-edit recovery now carries a compact
`manual_edit_evidence.v1` bundle through workflow control, run signals,
Temporal signal logs, and audit records, binding a restartable continuation to
the relevant sanitized manual edit events instead of only recording that an
operator clicked continue.
The Web UI Workflow Evidence panel now surfaces that manual-edit recovery bundle
as safe event counts, edit-type categories, post-failure scope, and checksum
presence, without exposing raw audit event IDs, actors, comments, or asset IDs.
Workflow replay now also projects safe manual-edit signal evidence from the
event log and compares projected signal content with `workflow_control.run`, so
restart/audit checks catch missing or divergent manual-recovery evidence instead
of only comparing signal counts.
Production manifests also redact embedded asset generation metadata, model
endpoint capabilities, QC-result details, and publish-job delivery and result
snapshots for token, secret, password, API-key, authorization, and credential
fields before they become durable audit handoff artifacts, including
camelCase/PascalCase provider key variants. Manual-edit audit details embedded
in per-asset manifest entries now pass through the same recursive redaction
path while retaining safe edit linkage and reason context.
Publish readiness now distinguishes valid package-linked `production_manifest.v1`
assets from invalid completed manifest assets, warns on invalid manifests,
exports invalid-manifest counts in Prometheus, and blocks live non-mock
publishing when the selected package only has an invalid manifest placeholder.
Production-manifest generation itself now requires the selected export package's
latest `youtube_package_integrity` QC result to be present and non-failing, so
durable audit handoff artifacts cannot be minted from unchecked or rejected
delivery packages.
Live publishing now also requires a recorded non-failing
`youtube_package_integrity` QC result for the selected export package, and
publish readiness/Prometheus expose completed packages that are missing package
QC or whose latest package QC is failing as live-delivery blockers.
Production completion readiness now applies the same delivery handoff evidence
before workflow control can record `COMPLETED`: the selected export package must
have non-failing `youtube_package_integrity` QC, and the selected production
manifest must embed a structurally valid package-linked `production_manifest.v1`
payload.
Production-manifest validity also requires chaptered timelines to retain
matching chapter entries in the embedded delivery-package manifest across
workflow completion readiness, system health/readiness summaries, and worker
handoff evidence.
Workflow production handoff evidence now also includes a
`character_configuration_handoff.v1` section for active playable speakers,
blocking review/delivery readiness when a participant profile is missing or a
speaker no longer has model endpoint/model ID, voice profile, or visual profile
assignment evidence. Production completion readiness now applies that same
character-configuration gate before `COMPLETED` can be recorded, preventing old
media/package artifacts from satisfying delivery completion after cast
configuration is removed or left incomplete. Handoff and completion readiness
also block stale transcript/model and generated media evidence when recorded
model endpoint/model ID, voice profile ID, or visual profile ID no longer
matches the speaker's current assignments.
Workflow handoff now includes `localized_output_handoff.v1`, and completion
readiness includes `localized_output_readiness.v1`; both block readiness when
configured non-canonical output languages are missing, not approved, missing
localized transcript semantic QC, or failing localized transcript semantic QC.
System health, live-readiness, and Prometheus package-manifest evidence now also
count completed export packages missing checked thumbnail or generated subtitle
package entries, so operators can detect package evidence blockers before final
completion or live publishing attempts.
The Web UI completion gate now labels missing/failing package QC and invalid
production-manifest blockers explicitly, and publish-job health summaries expose
packages missing or failing package QC alongside invalid and missing
package-linked manifests plus the latest invalid-manifest reason without
rendering raw storage URIs or full package identifiers.
The top-level System Health summary now also shows missing and failing package
QC counters plus invalid and missing manifest counters next to package and
manifest coverage, keeping live-delivery blocker visibility available without
opening detailed readiness JSON. Headless metrics regression coverage now locks
the same completed-package, valid-manifest, invalid-manifest, missing/failing
package-QC, and missing-manifest counter set under
`dialecticore_publish_package_manifest_count`.
Production-manifest validity now requires an embedded `delivery_package.asset_id`
that matches the selected export package across workflow completion readiness,
system health/readiness summaries, and live publishing, preventing schema-valid
but unlinked manifest placeholders from satisfying delivery handoff gates.
When a production manifest embeds delivery-package checksum, storage URI, or
package ID evidence, completion readiness, worker orchestration, system
health/readiness, and live publishing now require those values to match the
selected completed export package, blocking stale manifests after package
replacement or mutation.
The Web UI system-health publish summary maps production-manifest invalid
reasons through a safe allowlist, including stale package checksum, storage URI,
and package ID evidence, so operators see the corrective class without exposing
raw package object paths or identifiers from unexpected backend reason strings.
Live publishing now treats package QC with either `status=fail` or
`severity=fail` as blocking, aligning publisher behavior with completion
readiness, manifest generation, and system-health package-QC summaries.
Publish delivery QC now records payload package ID/checksum match evidence plus
production-manifest handoff evidence (`production_manifest_asset_id`, checksum
presence, and schema version), and fails live non-mock delivery payloads that do
not match the selected package or lack the manifest ID, checksum, or
`production_manifest.v1` schema version.
Durable workflow-worker orchestration now counts publishing-stage production
manifest creation as stage progress in the persisted
`workflow_worker_orchestration_attempt.v1` history, aligning replay/audit
evidence with the publishing worker heartbeat contract. The workflow worker and
production-control durable log now share the same stage-progress counter, with
regression coverage locking production-manifest handoff as publishing progress.
The Web UI API client now preserves structured FastAPI error details instead of
collapsing failures to status codes, and the dashboard renders a compact action
error panel for failed operator mutations. This makes blocked deletes,
validation failures, readiness gates, CA/bootstrap errors, and remote adapter
failures visible in normal UI operation with frontend regression coverage for
string details, validation arrays, and text fallbacks.
Native B1 ComfyUI endpoint setup now mirrors the B1 Voicebox trust pattern: a
Web UI preset configures `https://comfy.ai.b1.germering` with bearer
credential-reference auth, preserved native ComfyUI route metadata,
scheduler-aware job evidence, `/object_info` health probing, and CA bootstrap
from the public B1 root certificate endpoint into runtime-state certificate
storage. Participant character configuration now supports Web UI image upload
for visual profiles; uploaded PNG/JPEG/WebP reference images are validated,
stored through object storage, audited with checksum/size evidence, propagated
into ComfyUI prompt inputs, and surfaced in timeline/render scene metadata so
video scenes can prove which participant image basis is being animated.

## Verification Policy

Each increment must be proven against its acceptance criteria with current
evidence. Passing tests for a subset do not close the broader product goal.
