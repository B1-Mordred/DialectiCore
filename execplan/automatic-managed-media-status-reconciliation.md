# Reconcile managed media status automatically

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

Once DialectiCore submits media to B1, its local asset status and browser UI must
converge automatically as B1 moves the job through queued, running, completed, or
failed states. Pausing an episode must stop new production stages but must not stop
passive observation and materialization of work that was already submitted. A user
can observe this by leaving an episode page open: within the existing worker and UI
poll intervals, remote completions appear without pressing `Sync ComfyUI Jobs` or
reloading the page.

## Progress

- [x] (2026-08-15 22:04Z) Traced the live B1 jobs, frontend query polling, manual sync route, and ComfyUI adapter worker.
- [x] (2026-08-15 22:05Z) Identified the workflow-pause gate that prevents passive reconciliation.
- [x] (2026-08-15 22:12Z) Added regression coverage for submitted managed image media on paused and workflow-detached episodes.
- [x] (2026-08-15 22:12Z) Moved passive ComfyUI reconciliation outside active workflow admission without weakening any producer-stage controls.
- [x] (2026-08-15 22:28Z) Restarted only the workflow coordinator and proved automatic API and open-browser convergence from active to six completed assets.
- [x] (2026-08-15 22:35Z) Committed and pushed `3480c6fb7e4435d67e8ea31c1655a54931ecc152`; GitHub CI run `31912244361` passed Compose, frontend, Ruff, and all backend tests.

## Surprises & Discoveries

- Observation: the frontend already refetches episode detail every 30 seconds, and
  the ComfyUI adapter worker already polls every 15 seconds.
  Evidence: `frontend/src/main.tsx::useEpisodeDetail`,
  `backend/app/core/config.py::Settings.worker_poll_interval_seconds`, and
  `backend/app/workflows/worker_placeholder.py::run_comfyui_adapter_worker`.
- Observation: `run_comfyui_adapter_once` applies
  `_workflow_control_blocks_stage_work` before looking for submitted visual jobs,
  so a paused episode never reaches `ComfyUiService.sync_visual_results`.
  Evidence: the current Production v2 episode remained locally submitted after all
  six B1 jobs completed; the worker function increments `workflow_blocked` and
  continues before collecting pending languages.
- Observation: the workflow coordinator also wrapped the adapter in
  `_ActiveWorkflowRunRepository`, so an episode with no current workflow ID was
  excluded before the adapter could scan it.
  Evidence: the live episode had `current_workflow_id=null`; after the adapter was
  given the full cached snapshot, its heartbeat reported five episodes scanned and
  one synchronized while every production stage still scanned zero admitted runs.
- Observation: reusable seated-character plates are `AssetType.image`, which the
  adapter's narrower pending-type list omitted even though the service-level visual
  selector already supports images and citation cards.
  Evidence: the three remaining live assets were managed `image` records; adding
  the missing visual types produced a workflow-worker audit and automatic
  materialization.
- Observation: a restarted coordinator cannot immediately acquire the previous
  process's valid 45-second lease, but begins automatically when that lease expires.
  Evidence: the new worker initially reported `lease_skipped=true`, then acquired
  the lease and reconciled the episode without operator action.

## Decision Log

- Decision: treat polling and materializing an existing remote job as passive state
  reconciliation, not workflow stage execution.
  Rationale: it creates no remote job and is required for truthful local state;
  pause remains binding for all work that starts or advances production.
  Date/Author: 2026-08-15 / Codex.
- Decision: retain the manual sync action as an explicit recovery tool.
  Rationale: operators still need a retry surface after transient provider or
  storage failures, but normal progress must not depend on it.
  Date/Author: 2026-08-15 / Codex.
- Decision: run only the ComfyUI adapter against the full cached episode snapshot;
  keep research, discussion, localization, QC, audio production, visual production,
  timeline, render, publishing, and completion behind active-run admission.
  Rationale: this gives external jobs a lifecycle observer without turning pause or
  missing workflow state into permission to start production.
  Date/Author: 2026-08-15 / Codex.

## Outcomes & Retrospective

Managed media status now converges automatically. The coordinator polls submitted
and running visual jobs from the full episode snapshot, including managed images,
while every producer stage remains bound to active workflow admission. On the live
episode, the workflow-worker changed the managed totals from three completed plus
three pending/running to six completed and zero active; its audit actor was
`workflow-worker`. A Chromium page left open changed from `1 B1 jobs` to no active
B1 jobs through its existing 30-second query polling, without reload or a sync
button. The subsequent `Media planned, not running` state truthfully describes 21
separate performance clips that remain planned and were never submitted.

Validation passed 835 backend tests, 110 frontend tests, Ruff, the production Web
build, and `git diff --check`. The only backend warning is the existing Starlette
`httpx` deprecation. Implementation commit
`3480c6fb7e4435d67e8ea31c1655a54931ecc152` is on `origin/main`; GitHub CI run
`31912244361` passed all three jobs. All four development services are active, the
worktree is clean, and local and remote SHAs match.

## Context and Orientation

The React UI lives primarily in `frontend/src/main.tsx`. Its `useEpisodeDetail`
query reads the persisted episode every 30 seconds. Managed B1 media is submitted
and polled by `backend/app/services/comfyui_service.py`. The long-running adapter
loop in `backend/app/workflows/worker_placeholder.py` calls
`run_comfyui_adapter_once` every 15 seconds and persists changed episodes through
the repository. The manual POST route at
`/api/v1/episodes/{episode_id}/visual-assets/sync` calls the same service method.

An episode workflow pause prevents automatic production stages such as generation,
timeline construction, rendering, and publishing. It must not prevent status
observation of external jobs that predate the pause. A remote completion is
materialized into immutable local object storage and then saved on the episode.

## Plan of Work

Add a focused worker regression test that creates a paused episode with a submitted
remote visual job and proves the adapter polls, materializes, and saves it. Adjust
only `run_comfyui_adapter_once`: identify submitted/running visual assets and sync
them regardless of stage-work pause. Leave all producer workers and their existing
pause gates unchanged. Ensure episodes without pending jobs remain untouched.

After unit validation, restart only the render-worker user service that owns the
ComfyUI adapter loop. Observe worker heartbeat/audit evidence and the episode API
until the six completed B1 jobs are automatically materialized. Keep the Web UI
open in Chromium and prove that ordinary query polling updates the displayed state
without invoking the manual sync endpoint.

## Concrete Steps

From `/srv/DialectiCore`:

    apply_patch ...
    pytest -q backend/tests/test_worker.py -k comfyui_adapter
    npm --prefix frontend test -- --run
    systemctl --user restart dialecticore-worker-dev-render-worker.service
    systemctl --user is-active dialecticore-worker-dev-render-worker.service

Use the Playwright CLI against `http://127.0.0.1:5173` for browser evidence. Then
run the repository's complete relevant test/build commands, commit, push `main`,
watch GitHub CI, and verify local and remote commit equality.

## Validation and Acceptance

Acceptance requires all of the following:

1. A paused episode with a submitted visual job is synchronized by the adapter
   worker in a deterministic unit test.
2. No new remote media request is submitted by reconciliation.
3. The live episode's completed B1 jobs change from local `submitted` to local
   `completed` after the worker restart, without calling the manual sync route.
4. The open browser reflects the persisted completion through ordinary polling.
5. Workflow pause remains set and no timeline, render, package, or publication work
   starts.
6. Tests, build, services, CI, and repository state are clean.

## Idempotence and Recovery

Polling is idempotent for submitted/running assets. Once an asset becomes terminal,
the normal worker selection no longer polls it. Provider/network/materialization
errors remain recorded as sync errors and are retried on later loops; the manual
sync action remains available. Reverting the worker change restores the prior gate
without altering already materialized immutable assets. Do not resubmit or cancel
remote jobs during validation.

## Artifacts and Notes

Initial live evidence on 2026-08-15 showed all six B1 jobs terminal and successful,
while their local episode assets still said `submitted`. The dashboard status-count
fix in commit `fbb7bc44b68a37b7ff92203acb917b666003990d` made the discrepancy visible
but did not reconcile the records.

Final live evidence: coordinator `userver:169792` reported
`episodes_scanned=5`, `episodes_synced=1`, `pending_visual_assets=1`, and zero
errors during the last active transition. The episode then reached six completed,
zero submitted, zero running, and zero failed managed seated-character assets. The
open Playwright session `dialecticore-auto-status` observed both sides of the final
transition without navigation or manual refresh.

## Interfaces and Dependencies

No new endpoint, dependency, process, port, credential, or database migration is
needed. The change reuses `ComfyUiService.sync_visual_results`,
`VisualResultSyncRequest`, the repository `save` method, the existing adapter worker
lease, B1 authentication, and React Query polling.

Plan update note (2026-08-15): created after tracing the current live discrepancy;
the plan separates passive state convergence from stage execution.

Plan update note (2026-08-15 22:28Z): recorded the additional active-run and image
type filters found by live activation, the final architecture, browser evidence,
and complete local validation.

Plan update note (2026-08-15 22:35Z): recorded the pushed implementation, green CI,
service health, and repository equality as terminal evidence.
