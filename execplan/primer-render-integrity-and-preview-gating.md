# Render declared primers and quarantine failed previews

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

An episode timeline that declares a produced topic primer must render that primer as the opening video and audio. If the primer or any other declared programme interval cannot be resolved, rendering must fail before a reviewable artifact is published. A preview whose render-integrity quality check fails must not receive a pending approval or appear as the current usable preview. The existing broken preview for episode `9d145344-82c9-46cc-b4c1-661d95f0bf56` will be preserved for audit, marked non-current, and replaced by a newly rendered immutable artifact whose opening has visible frames and audible programme audio.

## Progress

- [x] (2026-08-15 00:00Z) Reproduced and measured the defect: asset `cd45a9f7-280d-487a-b234-923b61f94368` has a 64.04-second black opening corresponding to an unmaterialized 63.927-second primer.
- [x] (2026-08-15 00:00Z) Confirmed the renderer generated a dark visual gap and silence while QC independently failed, yet approval and UI selection still treated the artifact as current.
- [x] (2026-08-15 22:57Z) Added deterministic legacy-primer materialization with immutable episode-local asset validation and hard failures for unresolved or misaligned declarations.
- [x] (2026-08-15 22:57Z) Gated approval creation and backend/frontend current-preview selection on non-failing render-integrity QC; failed outputs are quarantined as rejected.
- [x] (2026-08-15 22:57Z) Added backend and frontend regression tests for valid and missing primers, failed QC quarantine, and rerender availability.
- [x] (2026-08-15 22:57Z) Passed 838 backend tests, 111 frontend tests, Ruff, and the production frontend build.
- [x] (2026-08-15 23:13Z) Backed up and repaired the affected episode non-destructively, rendered a new immutable preview, and verified frames, audio, QC, approval, browser selection, and automatic status refresh.

## Surprises & Discoveries

- Observation: Container-level probing reported both H.264 and AAC streams even though the opening programme interval was unusable.
  Evidence: `ffmpeg` black detection reported `black_start:0 black_end:64.041667`; later frames and dialogue were valid.
- Observation: The current timeline stores the primer only in metadata while its first renderable segment starts at 63,927 ms.
  Evidence: `timeline_json.program_structure.primer` references asset `9e052a27-301c-4bb5-a0d3-d5eec44f1238`, but `segments[0].start_ms == 63927`.
- Observation: The quality check already failed but approval creation is unconditional after QC.
  Evidence: `RenderService.render_episode` appends `_render_qc(...)`, then creates `preview_render_review` and sets `approval_status=pending` without checking the QC severity.
- Observation: The affected timeline uses an older top-level `primer` declaration, while current `TimelineService` output already creates explicit `topic_primer` segments.
  Evidence: live timeline `acd9554b-53b7-4662-b3a5-11c9732c6686` has `primer.asset_id` and starts its first discussion segment at 63,927 ms; current timeline builder emits both video and audio references in a leading segment.
- Observation: The first corrected render had perfect primer signal but was still quarantined by existing studio/bridge integrity gates.
  Evidence: asset `07414cd0-b0e7-45bc-bf9d-73ba7bf5a958` measured zero black and silence coverage, but QC failed `render_studio_context_missing` and `render_post_primer_host_bridge_missing`; no approval was created.
- Observation: Production-v2 speaking assets are already precomposed studio footage, but the hand-authored timeline did not record that role.
  Evidence: all 21 primary assets carry `visual_role=production_v2_speaking_character`; the repaired timeline records each as checksum-backed precomposed studio context and identifies turn 1 as the participant-introduction host bridge.

## Decision Log

- Decision: Treat a declared primer as required programme content, not as an optional timeline gap.
  Rationale: Replacing declared content with synthetic black and silence creates a structurally valid but editorially false artifact.
  Date/Author: 2026-08-15 / Codex
- Decision: Preserve immutable render history; reject or replace the bad asset rather than overwrite or delete it.
  Rationale: Existing review and checksum evidence must remain auditable.
  Date/Author: 2026-08-15 / Codex
- Decision: Make backend QC authoritative and also filter failed renders in the UI.
  Rationale: Backend enforcement protects API clients; UI filtering prevents stale or historical invalid artifacts from being presented as current.
  Date/Author: 2026-08-15 / Codex
- Decision: Support legacy primer materialization only with an episode-local, completed, checksum-backed asset and exact interval alignment.
  Rationale: This provides compatibility without adding hidden cross-episode lookup or accepting ambiguous media lineage.
  Date/Author: 2026-08-15 / Codex
- Decision: Preserve QC strictness and repair legacy timeline semantics instead of special-casing or suppressing studio/bridge failures.
  Rationale: The produced footage genuinely contains studio context and the first turn is the post-primer participant introduction; recording those facts makes the timeline truthful and keeps the verifier authoritative.
  Date/Author: 2026-08-15 / Codex

## Outcomes & Retrospective

The user-visible defect is resolved. The current preview is asset `1c3de54d-bcc6-44db-8c36-5654bb0f0806`, checksum `sha256:97d0a7d4fb1d988b574c6d319fb390bb9e348798e3ce026b01426897455427b0`, at 1280x720, 24 fps, and 364.292 seconds. Its `render_preview_integrity` result `c61b6f77-2a8c-4f78-ab8c-d83c7cc9aeac` passes with zero issues, 21 studio-context segments, one post-primer bridge, and zero measured black/silence coverage in the 63.927-second primer. Independent ffmpeg inspection found no black interval and measured primer audio at -23.8 dB mean / -5.8 dB peak. Frames sampled at 1, 30, 63, 65, 100, and 350 seconds contain visible programme material.

The browser delivery panel selects the new asset as `Current preview`. Without a reload, its workflow heartbeat advanced from 23:12:12 to 23:12:43, confirming automatic status polling. The old black preview and the intermediate QC-failed render remain immutable and quarantined for audit; neither is reviewable. The only timeline QC warning is the pre-existing absence of linked subtitles.

## Context and Orientation

`backend/app/services/timeline_service.py` assembles an episode timeline. A completed primer should appear as a `topic_primer` segment with the same render asset as both its video and audio source. `backend/app/services/render_service.py` turns timeline segments into normalized visual and audio pieces, concatenates them, probes the output, creates a `render_*_integrity` result, and creates human approval records. `frontend/src/main.tsx` selects the latest render for the delivery player. `frontend/src/renderGates.ts` controls whether preview and final render actions are available.

The affected timeline is a legacy/imported form: `program_structure.primer` declares a primer and reserves its duration, but no `topic_primer` segment exists. The renderer interprets the reserved interval as an ordinary gap. The source primer render exists as immutable object-store media but is associated with a source episode rather than the affected episode.

## Plan of Work

Add a normalization/validation boundary before manifest construction and ffmpeg composition. It will recognize a declared primer, require a resolvable completed render asset, and materialize a leading `topic_primer` segment only when doing so is unambiguous and checksum-backed. Unresolved declared primers and unexplained programme gaps will raise a clear render error before output publication. Normal timeline generation remains explicit and unchanged.

After rendering, create a review approval only when the just-created integrity result passes. Failed renders will remain recorded but will be marked failed/rejected for review with failure metadata and no pending approval. Update frontend render selection and gates to require passing matching render QC for a reviewable/current preview.

For the existing episode, preserve the bad render, establish episode-local immutable lineage to the already-produced primer if necessary, rebuild or normalize the timeline, regenerate the preview, and confirm that the UI selects only the passing artifact.

## Concrete Steps

Work from `/srv/DialectiCore`.

    .venv/bin/pytest -q backend/tests/test_render_service.py
    npm --prefix frontend test -- --run frontend/src/renderGates.test.ts
    npm --prefix frontend run build

Use the running API/worker path to request the replacement preview. Inspect the resulting MP4 with `ffprobe`, `blackdetect`, and `volumedetect`; then inspect the episode JSON and browser delivery panel.

## Validation and Acceptance

A unit test must show that a valid explicit primer is rendered from time zero. A legacy declared primer with a resolvable immutable asset must normalize deterministically; an unresolved primer must fail before a completed render or approval exists. A deliberately failing render QC must produce no pending approval. Frontend tests must show that failed-QC previews cannot gate final rendering or appear as the current usable preview.

For the live affected episode, acceptance requires a new checksum and asset ID, visible non-black opening frames, audible primer audio, passing `render_preview_integrity`, exactly one pending approval for the replacement, the old asset marked non-current, and the delivery panel showing the replacement after automatic refresh.

## Idempotence and Recovery

Normalization must be pure and safe to repeat; it must not duplicate an existing `topic_primer` segment. Render requests retain existing `regenerate` semantics. If live rendering fails, the queued artifact records the failure and the prior artifact remains preserved. Episode data should be backed up before the one-time repair. Rollback consists of reverting source changes and restoring the episode backup; object-store artifacts are immutable and need not be deleted.

## Artifacts and Notes

Broken render: `cd45a9f7-280d-487a-b234-923b61f94368`, checksum `sha256:cf56b4749d1987a4a1eb36c4ad76228e845e48cfd472ca614a234ff13edf9c28`.

Referenced primer render: `9e052a27-301c-4bb5-a0d3-d5eec44f1238`, duration 63,927 ms, checksum `sha256:d8e9a411618624160ab48c367b804be53c7f8c876e0333ac0105e866a05a2394`.

Pre-repair database backup: `storage/backups/dialecticore-backup-20260815T225829Z-pre-primer-preview-repair.tar.gz`, checksum `sha256:0bc22a4565c9f06cf62820a2177b7e1cca5fce55e6219b2c766855d78ee7adfd`.

Episode-local primer reference: `e11b52c6-5333-43d7-9f02-12a46d677506`. Passing timeline: `cc7452a0-d78c-48fe-9408-c3f4993477e9`. Passing preview approval: `eaec77f3-016c-4f38-8630-8296a90fe150`.

## Interfaces and Dependencies

No new external dependency is planned. Existing `Asset`, `QualityResult`, `Approval`, timeline JSON, object-store, ffmpeg, and React query interfaces remain authoritative. New helpers should accept and return ordinary timeline dictionaries without mutating persisted source metadata unexpectedly.

Plan note (2026-08-15): Created after live artifact diagnosis to guide the cross-cutting render, QC, approval, UI, and recovery change.

Plan note (2026-08-15 22:57Z): Updated after implementation and automated validation; live deployment and episode repair remain.

Plan note (2026-08-15 23:13Z): Completed after managed render, independent media inspection, and browser/automatic-refresh validation.
