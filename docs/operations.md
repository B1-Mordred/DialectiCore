# Operations

Production operations require observable stages, durable retries, cancellation,
audit logs, health checks, and worker restart safety. The current scaffold
exposes structured health, workflow-control, worker heartbeat, live status
events, metrics, and audit surfaces; Temporal-backed durability and worker
scaling continue through Increment 6.

Persistence stores episode aggregates, projections, assets, configuration
records, and audit events in SQLAlchemy-managed tables. Created episodes,
transcript-review results, lifecycle/configuration evidence, production-run
state, replay journals, retry queues, and media/publishing projections survive
process restarts.

Operators can inspect the latest global audit records with
`GET /api/v1/audit-events?limit=50`. The stream includes episode creation,
discussion turns, transcript revisions, approvals, model endpoint changes, and
participant profile changes. Secret values are never written to audit details.

The production API emits structured JSON request logs under the
`dialecticore.api` logger. Each request accepts `x-correlation-id` or
`x-request-id`, sanitizes and propagates the value back as `x-correlation-id`,
or generates a `req-...` ID when no request ID is supplied. Request log events
use `dialecticore.api_request_log.v1` evidence with method, path, status,
duration, client host, correlation ID, and path-derived IDs such as
`episode_id`, `asset_id`, `approval_id`, `turn_id`, and ComfyUI
`workflow_id` when relevant. Job routes that carry a concrete job identifier
are also logged with `job_id`; publish-job list routes only log their enclosing
`episode_id`. `DIALECTICORE_LOG_LEVEL` controls the DialectiCore logger level.

Operators can inspect runtime and queue health with `GET /api/v1/system/health`.
The summary reports repository/database reachability, Alembic migration
revision drift, object-storage backend readiness, local object-storage
checked-path writability, S3/MinIO endpoint
reachability for remote object storage, bucket configuration,
credential-reference coherence, backup directory/archive
readiness, runtime path readiness, deployment readiness posture,
FFmpeg/ffprobe availability with normalized tool availability gates,
configured and enabled provider endpoints with normalized collection gates,
including failed gates when enabled model, Voicebox, or ComfyUI endpoints are
unhealthy or still have unknown health, episode counts,
paused/cancelled/failed episode counts, pending audio/visual jobs, failed
assets, completed renders, configured publisher targets, active/stale/failed
worker counts, worker signal counters, active publish job totals, completed and
failed publish job counts, dry-run/live publish job counts, auth runtime
readiness, workflow orchestration attempts, Temporal dispatch status, workflow
retry backlog counts, production-run attention counts, provider-session
revocation counts, decision-log counts, and safe settings such as configured
backend names, worker polling limits, auth header names, provider-session claim
names, default role labels, and whether optional credential-reference pairs are
configured.
Remote model, Voicebox, ComfyUI, and publisher-target health probes redact
provider-supplied capability/device metadata before persistence, backup export,
API responses, or operator dashboards can display it.
The database component emits normalized `readiness_checks` and
`failed_readiness_checks` for repository reachability in both successful and
failed query paths.
The Web UI System Health panel surfaces the same object-storage evidence,
including local checked-path state for filesystem deployments and S3 endpoint,
bucket-probe, region, and credential-reference pairing status for remote
deployments, without showing secret values. The object-storage summary also
shows failed readiness gate names and the boolean readiness checklist so local
path, endpoint, credential-pair, bucket-name, and bucket-availability failures
are visible without opening raw JSON.
Live Provider Readiness summarizes the same storage posture before production:
local deployments show checked path and writability, while S3/MinIO deployments
show endpoint TCP reachability, credential-pair readiness, bucket name
configuration, bucket availability, bucket-probe failure reasons, and a
normalized readiness checklist with failed gate names for local path and S3
deployments.
The `deployment_readiness` component is informational outside production. When
`DIALECTICORE_ENV=production`, it degrades if SQLite is still selected,
API CORS still allows wildcard browser origins, authentication is disabled or
lacks a viable configured mode, object storage is local instead of S3-compatible,
Redis fan-out or worker signals are disabled, or backup/runtime-state/local
object-storage paths are missing, unwritable, or below
`DIALECTICORE_RUNTIME_PATH_MIN_FREE_BYTES` when that floor is configured.
Auth modes only count as viable when their runtime prerequisites match
`auth_runtime`, including non-empty required headers, HTTPS provider
introspection, provider claim names, and complete optional provider client
credentials.
It also degrades when production still uses Compose placeholder/default secret
material such as the sentinel API key, MinIO root user/password, Postgres
password, default Postgres password embedded in the configured database URL, or
the same sentinel values resolved through configured `env:`, absolute `file:`,
or `docker-secret:` references. The health payload reports safe setting labels in
`unsafe_default_secret_labels`, not the secret values.
It also checks that the selected Temporal runtime contract is internally
configured: `bridge` mode requires signal transport plus an endpoint, and
`external` mode requires a backend address, task queue, and native-worker
enablement.
The component also emits normalized `readiness_checks` and
`failed_readiness_checks` for the boolean production posture gates while keeping
the legacy `checks` map for deployment metrics and Web UI pass/fail cards.
When `DIALECTICORE_DATABASE_URL` is assembled from component settings, the
`database_url_resolved` gate shows whether the database password reference can
be resolved without exposing the secret.
The Web UI System Health panel lists the same deployment-readiness checks as
pass/fail cards so operators can see the failed production posture item without
scraping Prometheus or expanding the raw component payload. When placeholder or
default secrets are active, the dashboard also shows the safe setting labels
that must be replaced. The compact deployment summary also shows safe model,
Voicebox, and ComfyUI endpoint posture counts, including configured, enabled,
remote, missing-base-URL, unhealthy, and unknown-health totals.
Live Provider Readiness also summarizes the selected environment, database
driver, object-storage backend, Temporal mode, Temporal contract validity, and
the first missing Temporal settings when bridge or external mode is incomplete,
plus the same safe AI/media endpoint posture counts.

For local development outside Docker Compose, start or restart the API through
the helper so the user-scoped systemd unit receives credential references from
`.env` without printing secret values:

```bash
scripts/start_dev_api_service.sh
```

The helper loads `B1_API_KEY` and `OPENROUTER_API_KEY` when present, starts
`dialecticore-api-dev.service` with uvicorn on `127.0.0.1:8000`, and preserves
the same process-local environment shape that live provider readiness expects.
Use `DIALECTICORE_DEV_ENV_FILE=/path/to/env` to point it at a different env
file.

For a quick real-provider smoke before a full pilot, run:

```bash
.venv/bin/python scripts/live_provider_smoke.py \
  --participant-id chatgpt \
  --output output/smoke/chatgpt-live-provider-smoke.wav \
  --evidence-output output/smoke/live-provider-smoke-latest.json
```

The script reads credential references from the process environment or `.env`
without printing secret values. It calls the configured API readiness endpoint,
submits a small OpenRouter chat-completion request for the selected participant
model, generates one B1 Voicebox WAV through the selected voice profile, and
prints a JSON summary. It writes the same redacted result when
`--evidence-output` is supplied. A `status=pass` result proves the model and
speech path are usable for an audio-first pilot. A Voicebox HTTP failure is kept
as structured `voicebox_stream_smoke_evidence.v1` with status code, content
type, byte count, RIFF/WAVE detection, profile ID, engine, endpoint ID, and
required action instead of being collapsed into a generic exception. When the B1
speech path fails, append a Codex-readable B1 fix note with:

```bash
.venv/bin/python scripts/live_provider_smoke.py \
  --skip-openrouter \
  --participant-id claude \
  --evidence-output output/smoke/live-provider-smoke-voicebox-latest.json \
  --requirements-output /home/mordred/media-requirements.md
```

Before a real multi-character talkshow test, verify every configured
participant model and voice in one pass:

```bash
.venv/bin/python scripts/live_provider_smoke.py \
  --all-participant-models \
  --all-participant-voices \
  --frontier-cast \
  --output output/smoke/live-provider-voice.wav \
  --evidence-output output/smoke/live-provider-smoke-all-voices-latest.json \
  --requirements-output /home/mordred/media-requirements.md
```

This writes one WAV per requested participant when generation succeeds, adds
`participant_scope.scope=frontier_cast`, `model_participants`,
`model_summary`, `voicebox_participants`, and
`voicebox_summary.failed_participant_ids` so the operator can distinguish a
model-routing issue, a single broken frontier voice profile, and a global
Voicebox outage. `--frontier-cast-voices` remains accepted as a compatibility
alias. Use `--participant-ids` for a custom cast. Omit both cast filters only
when every stored participant profile should be tested, including legacy/mock
characters.
The full `scripts/live_episode_smoke.py` production run also accepts
`--requirements-output` and defaults it to `/home/mordred/media-requirements.md`.
When the production report shows failed B1 speech generation, failed B1
managed-media execution/smoke, missing B1 media presets, or an unhealthy native
ComfyUI gateway, it appends a bounded **DialectiCore Production Provider
Handoff** section for Codex on the B1 server. After writing that section, the
smoke refetches the production test report so the exported JSON includes the
updated sanitized `provider_repair_handoff` row in the same run. Native-visual
preflight blocks use the same handoff path before exiting, so missing B1 media
presets or unhealthy ComfyUI prompt admission do not disappear just because the
workflow was stopped before media generation. Use `--no-requirements-update` for
dry local rehearsals where no handoff file should be touched.
When `--provider-smoke-preflight` blocks on a selected participant voice, the
episode smoke also writes the same Voicebox recheck section used by
`scripts/live_provider_smoke.py`, including the failed participant IDs, HTTP
status, content type, byte count, RIFF/WAVE detection, and required B1-side
action.

Operators can run the same frontier-cast gate from the Web UI Dashboard with
`Live Provider Readiness -> Check Cast`, or through the API:

```bash
curl --fail \
  -H "Content-Type: application/json" \
  -d '{
    "frontier_cast": true,
    "include_models": true,
    "include_voices": true,
    "user_id": "operator"
  }' \
  http://127.0.0.1:8000/api/v1/system/live-provider-preflight
```

This endpoint runs selected participant model and Voicebox checks concurrently,
records `live_provider.cast_preflight_checked` audit evidence, and returns
`model_summary` plus `voicebox_summary` with failed participant IDs. A
`status=pass` result is the admission signal for starting a real frontier-cast
discussion with speech. A `blocking_sections=["voicebox"]` result means model
discussion can run but speech production must wait for B1 Voicebox generation
to return valid WAV payloads.

The same output also includes the current ComfyUI prompt-admission state; if
that state reports
`hardware_resource_policy`, the full visual-video smoke must wait until the B1
appliance can admit `/prompt` work.

After starting the local API with `scripts/start_dev_api_service.sh`, the
July 30, 2026 ChatGPT provider smoke passed with OpenRouter model
`openai/gpt-4.1-mini`, B1 bridge voice `A_ChatGPT`, and a valid WAV written to
`output/smoke/chatgpt-live-provider-after-env.wav`. Live provider readiness
still reported native ComfyUI blocked by `hardware_resource_policy`, so this
proves the model-plus-speech path without claiming full native visual readiness.

The same pilot can also be prepared from the Web UI. Open
`http://userver:5173/`, choose **Episodes**, and use the default Episode Editor
state or **Load Real Pilot Preset**. The preset creates a two-minute German
`audio_first` frontier-model talkshow with Claude as moderator and ChatGPT,
DeepSeek, Grok, Gemini, and Mistral as panelists. `Output Languages` preserves
the first listed language as the canonical source language, so `de` creates a
German-source pilot. The episode list is summary-backed, so it remains usable
when completed smoke runs have accumulated large transcript, media, and manifest
payloads. It polls the compact list at a slower interval and refreshes
immediately after operator actions; selecting one episode loads the full
aggregate for detail panels. Use **Create From Editor** or **Create Real
Pilot**, check the Pilot Run Readiness panel, then use **Open Characters** when
the panel reports participant model, voice, portrait, full-body, or visual
workflow setup blockers. Use **Refresh ComfyUI** in that panel to recheck
current native ComfyUI prompt admission before starting a visual pilot.
Start the episode with **Start Production** when `audio_first` has no blockers.
If native visual readiness is blocked by ComfyUI prompt admission, the UI keeps
that diagnostic separate from the audio-first start decision.

For a focused B1 managed media smoke before a full native-visual pilot, run:

```bash
.venv/bin/python scripts/b1_managed_media_smoke.py \
  --model image-default \
  --evidence-output output/smoke/b1-managed-media-smoke-image-default.json \
  --requirements-output /home/mordred/media-requirements.md
```

Operators can run the same check from the dashboard with **Live Provider
Readiness -> B1 Smoke**, or through the API:

```bash
curl -fsS \
  -H 'Content-Type: application/json' \
  -d '{"model":"image-default","requirements_output":"/home/mordred/media-requirements.md","user_id":"operator"}' \
  http://127.0.0.1:8000/api/v1/system/b1-managed-media-smoke
```

For a quick live cast preflight before starting an audio-first pilot, the API
also supports default frontier-cast checks without a JSON body:

```bash
curl -fsS -X POST http://127.0.0.1:8000/api/v1/system/live-provider-preflight
curl -fsS http://127.0.0.1:8000/api/v1/system/live-provider-preflight
```

Both forms run the default ChatGPT, Claude, DeepSeek, Grok, Gemini, and Mistral
model-plus-voice preflight, record `live_provider.cast_preflight_checked` audit
evidence, and return participant summaries with failed model IDs or voice
profile IDs. Send a JSON body only when narrowing participants, disabling model
or voice checks, or overriding the spoken smoke text. The latest audited live
preflight is also summarized in each episode production test report as
`live_provider_preflight`; failed OpenRouter or Voicebox checks appear as scoped
operator actions so a mock-complete episode cannot hide real frontier-cast
provider blockers.

The script and API read `B1_API_KEY` from the process environment or `.env`,
verify the stored B1 CA certificate, submit a tiny managed media job to
`https://api.ai.b1.germering/v1/media/jobs`, polls
`/v1/media/jobs/{job_id}`, and writes `b1_managed_media_smoke_evidence.v1`.
The API additionally records a global `b1_managed_media.smoke_checked` audit
event. When `--requirements-output` or `requirements_output` is supplied and
submit, polling, runner, or timeout evidence is failing, it appends a
Codex-readable B1 appliance fix section to `/home/mordred/media-requirements.md`.
The note includes the model alias, modality, operation, job ID, terminal state,
failure category/message, native ComfyUI prompt ID, artifact count, and
acceptance criteria for proving the B1-side media fix.
Exit `0` means the managed runner completed and produced terminal evidence.
`status=runner_failed` means the API hub accepted and scheduled the job but the
B1 runner failed terminally; rerun with `--allow-runner-failure` when preserving
that remote failure as non-blocking diagnostic evidence while fixing B1.
ComfyUI endpoint health also checks the B1 API hub model catalog at
`/v1/models` when `remote_nodes_api_base` is configured. This is a no-render
readiness probe: it proves aliases such as `image-default`, `image-edit`,
`image-upscale`, `video-text`, and `video-image` are advertised before native
visual production starts, but it does not prove the GPU runner can complete a
job. Use the managed media smoke above for terminal runner evidence.

The July 30, 2026 live audio-first pilot smoke completed end to end with real
OpenRouter discussion and B1 Voicebox speech after two production-hardening
fixes: discussion outputs now strip evidence references that are not present in
the episode evidence pack, and discussion duration control reserves TTS
headroom before real speech generation. Evidence episode
`199de42d-1155-48d6-97ee-17ea900a6d59` completed with 19 discussion turns, 19
completed B1 audio assets, one subtitle asset, one synchronized timeline, an
approved 30s preview render, an approved 175.68s final render under the 180s
maximum, YouTube package
`38ed36a0-9fa8-46e9-b64b-9050fa20bb02`
(`sha256:a66b1c0147072034d19621629858e2228b3ab3bb684bb706113fd7063595e3ed`),
completed production manifest `d0adad2e-6203-4164-9d59-9dcf50b7684d`
(`sha256:4c0c885680043576103d2abe8aa599200604160936874c4754d9aa9a5c92ea6e`),
and completed dry-run publish job `1ae15164-3f12-47c4-be83-4ef21fe835a6`
(`mock://youtube/mock-youtube-1ae15164`). This proves the current
audio-first/model/speech/fallback-render/package path; native ComfyUI character
animation still depends on prompt admission health for the B1 ComfyUI endpoint.

For a source-bound pilot, use **Load Evidence Pilot** or **Create Evidence
Pilot** in the Episodes UI. This keeps the German frontier-model show setup but
adds a manual evidence brief, enables required source links, requires research
approval, enables citation-card planning, and blocks unsupported high-impact
claims. The normal workflow then builds an evidence pack before discussion,
waits at `research_review`, and only lets participants cite source IDs that
exist in the approved pack. The source-bound live smoke can be repeated from the
CLI with:

```bash
.venv/bin/python scripts/live_episode_smoke.py \
  --research-mode manual \
  --max-advances 8 \
  --evidence-output output/smoke/live-episode-smoke-evidence-backed.json \
  --artifact-output-dir output/smoke/live-episode-artifacts
```

The July 30, 2026 evidence-backed smoke produced episode
`15d1f510-be6b-482d-a344-0ce71cbc9399`, built and approved one evidence pack,
completed 13 source-bound discussion turns, completed 13 B1 audio assets,
rendered preview and final videos, produced one delivery package and one
completed production manifest, completed dry-run publish job
`852b186c-2704-41eb-b427-9d02336c9fc3`, and wrote smoke evidence
`output/smoke/live-episode-smoke-evidence-backed.json` with SHA-256
`9ed8ac07a4c8a32ade982169e7feecc85f4f650046f01f29c545cf09360dfa62`.
New evidence-backed runs also write `production_test_report` and
`production_test_summary` entries that aggregate completion readiness, the
declared production target, final render, export package, production manifest,
approval state, publish job state, and native-visual readiness into one
operator-facing acceptance object. Stored deliverables in
`production_test_report.deliverables` include same-origin `download_url` values
for the final render, export package, and production manifest when the
configured object-store file is available through the API. If a required final
render, export package, or production manifest cannot be resolved to an
available object-store file, the report stays at `status=warning` and adds a
`*_not_downloadable` blocker with `download_missing_reason` evidence.
Every smoke evidence file includes `invocation`
(`live_episode_smoke_invocation.v1`) with the script name, sanitized argv, and a
shell-quoted rerun command. Secret-like flags are redacted before writing, so
operators can compare or repeat a run without copying credentials into the
evidence artifact.
The report also embeds `package_inspection`, which opens the package ZIP and
checks the internal `youtube-package.json`, video entry, manifest schema, and
manifest-declared thumbnail/subtitle entries, and file-list consistency. A
package that is downloadable but not inspectable adds
`export_package_not_inspectable`. The smoke script also performs a direct package
inspection request and bounded range-download checks for the final render,
export package, and production manifest. Those checks are written as
`package_inspection` and `artifact_download_checks`; a completed smoke run fails
if the production test report is not `pass`, the package inspection fails, or
any required artifact cannot be streamed through its API download URL.
When `--artifact-output-dir` is supplied, the same checks save the full
downloaded deliverables as `final-render.mp4`, `youtube-export-package.zip`, and
`production-manifest.json`, and verify SHA-256 checksums whenever the production
test report includes them.
It also writes `production_acceptance_summary`, a compact comparison-stable
rollup of the episode status, production target, blockers, required deliverable
IDs/checksums/file sizes, package inspection counts, and per-artifact download
status. The summary also includes compact `publish_evidence` binding fields so
operators can verify that the latest dry-run or live publish job still matches
the selected package and current production manifest without opening raw
publish payloads. It also includes `workflow_run_until_blocked`, a bounded
record of the last **Run Until Review** execution: status, stop reason, pass
count, progressed stages, pending review stages, completion status, compact
talk-show handoff blockers/readiness/asset IDs, `next_handoff_action`, and
bounded orchestration attempt IDs. It also includes
`real_life_test_readiness`, a bounded summary that separates local/mock package
acceptance from actual real-life readiness for `audio_first` and
`native_visual` modes, including live provider preflight readiness, managed
media smoke readiness, mode-specific blockers, and the next rerun action. Use
this summary as the first smoke artifact to compare between runs; the full
`production_test_report` remains available for deeper debugging.
The production-test API response exposes the same first-glance evidence as
`acceptance_summary`, so operators can inspect it in the Web UI under
**Workflow Evidence** -> **Real Test Report** before opening the smoke JSON.
Use **Refresh Evidence** in that card for a non-mutating refresh of workflow
replay, completion readiness, pilot readiness, live-provider readiness, and the
production-test report. The card also turns `operator_next_action` and
`operator_next_actions` into focused operator controls: approval blockers expose
**Open Approvals**, publish blockers expose **Publish Dry Run** when a target is
configured, passing delivery reports expose direct final-render, package, and
manifest downloads, and B1 provider blockers expose the matching cast, media
smoke, or native-visual retry controls. Workflow handoff actions from the latest
**Run Until Review** evidence are also promoted into the same action list unless
a concrete Voicebox/B1 provider failure has higher priority, so transcript,
preview-render, final-render, and media-handoff gates do not get buried behind
generic package or publish work. The same report renders scoped action rows for
every `operator_next_actions[]` entry, preserving backend scope, status,
failed-check count, asset-count evidence, review stages, stop reasons, and
bounded blockers so workflow, speech, B1 media, native visuals, delivery
artifacts, package inspection, publishing, and completion follow-ups can be
scanned independently.
To refresh the same evidence for an already completed episode without starting,
approving, or advancing workflow state, run:

```bash
scripts/live_episode_smoke.py \
  --episode-id <episode-id> \
  --evidence-only \
  --evidence-output output/smoke/live-episode-evidence-refresh.json
```

This mode still reloads cast readiness, completion readiness, final pilot
readiness, live-provider readiness, the production test report, package
inspection, and artifact download checks, so it is useful after B1 media or
Voicebox repairs when a full talkshow rerun would waste GPU time.
The same report now includes `media_readiness.audio_generation` for Voicebox
speech assets and promotes provider failures to the top-level
`operator_next_action`. If B1 speech generation is returning HTTP 500s, the
next action is `fix_voicebox_generation_then_retry_audio_assets` instead of a
generic readiness blocker. If only some Voicebox assets have completed, speech
readiness is `partial` and the next action is
`produce_remaining_speech_assets`. When those remaining or completed speech
assets resolve to an unhealthy Voicebox endpoint, provider health takes
precedence and the top-level action becomes
`fix_voicebox_generation_then_retry_audio_assets`; this prevents operators from
retrying speech while the B1 generation canary is known to be failing.

To run a short end-to-end episode through the normal API workflow gates from the
command line, run:

```bash
.venv/bin/python scripts/live_episode_smoke.py \
  --evidence-output output/smoke/live-episode-smoke-evidence.json \
  --artifact-output-dir output/smoke/live-episode-artifacts
```

The smoke first calls `/workflow/start` and records
`workflow_start.schema_version=live_smoke_workflow_start.v1`, the durable
`run_id`, run state, current stage, and compact workflow replay evidence. A
duplicate start is treated as `already_running` only when the API reports that
an active workflow run already exists. Workflow advance summaries also include
`workflow_admission.schema_version=workflow_stage_admission_summary.v1`; stage
execution only admits episodes with a running durable workflow run, so
`missing_run_episode_count>0` means the operator must start production before
advancing media stages. To check only the durable start gate
without advancing into B1-dependent media work, run the cleanup form below. It
cancels only the episode created by this start-only smoke and records
`cleanup.schema_version=live_smoke_start_only_cleanup.v1` in the evidence file.

```bash
.venv/bin/python scripts/live_episode_smoke.py \
  --provider-smoke-preflight \
  --cleanup-preflight-draft \
  --max-advances 0 \
  --no-auto-approve \
  --cleanup-start-only-run \
  --evidence-output output/smoke/live-episode-smoke-durable-start.json
```

For interactive production from the Web UI, use **Run Until Review** on the
Episode Production page. That action calls
`/workflow/run-until-blocked`: it starts the selected episode's durable run when
needed, advances the normal worker stage chain in bounded passes, and stops at
the next transcript, preview-render, final-render, research, or localization
approval gate. It returns all pass summaries plus the current completion gate,
so **Workflow Evidence** can still show which stages produced speech, visuals,
timeline, render, package, or completion evidence. It does not auto-approve
human review gates. After each bundled run, **Workflow Evidence** shows a
**Run Until Review** card with the stop reason, pass count, progressed-stage
count, pending review stages, compact handoff status, `next_handoff_action`,
readiness flags, key asset evidence, and bounded production blockers before the
lower-level worker-pass details.
The same compact stop evidence is persisted under
`workflow_control.last_run_until_blocked`, so the card can survive a browser
reload without replaying the worker pass. The persisted evidence also carries a
compact production handoff with bounded blocking reasons, readiness flags, key
asset IDs, and `next_handoff_action`, so a stopped run points at the next
talk-show production action rather than only saying `no_progress` or
`pending_approval`. Production test reports and live smoke acceptance summaries
expose the same compact evidence as `workflow_run_until_blocked`, so exported
real-life test artifacts show why the producer run stopped.

The full smoke creates a two-minute German frontier-model panel with the same
six-character default cast as the UI preset: Claude as moderator, with ChatGPT,
DeepSeek, Grok, Gemini, and Mistral as panelists. Episode creation applies these
roles from the episode definition, so a reusable participant profile can be a
moderator in one show and a panelist in another without mutating the stored
profile default. The smoke repeatedly calls `/workflow/run-until-blocked`, the
same bundled producer action used by **Run Until Review** in the Web UI. Between
bounded production attempts it auto-approves transcript, preview-render, and
final-render review gates unless `--no-auto-approve` is set. Its evidence file
records compact `run_until_blocked_results` with handoff status,
`next_handoff_action`, bounded blockers, and readiness/asset evidence, plus the
underlying worker pass summaries, final asset counts, completion-readiness
status, pilot-readiness status, production-test status, concrete delivery
artifacts, and dry-run publish job. It is intentionally a real workflow smoke,
not a provider bypass:
discussion turns come from the configured participant model endpoints, speech
comes from the configured Voicebox profiles, timeline/render/package/publish use
the normal API services, and ComfyUI prompt failures are handled by the existing
visual fallback policy. Use `--participant-ids` and
`--moderator-id` to intentionally narrow or reorder the cast for a diagnostic
run. The script creates episodes with `workflow.production_target=audio_first`;
normal episode definitions default to `native_visual`. When B1 ComfyUI reports
`hardware_resource_policy`, the script can still prove the
audio/model/render/package path. `pilot_readiness.status`, `blockers`, and
`warnings` describe the declared production target, while `pilot_modes`,
`stages`, `all_stage_blockers`, and `all_stage_warnings` retain the full native
visual diagnostics. Before starting the workflow, the smoke writes
`cast_readiness`, a compact per-character check that every selected participant
exists, is enabled, has a model endpoint and model ID, and has enabled Voicebox
and visual profiles. A missing or disabled cast binding returns
`status=cast_preflight_blocked` without starting production. With
`--provider-smoke-preflight`, it then runs real model and Voicebox smokes for
that selected cast after loading `.env` by default, and returns
`status=provider_preflight_blocked` before
workflow start if any selected character cannot answer or produce a RIFF/WAVE
voice sample. Use `--provider-env-file` if the provider credentials live in a
different env file. With `--cleanup-preflight-draft`, a draft episode created
only for that blocked preflight is cancelled through the normal workflow action
and recorded as `preflight_cleanup` evidence. The smoke result also records
`discussion_speaker_coverage` after the workflow attempt. A full
completed smoke cannot pass unless every selected cast member has at least one
playable discussion turn; missing speakers are reported as
`selected_cast_speaker_coverage_incomplete`. The smoke result also writes
`pilot_readiness.native_visual_preflight_summary`, a compact native-visual
triage block with prompt-admission status, blocked endpoint count, redacted
admission code/detail, and affected participant/workflow/visual-profile IDs.
Use `pilot_readiness.target_status` and `selected_pilot_mode` for the declared
production target. The script writes the stable JSON smoke result to the
evidence path and prints the path, byte count, and SHA-256 checksum alongside
the same result for audit and later comparison. The episode pilot-readiness
payload includes `pilot_modes`:
`audio_first` covers real model discussion, speech, and render/package workflow,
while `native_visual` also requires ComfyUI-backed character animation to be
healthy. The completion readiness payload also includes
`visual_source_summary`, which counts native, fallback, and missing primary
visual turns so a completed episode can still prove whether its visuals came
from ComfyUI or from fallback generation. For `native_visual` episodes,
completion readiness requires native primary visuals for every playable turn;
fallback primary visuals are completion-ready only when the episode declares
`workflow.production_target=audio_first`.

When B1 media services are under repair, use the deterministic synthetic mode to
prove the local talkshow production pipeline without consuming remote GPU time:

```bash
.venv/bin/python scripts/live_episode_smoke.py \
  --synthetic-mock-mode \
  --title "Synthetic Mock Talkshow Pipeline Check" \
  --target-duration-minutes 2 \
  --permitted-deviation-percent 50 \
  --max-advances 8 \
  --evidence-output output/smoke/synthetic-mock-live-episode-smoke.json \
  --artifact-output-dir output/smoke/synthetic-mock-artifacts
```

Synthetic mode uses the seeded `host`, `optimist`, `skeptic`, and
`practitioner` cast unless `--participant-ids` or `--moderator-id` is explicitly
overridden. It temporarily enables `mock-voicebox` and `mock-comfyui`,
temporarily points enabled ComfyUI workflows at `mock-comfyui`, and temporarily
enables the selected mock participant profiles. Normal completion and controlled
errors restore the endpoint records, participant `enabled` flags, and workflow
`comfyui_endpoint_id` values before the script exits. The resulting evidence is
a local API/workflow/render/package proof, not proof that B1 Voicebox,
OpenRouter, or native ComfyUI animation are healthy.

The July 30, 2026 synthetic smoke produced episode
`0fd2d91c-2471-4da9-b401-dd20700ba384`, completed 13 discussion/audio/video
turns, rendered a 1920x1080 final MP4, built a YouTube export ZIP, wrote a
production manifest, completed a dry-run publish job, and validated API
downloads for all required deliverables. Evidence was written to
`output/smoke/synthetic-mock-live-episode-smoke-20260730.json` with SHA-256
`cc7b1788f1e5aafc4bc967de263d1c171860a23b5c06c46365fe197fe38ea0a7`; the saved
deliverables are under `output/smoke/synthetic-mock-artifacts/`.

For any completed or in-progress episode, operators can fetch the same compact
test evidence without rerunning the smoke harness:

```bash
curl -fsS \
  http://127.0.0.1:8000/api/v1/episodes/<episode-id>/production-test-report \
  | jq '{status, production_target, audio_first_test_ready, native_visual_test_ready, publish, blockers}'
```

To inspect media readiness without the larger deliverable payload:

```bash
curl -fsS \
  http://127.0.0.1:8000/api/v1/episodes/<episode-id>/production-test-report \
  | jq '.media_readiness | {audio_first_ready, native_visual_ready, native_prompt_admission_ready, managed_media_catalog_ready, managed_media_required_endpoints, managed_media_missing_preset_endpoints}'
```

To inspect only the delivery bundle contents:

```bash
curl -fsS \
  'http://127.0.0.1:8000/api/v1/episodes/<episode-id>/youtube-package/inspect' \
  | jq '{status, file_count, manifest_schema_version, chapter_count, subtitle_count, evidence_source_count, issues}'
```

The Web UI shows this report in **Workflow Evidence** as **Real Test Report**.
`status=pass` means the selected production target satisfies the completion
gate, has a completed publish job, and the final render, export package, and
production manifest can be downloaded through the configured object store, and
the delivery package can be inspected as a coherent YouTube bundle.
`audio_first_test_ready=true` means the current appliance can be tested end to
end through discussion, speech, render, package, manifest, and dry-run publish.
`native_visual_test_ready=true` requires completed native primary visuals, not
only a configured ComfyUI endpoint. `native_visual_test_ready=false` keeps the
character-animation gap visible even when audio-first delivery is valid.
`media_readiness` records the native prompt-admission and selected B1
managed-media preset catalog checks that fed that decision. These checks do not
prove that the B1 GPU runner can finish a managed job; use
`scripts/b1_managed_media_smoke.py` for terminal runner and artifact evidence.
When an audio-first real-life report passes while native visuals still rely on
fallback assets, **Next Action** keeps the acceptance pass visible and also lists
nonblocking speech, B1, and native-visual follow-ups such as rerunning cast
preflight, rerunning managed media smoke, or retrying fallback visuals as native
assets after provider recovery.
When those follow-ups are present, the same **Real Test Report** card exposes
operator buttons for **Check Cast**, **Run B1 Smoke**, and **Retry Native
Visuals** so the repair loop can be launched from the evidence card instead of
requiring the operator to find the separate media controls. The report-scoped native retry follows
`visual_source_summary.fallback_primary_visual_turn_ids`, so its count matches
the primary character-turn fallback evidence in the report; the broader media
workbench retry can still be used for all fallback visual assets, including
studio or reusable scene material.
The Web UI report separates `Visual source`, `Speech generation`, `B1 media
execution`, `B1 media smoke`, `Repair handoff`, and `Publish evidence` rows so
audio-first readiness, fallback visual use, Voicebox provider health, B1 runner
failures, publish/package/manifest binding, and the
`/home/mordred/media-requirements.md` B1-side fix handoff remain visible without
opening the raw JSON payload. The handoff row is intentionally sanitized: it
reports file presence, section counts, latest headings, and whether Voicebox/B1
media sections exist, but not the full markdown body. B1-related scoped action
rows repeat the repair-handoff status/path so the operator can connect the
recommended retry or provider-fix action with the handoff file immediately.
`Publish evidence` can be
`warning` for a valid dry-run when the current production manifest was refreshed
after the original publish payload; inspect its package and manifest binding
phrases before treating it as a delivery problem.
The **Localization & Media** panel also includes a compact **Visual Job
Workbench** once visual assets exist. It groups visual jobs by status, role, and
provider adapter, then lists the most actionable assets first: failed, running,
submitted, planned/cancelled, and fallback visuals. Use this after B1 recovery to
decide whether to sync remote ComfyUI/B1 jobs, cancel stale submitted jobs, retry
native generation, or accept fallback render-ready assets for an audio-first
pilot.
When a completed episode contains render-ready fallback visuals, the **Retry
Native Visuals** action targets only those fallback visual asset IDs and submits
them again with `regenerate=true`, `fallback_on_failure=false`, and
`local_fallback_only=false`. Successful submission clears stale fallback
storage/checksum and fallback metadata from the retried asset, records the native
B1 managed-media job ID, and leaves the asset in submitted/running state for the
normal **Sync ComfyUI Jobs** path.
The same panel exposes **Delivery Artifacts** for the current episode: preview
render, final render, thumbnail, YouTube package, and production manifest. Each
slot shows the latest active asset status plus compact media metadata. Download
links are shown only when the asset is completed and has a stored object URI, so
pending, failed, or metadata-only assets remain visible without presenting broken
delivery links.
The report's final render, package, and manifest rows are clickable when the
stored artifacts can be streamed through
`GET /api/v1/episodes/{episode_id}/assets/{asset_id}/download`.
The CLI smoke evidence mirrors those UI checks with HTTP status, byte-count,
content-type, range, and disposition metadata for each required artifact. It
also records `initial_pilot_readiness`, `final_pilot_readiness`, and a compact
`live_provider_readiness` summary. The live-provider summary keeps the
Voicebox, ComfyUI, B1 managed-media smoke, workflow-orchestration,
production-run, and publishing posture that would affect a real run, including
failed readiness gates and redacted endpoint failure evidence, without copying
credential references or native remote profile UUIDs into the smoke artifact.
When `--cleanup-start-only-run` is used, the artifact also includes
`post_cleanup_live_provider_readiness` so a start-only harness check can prove
the temporary active-run warning was cleared after cancellation.

To run the same harness against the full native visual acceptance target after
B1 ComfyUI prompt admission is healthy, pass:

```bash
.venv/bin/python scripts/live_episode_smoke.py \
  --production-target native_visual \
  --evidence-output output/smoke/live-episode-smoke-native-visual.json \
  --max-advances 8
```

For `native_visual`, the smoke harness first checks episode pilot readiness and
returns `status=preflight_blocked` without advancing production when the native
visual mode is not ready. Before that preflight it refreshes enabled non-mock or
native ComfyUI endpoint health through
`POST /api/v1/comfyui-endpoints/{endpoint_id}/health`, so the decision uses
current B1 prompt-admission state rather than stale stored health. The evidence
includes `comfyui_health_refresh` with compact status and prompt-admission
fields. Use `--no-refresh-native-visual-health` only when intentionally testing
against stored endpoint health. The preflight evidence still includes
`initial_pilot_readiness.native_visual_preflight_summary` so the blocked
endpoint, admission code/detail, and affected characters are captured before any
expensive workflow work starts. Use
`--ignore-native-visual-preflight-blockers` only when deliberately exercising
fallback/error handling despite a known native visual blocker.

`--target-duration-minutes` and `--permitted-deviation-percent` can be adjusted
for longer provider-generated speech. A native-visual smoke is expected to fail
completion readiness with `native_primary_visuals_missing` while native or
managed B1 visual generation fails and fallback visuals are used. Distinguish
prompt-admission failures (`hardware_resource_policy` or HTTP admission errors)
from managed-runner failures (`gpu_runner_error` after `/v1/media/jobs` accepted
the job); they point to different B1 appliance layers.

The `runtime_paths` component reports `runtime_paths.v1` evidence for the
configured backup directory, runtime-state directory, and local object-storage
directory when local storage is active. It records whether each path is required,
configured, present, has an existing parent, has a writable target or parent,
the free bytes visible from the checked filesystem, and whether those bytes meet
the configured free-space floor. Live Provider Readiness names unavailable or
low-space required paths and shows the configured free-space floor beside the
pass/warning/fail summary. Runtime path health and live readiness also emit
normalized `readiness_checks`/`failed_readiness_checks` for configured required
paths, available/writable required paths, and required-path free-space
sufficiency. The Web UI System Health panel surfaces the
same per-path checked location, free-space, parent, and writability evidence
next to the aggregate missing-path and low-space counters.
The `credential_references` component resolves active `env:`, absolute `file:`,
and `docker-secret:` references used by enabled model, Voicebox, ComfyUI, and
publisher targets plus active database password, auth, and S3 object-storage
settings. It reports
checked, resolved, and unavailable counts, owner-type/scheme breakdowns, and the
failing reference labels without returning secret values. It also emits
normalized `readiness_checks`/`failed_readiness_checks` for active credential
resolution and supported reference schemes; references outside `env:`, `file:`,
and `docker-secret:` are counted under the `unsupported` scheme, while malformed
raw values are counted under the `invalid` scheme. The Web UI System Health
panel surfaces the aggregate checked/resolved/unavailable/unsupported/invalid
counts, failed gates, and owner-type and scheme breakdowns, but keeps raw
reference labels out of the dashboard.
Live Provider Readiness summarizes the same active reference counts,
owner-type breakdowns, scheme breakdowns, and safe record count so operators can
locate which integration class needs secret provisioning before live production.
`GET /api/v1/system/live-provider-readiness` provides a single production
preflight view over the same safe evidence plus enabled model-provider,
Voicebox, ComfyUI, publisher, object-storage, backup-storage, Redis, auth,
Temporal runtime, worker registry, worker signal, workflow orchestration,
workflow retry backlog, production-run state, media queues, publish jobs, and
runtime path checks. Missing or
unwritable required runtime paths are treated as hard blockers because backup,
worker coordination, and local media writes cannot be trusted. Missing backup
archives or missing dry-run restore validation are surfaced as warnings so
operators can correct disaster-recovery posture before a live run. Missing
active worker heartbeats, incomplete worker-role coverage, scheduled workflow
retries, paused active production runs, already-running production runs,
pending media jobs, and submitted publish jobs are also surfaced as warnings so
supervisors can start expected worker roles and let
due/backoff/run/media/publishing work drain before production. Worker registry
status itself degrades when active heartbeats do not cover every configured
worker role, so `/api/v1/system/workers`, system health, live readiness, and
metrics share the same missing-role posture. Worker registry readiness also
emits normalized `readiness_checks` and
`failed_readiness_checks` for supplied status evidence, active heartbeats, full
configured-role coverage, fresh/non-failed/non-degraded heartbeats, and
parseable runtime-state files. The media queue
readiness card breaks that work down by pending audio, submitted/running audio,
submitted/running visual, submitted/running subtitle, planned assets, completed
renders, and failed audio/visual/subtitle assets so operators can route the
backlog to the right adapter before a live run. It also emits normalized
`readiness_checks`/`failed_readiness_checks` for failed media assets and pending
audio, visual, and subtitle work gates. Aggregate failed/pending counters remain
historical across all episodes, while `current_failed_*` and `current_pending_*`
counters drive live-readiness gates and exclude terminal cancelled or failed
episodes. The `pending_*_jobs` and `pending_job_count` values are aggregate
queued/running counts; submitted and running fields are sub-breakdowns for
operator routing rather than additional work to add on top. Workflow
orchestration errors, blocked Temporal dispatches, exhausted workflow retries,
failed media assets, failed publish jobs, failed worker signal delivery, active
`drain` or `stop_after_current` worker signals, failed or cancelled active
production runs, and automated live publishing enabled without an enabled
`automated_live_publish` target are blockers because
operator intervention is required before a live run can be trusted. Worker
signal readiness also emits normalized `readiness_checks` and
`failed_readiness_checks` for supplied signal-summary evidence, failed delivery,
and active blocking control signals. System health exposes the same worker-signal
gate names, attention count, and failed-gate list in the `worker_signals`
component and System Health dashboard summary. The Web UI Live
Provider Readiness panel surfaces pass, warning, failure, blocker, and
per-category readiness evidence so operators can provision remote services,
active credential references, future live-target credential references, volumes, backup
validation, Redis transport, auth/session runtime state, Temporal execution,
model-provider endpoint type/base-URL/health breakdowns, Voicebox/ComfyUI base
URL coverage, adapter-type breakdowns, endpoint health breakdowns, bounded safe
endpoint issue entries for missing base URLs, unhealthy records, and unknown
health records, normalized endpoint readiness gates, orchestration
attempt/dispatch health, due/backoff/unknown/exhausted retry backlog,
media queues, publish-job outcomes, publisher dry-run/live/automated capability
posture, worker signal state, worker registry health, per-media failed/pending
work, and active production-run state before running live production jobs.
`GET /api/v1/system/credential-provisioning` returns the companion
`credential_provisioning_plan.v1` checklist. It inventories configured
credential references, including disabled live targets by default, groups the
environment variable names, Docker secret names, and absolute file paths that
must be provisioned, and reports which references currently resolve. The Web UI
Credential Provisioning panel surfaces the same checklist next to the live
readiness report without exposing raw secret values. System health also exposes
`credential_provisioning` with active/all reference counts; missing active
references degrade health, while missing disabled-target references are counted
as planning evidence. It also emits normalized `readiness_checks`,
`failed_readiness_checks`, and bounded missing active/disabled-target reference
samples with owner, field, reference, scheme, target, and reason metadata. The
Web UI System Health panel also shows active missing, total missing, future
disabled-target missing, failed gates, and target-kind counts as sanitized
deployment evidence.
Live Provider Readiness summarizes the same active/all/disabled-target missing
counts, target-kind counts for environment variables, Docker secrets, and files,
unsupported-reference counts, invalid-syntax counts, attention count, and
live-readiness policy so operators can see whether missing secrets block the
current run or only future live-target cutover, and whether a configured scheme
or pasted raw value needs operator correction
before it can be provisioned.
The `workflow_orchestration` component reports total coordination attempts,
progressed/failed stages, orchestration errors, Temporal dispatch totals,
blocked dispatches, failed/progressed stage breakdowns, blocked/ready dispatch
stage breakdowns, and latest attempt/dispatch evidence. Historical counts remain
visible for all episodes, while `current_*` counts drive live-readiness gates
and ignore terminal cancelled or failed episodes so abandoned runs do not block a
new pilot. The
`workflow_retries` component reports active scheduled and exhausted stage retry
entries by status, stage, and schedule readiness. It separates scheduled entries
that are due now from entries still held by backoff, reports unknown retry
schedules, reports due/backoff/unknown/exhausted stage breakdowns, includes the
latest and next retry identifiers/target stages, and carries historical/resolved
retry counts separately from active backlog totals. The
`auth_runtime` component reports whether authentication is disabled, ready, or
degraded by missing mode configuration, invalid default roles, unreadable
provider-session runtime files, missing provider introspection configuration, or
an unavailable configured API-key reference. It also preflights provider-session
introspection URL scheme safety plus optional client ID/secret references, and
degrades on non-HTTPS introspection, mismatched references, or unavailable pairs
without returning the full URL or either secret value. Enabled API-key,
trusted-identity, and provider-session modes also require non-empty runtime
header names, and provider sessions require non-empty user and groups claim
names for introspection mapping.
It also emits normalized `readiness_checks` and `failed_readiness_checks` for
auth-mode viability, header-name and provider-claim-name configuration, API-key
reference resolution, trusted/provider default-role validity, provider
introspection configuration and HTTPS policy, provider-session client credential
readiness, and provider-session revocation/decision-log readability.
Live Provider Readiness summarizes the same
authentication mode evidence, including API-key reference readiness,
trusted-identity and
provider-session mode enablement, provider introspection configuration,
provider-session revocation counts, retained decision-log counts, denied/error
decisions, failed auth gates, and unreadable runtime-file blockers without
exposing API keys or bearer tokens.
The `backup_storage` component reports
whether the configured backup path or parent exists, how many `*.tar.gz`
archives are present, whether the checked target or parent path is writable by
the API process, and whether the latest archive's `manifest.json` can be read.
It computes dashboard checksums only for bounded-size archives; larger archives
report `checksum_status=skipped` and `checksum_not_evaluated` instead of being
fully hashed during routine health/UI refresh. Run dry-run restore validation to
produce full archive integrity evidence for those larger files. The component
also reports whether the latest archive has matching dry-run restore validation
audit evidence for the current evaluated archive checksum, including
restore-plan counts, validation age, and safe per-scope object-storage and
runtime-state content-validation summaries when available, plus readable,
unreadable, validated, and unvalidated archive counts. If an archive changes after validation, the
restore-validation status becomes `checksum_mismatch` and the archive is treated
as unvalidated until validated again. It also emits
normalized `readiness_checks` and `failed_readiness_checks` for backup path
availability/writability, archive availability, archive readability, latest
manifest readability, and latest restore-validation currency. The Web UI Backups
panel surfaces the same status, validation coverage, latest manifest
readability, restore-validation status, latest archive age, content-validation
summaries for object-storage/runtime-state, failed gates, and health reason next
to backup creation and archive listing controls.
The Live Provider Readiness panel also summarizes backup archive count,
readable/validated/unvalidated/unreadable coverage, latest manifest readability,
latest restore-validation status, validation age, and failed backup gates so
disaster-recovery drift is visible in the same preflight used before live
production.

Worker liveness is visible through `GET /api/v1/system/workers`. The registry is
stored as JSON heartbeat files under `DIALECTICORE_RUNTIME_STATE_PATH` so the API
and Docker worker containers can share a simple coordination surface before full
Temporal worker coordination lands. Each heartbeat includes role, worker ID,
status, details from the latest poll, heartbeat age, and a stale flag computed
from `DIALECTICORE_WORKER_HEARTBEAT_TTL_SECONDS`. The same runtime-state volume
also powers Docker worker healthchecks through `python -m app.worker_healthcheck`,
which requires a fresh non-failed heartbeat for the configured role and current
container hostname. Production deployment readiness flags worker heartbeat TTLs
that are not greater than the worker poll interval because they can make healthy
idle workers appear stale between polling passes, and flags worker lease TTLs
that are not greater than the poll interval because scaled duplicates can take
ownership before the current worker's next pass. It also stores per-role worker leases. Adapter workers acquire and renew a lease
before scanning remote jobs; a second scaled container for the same role reports
a heartbeat but skips the polling pass until the active lease expires after
`DIALECTICORE_WORKER_LEASE_TTL_SECONDS`. System health and Live Provider
Readiness include the configured worker role names, active role names, missing
role names, stale/failed/degraded role names, and worker breakdowns by status
and role so supervisors can start the exact missing worker class before a live
run instead of inferring it from aggregate counts. `GET /api/v1/system/metrics` exports the
same health, queue, publish job, backup archive, worker liveness, lease expiry,
remote object-storage reachability, workflow orchestration/dispatch evidence,
workflow stage retry backlog, production-run state, deployment readiness
posture, Redis runtime reachability, model-generation latency/token usage, and worker signal counters as
Prometheus-style plain text for
external monitoring. Generic component readiness metrics include
`dialecticore_component_readiness_check`, with component, check, and pass/fail
labels for every boolean `readiness_checks` entry emitted by system-health
components. Deployment readiness metrics include
`dialecticore_deployment_readiness_status`,
`dialecticore_deployment_readiness_issues`, and
`dialecticore_deployment_readiness_check`. Database migration readiness is
exported as `dialecticore_database_migration_status`, with sanitized
current/head Alembic revision labels and a production enforcement label. Runtime
path metrics include
`dialecticore_runtime_path_ready`,
`dialecticore_runtime_path_state`,
`dialecticore_runtime_path_free_bytes_sufficient`, and
`dialecticore_runtime_path_free_bytes` for backup, runtime-state, and local
object-storage path readiness, with `dialecticore_runtime_path_state` exposing
path configuration, target/parent existence, directory status, writability, and
free-space sufficiency booleans. Credential-reference metrics include
`dialecticore_credential_reference_count` for active references by status,
owner type, and scheme. Credential-provisioning metrics include
`dialecticore_credential_provisioning_count` for active/all reference counts,
unavailable active/all references, disabled-target unavailable references, and
scheme target counts without secret names or values. Workflow
orchestration metrics include `dialecticore_workflow_orchestration_count` for
attempts, stages, errors, dispatches, worker, policy, and dispatch-status
breakdowns. Workflow retry metrics include
`dialecticore_workflow_stage_retry_count` for total, status, stage, and
schedule-status breakdowns.
Model-generation observability metrics include
`dialecticore_model_generation_turn_count`,
`dialecticore_model_generation_latency_ms_sum`,
`dialecticore_model_generation_latency_ms_count`,
`dialecticore_model_generation_token_usage_records`,
`dialecticore_model_generation_token_count`,
`dialecticore_model_generation_provider_turn_count`,
`dialecticore_model_generation_provider_latency_ms_sum`,
`dialecticore_model_generation_provider_latency_ms_count`, and
`dialecticore_model_generation_provider_token_count`, derived from persisted
discussion-turn metadata. System health exposes the same evidence in the
`model_generation_observability` component, including provider breakdowns and a
readiness check for turns missing latency metadata.
Asset-production observability metrics include
`dialecticore_production_asset_count`,
`dialecticore_production_asset_failure_rate`,
`dialecticore_production_asset_duration_ms_sum`,
`dialecticore_production_asset_duration_ms_count`, and
`dialecticore_production_asset_storage_size_bytes`, with aggregate, asset-type,
and language breakdowns. These metrics are derived from persisted asset status,
duration, language, type, and object-size evidence, including local
`object_storage_path` probing when a stored byte count is not present.
Workflow-duration observability metrics include
`dialecticore_episode_production_duration_ms_sum`,
`dialecticore_episode_production_duration_ms_count`,
`dialecticore_workflow_stage_duration_ms_sum`,
`dialecticore_workflow_stage_duration_ms_count`,
`dialecticore_language_production_duration_ms_sum`, and
`dialecticore_language_production_duration_ms_count`. Run and stage durations
come from durable `workflow_control.run` timestamps and stage history, while
per-language production duration is derived from persisted asset timestamp
spans for each output language.
Queue-wait observability metrics include
`dialecticore_queue_wait_duration_ms_sum` and
`dialecticore_queue_wait_duration_ms_count`, with pending and completed states
plus queue-type and language breakdowns where applicable. Pending spans are
derived from submitted/running asset timestamps or submitted publish jobs;
completed spans are derived from submitted-to-completed asset metadata and
publish-job requested/completed timestamps.
Production-run metrics include `dialecticore_production_run_health_status` and
`dialecticore_production_run_count` for total, active, running-active,
paused-active, failed-active, cancelled-active, completion-blocked, and unique
attention-run counts, plus `kind="completion_failed_check"` samples labeled by
failed completion gate name for alerting on specific blocked closeout reasons.
Redis
metrics include `dialecticore_redis_runtime_enabled`,
`dialecticore_redis_runtime_reachable`, and
`dialecticore_redis_worker_signal_maxlen` for fan-out mode, worker-signal mode,
TCP reachability, and stream retention. Live Provider Readiness summarizes the
same Redis posture with URL configuration status, event channel, worker-signal
stream, stream max length, timeout, TCP host/port reachability, normalized
runtime gates, and failed gate names without returning the Redis URL itself.
The System Health Redis card now shows the same safe channel/stream names,
stream max length, failed gate names, and boolean readiness checklist so
operators can diagnose Redis fan-out and worker-signal transport from the main
health dashboard. A reachable Redis socket no longer makes the Redis component
healthy by itself; enabled fan-out or worker-signal modes also require valid
channel/stream names and stream retention settings.
Auth metrics include
enabled auth modes plus provider-session active
and expired revocations and retained, accepted, denied, and error decision-log
records. Publish metrics include total, submitted, completed, failed, dry-run,
and live persisted publish jobs. Object-storage metrics include
`dialecticore_object_storage_remote_reachable` for S3/MinIO TCP reachability and
`dialecticore_object_storage_bucket_available` for the configured bucket
`head_bucket` probe. Local filesystem deployments also export
`dialecticore_object_storage_local_path_ready` and
`dialecticore_object_storage_local_path_state` for the object-storage
checked-path existence, directory status, parent availability, and normalized
`writable_target_or_parent` writability.
Backup metrics include archive count plus
validation coverage counts, latest-archive manifest readability, age, byte size,
restore-validation status, and restore-validation age. Heartbeat and lease
files are transient operational records:
stale heartbeats and expired leases remain visible until they exceed
`DIALECTICORE_WORKER_RUNTIME_STATE_RETENTION_SECONDS`, then API reads prune only
valid heartbeat/lease JSON records and count pruned expired leases separately
from malformed cleanup. Worker-owned malformed heartbeat and lease
JSON files are counted separately, degrade the worker registry, and are pruned
after the same retention window; worker signal registries and other diagnostic
files are left in place for audit and incident diagnosis. The Web UI Worker
Status panel shows retained and pruned heartbeat counts, malformed/pruned
malformed counts, expired-lease cleanup, and the configured retention window
beside active/stale worker and lease counts.
`GET /api/v1/system/events` provides a server-sent event stream of bounded
`system.snapshot` records for browser dashboards and lightweight monitors. Each
snapshot includes health status, counts, queue counters, worker status, stale
worker count, active lease count, retained/pruned heartbeat and expired-lease
cleanup counts, worker TTL/retention settings, worker signal counters, and
recent audit records;
`once=true` returns a single event for smoke checks. When Redis event
fan-out is enabled, the API also
publishes the same snapshot to `DIALECTICORE_REDIS_EVENT_CHANNEL` and includes
`redis_fanout` delivery evidence in the local SSE payload. The Web UI Live
Status Stream panel surfaces that delivery status, channel, and delivered
subscriber count beside the health, worker, queue, and audit summaries.

Distributed worker control signals can be recorded with
the Web UI Worker Status panel or `POST /api/v1/system/workers/signals`. The API
writes a `worker_signal_delivery.v1` record to runtime state and, when Redis
worker signals are enabled, appends the same record to
`DIALECTICORE_REDIS_WORKER_SIGNAL_STREAM` for workers or supervisors to consume.
Redis stream writes use approximate `MAXLEN` trimming from
`DIALECTICORE_REDIS_WORKER_SIGNAL_MAXLEN` so stream history remains bounded; the
same limit also caps the local runtime-state signal registry, API recent-signal
lists, health summaries, and worker gate lookups.
Supported signals are `drain`, `resume`, `reload`, and `stop_after_current`.
Worker signal reads merge the local runtime-state registry with recent Redis
stream entries, then deduplicate records by signal ID. This keeps API-local
audit evidence useful while allowing workers on separate hosts to honor
Redis-only signals without a shared filesystem. Workers inspect the latest
applicable role-specific or wildcard signal before starting a new polling pass.
Malformed or unsupported signal records remain visible in recent diagnostic
counts, but are ignored when computing the latest actionable signal and active
blocking state.
`drain` and `stop_after_current` skip new work and are surfaced in heartbeat
details as `signal_skipped=true`; `resume` clears the block by becoming the
latest signal. Very small signal retention values can therefore expire an older
blocking signal once enough newer signals arrive. The Web UI lists recent
signals with status, target role, reason,
Redis stream ID when available, and `delivery_sources` evidence. Every posted
signal also writes a global `worker.signal.recorded` audit event with target
role, signal type, delivery status, Redis stream metadata, delivery sources, and
payload key names. System health and Live Provider Readiness summarize the same
signal backlog by status, signal type, target role, active blocking target role,
delivery source, and malformed control-record count so
operators can identify whether a live-run block comes from a role-specific
drain, a wildcard stop, failed Redis delivery, or local-only evidence. The
`blocking_count`, `active_blocking_target_roles`, and
`by_active_blocking_target_role` fields are computed from the latest applicable
role-specific or wildcard signal. A later role-specific `resume` clears an older
`drain` for that target; a later wildcard `resume` clears older target-specific
blocks. Historical drains still appear in recent signal type breakdowns.
Live-provider readiness fails when failed signal delivery or active blocking
`drain`/`stop_after_current` signals are present, so operators must resume or
clear worker controls before starting a live run.
Use `scripts/worker_readiness_smoke.py` for a non-mutating worker registry
diagnostic. It records `worker_readiness_smoke_evidence.v1`, reports active,
missing, stale, failed, degraded, and malformed heartbeat state, and can be run
with `--require-all-roles` in production automation to fail unless every
configured worker role has a fresh heartbeat.

Backups are visible and creatable through the Web UI and the
`/api/v1/system/backups` API. Archives are written under
`DIALECTICORE_BACKUP_PATH` and include database table exports, local
object-storage files for filesystem deployments, authoritative S3/MinIO bucket
objects for S3-compatible deployments, and runtime-state files when requested.
Restore requests validate by default and return `backup_restore_plan.v1`
evidence for the scopes, record counts, file counts, included payloads, and
replace-existing policy before any state changes. Dry-run validations write a
`backup.restore_validated` audit event containing the archive checksum; applied restores still require
`apply=true` before replacing database rows, extracting files, or uploading
archived S3 objects. System health and metrics degrade the latest backup archive
until a matching dry-run validation audit event for the current archive checksum
is present. Use `scripts/backup_smoke.py` for a repeatable operator check that
creates a backup, performs a dry-run restore validation, writes stable evidence
under `output/smoke/`, and confirms the backup-storage readiness gate. See
`docs/backup-and-restore.md` for the archive contract and recovery commands.

Publisher operations are controlled through publisher targets, publish jobs,
audit events, and delivery QC. The Web UI can create, update, enable, disable,
delete, and health-check publisher targets without storing or showing raw
secrets. `POST /api/v1/episodes/{episode_id}/publish` creates a publish job
from a completed YouTube package. The default
`mock-youtube` target is a dry run: it records the upload payload and a
`mock://youtube/...` result URI, emits `publisher.job.*` audit events, and
records `publish_delivery_integrity` QC without uploading to a live account.
Non-mock `http`, `http_upload`, and `generic_http` targets can deliver the same
payload, plus the local package ZIP when available, to a configured delivery
endpoint. Failed delivery attempts remain persisted as failed publish jobs with
QC evidence and are included in global system health, system snapshots, and
`dialecticore_publish_job_count` metrics. Publish delivery QC also records
payload package ID/checksum match evidence plus production-manifest handoff
evidence, and fails live payloads that do not match the selected package or lack
a manifest asset ID, checksum, or `production_manifest.v1` schema version.
Completed package and production
manifest coverage is exported as `dialecticore_publish_package_manifest_count`
so alerting can catch packages waiting for package QC repair, thumbnail/caption
package repair, or manifest generation before live delivery. The live-provider
readiness preflight
warns on submitted publish jobs and completed export packages missing linked
production manifests, blocks on failed publish jobs or missing/failing package
QC, blocks on missing package thumbnail/subtitle evidence, and blocks when
`DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED=true` but no enabled publisher
target declares `automated_live_publish=true`, excluding replaced jobs from
active counts. It also emits normalized `readiness_checks` and
`failed_readiness_checks` for failed, submitted, missing-manifest, and missing
package-evidence publish gates, and shows the latest failed/submitted
publish-job target context plus the latest package missing a production manifest
or package evidence so operators can identify stale delivery work without
opening the episode. The publishing worker creates missing
production manifests after YouTube package export and before publish attempts,
then regenerates the active manifest after publish job/QC creation so the final
manifest embeds publish evidence. It reports `production_manifests_created` and
`production_manifests_refreshed` beside package and live/dry-run job counts in
heartbeat evidence. If manifest or publish handoff is blocked by
missing/failing package QC or by an invalid production manifest, the heartbeat
also reports `package_qc_blocked_handoffs`,
`production_manifest_blocked_handoffs`, and per-error `error_kind` values so
operators can distinguish expected gate failures from unexpected worker errors.
System health exposes the same publisher-target capability
posture through `publisher_targets` with enabled, live-enabled,
automated-live-capable, mock, dry-run-only, health, and issue counts; Prometheus
exports those counts as `dialecticore_publisher_target_count` plus
`dialecticore_publisher_target_health_status`. Enabled publisher targets with
unknown health degrade system health and warn in live-provider readiness until
their health check records a known status. Publisher-target health and live
readiness also emit normalized `readiness_checks`/`failed_readiness_checks` for
enabled targets, live-capable targets, automated-live capability when globally
required, unhealthy targets, and unknown-health targets. The Live Provider
Readiness card surfaces the same enabled/live/automation/mock/dry-run/health/issue
posture and the enabled target breakdown by adapter type, health status, and
platform plus the automation policy so operators can distinguish an intentional
dry-run-only deployment from a misconfigured automated-live deployment. The
System Health card also summarizes failed publisher-target readiness gates and
the boolean publisher-target readiness checklist next to the capability counts,
so operators do not have to inspect raw JSON to see why direct publishing is or
is not ready. Package QC with `status=fail` or `severity=fail` blocks publish
jobs.
The disabled `youtube-resumable` target can be enabled after configuring a
credential reference for a YouTube OAuth access token or refresh-token
credential references for the refresh grant. Its live adapter resolves an
access token directly or through refresh-token exchange, records safe OAuth
source/refresh metadata, creates a YouTube resumable upload session, persists
session/video ID evidence on the publish job, uploads the packaged
`video/render.*` bytes, and records the resulting watch URL. It then uploads the
packaged thumbnail and caption files to YouTube-native thumbnail/caption
endpoints and persists per-entry upload status, response payloads, content
types, byte sizes, and aggregate caption counts on the publish job and delivery
QC.

Workflow control actions are exposed at
`POST /api/v1/episodes/{episode_id}/workflow/actions`. The current API-level
control plane supports `pause`, `resume`, `cancel`, `retry_failed_stage`,
`stop_run`, `approve_stage`, `reject_stage`, `continue_after_manual_edit`, and
`complete`. Use `stop_run` when an active run should be abandoned while keeping
the episode editable; use `cancel` only when the episode itself should move to
`CANCELLED`.
Starting production creates `workflow_control.run` with schema
`production_workflow_run.v1`, a run ID, run sequence, current stage, stage plan,
stage history, operator signal history, and `workflow_control.workflow_event_log`
entries for deterministic local replay. Workflow control also enforces delivery
completion readiness before recording `COMPLETED`. The
`production_completion_readiness.v1` gate requires the latest completed final
render to be approved, an approved canonical broadcast transcript, passing
discussion structure QC with all required topic dimensions covered, completed
model endpoint/model ID, voice profile, and visual profile assignments for each
playable speaker, completed speech and primary visual assets for every playable
turn, approved and QC-passing transcript variants for configured non-canonical
output languages, subtitles and timeline segments for the transcript, any shot-planned reusable reaction-loop and
studio-scene assets linked into those timeline segments as completed
render-ready media, an approved preview render with matching preview QC, a final
render linked to that transcript timeline, matching speech and visual
media-integrity QC, matching subtitle synchronization and
timeline-integrity QC, including checksum-bound timeline media fingerprints for
linked audio/visual/subtitle assets, matching final-render QC with
checksum-bound render-manifest source asset snapshots for the timeline, audio,
visual, subtitle, fallback, and citation-overlay inputs, required research evidence-pack
and claim/citation QC when research is enabled, required research-review
approval when configured, a completed checked thumbnail
linked to the final render, a completed export package that includes that
thumbnail plus generated subtitles/captions and points at that render, a
completed production manifest that embeds the package thumbnail and
subtitle/caption evidence and points at that package, no active asset in
failed/corrupt/missing/error state, and no latest QC row with `status=fail` or
`severity=fail` when that failure blocks under the episode's quality policy.
If preview or final render QC records stale or missing render-manifest source
assets, readiness reports `preview_render_source_assets_stale` or
`final_render_source_assets_stale` and refuses completion until the affected
render is regenerated from the current timeline/media inputs.
The readiness payload includes the same `character_configuration_handoff.v1`
shape used by workflow production handoff evidence, so incomplete speaker
configuration blocks completion with `character_profile_missing`,
`character_model_missing`, `character_voice_missing`, or
`character_visual_missing`. It also reports `stale_model_turn_ids`,
`stale_voice_asset_turn_ids`, and `stale_visual_asset_turn_ids`; transcript
source turns or completed turn media that record an older model, voice, or
visual assignment than the speaker's current configuration block completion
with `character_model_turn_stale`, `character_voice_asset_stale`, or
`character_visual_asset_stale` until those turns are regenerated.
Configured localized outputs are reported in `localized_output_readiness.v1`;
missing, pending/rejected, missing-QC, or failing-QC localized transcript
variants block completion with `localized_output_missing`,
`localized_output_not_approved`, `localized_output_qc_missing`, or
`localized_output_qc_failing`.
Audio, subtitle, and unsupported-claim failures are classified by the production
definition's `block_on_missing_audio`, `block_on_missing_subtitles`,
`block_on_sync_error_ms`, and `block_on_unsupported_high_impact_claims` settings;
nonblocking failures stay visible in the readiness payload for audit. Successful runs store the readiness report in
`workflow_control.run.completion_gate`; blocked completion attempts leave the
run in progress, return the failed gate names, and persist a compact
`workflow_completion_handoff.v1` status on the worker orchestration attempt and
active run projection. The `complete` action is the
operator closeout after transcript approval, per-turn speech/visual/subtitle and
timeline coverage, final rendering from that timeline, required research/claim
QC, speech/visual media QC, subtitle/timeline QC, preview render QC with
current render source inputs and
approval, final render QC and approval, thumbnail generation and QC, export
package creation with thumbnail and subtitle/caption inclusion,
production-manifest creation, package QC, and dry-run or live publishing have
completed with publish delivery QC.
Dry-run publish delivery QC remains visible as nonblocking evidence; live
publish delivery failures still block completion.
Operators can inspect the same
readiness report through
`GET /api/v1/episodes/{episode_id}/workflow/completion-readiness`, and the Web
UI Workflow Evidence panel shows the pass/fail status, required asset IDs, final
render approval state, failed gate names, blocking failed asset/QC rows, and
nonblocking issue rows beside replay/Temporal evidence.
System Health, Live Provider Readiness, and Prometheus metrics also count
production runs whose current stage is `COMPLETED` while their completion gate is
missing or failing, plus running episodes whose worker completion handoff is
blocked, so fleet-level monitoring can alert on delivery completion blockers
without opening each episode.
Pause records the current stage in
`workflow_control.paused_stage` without discarding the episode aggregate and
adds a run signal when a run exists. Resume clears the pause and records a
resume signal. Cancel moves the episode to `CANCELLED`, records the stage it was
cancelled from, and closes the run as cancelled. Retry records retry metadata,
restores the failed stage when that stage is known, and reopens the run as
running. Stage approval writes durable decision evidence without advancing the
stage; stage rejection records the rejected stage as failed, moves the episode
to `FAILED`, and keeps the failed-stage retry path available after operator
correction. Continue-after-manual-edit records the correction handoff together
with `manual_edit_evidence.v1`, restores the failed/rejected/paused stage when
known, and reopens the run as running.
Retry and manual-edit reopen actions journal a matching `workflow.stage.entered`
event whenever they add a stage-history entry, keeping local replay consistent
with the reopened checkpoint. The normal approval endpoint for content gates
also updates the active run when the
decision changes the episode stage: approved research review records the return
to `DRAFT`, and approved transcript review records `READY`, both with
`approval.decision.recorded` as the stage-history source. Render approvals keep
the episode stage unchanged but still update the targeted render asset approval
metadata that later worker passes use.
The dashboard shows
the active run sequence, state, current stage, stage progress, signal count,
Temporal-attempt count, stage-dispatch count, and replay-event count. The
workflow evidence panel also fetches the replay report for the selected episode
and displays replay status, event-log checksum, issue count,
replayed-versus-stored state, recent Temporal bridge attempts, recent external
Temporal stage dispatches, and recent replay events.
`/api/v1/episodes/{episode_id}/status` reports `workflow_paused`,
`workflow_cancelled`, `retry_available`, and the durable `workflow_control`
block. `workflow_control.temporal_signal_log` records every local `start`,
`pause`, `resume`, `cancel`, and `retry_failed_stage` bridge attempt as sent,
skipped, failed, or disabled evidence. Enabling
`DIALECTICORE_TEMPORAL_SIGNAL_TRANSPORT_ENABLED` plus
`DIALECTICORE_TEMPORAL_SIGNAL_ENDPOINT` sends `temporal_signal_request.v1`
payloads to a trusted external Temporal bridge without replacing the local
control record. `GET /api/v1/episodes/{episode_id}/workflow/replay` rebuilds
run state from the event journal, returns an event-log checksum, and reports any
mismatch against the stored run state. `workflow-worker` coordination passes
append `workflow_control.worker_orchestration_log` entries with schema
`workflow_worker_orchestration_attempt.v1`; each entry records the per-stage
activity status, progress count, error count, skipped/scanned counts, and stable
summary checksums. Each stage attempt also embeds a compact
`workflow_stage_manifest.v1` with normalized progress metrics, sanitized
per-stage errors, the source stage-summary checksum, and a manifest checksum, so
restart/replay audits can inspect durable per-stage input/output evidence
without expanding the transient worker summary. Worker and Temporal execution
summaries carry a stable
`orchestration_attempt_id`; persisted logs use it as `summary_id`, so replaying
the same worker pass after restart does not duplicate retry queue entries or
consume another retry attempt. Every local and Temporal stage activity honors durable
`workflow_control.paused`, `workflow_control.cancelled`, and cancelled episode
state before mutating research, discussion, localization, QC, speech, visual,
timeline, render, or publishing outputs; skipped stages report
`workflow_blocked` in their stage summaries. Entries may also include a persisted
`talkshow_production_handoff.v1` payload that records whether transcript turns
are linked to configured speakers, speech, character visuals, subtitles,
timeline segments, renders, packages, manifests, publish handoff jobs, and
configured localized output transcript readiness.
The discussion stage separately reports `model_configuration_blocked` with
`discussion_model_configuration.v1` details when an otherwise eligible episode
cannot start because an enabled character has no model ID, references an unknown
model endpoint, or references a disabled model endpoint.
The `character_configuration` section lists active playable speakers and
whether each still has model endpoint/model ID, voice profile, and visual
profile assignments; missing speaker profiles or incomplete character
assignments block the handoff with `character_profile_missing`,
`character_model_missing`, `character_voice_missing`, or
`character_visual_missing`. Turn handoff evidence also reports
`stale_model_turn_ids`, `stale_voice_asset_turn_ids`, and
`stale_visual_asset_turn_ids`; stale transcript source turns or generated media
block review/delivery readiness with `character_model_turn_stale`,
`character_voice_asset_stale`, or `character_visual_asset_stale`.
The `localized_outputs` section lists required non-canonical output languages
and blocks handoff readiness with `localized_output_missing`,
`localized_output_not_approved`, `localized_output_qc_missing`, or
`localized_output_qc_failing` while multilingual transcripts are missing,
unapproved, missing semantic-fidelity QC, or failing that QC.
Character and studio handoff fields also report
expected, linked, and missing shot-planned reusable reaction loops and
studio-scene segments. Missing or non-render-ready reusable
reaction/studio media blocks the handoff with
`shot_planned_reaction_loop_missing` or `shot_planned_studio_scene_missing`
instead of allowing the show to continue with silently degraded cast/set
animation. Render handoff fields also report
preview/final approval state, latest render QC status, delivery package QC,
delivery package thumbnail/subtitle inclusion, production manifest validity and
publish-evidence freshness, publish-job status, and publish delivery QC;
`delivery_ready` is only emitted after the final render is approved, package QC
is non-failing, the package and production manifest include the required
thumbnail and subtitle/caption evidence, the production manifest contains the
latest publish job/QC evidence, publishing completed, and publish delivery QC exists. Missing
publish delivery QC, stale manifest publish evidence, and blocking publish
delivery QC failures are reported as production-handoff blockers. System health
and live-readiness aggregate completion-blocked production runs by failed gate
name, and metrics aggregate those entries
across episodes through the `workflow_orchestration` component and
`dialecticore_workflow_orchestration_count`, including blocked external
Temporal dispatch counts and blocked/review-ready/delivery-ready production
handoff counts. System health and live-provider readiness also name which
workflow stages failed or progressed, which Temporal dispatch stages are blocked
or ready, and which production-handoff blockers remain, giving operators a
direct stage target before restarting workers or changing Temporal runtime
settings. They also emit normalized
`readiness_checks`/`failed_readiness_checks` for orchestration errors, failed
workflow stages, and blocked Temporal dispatches. System health, metrics, and live-provider readiness
also aggregate durable `workflow_control.run` and active pause/cancel/failure
state through the `production_runs` component and
`dialecticore_production_run_count`, warning on paused or already-running active
runs and blocking on failed or cancelled active runs. The live preflight includes
run-state and stage breakdowns, attention-reason breakdowns for paused, running,
failed, and cancelled active runs, a deduplicated attention-run count, a bounded
list of attention-run IDs/stages,
normalized `readiness_checks`/`failed_readiness_checks` for active
failed/cancelled/paused/running run gates, plus latest-run pause/cancel/failure,
stage history, and signal counts for
operator triage. The Web UI System Health panel also lists production-run
attention rows from `details.attention_runs`; use `Cancel run` for obsolete
blocked or stale runs that should not continue. This calls the normal
`cancel` workflow action, records workflow signal/audit evidence, marks the run
terminal, and removes it from current production-run and orchestration
readiness pressure without deleting episode history. `GET
/api/v1/system/workflow-orchestration` returns
a bounded cross-episode view of recent orchestration attempts and Temporal
dispatches, and the Web UI Workflow Orchestration Evidence panel surfaces
attempt, error, dispatch, and blocked-handoff evidence without selecting a
single episode. Stage errors for the active episode append
`workflow_control.stage_retry_queue` entries with schema `workflow_stage_retry.v1`,
including target stage, attempt number, max attempts, backoff, next retry time,
and exhausted/scheduled state. Operator `retry_failed_stage` and
`continue_after_manual_edit` actions mark matching scheduled or exhausted
entries for the reopened target stage as `operator_retried` or
`manual_edit_resolved`, preserving the historical queue records while removing
them from the active backlog. Continue-after-manual-edit also persists
`manual_edit_evidence.v1` on workflow control, the run/Temporal signal logs,
and the continuation audit event; the evidence is scoped to manual edit audit
events after the failed checkpoint when that timestamp is available, includes
event counts and sanitized IDs/checksums/comments, and carries a stable
`evidence_checksum` for replay and operator handoff review. Workflow-worker passes also inspect scheduled
retry entries before running stage activities; when `next_retry_not_before` has
elapsed and the run is active, the worker reopens that target stage, marks that
specific queue entry as `automatic_retried`, records
`workflow.stage_retry.automatic_retry_requested`, and then runs the normal stage
chain. Later automatic retry entries still calculate their attempt number from
the persisted per-stage retry history, including resolved records, so automatic
or operator recovery does not reset the configured retry budget. Each retry
resolution also appends a
`workflow.stage_retry.resolved` event to the local workflow journal for replay
and audit inspection. System health and metrics aggregate active retry entries
across episodes through the `workflow_retries` component and
`dialecticore_workflow_stage_retry_count`, including due-now, backoff-delayed,
unknown-schedule, and exhausted/non-scheduled retry states, while also exposing
historical and resolved retry counts by resolution status and stage. System
health and live-provider readiness also break the backlog down by due, backoff,
unknown, and exhausted stage names so operators can identify which worker path
needs retry attention before a live run. The Web UI System Health, Live Provider
Readiness, and Workflow Retry Backlog panels show active, historical, and
resolved retry totals plus safe resolution breakdowns. They also emit normalized
`readiness_checks`/`failed_readiness_checks` for exhausted, scheduled, due,
backoff-delayed, and unknown-schedule retry gates. `GET
/api/v1/system/workflow-retries` returns a bounded cross-episode retry queue
sorted by the same schedule readiness, and the Web UI Workflow Retry Backlog
panel shows active, historical, resolved, scheduled, due, backoff, exhausted,
and the first queued entries for operator triage. Use retry or manual-edit
continue when the episode should be resumed. Use the backlog row's
`Acknowledge` action, or `POST
/api/v1/episodes/{episode_id}/workflow/retries/{retry_id}/resolve`, only for
obsolete retry debt that has been superseded by newer evidence; it marks the
single retry `operator_acknowledged`, preserves journal/audit evidence, and does
not reopen or rerun the episode. The live-provider readiness preflight uses the same
orchestration and retry summaries, blocking on orchestration errors, blocked
Temporal dispatches, and exhausted retry budgets while warning on scheduled
retry work.
These controls provide
operator-visible state, audit events, production-start guards, signal transport
evidence, local replay evidence, and durable local retry evidence. When
`DIALECTICORE_TEMPORAL_BACKEND_MODE=external`, workflow-worker passes also append
`workflow_control.temporal_stage_dispatch_log` entries with schema
`temporal_stage_dispatch.v1`. Each dispatch envelope records the active run,
stage, target episode status, activity name, namespace, task queue, readiness
or blocking settings, and a stable idempotency key for native Temporal worker
pickup. The `temporal-worker` role consumes the same stage contract in an
external-mode Docker worker pass, records `temporal_worker_execution_summary.v1`
heartbeats, records one `temporal_stage_activity_execution.v1` summary per
stage, and persists the same orchestration, retry, replay, and dispatch evidence
for running episodes. System health treats external mode as healthy only when
the Temporal backend TCP probe succeeds, native worker mode is enabled, a
non-stale `temporal-worker` heartbeat exists, and that heartbeat contains
runnable `temporal_worker_execution_summary.v1` evidence. The Web UI Temporal
status summary includes that execution status and progressed-stage count.
Live Provider Readiness summarizes the same Temporal runtime contract with mode,
namespace, task queue, bridge signal configuration, backend address/TLS posture,
native-worker enablement, active worker state, backend TCP reachability, missing
settings, temporal-worker execution activity/error/progress evidence, and
normalized `readiness_checks`/`failed_readiness_checks` for mode validity,
bridge transport requirements, external backend configuration/reachability,
native worker enablement, worker heartbeat, and execution readiness.

Increment 2 adds a concrete `voicebox-adapter` worker behavior. When
`DIALECTICORE_WORKER_ROLE=voicebox-adapter`, the worker scans persisted episode
aggregates for submitted or running audio assets, groups pending work by
language, polls the configured remote Voicebox job status endpoint, saves updated
assets, and records `audio.jobs.synced` plus audio QC evidence.

Increment 3 adds a concrete `comfyui-adapter` worker behavior. When
`DIALECTICORE_WORKER_ROLE=comfyui-adapter`, the worker scans persisted episode
aggregates for submitted or running visual assets, groups pending work by
language, polls the configured ComfyUI history/status endpoint, saves updated
assets, and records `visual.jobs.synced` plus visual generation QC evidence.

Increment 6 adds concrete research, discussion, localization, QC, speech,
subtitle, visual, timeline, render, and publishing worker behaviors. `research-worker`
scans research-enabled episodes without an evidence pack and builds
deterministic evidence assets while preserving any configured research-review
approval gate. `discussion-worker` starts only draft/ready episodes whose
research prerequisites are satisfied, records production-run metadata, and runs
the turn-by-turn discussion to transcript review. It tracks required topic
dimension coverage on each accepted turn, rebuilds that coverage after
regeneration or exclusion edits, and records missing dimensions in
`discussion_minimum_structure` QC before downstream approval.
`localization-worker` creates missing configured language variants only after
the canonical transcript is approved. `qc-worker` runs missing source-bound claim QC only when an evidence
pack and approved canonical/broadcast transcript are present, so unapproved
transcript-review drafts do not become downstream factual evidence. The
workflow worker's `audio` stage plans and generates/submits per-turn speech for
approved transcripts through configured Voicebox profiles; localized transcripts
created by the localization stage stay blocked from speech, subtitles, visuals,
timeline, and render until their `localized_transcript_review` approval is
accepted. Its `subtitles` stage waits for completed per-turn audio and
generates synchronized VTT subtitle assets with subtitle QC. Its `visuals`
stage waits for completed per-turn audio, then plans and generates/submits
character visuals through configured visual profiles and ComfyUI workflows
before recording visual QC. Audio and visual stage summaries include targeted
asset counts plus repair counts for failed/cancelled media, so a damaged speech
turn, character clip, reaction loop, studio shot, B-roll, or citation overlay can
be repaired by the normal worker pass without restarting the discussion or
completed media. The counts are stored on per-stage orchestration attempts and
surfaced in the Web UI Workflow Evidence panel. `timeline-worker` scans for
approved transcripts whose dialogue audio and renderable visual prerequisites
are complete, then
builds the next missing timeline. `render-worker` scans
completed timelines and creates one missing preview render first, then one
missing final render on a later poll.
`publishing-worker` scans completed approved final renders, creates a thumbnail,
exports a YouTube package, creates its linked production manifest, and submits a
publish job to an enabled publisher target. By default the job is dry-run. Live
automated publishing requires both
`DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED=true` and target capability
`automated_live_publish=true`; when multiple targets are enabled, the worker
prefers a live-capable target only under that global opt-in. The publishing
worker heartbeat reports `dry_run_publish_jobs_created`,
`live_publish_jobs_created`, `production_manifests_created`,
`production_manifests_refreshed`,
`automated_live_enabled`, `automated_live_capable_targets`,
`package_qc_blocked_handoffs`, and
`production_manifest_blocked_handoffs`. Blocked package QC and manifest handoffs
are also tagged in heartbeat `errors[].error_kind`. Final renders waiting for
`final_render_review` approval are reported as
`pending_final_render_approvals` instead of being counted as packaging errors.

The Web UI also exposes a selected-episode "Advance Workflow" action backed by
`POST /api/v1/episodes/{episode_id}/workflow/advance`. Use it when operating
without continuously running background workers, or when a producer has just
approved a manual gate and wants the chosen talk-show episode to take the next
available worker steps. It runs the same ordered worker stages as
`workflow-worker` with `batch_limit=1`, admits only the selected episode when it
already has a running durable workflow run, records subsequent research and
discussion stage transitions into that run, records orchestration evidence on
the active run, and does not scan other episodes. If the episode has no running
run, click **Start Production** first or call
`POST /api/v1/episodes/{episode_id}/workflow/start`; an advance attempt without
a run reports `summary.workflow_admission.missing_run_episode_count>0` and makes
no media-stage progress. In the mock-provider
path, repeated advances form a bounded local smoke test: the first pass can
complete speech, subtitles, render-ready visual SVGs, timeline, and preview
render; the second pass creates the final render; after final-render approval,
the third pass creates the thumbnail, YouTube package, production manifest, and
dry-run publish job, refreshes the manifest with publish evidence, and records
workflow completion when the completion-readiness gate passes.

`workflow-worker` now coordinates a
local ordered pass across those same gated stage pollers and records an
aggregate orchestration heartbeat with per-stage summaries, progress count, and
error count. Normal operation requires an operator or API client to create the
production run with `POST /api/v1/episodes/{episode_id}/produce` before workers
advance research, discussion, or downstream production stages. Set
`DIALECTICORE_WORKER_AUTO_START_PRODUCTION_RUNS_ENABLED=true` only for bounded
queue-drain or smoke-test runs where the coordinator should create run journals
for eligible episodes it discovers while polling. Each worker pass is recorded
in the episode aggregate as durable activity-attempt evidence and, for stage
failures, retry-queue entries. It does not bypass research approval, transcript
approval, media readiness, or dry-run publishing gates.

The polling loop is intentionally bounded by `DIALECTICORE_WORKER_SYNC_BATCH_LIMIT`
and sleeps for `DIALECTICORE_WORKER_POLL_INTERVAL_SECONDS` between scans. Each
poll records a heartbeat in the shared runtime-state volume. Unknown
`DIALECTICORE_WORKER_ROLE` values record a single failed
`unsupported_worker_role.v1` heartbeat with the supported role list and then
exit non-zero so Compose or a supervisor surfaces the misconfiguration. Active
pollers publish lease ownership and expiry in worker status, health, and
metrics, giving operators evidence that scale-out is not duplicating provider
sync, research, discussion, localization, QC, timeline, render,
delivery-preparation, or local workflow-coordination work.
API-level workflow pause/resume/cancel/retry metadata, production-run
stage/signal evidence, replayable event journals, optional Temporal bridge
delivery attempts, and gated publishing-worker live automation evidence are
durable today. Local workflow-worker pass journals and stage retry queues are
also durable today. In external Temporal mode, the same passes write durable
stage-dispatch envelopes for native workers; external Temporal activity passes
and Redis-backed worker signals are durable today.

For local development without Compose, start the API and the default local worker
set with:

```bash
scripts/start_dev_api_service.sh
scripts/start_dev_worker_services.sh
```

The worker helper starts the `workflow-worker` coordinator for local production.
That coordinator owns the ordered local pass across research, discussion, speech,
visuals, timeline, rendering, and delivery. Set `DIALECTICORE_DEV_WORKER_ROLES`
to one explicit diagnostic role only when needed; do not run individual stage
workers beside the coordinator against the SQLite development database. It starts
`temporal-worker` too when the loaded environment configures
`DIALECTICORE_TEMPORAL_BACKEND_MODE=external` and
`DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED=true`. Set
`DIALECTICORE_DEV_WORKER_ROLES=all` to force all roles, or provide a
space-separated role list to start a smaller debugging subset. The helper
lowers the poll interval to five seconds by default for interactive
development; override with
`DIALECTICORE_DEV_WORKER_POLL_INTERVAL_SECONDS`.

After startup, verify worker evidence with:

```bash
.venv/bin/python scripts/worker_readiness_smoke.py
```
