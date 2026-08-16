# Preserve high-quality speaking animation while honoring UI camera direction

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

An operator must be able to choose establishing wide, speaker medium, speaker
close-up, panel two-shot, camera motion, transition, and rear-screen B-roll
settings in DialectiCore's timeline editor and see those decisions in the MP4
without sacrificing character or B-roll quality. The corrected renderer must use
the existing high-detail, audio-driven Production-v2 speaking performances for
the moving speaker and use UI direction to compose or crop the studio view. It
must not substitute the extremely compressed whole-studio camera files merely
because a camera view changed. Rear-screen media must preserve aspect ratio and
offer a fit policy that does not silently discard most of the source image.

The observable result is a browser-saved timeline whose distinct camera settings
appear in a short multi-shot proof and corrected full preview, with sharper faces,
visible localized mouth motion, preserved studio context, properly fitted B-roll,
valid audio, and a render manifest that names the exact sources and fit policy
used for the pixels.

## Progress

- [x] (2026-08-16 15:55Z) Reviewed the current full preview frame by frame and
  compared its selected camera-specific sources with retained Production-v2
  speaking performances.
- [x] (2026-08-16 15:58Z) Proved the regression source and rear-screen crop
  behavior from media probes and the active rendering code.
- [x] (2026-08-16 16:10Z) Add regression-first tests for source preservation, UI camera composition,
  rear-screen fit, manifest evidence, and quality preflight.
- [x] (2026-08-16 16:31Z) Implement the timeline/render/UI source separation,
  high-resolution studio compositor, aspect-safe B-roll controls, real camera
  crops/moves, integrity policy, and high-quality preview preset.
- [x] (2026-08-16 16:44Z) Validate focused and full backend/frontend suites and production builds.
- [x] (2026-08-16 16:27Z) Save representative editorial settings through the browser and inspect a
  bounded multi-shot proof before requesting a full render.
- [x] (2026-08-16 16:45Z) Produce and inspect a corrected immutable full preview
  and update this plan.
- [ ] Commit, push, watch CI, and verify local/remote commit equality.

## Surprises & Discoveries

- Observation: camera correctness and character quality became coupled in the
  previous repair.
  Evidence: the current timeline makes exact UI-selected native camera assets the
  `video_primary`; all are 1024x576 at 12 fps and approximately 29-97 kb/s, while
  the retained Production-v2 speaking assets are 1024x1024 at approximately
  212-366 kb/s and visibly preserve localized mouth motion.
- Observation: the high-resolution source artwork is still present.
  Evidence: representative portrait and full-body character references are
  1024x1536 PNGs; the loss occurs after source selection/generation, not because
  the original artwork is unavailable.
- Observation: rear-screen media is not geometrically stretched, but its default
  cover crop is editorially destructive.
  Evidence: the renderer uses aspect-preserving `scale=...:force_original_aspect_ratio=increase`
  followed by a 640x165 center crop. A 16:9 source therefore loses 54.2 percent
  of its vertical image, and one current B-roll source is only 640x360.
- Observation: final H.264 normalization is not the first quality bottleneck.
  Evidence: the 1280x720 master is approximately 2 Mb/s, but it is built from
  character sources as small as 250-620 KB for ten to twenty-one seconds.
- Observation: the first real proof exposed two integration defects that focused
  filter tests did not exercise.
  Evidence: camera provenance referenced a helper owned by TimelineService, and
  the first proof script sliced before parallel camera-track materialization.
  Both failed explicitly before publishing output and were corrected.
- Observation: aspect-preserving contain with black padding still looked like a
  fitting defect on the ultra-wide rear display.
  Evidence: proof frames preserved the complete 16:9 source but showed black
  side bands. The accepted follow-up uses a blurred, darkened cover copy behind
  the uncropped foreground copy, preserving all source pixels without empty bars.
- Observation: the initial 1100-pixel panel crop was not meaningfully distinct
  from a wide panel view.
  Evidence: the proof showed four to five cast members. Tightening it to 650
  pixels yields the active speaker with immediate neighbours and visibly differs
  from the wide, medium, and close treatments.

## Decision Log

- Decision: separate performance source from camera direction.
  Rationale: lip motion is an actor/performance concern; establishing, medium,
  close-up, two-shot, and motion are editorial composition concerns. Treating a
  low-detail whole-studio camera file as both destroys the better performance.
  Date/Author: 2026-08-16 / Codex.
- Decision: retain immutable timeline/version semantics and preserve operator
  edits rather than rebuilding the timeline.
  Rationale: B-roll trims, timing, transitions, and screen graphics are already
  correct and must not be lost while correcting source selection.
  Date/Author: 2026-08-16 / Codex.
- Decision: introduce an explicit rear-screen fit contract with aspect-preserving
  contain as the safe default, while retaining operator-selectable cover and focal
  positioning where intentional.
  Rationale: a silent center crop that removes more than half the image is not a
  reliable default for evidence, people, or text-heavy B-roll.
  Date/Author: 2026-08-16 / Codex.
- Decision: validate with rendered pixels and temporal frame strips in addition
  to state, manifests, and codecs.
  Rationale: the previous preview had correct metadata and camera variety while
  visibly containing poor character animation.
  Date/Author: 2026-08-16 / Codex.
- Decision: use an 8 Mb/s 720p preview preset for operator review of this and
  future UI previews.
  Rationale: the preview is the actual visual approval surface; 2 Mb/s obscures
  whether small facial motion and detailed B-roll survived composition.
  Date/Author: 2026-08-16 / Codex.

## Outcomes & Retrospective

Implementation and live qualification are complete. Browser-authored timeline version 19 is active;
its integrity result has no failure (only the pre-existing missing-subtitle
warning). A 61.625-second proof at 1280x720/24 fps, 7.8 Mb/s H.264 and 192 kb/s
AAC passed visual inspection. Its temporal Grok strip shows mouth changes while
the hair/head silhouette stays stable. The corrected browser-requested full
preview is immutable render asset `1bd4bc7c-99f2-49dd-a8e8-282ef18d1f52`,
SHA-256 `92edbaa2087a814c14ce8090af1b87a9bf1c5fc5a90554b5144e3fcbac5e2f95`,
against timeline asset `00af9dcf-355c-4ac5-9f26-fa07e8cf6be2`.

The full output is 364.333 seconds at 1280x720/24 fps with approximately
8.05 Mb/s H.264 video and 192 kb/s AAC stereo. Media QC measured a 41 ms A/V
duration difference, no black programme intervals, and no silent programme
intervals. Full-frame inspection covered introduction, close speaker framing,
the Mistral-to-Claude pan, the neighbour composition, DeepSeek scale/contact,
rear-screen footage, and the total-studio conclusion. The command-centre review
surface reports the asset completed and offers its download link.

Automated results: 849 backend tests passed, 115 frontend tests passed, focused
timeline/render tests passed, Ruff passed, the TypeScript/Vite production build
passed, and `git diff --check` passed. The only build observation is Vite's
pre-existing large-chunk advisory.

## Context and Orientation

The live episode is `9d145344-82c9-46cc-b4c1-661d95f0bf56`. Its current
timeline asset is `82f4d06a-e2b3-4332-9fe6-dc995a7ff330`, and the reviewed preview
render is `1be98f8a-00ee-40c0-a7fe-2650075ad483`. Timeline versioning and UI edit
normalization live in `backend/app/services/timeline_service.py`; source planning
lives in `backend/app/services/comfyui_service.py`; layered FFmpeg composition and
manifest generation live in `backend/app/services/render_service.py`; API
orchestration is in `backend/app/api/routes.py`; the browser timeline editor and
render gates are under `frontend/src`.

In this plan, a "speaking performance" is the 1024x1024 Production-v2 clip that
animates one seated character from dialogue audio. A "camera-specific source" is
the newer 1024x576 whole-studio file generated for a requested view. A "camera
direction" is saved UI state such as `speaker_close_up` plus `slow_push`. A
"rear-screen fit policy" determines whether 16:9 B-roll is contained inside the
wide studio screen or cropped to cover it.

## Plan of Work

First add tests that reproduce the regression: save a camera edit on a segment
that has both source families, prove that the higher-quality speaking performance
remains available to composition, and prove that the requested view/action still
changes layout and rendered filters. Add tests for contain and intentional cover
rear-screen policies, source-quality metadata, and manifest truthfulness.

Then adjust timeline relinking so camera edits do not erase or disguise the
speaking-performance relationship. Adjust render source ordering and layout
selection so the Production-v2 performance is the animated foreground over the
appropriate studio/group base, with UI direction selecting establishing, medium,
close-up, two-shot, and motion. Exact native camera files may remain provenance or
fallback evidence but may not displace a demonstrably better speaking source.

Add an explicit B-roll fit value to the existing editorial clip representation
and UI. The default for rear-screen B-roll is contain; cover remains available
with a focal position. Render contain without distortion, using a deterministic
background fill inside the measured screen aperture. Record the chosen fit and
source dimensions in the manifest, and warn or fail before final production when
source quality is below the configured floor.

After automated validation, operate the actual browser editor to save several
distinct camera views and both B-roll fit modes. Render a short proof covering a
wide introduction, close-up, medium, two-shot, and rear-screen media. Inspect
whole frames and consecutive speaker frames. Only after that proof passes, render
the full timeline and inspect representative frames and A/V duration.

## Concrete Steps

Work from `/srv/DialectiCore`. Run focused backend tests for timeline and render
services and frontend tests for timeline editing/render gates while iterating.
Run Ruff on changed Python files, the full backend suite, `npm test -- --run`,
`npm run build`, and `git diff --check` before live rendering.

Use `/home/mordred/.codex/skills/playwright/scripts/playwright_cli.sh` for real UI
edits. Use the existing local API and media worker paths rather than direct record
mutation. Use `ffprobe`, FFmpeg frame extraction, and local image inspection on
proof and final artifacts. Do not overwrite existing immutable assets.

## Validation and Acceptance

Automated acceptance requires preservation of timeline duration, dialogue audio,
B-roll trims, screen graphics, transitions, and unrelated tracks. Tests must show
that each canonical UI camera view produces its intended composition while the
high-quality speaking performance remains the moving speaker. Rear-screen contain
must preserve the complete source frame without distortion; cover must preserve
aspect ratio and expose its crop/focal policy.

Integration acceptance requires a browser-created immutable timeline version and
a rendered proof whose manifest agrees with the UI direction and actual source
files. Consecutive frames must show localized mouth changes without whole-face or
whole-head corruption. The full preview must include the total-studio branded
introduction and conclusion, visibly distinct medium/close/two-shot views, moving
B-roll fitted to the screen, H.264/AAC streams, synchronized duration, successful
QC, and no stale render satisfying the new review.

## Idempotence and Recovery

All UI saves create new timeline assets, and all renders create new preview
assets. Tests and proof renders are safe to retry. Existing previews and source
files remain immutable. If the proof fails, stop before the full render and keep
the current production timeline available through normal version restoration.
Rollback is a code revert plus restoring the prior timeline version; no database
or object-store deletion is required.

## Artifacts and Notes

Reviewed bad-quality preview:
`storage/object-store/dialecticore/renders/9d145344-82c9-46cc-b4c1-661d95f0bf56/1be98f8a-00ee-40c0-a7fe-2650075ad483.mp4`,
SHA-256 `f4987d1ff24d33059e2184f7be196336db1a7537fbb2879573e5d8054fe6f179`.

Representative low-quality camera source:
`visual/.../video_primary/289b7057-230a-488f-8134-9ed703380d4f.mp4`,
1024x576, 12 fps, approximately 46 kb/s.

Representative retained speaking performance:
`production-v2/full/.../speaking/02-grok.mp4`, 1024x1024, 12 fps,
approximately 359 kb/s.

## Interfaces and Dependencies

No new service, model, port, or external dependency is required. Reuse the
existing `TimelineService`, `RenderService`, FFmpeg, object storage, immutable
asset model, P40 media workflow, managed B1 scheduling, and frontend timeline
editor. Any added schema field must be backward compatible so old timelines
receive a deterministic safe default.

Plan update note (2026-08-16 16:02Z): created from direct inspection of the live
preview, both source families, active FFmpeg filters, and current UI-authored
timeline before implementation.
