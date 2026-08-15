# Add branded introductions, plate-aware cameras, and direct B-roll editing

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

DialectiCore productions must open with an unmistakable, reusable show identity:
the complete studio, a gentle camera fly-in, and the show's logo with the exact
episode title on the rear screen. That same reviewed introduction must become the
automatic thumbnail, so a delivery can never silently fall back to an unrelated
frame. Directors must also be able to move the virtual camera deliberately between
participants, select only fully calibrated and approved alternate studio plates,
and edit parallel B-roll through a source-library and draggable timeline workflow
that behaves like a compact nonlinear editor.

The existing Production v2 final render, thumbnail, package, production manifest,
recovery archive, approvals, and publication dry-run remain immutable. This plan
adds reusable product behavior first, then creates a new revision of episode
`9d145344-82c9-46cc-b4c1-661d95f0bf56` for human review. It never edits
`/srv/TubeFactory`, never publishes live, and never accesses the P40 except through
the managed B1 scheduler API.

## Progress

- [x] (2026-08-15 15:12Z) Confirmed the repository is clean on `main` at `77dffed8ef3821f9fdcf2356ebaebd97609d3116`, after the accepted Production v2 delivery.
- [x] (2026-08-15 15:13Z) Re-read the ExecPlan, UX heuristics, and Refactoring UI instructions and converted the approved requirements into this living implementation plan.
- [x] (2026-08-15 15:34Z) Created and dry-run validated the full pre-change database, 703-object, and runtime-state archive `dialecticore-backup-20260815T152704Z-pre-branded-intro-camera-editor-20260815.tar.gz` (`sha256:34cf4a64138a4cb502cb6e693de541951d9e3e1eb96f6b24917aae2c2da04974`).
- [x] (2026-08-15 16:01Z) Added backward-compatible show branding metadata, immutable normalized logo uploads, deterministic identity-slate generation, and API/service tests.
- [x] (2026-08-15 16:01Z) Added the `screen_graphics` directing lane, semantic participant-introduction rules, branding conflicts, and checksum-bound thumbnail provenance.
- [x] (2026-08-15 16:01Z) Extended camera clips with participant-targeted motion and reviewed camera-plate bundles, including managed generation, visual calibration, approval, and rejection rules.
- [x] (2026-08-15 16:01Z) Materialized screen-graphics and independent camera boundaries while preserving dialogue, source, and audio offsets.
- [x] (2026-08-15 16:03Z) Upgraded the Web timeline editor with an eligible source library, playhead, drag/resize, linked edits, snapping, undo/redo, source in/out, camera controls, plate calibration, and actionable validation.
- [x] (2026-08-15 16:33Z) Ran focused and full backend/frontend tests, production builds, and real-browser checks of branding, source eligibility, linked duplication/undo, camera plates, timeline validity, and pending approval visibility.
- [x] (2026-08-15 16:32Z) Created immutable branded-introduction timeline `2087a218-3b0a-494d-b2d5-e920650b018b`, qualification render `ab9fd6ab-84ee-432e-9fc2-3a103210f81e`, and thumbnail `51b3af24-a4aa-4f6c-a2ff-d0d5e5c95cf9`; stopped at approval `7eee0eb7-d705-410d-b5fc-2f77f1320cdd` before final/package work.
- [ ] After human approval, create and validate the new full preview/final/package/manifest/recovery chain without live publication.
- [x] (2026-08-15 16:42Z) Committed and pushed implementation `775ba775c113ab073da42a91ed2d4c8a5bac072c`; CI run `31896047758` passed Compose, frontend, backend lint, and all tests. A documentation-only follow-up records this terminal evidence.
- [x] (2026-08-15 19:35Z) Reproduced the timeline-trimming usability failure in Chromium without saving: source cards report `00:00.000`, a new five-second clip is only a few pixels wide in a 364-second Fit view, and the inspector exposes four unexplained millisecond fields.
- [x] (2026-08-15 19:52Z) Corrected browser media-duration discovery and source-aware edge trimming, added deterministic playhead trim actions and focus-selected zoom, and replaced the raw inspector with a guided clip/source-time workflow.
- [x] (2026-08-15 19:55Z) Validated pointer, playhead, preview-source, keyboard, undo/redo, linked-presentation, reload, build, and accessibility behavior in tests and Chromium without saving or modifying accepted media assets.
- [x] (2026-08-15 20:01Z) Committed and pushed `17cda8fc6695e50630bdb1a542a948a7e6d5c802`; GitHub CI run `31905346193` passed Compose, frontend, backend lint, and the complete backend test suite.

## Surprises & Discoveries

- Observation: the accepted episode already has a semantic participant-introduction
  bridge and virtual camera policy, but automatic thumbnail selection still uses a
  heuristic rear-screen/25-percent seek and can choose an unrelated image.
  Evidence: `TimelineService._compose_timeline`,
  `RenderService._thumbnail_seek_seconds`, and the rejected Production v2 automatic
  thumbnail recorded in `execplan/production-v2-character-performance-and-broll.md`.
- Observation: project records store their Pydantic payload as JSON, so nested
  branding metadata can remain backward compatible without a database migration.
  Evidence: `backend/app/infrastructure/models.py` and project repository mapping.
- Observation: the current parallel directing schema already separates B-roll
  content time from presentation mode, which is the correct base for direct
  manipulation; the missing pieces are source discovery, spatial editing, local
  history, and explicit validation feedback.
  Evidence: `backend/app/services/timeline_service.py` and
  `frontend/src/main.tsx::TimelineEditorPanel`.
- Observation: complete alternate camera plates can be selected safely today, but
  overlaying a new rear-screen graphic into a non-frontal plate requires the
  calibrated quadrilateral to be materialized by the compositor. Until that
  transform is implemented, validation rejects alternate-angle and screen-graphic
  overlap instead of silently dropping the graphic.
  Evidence: `TimelineService._validate_timeline_track_resources` and
  `RenderService._apply_camera_clip_to_piece`.
- Observation: the accepted Production v2 episode predates reusable seated-panel
  assets and registers flattened speaking clips; its generic qualification render
  therefore replaced the studio with the slate. The failed output was retained and
  rejected as asset `8e8d4bc0-2fb0-444f-8e3f-1567d2db71c6`. Reusing the already
  qualified Production v2 matte/desk compositor produced the correct total-studio
  result without changing accepted v2 assets.
  Evidence: rejected render metadata and
  `scripts/production_v3_branded_intro_qualification.py`.
- Observation: a normal full-frame slate put its lower title lines behind the cast
  when transformed onto the rear screen. Version 2 constrains identity and exact
  title pixels to the visible upper screen band; the earlier qualification render
  `4ef692b6-7ffd-49d3-a5a7-7cd8baf85ee1` remains as rejected evidence.
  Evidence: `show_identity_slate.v2`, visual frames under `output/playwright`, and
  immutable asset review metadata.
- Observation: the five immutable B-roll assets have valid MP4 files lasting about
  118-508 seconds, but their API `duration_ms` values are null. The browser therefore
  labels every source `00:00.000`; direct `ffprobe` measurements prove the media is
  not zero-length. At Fit scale, even the five-second fallback clip occupies roughly
  eight pixels and its two seven-pixel handles overlap.
  Evidence: Chromium snapshot `page-2026-08-15T19-35-00-391Z.yml`, screenshot
  `output/playwright/timeline-before-usability-fix.png`, episode asset JSON, and
  local `ffprobe` output.
- Observation: pointer edge trimming currently changes only programme start/end;
  it does not move the matching source in/out boundary. The visible source clock
  can consequently disagree with the frames implied by the shortened clip.
  Evidence: `frontend/src/main.tsx::beginClipPointerEdit`.
- Observation: the source MP4 endpoint correctly supports byte ranges, and Chromium
  can obtain the true durations from native media metadata without a database or
  asset mutation. Large files whose metadata takes longer now say “Reading
  duration…” instead of falsely claiming zero duration.
  Evidence: HTTP 206/`Accept-Ranges: bytes`, browser `readyState`/duration probes,
  and final source-library snapshots.

## Decision Log

- Decision: persist a generated identity slate as an immutable episode image asset
  derived from effective brand metadata and the exact `Episode.title`.
  Rationale: render, thumbnail, review, and package stages can then verify one
  checksum-bound visual rather than independently reconstructing branding.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: old projects inherit the bundled DialectiCore mark and project name;
  project and episode logo overrides are normalized to lossless RGBA PNG and stored
  immutably.
  Rationale: existing productions remain renderable while custom branding gains
  explicit provenance and deterministic pixels.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: a semantic participant introduction is the sole automatic-thumbnail
  source, and missing branded-introduction provenance is a hard 422 response.
  Rationale: a technically valid but editorially wrong fallback thumbnail is more
  damaging than a clear, actionable failure.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: true alternate angles require an approved complete camera-plate bundle;
  targeted participant moves may use the existing frontal studio plate and measured
  seat anchors.
  Rationale: a crop/pan can be deterministic from known geometry, while a fabricated
  angle without screen, desk, and seating calibration cannot preserve composition.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: direct manipulation is local and reversible until Save creates the next
  immutable timeline revision; invalid clips remain visible with actionable errors
  and cannot be saved.
  Rationale: this applies visibility, recognition, undo, and error-prevention
  heuristics without weakening backend validation.
  Date/Author: 2026-08-15 / Codex.
- Decision: alternate plates and screen graphics are mutually exclusive in the
  current renderer boundary; the branded introduction stays on the calibrated
  frontal total-studio plate.
  Rationale: correct pixels and explicit constraints are preferable to pretending
  that plate calibration has been applied while replacing the screen layer.
  Date/Author: 2026-08-15 / Codex.
- Decision: qualification-slice renders rebase the semantic introduction to zero
  while preserving programme/source offsets and retaining the programme thumbnail
  timestamp in provenance.
  Rationale: directors can review a 21-second introduction rather than re-encoding
  six minutes, without making the review artifact ambiguous.
  Date/Author: 2026-08-15 / Codex.
- Decision: retain direct manipulation, but pair it with large labelled edge grips,
  source-aware trim semantics, and explicit “trim to playhead” buttons. Selecting a
  short clip can focus the timeline to a useful scale; raw precision fields move
  behind an advanced disclosure.
  Rationale: pointer precision should be optional, and every trim must have a clear,
  deterministic route that preserves source continuity and is reversible.
  Date/Author: 2026-08-15 / Codex.
- Decision: the accepted legacy Production v2 episode uses its pinned, measured
  character-matte/desk composition recipe for this one qualification; new normal
  episodes continue through the reusable managed renderer.
  Rationale: reconstructing a studio from flattened speaking clips is impossible,
  while the exact qualified source layers and recipe remain available and auditable.
  Date/Author: 2026-08-15 / Codex.

## Outcomes & Retrospective

The reusable implementation and its qualification slice are complete. Project and
episode brands resolve to immutable normalized logos; exact titles generate a
rear-screen-safe slate; semantic introductions force a frontal total-studio fly-in;
thumbnail selection is checksum-bound and has no heuristic fallback. Direct camera
targeting, calibrated plate review, screen-graphics materialization, source-clock
preservation, and the nonlinear B-roll editor are exercised by automated and browser
tests. The Production v2 qualification is visible in the normal approval workflow
with its deterministic thumbnail.

The intentional gate remains: no new final, package, manifest, recovery archive, or
publication was created because approval `7eee0eb7-d705-410d-b5fc-2f77f1320cdd`
is pending. Non-frontal plate plus screen-graphic overlap is explicitly disabled
until the calibrated quadrilateral is applied by the compositor; frontal targeted
camera motion and reviewed alternate plates without new screen graphics are ready.

The follow-up usability pass repaired the practical clipping workflow. A selected
short clip now opens at a useful zoom and is centered; large striped grips and
playhead buttons both trim it; B-roll programme and source clocks stay aligned;
preview-frame source changes preserve clip length; precise millisecond inputs are
progressively disclosed; and real media durations replace the false zero values.
In Chromium, a start trim to 3.009 seconds changed programme/source ranges together,
a pointer end trim extended both to 7.000 seconds, preview source start at 10.000
seconds retained the 3.991-second clip length, ArrowRight moved only the programme
range by 100 ms, and Undo/Redo restored both clocks. The browser draft was never
saved, so timeline v8 and every immutable production/approval artifact remain
unchanged.

## Context and Orientation

The FastAPI backend lives in `backend/app`. Domain contracts are Pydantic models in
`backend/app/domain/schemas.py`; HTTP routes are in `backend/app/api/routes.py`;
repository persistence is JSON-backed SQL records under
`backend/app/infrastructure`; timeline composition and validation are in
`backend/app/services/timeline_service.py`; FFmpeg rendering and thumbnails are in
`backend/app/services/render_service.py`; managed B1 media work is routed through
the existing ComfyUI/B1 service rather than a GPU port.

The React/TypeScript application is primarily in `frontend/src/main.tsx`. Its
`TimelineEditorPanel` renders the existing parallel directing tracks. A “content”
clip defines which source frames play on the programme clock; its linked
“presentation” clip defines whether those frames appear on the rear screen or
fullscreen and how they transition. A new `screen_graphics` lane will represent
brand graphics on the same programme clock. A camera-plate bundle is an immutable
image plus calibrated rear-screen corners, desk occlusion geometry, participant
seat anchors, provenance, and a human review state.

The development system runs four user services: API, Web, render worker, and
workflow worker. Stateful acceptance work uses the API at its configured local
address and object storage under this repository. Existing accepted assets are
never overwritten: every upload, slate, timeline, preview, thumbnail, render,
package, and manifest receives a new identifier and checksum.

## Plan of Work

First create and validate a full recovery archive while the baseline is known clean.
Then extend project and episode schemas with optional brand metadata, add a focused
branding service that validates uploads, normalizes images, generates the bundled
fallback mark, lays out an exact non-truncated title, and registers immutable assets.
Expose project and episode logo endpoints without changing existing create/update
callers.

Next extend timeline normalization and composition with `screen_graphics`, richer
camera clips, explicit clip-link validation, and semantic introduction detection.
The introduction must span a total-studio fly-in and an identity-slate clip, reject
overlapping B-roll presentation, and record a stable thumbnail frame in render
provenance. Change thumbnail generation and QC to consume that marker only.

Add camera-plate upload, managed-generation, calibration, and review endpoints.
Validate complete geometry and approved state at timeline save and render time.
Implement participant-targeted pans by interpolating measured seat/face anchors.
Restrict alternate plates to silent or total-studio material; speaking closeups stay
on the frontal plate.

Refactor the render materializer around a single sorted boundary set for B-roll,
screen graphics, and camera clips. Every derived piece retains the original audio
and dialogue offset. Apply the identity slate ahead of rear-screen B-roll, and apply
virtual camera or approved plate geometry only within its clip interval.

Finally upgrade the timeline UI in contained components: source library and preview,
synchronized playhead, pointer-based clip movement and edge resizing, snapping,
keyboard nudging, local undo/redo, duplicate/remove, an inspector, camera presets,
and visible validation. Preserve numeric controls as accessible precise-entry
fallbacks. Validate with unit tests, a production build, and a real Chromium flow.
Create a short new episode qualification revision and stop at the explicit human
review gate before delivery derivatives.

## Concrete Steps

All commands run from `/srv/DialectiCore`.

Create and validate the recovery archive through the authenticated local API, using
a 1,200-second client timeout because the object store is several gigabytes:

    curl --max-time 1200 -sS -X POST "$DIALECTICORE_API/api/v1/system/backups" ...
    curl --max-time 1200 -sS -X POST "$DIALECTICORE_API/api/v1/system/backups/restore" ...

Run focused backend tests after each service increment:

    .venv/bin/pytest -q backend/tests/test_timeline_service.py \
      backend/tests/test_render_service.py backend/tests/test_api.py

Run frontend tests and its production build after editor increments:

    npm --prefix frontend test -- --run
    npm --prefix frontend run build

Run the complete project checks before stateful qualification:

    .venv/bin/pytest -q
    npm --prefix frontend test -- --run
    npm --prefix frontend run build

Restart only the four DialectiCore development services, then exercise a real browser
flow and confirm saved timeline persistence after another restart. Exact service and
browser commands will be appended here once discovered from the live unit and
Playwright configuration.

## Validation and Acceptance

Acceptance requires automated and browser evidence that an exact long episode title
and effective logo remain legible for the entire semantic introduction, that the
camera begins in total studio and performs a smooth fly-in, and that rear-screen
B-roll cannot replace or overlap the identity slate. Thumbnail generation must use
the checksum-bound frame marker at 35 percent of that introduction, constrained to
1.0–2.5 seconds after start and at least 500 ms before its end. An episode without
the marker must receive an actionable HTTP 422, not a fallback thumbnail.

A targeted participant pan must keep programme audio continuous and finish at the
selected participant anchor. An alternate angle must work only after a complete
bundle is calibrated and approved; an absent, incomplete, or rejected bundle must
be visibly disabled and rejected by the backend. Camera clips must not introduce
lip-sync or timing discontinuities.

In the browser, a director must be able to select an eligible source, preview and
seek it, add it to the programme, drag and resize its content clip, move its linked
presentation interval, set source in/out at the visible frame, snap or bypass snap,
undo/redo locally, save a new immutable timeline revision, reload it, and render the
same continuous source clock. Brand conflicts, invalid ranges, source overflow,
orphaned links, missing anchors, and unapproved angles must explain the correction
needed and block Save.

The new Production v2 qualification must preserve every accepted prior artifact and
pass deterministic media QC before appearing in the human approval queue. No final
render, package, production manifest, or live publication occurs before approval.

## Idempotence and Recovery

Schema additions are optional and have deterministic defaults. Uploads and slates are
content-addressed by checksum and may safely be retried without overwriting old
objects. Timeline saves remain immutable revisions. Stateful qualification scripts
must discover an already-created matching asset before retrying expensive work.

Before edits, create a database/object/runtime archive and verify it using restore
`apply=false`. Source rollback is the clean commit
`77dffed8ef3821f9fdcf2356ebaebd97609d3116`; production rollback continues to be
the accepted final/package/manifest and the previously validated recovery archive.
Do not use destructive Git resets or overwrite storage. If a stateful stage fails,
retain its immutable evidence, correct the cause, and create a new revision.

## Artifacts and Notes

Immutable Production v2 baseline:

- final render `7d2e95c1-56d2-4840-af59-c83c2a3c17fb`, SHA-256 `c2688f770873d53853b9f2837a3b87b0f305be57d88aa375e257a869db04736c`;
- reviewed thumbnail `f6e0a3b1-f22f-4b4d-bbb7-cc3096bbd3f4`, SHA-256 `fa62ba51341be2afb54ba2156de2e08290294476936d05da0bd88aa01e1d4a20`;
- delivery package `38fe0c47-86d4-472a-891d-43ab9df97d65`, SHA-256 `69a22a4a04cdc67236de9da3d0f685a76657ee737c8b2e73caa04d477c4ecd4c`;
- production manifest `b6e13ce5-39f8-4995-a512-4fc2c7b68820`, SHA-256 `675418356c92d8a77e8d96b1459b12ea497a8b8c8d26e6133a6a906d4bb4e516`;
- validated recovery archive `dialecticore-backup-20260815T143526Z-production-v2-delivery-retry-20260815.tar.gz`, SHA-256 `7c28194f9c894eb9d2b6bd6df093ac3e5b620cfe3108d946cc209304d9bf0312`.

Pre-change recovery and new qualification evidence:

- recovery archive `dialecticore-backup-20260815T152704Z-pre-branded-intro-camera-editor-20260815.tar.gz`, SHA-256 `34cf4a64138a4cb502cb6e693de541951d9e3e1eb96f6b24917aae2c2da04974`, validated with restore `apply=false` across 5,579 database records, 703 objects, and 17 runtime files;
- identity slate `5c5b26f5-0549-48bf-8d08-97480326cc37`, SHA-256 `e1ca45e1e3e80daf9ddee117e52eab6caf44b6789653174c65044aa3ce1c44d5`;
- timeline `2087a218-3b0a-494d-b2d5-e920650b018b`, SHA-256 `3555f70b7e2a590b8f676c398683c23f03cdf9c08fc0016c7a57b106b621e5d5`;
- 21-second qualification render `ab9fd6ab-84ee-432e-9fc2-3a103210f81e`, SHA-256 `aa16b95316b92b1da8a48772c98bbec7e3d5916180b81d7de19b6a0732f9c7c6`;
- deterministic thumbnail `51b3af24-a4aa-4f6c-a2ff-d0d5e5c95cf9`, SHA-256 `b09564a01fda94f71078f6af7f4a8116abefa6204987988ed49e8b426acd5f48`;
- pending human approval `7eee0eb7-d705-410d-b5fc-2f77f1320cdd`.

Validation evidence: the final complete backend suite passed 834 tests in 247.81
seconds; the focused branding/timeline/render slices also passed, Ruff passed, all
103 frontend tests passed, and the production Web build passed. Chromium confirmed
five eligible B-roll sources, linked content and
presentation duplication, two-step undo, valid save state, project branding
controls, camera-plate controls, and the pending qualification review.

Timeline usability evidence: all 108 frontend tests and the TypeScript/Vite
production build pass. The pure editing tests cover source-aware boundary clamps,
source-length preservation, invalid precision input, duration selection, and focus
zoom. Chromium evidence is in
`output/playwright/timeline-before-usability-fix.png`,
`output/playwright/timeline-after-usability-fix-draft.png`, and
`output/playwright/timeline-trim-inspector-final.png`; the final live snapshot
reported a valid timeline and 32x focus without the misleading video fallback.
Implementation commit `17cda8f` is published on `origin/main`; GitHub CI run
`31905346193` completed successfully (Compose 6 seconds, frontend 24 seconds,
backend 3 minutes 51 seconds).

## Interfaces and Dependencies

Add optional `Project.branding` and episode-level logo override contracts while
preserving old JSON payloads. Branding metadata includes show name and immutable logo
URI, SHA-256, width, height, and MIME type. Add project and episode multipart logo
upload endpoints accepting PNG, JPEG, or WebP up to 10 MiB and producing RGBA PNG.

Add `screen_graphics` clips with `kind=show_identity`, `asset_id`, programme range,
and thumbnail-candidate metadata. Add camera clip fields `angle_id`,
`from_participant_id`, `target_participant_id`, and `easing`, plus
`action=pan_to_participant`. Camera-plate bundles include the image asset identifier,
rear-screen quadrilateral, desk occlusion polygon or mask, seat anchors, provenance,
and review decision. All APIs use current authentication, repository, object-store,
approval, and managed-B1 dependencies; no new database, GPU port, or runtime service
is introduced.

Plan change note (2026-08-15 15:13Z): created this plan from the user-approved
feature and rollout decisions, explicitly preserving the completed Production v2
delivery and its human-review boundary.

Plan change note (2026-08-15 16:04Z): recorded the verified recovery archive,
completed implementation milestones, and the explicit non-frontal screen-graphics
guard discovered during compositor validation.

Plan change note (2026-08-15 16:34Z): recorded complete automated/browser evidence,
the two preserved rejected qualification attempts, the corrected immutable render
and thumbnail, and the active human-review gate.

Plan change note (2026-08-15 16:42Z): recorded successful source publication and
GitHub CI completion for implementation commit `775ba775`.

Plan change note (2026-08-15 19:35Z): reopened the editor milestone after real-user
feedback, recorded the browser-reproduced duration/scale/source-clock failures, and
added a focused usability-and-correctness validation pass without changing the
pending production approval or immutable assets.

Plan change note (2026-08-15 19:55Z): recorded the completed clipping-usability
repair, source-clock semantics, 108-test/build result, real Chromium pointer and
keyboard evidence, and confirmation that the browser-only qualification draft was
not saved.

Plan change note (2026-08-15 20:01Z): recorded the published implementation commit
and successful terminal GitHub CI evidence.
