# Make UI editorial decisions drive the rendered studio programme

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

An operator must be able to change camera views and other editorial settings in
DialectiCore's timeline editor and see those exact decisions in the produced MP4.
The current episode must retain its primer, dialogue timing, B-roll edits, audio,
and transitions while its 21 discussion turns are relinked from obsolete isolated
character videos to the current P40 native studio-panel speaking clips. Rendering
must fail before encoding if a saved timeline points at an obsolete generation of
speaking clips. The observable outcome is a browser-edited camera setting that is
present in the new timeline version and visibly changes a qualified render, then a
corrected full preview containing studio views rather than isolated characters.

## Progress

- [x] (2026-08-16 13:15Z) Diagnosed completed preview `2ee7b2b6` as structurally invalid despite valid H.264/AAC media.
- [x] (2026-08-16 13:17Z) Proved all 21 timeline segments reference pre-recovery `production_v2_speaking_character` assets while 21 matching current P40 studio-panel assets exist.
- [x] (2026-08-16 13:31Z) Traced and tested UI edit, timeline-versioning,
  asset-selection, and render-preflight boundaries.
- [x] (2026-08-16 13:39Z) Implemented version-safe relinking, targeted camera
  source planning, native-source editorial motion, and strict render preflight.
- [x] (2026-08-16 14:29Z) Proved real UI camera changes survive save,
  materialization, manifest generation, and pixel output.
- [x] (2026-08-16 14:53Z) Repaired the live timeline, qualified a short
  structural proof, and produced a corrected full preview.
- [ ] Run complete checks, document results, commit, push, verify CI, and confirm local/remote equality.

## Surprises & Discoveries

- Observation: a `studio_scene` layer name does not prove a studio image exists.
  Evidence: each current timeline turn has exactly one `studio_scene` layer, but
  that layer points to an older isolated `production_v2_speaking_character` MP4.
- Observation: the renderer correctly followed a stale immutable timeline.
  Evidence: timeline edit version 11 predates the recovered clips and every one of
  its 21 `video_asset_id` values differs from the current asset for the same
  transcript turn.
- Observation: media-integrity acceptance was too weak for editorial acceptance.
  Evidence: preview `2ee7b2b6` has matching checksum, H.264/AAC, 364.292 seconds,
  and a 1 ms A/V offset while containing the wrong visual programme.
- Observation: the live edit-version-11 timeline predates the explicit seated
  composition marker and stores the legacy view name `speaker_centered`.
  Evidence: the API payload has no `media.composition_policy`; the episode's
  definition still deterministically declares `studio_directed/seated_panel`.
  The repair therefore infers the contract from the episode and canonicalizes
  `speaker_centered` to `speaker_medium`.
- Observation: a no-op visual replan regenerated every seated plate when its
  optional portrait/full-body URI was absent.
  Evidence: stored `None` was compared to normalized empty string. Normalizing
  both sides now preserves all unaffected plates and turn clips.
- Observation: qualification renders were incorrectly treated as completed full
  previews by both UI gates and backend duplicate detection.
  Evidence: the UI initially disabled full rendering, then the API returned 422
  for the same timeline/preset even though only a 21-second qualification slice
  existed. Review scope is now part of render identity on both boundaries.
- Observation: parallel B-roll content and presentation are intentionally
  independent UI tracks, but the renderer required an optional explicit link.
  Evidence: timeline v16 retained five content clips and twenty presentation
  clips while the first full output showed a blue rear screen. Time-aligned
  content resolution now materializes 33 render pieces and visibly displays the
  saved B-roll clips.
- Observation: an upstream visual plate could end before the authoritative
  dialogue/timeline duration.
  Evidence: the first full UI pass had 364.292 seconds of audio but only 354.875
  seconds of video. Final-boundary frame padding and exact trimming now keep the
  streams within one 24 fps frame.

## Decision Log

- Decision: preserve the saved timeline and create a new version with narrowly
  relinked speaking assets instead of rebuilding the timeline from defaults.
  Rationale: rebuilding could discard operator-created B-roll trims, timing,
  transitions, and camera edits.
  Date/Author: 2026-08-16 / Codex.
- Decision: validate both structured state and rendered pixels.
  Rationale: database or metadata assertions alone did not catch the bad preview.
  Date/Author: 2026-08-16 / Codex.
- Decision: reject stale speaking-asset generations before FFmpeg starts.
  Rationale: a successful encoder cannot turn obsolete inputs into the requested
  editorial result.
  Date/Author: 2026-08-16 / Codex.
- Decision: treat native camera framing and editorial camera motion as separate
  contracts. The requested view selects an exact B1 source; push, pull, fly-in,
  and pan remain deterministic render-stage transforms of that verified source.
  Rationale: cropping a medium source cannot recreate a real wide view, while
  suppressing motion on native sources made UI camera actions inert.
  Date/Author: 2026-08-16 / Codex.
- Decision: make `review_scope` part of a render's uniqueness and user-facing
  review identity.
  Rationale: a short qualification artifact must neither block nor satisfy the
  full-preview gate.
  Date/Author: 2026-08-16 / Codex.
- Decision: resolve unlinked B-roll presentation by programme time and split at
  content boundaries, preferring the newly started content in an overlap.
  Rationale: this matches the UI's parallel-track model without rewriting saved
  editorial clips or inventing hidden links.
  Date/Author: 2026-08-16 / Codex.

## Outcomes & Retrospective

The browser saved immutable timeline v16 as asset
`82f4d06a-e2b3-4332-9fe6-dc995a7ff330`. Its UI-authored camera programme uses
an establishing-wide/fly-in introduction, a Grok close-up/slow-push, five
panel-two-shot/dissolve turns, normal speaker-medium turns, and an
establishing-wide/slow-pull conclusion. The save retained all five B-roll source
trims and generated only the two missing native camera sources.

The 21-second qualification render proved the total studio introduction and the
complete show logo/episode title without obscuring the six seated participants.
The final browser-requested full preview is asset
`bc016be4-3d20-4b28-bba8-221fbc5f0987`, render
`1be98f8a-00ee-40c0-a7fe-2650075ad483`, checksum
`sha256:f4987d1ff24d33059e2184f7be196336db1a7537fbb2879573e5d8054fe6f179`.
It is 1280x720 H.264 at 24 fps with 48 kHz stereo AAC. Video is 364.333333
seconds and audio is 364.292 seconds, a single-frame endpoint difference accepted
by integrity QC. The manifest records 22 source segments, 33 render segments, 21
camera clips, 20 B-roll presentation clips, 5 B-roll content clips, 30 overlays,
32 studio-context segments, and a preserved source clock. Extracted frames visibly
confirmed the intro wide, close-up, panel framing, medium framing, moving B-roll,
and conclusion wide. Preview approval remains pending for human review.

Validation completed with 847 backend tests and 115 frontend tests passing,
plus Ruff, TypeScript/Vite production build, and `git diff --check`. The only
remaining work is repository publication and CI confirmation.

## Context and Orientation

The live episode is `9d145344-82c9-46cc-b4c1-661d95f0bf56`. Its current timeline
asset is `cc7452a0-d78c-48fe-9408-c3f4993477e9`, edit version 11. Timeline creation
and editing are implemented in `backend/app/services/timeline_service.py` and API
routes in `backend/app/api/routes.py`; the web editor is under `frontend/`.
`backend/app/services/render_service.py` resolves timeline visual layers and
constructs the FFmpeg visual plate. A current speaking clip is a non-replaced,
completed `video_primary` asset whose `source_entity_id` is the transcript turn
and whose metadata contains native `studio_panel` evidence.

## Plan of Work

First trace the browser request used to save camera changes and the service method
that creates the next immutable timeline asset. Add regression tests showing that
camera settings and unrelated B-roll/timing data survive a targeted current-asset
relink. Add a render preflight test showing an obsolete speaking generation is
rejected, while a current native studio clip passes.

Implement the smallest shared timeline normalization or relink operation at the
timeline-version boundary. It must match assets by transcript turn, require a
unique current completed native studio-panel clip, update asset IDs, visual-layer
references, fingerprints, and provenance, and leave all unrelated segment and
track fields byte-for-byte equivalent. Camera edits remain editorial instructions:
they must select a compatible current source or derive the requested camera from
the native studio source during rendering, not silently revert to generation-time
metadata.

Use the live UI to change one representative turn between two supported camera
views. Save each version, capture the API/timeline evidence, render bounded proof
clips, and compare frames or crops so the output visibly differs in the requested
way. Restore the intended editorial value afterward. Then version-safely repair
the complete live timeline, ensure introduction and conclusion are total studio
views, qualify a short multi-shot proof, and only then request a full preview.

## Concrete Steps

From `/srv/DialectiCore`, run focused timeline, render, API, and frontend tests,
then the repository lint/build gates. Use the Playwright CLI wrapper at
`/home/mordred/.codex/skills/playwright/scripts/playwright_cli.sh` to operate the
actual editor. Query the local API at `http://127.0.0.1:8000`, and use ffprobe plus
frame extraction or image comparison for generated artifacts. Do not rewrite the
episode record directly and do not overwrite an existing timeline or render.

## Validation and Acceptance

Acceptance requires tests proving preservation of primer, dialogue timing, B-roll
source trims, transitions, parallel tracks, and screen graphics during relinking.
A saved UI camera change must appear in a new immutable timeline version, in the
render manifest, and in visibly different output frames. Rendering an obsolete
speaking generation must fail before FFmpeg. The live corrected preview must have
valid H.264/AAC, matching checksum, synchronized audio, a studio introduction and
conclusion, studio-context discussion views, and no regression to isolated
character cards.

## Idempotence and Recovery

Every edit creates a new timeline asset and marks the prior one replaced, so the
repair is retryable and rollback is the existing timeline-version restore path.
Proof renders are separate preview assets and never replace approved finals. If a
camera proof fails, stop before the full preview and retain both timeline versions
and render evidence. Never mutate historical assets or approved media files.

## Artifacts and Notes

Bad preview: asset `2ee7b2b6-1f0d-4430-9491-7c0989ebafc4`, render
`e77579d4-07df-45f6-a1cd-ecc3c67e31cb`, SHA-256
`97d0a7d4fb1d988b574c6d319fb390bb9e348798e3ce026b01426897455427b0`.
It is a technically valid but editorially rejected artifact.

## Interfaces and Dependencies

No new service, port, model, or dependency is required. Existing interfaces are
the episode timeline edit/version API, the timeline editor, `TimelineService`,
`RenderService`, local object storage, FFmpeg/ffprobe, and the existing managed P40
speaking assets. B1 scheduling and the P40 media deployment are not changed.

Plan update note (2026-08-16 13:17Z): created from live timeline, render process,
asset-generation, and user-observed visual evidence before implementation.

Plan update note (2026-08-16 15:06Z): live UI/render acceptance and full local
regression validation completed; publication checkpoint remains.
