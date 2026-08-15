# DialectiCore

DialectiCore is a self-hostable production platform for AI panel and talk-show
episodes. The target system is defined in `goal_DialectiCore.md`.

Current checkpoint: Increment 6 production-hardening groundwork.

- Typed episode definition contracts.
- Provider-neutral model client interface with deterministic mock,
  OpenAI-compatible, Ollama, Anthropic-compatible, Mistral-compatible, and
  generic HTTP providers, with model endpoint health/capability checks in the
  API and Web UI plus dashboard create/update/delete controls.
- Turn-by-turn discussion engine with host moderation, deterministic
  score-based speaker selection, private participant memories, session-linked
  turns/memory records, required-dimension coverage tracking, duration tracking,
  raw turns, transcript-turn version links, broadcast transcript, controller
  evidence, and audit events.
- Dashboard participant profile administration for model assignment, sampling,
  prompt templates, voice bindings, visual bindings, and enabled state.
- FastAPI endpoints for creating episodes, running the synthetic discussion path,
  and inspecting status/transcript/audit data.
- API-level workflow pause, resume, cancel, stage approve/reject,
  failed-stage retry, and continue-after-manual-edit controls with durable
  `workflow_control` metadata, versioned production-run stage/signal
  history, replayable workflow event journal, optional external Temporal bridge
  signal-attempt evidence, production-start guards, status flags, Web UI
  controls, replay/bridge evidence visibility, and audit evidence.
- Workflow-advance production handoff evidence that verifies approved
  transcript turns are linked to completed speech, primary character visuals,
  shot-planned reusable reaction loops/studio scenes, subtitles, timeline
  segments, preview/final renders, delivery packages, and production manifests.
- Structured system health summary for repository/database reachability,
  Alembic migration revision drift,
  local object-storage path readiness or S3/MinIO endpoint reachability,
  FFmpeg/ffprobe availability, endpoint configuration,
  Temporal runtime mode/readiness, queue counts, failed assets, completed
  renders, workflow orchestration/dispatch evidence, workflow retry backlog
  counts, production-run attention counts, deployment posture,
  runtime path writability/free-space evidence,
  active credential-reference readiness, auth runtime/session-log readiness,
  publish job outcome counts, and Web UI health visibility.
- Live-provider readiness preflight for production posture, credential
  references, model providers, remote Voicebox and ComfyUI endpoints, object
  storage, runtime paths, backup readiness, Redis, auth, Temporal runtime, and
  worker registry coverage, worker signal state, workflow orchestration,
  workflow retry backlog, production-run state, media queue state,
  publish-job outcomes, and publisher targets, surfaced through the API and Web UI without exposing raw
  secrets.
- Credential provisioning checklist for configured `env:`, `file:`, and
  Docker-secret references, including disabled live targets by default, with API
  and Web UI evidence for resolved/unavailable secret labels only, plus
  health/Prometheus counts that separate active missing references from
  disabled-target provisioning gaps, including assembled database password
  references, sanitized invalid or unsupported reference targets, and separate
  invalid-reference counts so raw pasted credential values are not reflected in
  readiness payloads.
- Production deployment readiness flags Compose placeholder/default API,
  MinIO, and Postgres secrets by safe setting label without exposing values,
  and flags wildcard API CORS in production, including Web UI System Health
  visibility for the labels and origins to replace.
- Production Compose secret override with Docker-secret references for the API
  key, database password, and MinIO password, plus assembled database URLs that
  avoid embedding the Postgres password in `DIALECTICORE_DATABASE_URL`, and
  production host-port resets that keep direct API and bundled MinIO access
  internal unless an operator adds an explicit override.
- Server-sent live status stream for health, queues, worker counts, active
  leases, stale workers, and recent audit evidence, with Web UI stream
  connection visibility.
- File-backed worker heartbeat registry with active/stale/failed worker counts,
  API/UI worker status visibility, Temporal runtime and native worker execution
  metrics, Redis fan-out/signal reachability metrics, generic component
  readiness-check Prometheus metrics, malformed runtime-state cleanup counters,
  and shared Docker runtime-state volume wiring.
- File-backed per-role worker leases for adapter scale safety, with active/
  expired lease counts, malformed lease cleanup counters, lease expiry metrics,
  Web UI lease visibility, and shared Docker runtime-state coordination.
- Concrete Docker worker polling for research, discussion, localization,
  source-bound claim QC, per-turn speech production, synchronized subtitle
  generation, character visual production, Voicebox and ComfyUI remote-job
  sync, timeline building, preview/final rendering, thumbnail/package
  preparation, and automated dry-run publishing, plus a `workflow-worker`
  coordinator that runs the same gated stage pollers in order, with per-role
  leases and heartbeat details;
  unsupported worker-role configuration records a failed heartbeat and exits
  non-zero.
- Health-gated Docker Compose startup where PostgreSQL, Redis, and MinIO must be
  healthy before API migrations/health checks, and workers wait for the API
  health endpoint before polling.
- Database migration readiness evidence compares the running database's
  recorded Alembic revision with the current migration head, exports
  Prometheus status, feeds the deployment-readiness schema gate when revision
  inspection succeeds, and blocks production live-readiness when a self-hosted
  database has no revision or is behind the app image.
- Docker build-context hygiene that excludes local databases, runtime storage,
  caches, build outputs, `.env` files, and common secret material from API,
  worker, and Web UI image builds.
- Non-root API and worker image runtime using UID/GID `10001`, with intended
  writable state constrained to the mounted `/data/object-storage`,
  `/data/runtime-state`, and `/data/backups` paths.
- Compose runtime isolation for API and worker roles with read-only container
  roots, dropped Linux capabilities, `no-new-privileges`, and explicit `/tmp`
  tmpfs for temporary media/backup work.
- Production Web UI container that serves the built React app through
  unprivileged Nginx with read-only runtime isolation and proxies same-origin
  `/api` requests to `production-api`.
- Durable workflow-worker orchestration journals for active production runs,
  with per-stage activity checksums, run-level last-pass pointers, retry queue
  entries, backoff metadata, external Temporal stage-dispatch envelopes with
  idempotency keys, audit evidence, and coordinator-started production runs so
  research and discussion stage transitions are journaled from the first
  selected-episode workflow advance.
- Backup archive creation/listing/restore-validation with database table
  exports, local object-storage or remote S3/MinIO bucket object inclusion,
  runtime-state file inclusion, Web UI backup control, Docker backup volume
  wiring, Web UI health display plus Prometheus backup archive readiness
  evidence, and audited create/apply restore events.
- Initial RBAC guard for `/api/v1/*` with API-key references, viewer/reviewer/
  editor/producer/admin roles, trusted reverse-proxy identity headers with
  upstream group-to-role mapping, provider-managed bearer session
  introspection, file-backed provider-session revocation hooks, Web UI
  revocation recording/listing, permission-classified routes, Web UI security
  policy visibility, browser session login/logout controls for provider bearer
  or API-key access, `env:`/`file:`/Docker-secret credential references, and safe
  auth settings plus provider-session revocation/decision-log counts in system
  health and Prometheus metrics.
- Publisher target administration, dry-run YouTube publish jobs, opt-in
  automated live worker publishing, generic HTTP package delivery, and an
  initial YouTube Data API resumable video-upload adapter linked to completed
  delivery packages, with YouTube-native thumbnail and caption uploads,
  credential-reference OAuth access/refresh tokens, persisted publish payloads,
  audit events, publish QC, Web UI publisher target create/update/delete
  controls, health checks, production deployment gates for enabled live
  non-mock/non-dry-run publisher targets with known non-unhealthy health,
  publish-job evidence, global publisher-target health/capability counters, and
  global publish-job health/Prometheus counters.
- First-class production manifests for completed delivery packages, with
  machine-readable episode configuration, timeline segment, render/package
  manifest, asset checksum/storage, QC, approval, publish-job, and evidence
  lineage records.
- SQLAlchemy/Alembic persistence for episode aggregates with searchable
  production-state columns, searchable `asset_records`, and a global audit event
  stream.
- Web UI episode editor for title, central question, duration, discussion
  controls, language fidelity policy, research policy, media dimensions and
  generation flags, workflow retry settings, quality gates, host, and three
  panelists.
- Persisted language profile administration with default `en`/`de` catalog,
  BCP 47 tags, default localization mode, subtitle direction, line-breaking
  policy, voice defaults, audited dashboard create/update/delete controls, and
  backup/restore coverage.
- Deterministic localization scaffold that creates source-linked localized
  transcript versions after transcript approval.
- Voicebox audio planning that creates per-turn planned audio assets and
  completeness QC before generation.
- Persisted Voicebox endpoint and voice profile administration with mock local
  defaults, health checks, capability discovery, audited dashboard
  create/update/delete controls, and participant voice assignment.
- Executable audio generation scaffold that completes planned assets through the
  deterministic mock Voicebox adapter or normalized remote `/tts` submissions.
- Metadata-based audio QC and selective failed/targeted audio regeneration
  without full episode regeneration.
- Manual remote Voicebox job synchronization for submitted/running async TTS
  jobs, including QC reruns and audit evidence.
- Remote Voicebox job cancellation and cancellation-aware retry recovery for
  submitted/running async TTS jobs.
- Local or S3-compatible object-store audio writes for generated WAV assets
  with measured media probe metadata and stable `object://bucket/key` or
  `s3://bucket/key` URIs.
- Actual TTS duration feedback from completed audio assets into source
  discussion turns and per-speaker actual speaking-time balance.
- Remote Voicebox HTTP result URL download into configured object storage for
  immediate and async TTS results.
- Waveform and FFmpeg loudness audio QC for peak level, true peak, silence
  ratio, clipping, integrated LUFS, and normalization target metadata.
- Normalized provider or estimated phoneme/viseme timing tracks for later
  lip-sync and styled subtitle workflows.
- `voicebox-adapter` worker polling for submitted/running remote Voicebox jobs.
- Subtitle generation scaffold that creates transcript-linked WebVTT/SRT assets
  with cue provenance, checksum metadata, QC, and audit events.
- Word-timestamp subtitle cue segmentation with sync drift, overlap, and line
  length QC.
- Persisted ComfyUI endpoint, workflow registry, and visual profile
  administration with mock defaults, health checks, audited dashboard
  create/update/delete controls, workflow JSON editing, and participant visual
  assignment.
- Visual asset planning scaffold that creates transcript-turn-linked primary
  video, B-roll placeholders, reusable participant reaction/listening loops,
  reusable studio scene assets, and shot-plan metadata with
  plan-completeness QC.
- ComfyUI visual job lifecycle scaffold with remote `/prompt` submission,
  `/history/{job_id}` sync, cancellation, object-store writes for returned media,
  deterministic render-ready mock SVG outputs, worker polling, Web UI controls,
  and visual generation QC.
- ComfyUI workflow input patching through explicit node bindings or common input
  names, plus PNG/JPEG header and FFprobe video probe evidence for stored visual
  outputs.
- Preset-specific default ComfyUI API workflow templates for talking-head video,
  reaction/listening loops, topic B-roll images, and studio-wide scenes, with
  explicit patch bindings exposed through the workflow registry, preset sampler
  settings, frame-count patching, and motion/camera/lighting control metadata.
- Deterministic SVG citation-card/fallback-still materialization for failed
  remote ComfyUI jobs, with provider failure metadata, SVG dimension probing,
  render-suitability QC counts, and Web UI fallback evidence.
- Rerunnable visual media integrity QC for generated visuals, covering storage,
  checksums, media probes, PNG pixel evidence, SVG structural evidence, render
  suitability, dimensions, FPS, video probe integrity, audio-duration alignment,
  lip-sync readiness, measured lip-sync offset, character identity/style
  consistency, and Web UI QC evidence.
- Increment 4 timeline groundwork: stored, checksummed `EpisodeTimeline` JSON
  assets with transcript/audio/visual/subtitle segment linking, chapters,
  typed `timeline_entity` projections, read/build/edit API routes, scene-based
  Web UI timing/transition edits, timeline QC, and Web UI build evidence.
- Generic manual asset replacement for corrected operator media, preserving
  source-entity lineage, marking superseded assets as `replaced`, rewriting
  active timeline references, and auditing the replacement.
- Initial FFmpeg rendering path with render presets, stored render manifests,
  preview and final MP4 assets, timeline scene composition with normalized
  multi-layer visual scene plates for studio, talking-head, B-roll, and reaction
  media, deterministic split-screen/focus layout policies, motion primitive
  evidence, cross-scene transition flags, FFmpeg xfade boundaries between
  adjacent scene plates, FFmpeg per-frame eased overlay position transforms plus
  scale/opacity keyframes, source-reveal arc motion, speaker-spotlight bounce
  motion, rounded-rectangle/diamond/circular alpha masks, timeline-ordered
  dialogue audio assembly, subtitle cue burn-in, citation overlay compositing,
  FFprobe-backed render QC, targeted preview/final render approval gates,
  thumbnail extraction, YouTube delivery ZIP exports, YouTube resumable video upload
  scaffolding, and Web UI render/delivery controls.
- Increment 5 evidence-pack path with configuration, supplied source metadata,
  explicit URL retrieval/tool logs, operator-configured live discovery with
  query/rank provenance, source de-duplication/scoring, source-text
  claim/statistic extraction, deterministic fact-pattern extraction for
  definitions/mechanisms/recommendations/tradeoffs, source-grounded
  relationship/quantity facets, deterministic causal/scope context extraction,
  optional trusted source-bound external advanced extraction,
  source ranking/freshness policy summaries, cross-source
  agreement/conflict summaries with shared-term, stance, and claim-facet
  relationship evidence, deterministic claim support groups, human per-source
  review decisions, research-review approval, evidence-pack QC, prompt grounding,
  persisted `ResearchSource`/`EvidenceClaim` projections with read APIs,
  claim/citation QC, source-linked citation-card overlays, evidence-linked
  render/delivery manifests, package-linked production manifests, API routes,
  and Web UI research controls.
- Persisted project administration with API/Web UI create, update, delete,
  episode linking, health counts, and audit evidence.
- Docker Compose skeleton for API, UI, Postgres, Redis, MinIO, Temporal, and
  independently scalable worker roles with shared object-storage for
  media-producing roles and workflow coordination, plus runtime-state/backup
  volumes.
- Minimal React/Vite dashboard shell for Increment 1 monitoring.

## Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
alembic upgrade head
uvicorn app.main:app --app-dir backend --reload
```

The API container runs `alembic upgrade head` before starting Uvicorn.
For a user-systemd local runtime that matches the always-on Compose worker
roles, use:

```bash
scripts/start_dev_api_service.sh
scripts/start_dev_worker_services.sh
```

Then run `.venv/bin/python scripts/worker_readiness_smoke.py` to verify the
coordinator heartbeat. The local helper starts the single `workflow-worker`
coordinator, which owns the ordered local production pass. Set
`DIALECTICORE_DEV_WORKER_ROLES` only when diagnosing an individual worker role;
do not run those roles alongside the coordinator against the same SQLite
development database. It starts `temporal-worker` automatically when the loaded
environment configures external Temporal with
`DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED=true`.

The first runnable production path uses the `mock` provider type so it does not
require paid APIs.

Open `http://127.0.0.1:5173` during local development to create an episode from
the editor or use `Create Real Pilot` for a short German four-character
frontier-model test, start production, review transcript QC, regenerate or
exclude turns, approve the transcript, inspect the selected episode's Pilot Run
Readiness panel for real discussion, speech, visual-animation, and render blockers,
advance the selected episode through the ordered worker stages, manage
projects, create localizations, produce speech, plan audio assets, inspect or
generate audio assets, rerun audio QC, retry failed audio, generate subtitles,
produce visuals, plan visual assets, sync or cancel remote Voicebox jobs,
inspect Voicebox and ComfyUI configuration, generate visual assets, sync or
cancel remote ComfyUI jobs, build/approve research evidence packs, run claim QC,
build/edit timeline assets, render previews and final videos, generate
thumbnails, approve final renders, export YouTube packages, inspect
system/worker/security health,
generate package production manifests, let the workflow worker automatically
complete a run once completion-readiness gates pass, inspect system/worker/security health,
create/list backup archives, and view recent audit events.

## Example Episode

`examples/episode-definition.yaml` defines a short synthetic episode with one
host and three panelists. `examples/prompt-templates.json` provides matching
moderator and panelist prompt templates for the default participant profiles.
`examples/synthetic-test.env` keeps model, media, research, publishing, auth,
and storage settings on mock/local paths so smoke runs do not require paid APIs.

## Roadmap

The implementation follows the increments in `goal_DialectiCore.md`:

1. Functional core.
2. Audio and localization.
3. Visual production.
4. Timeline and rendering.
5. Research and advanced QC.
6. Production hardening.
