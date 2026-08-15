# Build the high-quality v2 character and B-roll production workflow

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

DialectiCore must produce a second, visibly improved version of the completed
talk-show episode without modifying or replacing the accepted v1 artifacts.
Characters must look like people of compatible seated stature, retain clean
high-resolution identity detail, speak with credible mouth and subtle head or
expression motion, and remain the visual focus of every speaker shot. Real
B-roll video must be editable on parallel directing tracks while dialogue and
character performance continue beneath it. The same B-roll playback may remain
on the physical rear studio screen or expand smoothly to fullscreen and return
without restarting. A user must be able to inspect and edit this behavior in
the Web UI and reproduce the final v2 render through the managed workflow.

The existing v1 episode, final render, package, database recovery archive, and
Git history are the rollback baseline. No work in this plan may modify
`/srv/TubeFactory`, bypass B1 scheduling, expose the P40 directly, or weaken
deterministic production gates. B-roll source and licensing metadata are
retained when supplied but are optional and never block editing or rendering.

## Progress

- [x] (2026-08-15 06:18Z) Preserved the completed v1 episode and source revision as the immutable comparison baseline; verified the four DialectiCore user services are active and the repository is clean at `ac3bb589c01f072450e70bdad097d3b6b094c8a3`.
- [x] (2026-08-15 06:20Z) Inventoried the current character references, seated plates, timeline structure, UI editor, renderer, B1 upload conversion, storage capacity, RAM, swap, and idle P40 state.
- [x] (2026-08-15 07:09Z) Captured `output/production-v2/v1-character-quality-baseline.json` and `.md` with all six source/upload/seated/speaking records, exact hashes and probes, useful pixel regions, speaking counts, and representative FFmpeg block/blur metrics.
- [x] (2026-08-15 07:04Z) Proved the current authenticated B1 upload accepts the original 2,624,199-byte RGBA PNG with identical SHA-256 and no warning, removed the obsolete lossy JPEG conversion, and passed focused preservation and managed-upload tests.
- [x] (2026-08-15 07:11Z) Created and dry-run restored the pre-v2 database/runtime archive `storage/backups/dialecticore-backup-20260815T071129Z-pre-production-v2-character-broll.tar.gz` (SHA-256 `1a96d2c3ea968d1acce18ff079fde0a57854e908163654a5525dd89884fad841`).
- [x] (2026-08-15 07:46Z) Generated six new 1280x1280 transparent seated masters with actual-alpha-bound normalization, explicit stature offsets, an exact common desk baseline, deterministic QC, and a measured 5.08 percent body-height spread.
- [x] (2026-08-15 08:07Z) Benchmarked v1 and enhanced MuseTalk paths through managed B1 on ChatGPT, Claude, and DeepSeek, then qualified the selected path on Gemini, Grok, and Mistral.
- [x] (2026-08-15 08:07Z) Selected enhanced normalized masters for five characters and a native-scale cropped source for DeepSeek; recorded job IDs, artifact hashes, motion metrics, runtime, RAM, VRAM, face-detector failures, and contact sheets.
- [x] (2026-08-15 08:18Z) Changed virtual-camera focus from speaker/neighbor midpoint to the active speaker, retained neighbor context as metadata, added an explicit central 45-55 percent framing contract, and passed focused camera tests.
- [x] (2026-08-15 08:21Z) Added backward-compatible `episode_timeline.v3` parallel dialogue, character, camera, B-roll content, B-roll presentation, and caption clips while retaining legacy segment-index tracks.
- [x] (2026-08-15 08:22Z) Extended the Timeline Editor with stacked lanes and accessible numeric clip start, end, and source-in controls; all 102 frontend tests and the production build pass.
- [x] (2026-08-15 08:24Z) Added render-boundary materialization that preserves uninterrupted stored dialogue segments and continuous B-roll source time across rear-screen/fullscreen/rear-screen changes.
- [x] (2026-08-15 10:01Z) Produced and received human approval for revision 2 of the 24-second six-speaker integrated qualification after revision 1 exposed undersized/floating DeepSeek geometry, a rear-screen leak at the torso/desk seam, and abrupt B-roll presentation transitions. The approval now records `approved_for_full_production` durably.
- [x] (2026-08-15 09:49Z) Created and dry-run validated the full-production safety archive `storage/backups/dialecticore-backup-20260815T094937Z-pre-production-v2-full-20260815.tar.gz`, SHA-256 `17888466a8e427fb619c3edc43ac1a71123add966b3a23b78814b620a0af9bcd`, including 5,473 database records and 657 object-store files.
- [x] (2026-08-15 10:51Z) Completed the resumable 21-turn full-production animation batch through the managed B1 media API. Preserved two valid first-attempt jobs, cancelled the remaining queued invalid-header jobs, normalized all upload-only WAV headers, and completed 300.377 seconds of dialogue with zero failures in the corrected batch.
- [x] (2026-08-15 10:56Z) Rendered, checksum-verified, technically qualified, registered, and browser-played the complete 364.333-second Production v2 preview as separate episode `9d145344-82c9-46cc-b4c1-661d95f0bf56`; its human preview approval remains pending before final render and packaging.
- [ ] Human-approve the complete v2 preview, then create and approve the final render, thumbnail, package, manifest, dry-run publish record, and recovery test without replacing v1.
- [ ] Commit and push intentional checkpoints, obtain green CI, verify deployed/local/remote source provenance, and record exact v2 artifacts and limitations.

## Surprises & Discoveries

- Observation: all six canonical portrait references and at least one full-body
  reference per character are independent `1024x1536` files, generally RGBA
  PNG, and remain intact in the object store.
  Evidence: `ffprobe` over
  `storage/object-store/dialecticore/visual-profiles/visual-*/reference-images`.
- Observation: the six approved seated-character outputs use `1280x720` RGBA
  canvases, but most pixels are transparent. For ChatGPT the recorded body is
  about `397x425` pixels and the face about `192x166`, so the canvas dimensions
  overstate useful animation resolution.
  Evidence: asset `aba0db55-fd25-49cb-ac52-3dcd636ac483` records body and face
  regions `0.31x0.59` and `0.15x0.23` of a `1280x720` canvas.
- Observation: `_prepare_b1_image_for_upload()` converts any alpha PNG to
  baseline `yuvj420p` MJPEG with no explicit quality setting. One 2.44 MB
  `1024x1536` ChatGPT PNG reached B1 as a roughly 75 KB JPEG before seated-master
  generation.
  Evidence: `backend/app/services/comfyui_service.py` and the stored redacted
  request for B1 job `job_b80f98c491d2483cbc043c1d7a933ff7`.
- Observation: `episode_timeline.v2` already carries a `tracks` object, but the
  entries are lists of sequential segment identifiers. Rear-screen B-roll is
  implemented by `_seated_panel_broll_insert_segments()`, which splits one
  dialogue turn into before/insert/after pieces.
  Evidence: `backend/app/services/timeline_service.py` and current 36-segment
  v1 timeline `6299cb36-8fae-4b82-97b4-1e7e5f5c2909`.
- Observation: the Web UI Timeline Editor is a scene list with numeric timing
  and camera controls; it has no spatial lanes or independently movable overlay
  clips.
  Evidence: `TimelineEditor` in `frontend/src/main.tsx`.
- Observation: the host has about 804 GB free storage and 101 GiB available
  RAM, while 7.8 of 8 GiB swap is occupied despite that available RAM. The P40
  is idle at 233 MiB VRAM and retains its 125 W cap.
  Evidence: `df`, `free`, and `nvidia-smi` at plan start. Qualification must
  measure swap deltas rather than treating old swapped pages as new pressure.
- Observation: all six approved seated masters use the same declared face
  region, about `192x166` useful pixels, despite their intact source references
  being `1024x1536`. Representative first-clip block means range from `1.846`
  for ChatGPT to `3.971` for DeepSeek; Claude is also high at `3.395`.
  Evidence: `output/production-v2/v1-character-quality-baseline.json` generated
  by `scripts/production_v2_baseline.py`.
- Observation: B1's current authenticated staged-upload endpoint accepts the
  original 2,624,199-byte ChatGPT RGBA PNG as `image/png`, preserves SHA-256
  `48598fe5216584a81b44a01edff11ddbd835827a1a7420b507c3d0cee58806ba`,
  and returns no warning.
  Evidence: upload probe `upload_9ca3a012c34f432282b59f803931fd87`.
- Observation: the pre-v2 recovery archive contains 5,457 database records and
  16 runtime files; a dry-run restore succeeds when the absolute archive path
  is used. The restore endpoint rejects a relative path with HTTP 422.
  Evidence: archive
  `storage/backups/dialecticore-backup-20260815T071129Z-pre-production-v2-character-broll.tar.gz`.
- Observation: B1's Real-ESRGAN workflow runner requires a staged private-image
  descriptor even though the advertised workflow schema describes base64 input.
  A base64 probe failed deterministically and all corrected staged-upload jobs
  completed through the managed API.
  Evidence: failed job `job_1dcb239a443740d19665ed00b7cd106d` and six
  successful upscale jobs recorded by `scripts/production_v2_qualify_upscale.py`.
- Observation: FFmpeg emitted normalized PNG masters with more than 256 IDAT
  chunks, exceeding B1's bounded media sniffer and causing an incorrect
  `application/octet-stream` classification. Merging the existing compressed
  IDAT payloads into one chunk changes neither decoded pixels nor alpha and is
  accepted by the same endpoint.
  Evidence: `_rechunk_png()` and the transport records in
  `output/production-v2/normalized-seated-masters/manifest.json`.
- Observation: the pinned MuseTalk detector accepts the enhanced normalized
  master for ChatGPT, Claude, Gemini, Grok, and Mistral, but rejects DeepSeek at
  full enhanced, Lanczos-enlarged, and intermediate scale. It accepts a 640x640
  crop that preserves the original approximately 166-pixel face scale and
  removes empty canvas.
  Evidence: failed jobs `job_1e438fece92345838f5b87fc4f914b3b`,
  `job_6192e3a8fa8644b79f5729cd1b994fc1`, and
  `job_9bb848d4cd914d40806a304a40ed78fb`; selected job
  `job_12b0a92b69ce43e1925e2e94434412f6`.
- Observation: selected animation jobs peak at 19,721 MiB P40 VRAM and about
  29 GiB host RAM, remain below the managed worker's safety envelope, and did
  not create new swap pressure or CUDA OOMs.
  Evidence: `output/production-v2/animation-qualification/analysis.json` and
  the corresponding B1 job telemetry manifests.
- Observation: centering a crop on the two outer seats initially hit the studio
  image boundary and left the speaker at about 43 percent of frame width. A
  narrow studio-colored edge extension permits the same 800x450 camera window
  to center all six seat anchors without changing character scale.
  Evidence: the rejected first integrated contact sheet and the selected
  `output/production-v2/integrated-qualification/manifest.json`.
- Observation: the uploaded empty studio plate is downloadable through the
  existing DialectiCore show-media endpoint even though its object is not
  present in the local object-store mirror.
  Evidence: browser/API retrieval of
  `object://dialecticore/show-media/scene-reference-images/47d9f89bed32daac.png`.
- Observation: a qualification render with a custom review stage was visible
  in the approval queue but not playable on the production page. A scoped
  qualification player now resolves the approval target asset and presents its
  exact checksum plus a download link.
  Evidence: Playwright loaded the player at 1280x720, reported ready state 4,
  and advanced playback to 1.14 seconds.
- Observation: DeepSeek's accepted detector-safe input has a 640px canvas and
  a 414px alpha body, while the other normalized inputs have 1280px canvases
  and roughly 1024px alpha bodies. Scaling every canvas to 330px made
  DeepSeek's rendered torso about 20 percent shorter even though its top edge
  was raised.
  Evidence: deterministic FFmpeg alpha bounds and
  `test_character_layout_scales_deepseek_and_anchors_every_matte_behind_desk`.
- Observation: the first qualification's blend expression used FFmpeg `T`,
  which inherited the seeked B-roll timestamp and could evaluate the transition
  as already complete. This was the cause of the apparently instantaneous
  studio/fullscreen changes.
  Evidence: isolated DeepSeek segment frames before and after changing the
  expression to the segment-local frame counter `N`; revision 2 shows studio,
  intermediate blend, and fullscreen frames over two seconds.
- Observation: several canonical Voicebox streaming WAV files carry a sentinel
  frame count of `2147483647` while their playable data is only 8-21 seconds.
  FFmpeg derives the correct duration from file length, but B1's deterministic
  Python WAV validator correctly treats the header literally and rejects the
  request before inference.
  Evidence: the first full-batch jobs failed with `duration_ms differs from
  uploaded WAV duration by more than 250 ms`; local `wave` inspection exposed
  the sentinel while `ffprobe` reported the expected duration. Upload-only PCM
  rewrites now pass the same `wave` calculation for all 21 turns.
- Observation: the corrected 21-job B1 batch completed with no failed or
  cancelled jobs, peaked at 19,721 MiB P40 VRAM and 32,497 MiB host RAM, and
  accumulated 2,826,928 ms of measured managed-runtime work. The complete
  preview decodes without error, contains no silence interval of at least 1.5
  seconds at -50 dB, and differs from its planned duration by only 30 ms.
  Evidence: `output/production-v2/full-production/animation/manifest.json` and
  `output/production-v2/full-production/render/qc.json`.
- Observation: Chromium requested the registered 96,045,894-byte preview with
  HTTP range semantics, reached ready state 4, played from 0 to 1.19 seconds
  without an error, and loaded all 134 shifted German caption cues; at 65
  seconds the first ChatGPT cue was active as expected after the primer.
  Evidence: registered render asset `57547bc3-8e51-428f-9fb8-feba96a31eea`
  and the Playwright production-page validation.

## Decision Log

- Decision: preserve v1 and create new v2 assets, timelines, previews, renders,
  approvals, and packages rather than editing current production artifacts.
  Rationale: v1 is the known-working rollback and objective comparison surface.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: normalize apparent seated stature using face, eye, shoulder, body,
  seat, desk, and perspective anchors, with approximately plus or minus 4-6
  percent intentional stature variation instead of equal bounding-box height.
  Rationale: source canvases and character silhouettes differ; eye/shoulder
  geometry determines perceived seated scale more reliably.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: animate independent high-resolution character masters before
  compositing into the studio, and keep intermediates lossless or visually
  lossless until the final delivery encode.
  Rationale: enlarging tiny faces from a flattened panel cannot restore missing
  identity detail and repeated H.264/JPEG stages amplify blocks.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: treat mouth synchronization and subtle head/eye/expression motion as
  separate capabilities and benchmark candidate paths before selecting one.
  Rationale: MuseTalk mouth motion alone cannot satisfy the requested overall
  performance quality, especially for stylized faces.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: derive camera crops from the active speaker and require the speaker
  face center within the central 45-55 percent of frame width, largest face
  prominence, complete head/shoulders, and retained desk context.
  Rationale: fixed three-seat crops can leave the speaking character at an edge
  and make a silent neighbor visually dominant.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: model B-roll as parallel clips on the master episode clock, with a
  separate presentation envelope controlling hidden, rear-screen, transition,
  and fullscreen states.
  Rationale: dialogue, character performance, camera direction, and B-roll must
  overlap independently without splitting or restarting the dialogue segment.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: accept B-roll source and license evidence when available, but never
  require it for ingestion, editing, preview, render, packaging, or completion.
  Rationale: this is an explicit user requirement; optional provenance remains
  useful without becoming a production blocker.
  Date/Author: 2026-08-15 / user and Codex.
- Decision: use a versioned per-character animation-input policy instead of
  requiring a single preprocessing path. Five characters use the enhanced
  normalized master; DeepSeek uses the native-scale source crop until a
  detector-compatible enlarged input is proven.
  Rationale: this is the smallest reproducible policy that improves all six
  characters while retaining the pinned managed runtime and avoiding an
  identity-specific detector failure.
  Date/Author: 2026-08-15 / Codex.
- Decision: materialize overlapping B-roll clips only at the renderer boundary,
  leaving the stored editorial timeline unsplit.
  Rationale: source playback and dialogue remain continuous and independently
  editable while the existing piece-based FFmpeg renderer remains compatible.
  Date/Author: 2026-08-15 / Codex.
- Decision: register qualification evidence in a new episode with its own
  pending approval rather than attaching it to the completed v1 episode.
  Rationale: this exposes real UI playback and human gating while preserving
  the v1 episode, render status, bytes, and SHA-256 unchanged.
  Date/Author: 2026-08-15 / Codex.
- Decision: scale DeepSeek's detector-safe composition canvas to 414px while
  keeping the other five at 330px, then anchor every alpha baseline 12px behind
  the foreground desk rather than positioning characters from a shared canvas
  top.
  Rationale: this equalizes useful body height, keeps the improved detector-safe
  animation, and removes the transparent torso/desk strip without painting over
  faces or shoulders.
  Date/Author: 2026-08-15 / user review and Codex.
- Decision: make B-roll presentation transition duration a bounded 0-5000ms
  clip property, default it to 1500ms in generated timelines, and use a 2000ms
  cosine-eased transition in qualification revision 2.
  Rationale: transition pace is an editorial choice and must not be embedded as
  an accidental fixed FFmpeg timestamp behavior.
  Date/Author: 2026-08-15 / user review and Codex.
- Decision: preserve the canonical Voicebox WAVs byte-for-byte and create
  provenance-bound, seekable PCM copies only for managed B1 animation uploads.
  Rationale: this repairs invalid streaming headers without mutating approved
  audio assets, while matching the exact duration validator used by B1.
  Date/Author: 2026-08-15 / Codex.
- Decision: register the complete v2 preview as a separate episode with cloned
  transcript identities, provenance-linked immutable audio, 21 new speaking
  assets, a v3 timeline, shifted selectable captions, technical QC, and a
  standard preview-render approval instead of attaching it to v1.
  Rationale: the UI can review and continue the normal final/package workflow
  while every v1 asset remains unchanged and independently recoverable.
  Date/Author: 2026-08-15 / Codex.

## Outcomes & Retrospective

Transport, normalization, animation selection, active-speaker camera policy,
parallel directing tracks, render materialization, and the integrated
qualification are implemented. The qualification is visible and playable in
episode `05540416-dd18-4f6e-aa33-5bc683b06b9f`; render asset
`e44ff4b5-a286-4e60-9fd9-bc5b8cbfe9ed` has SHA-256
`51de78de7623c598ed21c52aa02a1544ebe57793129cc2e3caa35f1ebfb0cd3e`.
Human review found revision 1 materially improved but rejected it for DeepSeek
scale/contact, the torso/desk B-roll seam, and transition speed. Revision 2
supersedes the first qualification without overwriting it. Its render asset is
`e27872fe-e838-42a1-9621-848af0a2cd87`, approval is
`7b53dd3f-d558-4483-95a9-8550f6bd883c`, and SHA-256 is
`647b732587ef841d1d88e71d186729d3f0b733fd364a42ad562c79f2b15bf29b`.
The v1 final remains completed with SHA-256
`1837df46318eba0bd0a21dc60c0d97b5c0236423476401058f3bdfd0278b3218`.
The user approved revision 2 on 2026-08-15, and the durable gate reads
`approved_for_full_production`. All 21 full speaking clips completed through
B1's managed media queue. The full preview is registered as episode
`9d145344-82c9-46cc-b4c1-661d95f0bf56`; render asset
`57547bc3-8e51-428f-9fb8-feba96a31eea` has SHA-256
`cbd77b9312f27bcb99f027e02094ca8e17a55974cf1e4196820178ef3b6db8d0`,
duration 364.333 seconds, and pending approval
`ae9b59be-ebd8-485f-b332-6e7c4e984bfe`. Technical QC passes and the browser
plays the exact registered asset with 134 selectable German cues. Final render,
thumbnail, package, manifest, dry-run publication, and recovery validation
remain correctly gated on the user's full-preview review.

## Context and Orientation

The repository is `/srv/DialectiCore` on branch `main`. The FastAPI backend is
under `backend/app`, the React application is primarily
`frontend/src/main.tsx`, and tests live in `backend/tests` and alongside
frontend modules. User services are `dialecticore-api-dev`,
`dialecticore-web-dev`, `dialecticore-worker-dev-render-worker`, and
`dialecticore-worker-dev-workflow-worker`.

Episode `cc1ad449-9cad-4a40-a150-652db0b7dc7a` is the v1 baseline. Its final
render asset is `37fa74da-820c-49c7-96c2-d973dc6efb46`; the physical file is
`storage/object-store/dialecticore/renders/cc1ad449-9cad-4a40-a150-652db0b7dc7a/3a938a75-5b9a-4311-869c-189806673c6f.mp4`, SHA-256
`1837df46318eba0bd0a21dc60c0d97b5c0236423476401058f3bdfd0278b3218`.
Timeline asset `6299cb36-8fae-4b82-97b4-1e7e5f5c2909` has 36 sequential
segments and seven static rear-screen cards. The validated recovery archive is
`storage/backups/dialecticore-backup-20260815T050652Z-completed-youtube-episode-runtime-snapshot-20260815.tar.gz`, SHA-256
`d388802ab664dba3edfa75310b2f38ec8ddf6e426d8235f9ce4731f671d404bb`.

`ComfyUiService` in `backend/app/services/comfyui_service.py` plans and submits
B1-managed seated-character and lip-sync jobs. Private references are uploaded
by `_upload_b1_private_media()` after `_prepare_b1_image_for_upload()`. B1 owns
the GPU lease; DialectiCore must continue using
`https://api.ai.b1.germering/v1/media/jobs` and must never call an internal P40
backend directly.

`TimelineService` in `backend/app/services/timeline_service.py` builds and edits
JSON timeline assets. Its current `tracks` map is an index of segment IDs, not
an independent clip model. `RenderService` in
`backend/app/services/render_service.py` turns sequential segments and visual
layers into FFmpeg composition. `TimelineEditor` in
`frontend/src/main.tsx` edits one selected sequential scene at a time.

In this plan a master timebase means that every clip uses absolute milliseconds
from episode start. A content clip selects source media and source in/out points.
A presentation envelope controls where that same content is visible over time.
Changing presentation must never reset its source playback clock.

## Plan of Work

First write a baseline report and JSON record under `output/production-v2` from
the exact v1 assets. Record useful face/body pixel regions, file formats,
checksums, video bitrates, macroblock or blocking indicators, motion metrics,
and B1 upload byte counts for all six characters. Sample the source PNG, seated
plate, speaking MP4, composited preview, and final encode separately. Monitor
RAM, P40 VRAM, and swap deltas during every later qualification.

Next change the B1 image preparation contract. Preserve raw non-alpha images.
For alpha references, first test whether the current authenticated upload path
now accepts PNG despite its historical classifier warning. If it does, upload
the original bytes. If it rejects the request, flatten only the transport copy
against a deliberate neutral background and encode a lossless PNG or a JPEG at
a measured high quality with 4:4:4 or the least destructive accepted format.
Never modify the canonical reference. Record original and transport dimensions,
format, byte count, checksums, and transformation. Add tests around alpha,
dimensions, quality selection, and metadata.

Add a versioned seated-master geometry schema to generation metadata. It must
record source landmarks, target seat, target eye and shoulder anchors,
perspective scale, intentional stature offset, character bounds, face bounds,
desk occlusion, output dimensions, and normalization version. Generate new
high-resolution transparent masters without replacing approved v1 assets. Add
deterministic QC for face pixel dimensions, scale spread, eye-line residual,
alpha matte, headroom, desk intersection, and identity/reference binding. Show
the six current/new comparisons in the UI before approval.

Use representative short audio excerpts to compare the existing path, corrected
transport with current MuseTalk, high-resolution input with lossless
intermediates, and any P40-compatible head/expression candidate that survives a
small feasibility probe. A candidate wins only if it improves visual review and
measured mouth motion/head continuity without identity drift, block artifacts,
CUDA OOM, new swap activity, scheduler bypass, or unacceptable runtime. Keep
current fallback behavior available for rollback but do not promote its
procedural mouth bar as the quality target.

After normalized master landmarks exist, calculate speaker camera windows from
the active speaker rather than a fixed seat group. Validate face center,
relative face area, complete head and shoulders, headroom, neighbor prominence,
desk context, and available studio background. Create safe participant-specific
windows for the two outer seats. Expose the derived framing and validation in
timeline JSON and the editor.

Then evolve timeline JSON to a new backward-compatible schema. Preserve
`segments` as the primary dialogue compatibility layer, and add independently
timed clip arrays under versioned `tracks`: dialogue, character performance,
camera direction, B-roll content, B-roll presentation, captions, and optional
audio. Normalize and validate clip IDs, absolute time ranges, source in/out,
asset binding, presentation keyframes, crop mode, optional audio, and optional
provenance. Existing v1 timelines must still load and render unchanged.

Extend the editor from a scene list to stacked lanes with clip selection,
drag/trim controls, numeric accessibility controls, snapping, and explicit
linked-versus-absolute movement. Implement rear-screen perspective mapping from
the measured quadrilateral with proportional cover crop and slight overscan.
Layer studio background, B-roll, animated characters, desk/foreground matte,
and graphics in that order. Implement an eased spatial expansion to fullscreen
and its reverse while deriving source time from the unchanged content clip.
B-roll audio defaults to muted.

Finally produce a short integrated qualification timeline using all six
characters, a screen-only video, and one screen/fullscreen/screen round trip.
Review frame contacts and the actual video through the UI. Only after it passes
should the workflow create new v2 speaking clips, timeline, preview, approval,
final render, thumbnail, package, manifest, and dry-run publish record for the
complete episode. Restart all four user services and validate recovery without
changing or removing v1.

## Concrete Steps

Run all commands from `/srv/DialectiCore` unless a command names another
working directory.

Establish and repeatedly verify the source and runtime baseline:

    git status --short --branch
    git rev-parse HEAD
    systemctl --user is-active dialecticore-api-dev.service \
      dialecticore-web-dev.service \
      dialecticore-worker-dev-render-worker.service \
      dialecticore-worker-dev-workflow-worker.service
    curl -fsS http://127.0.0.1:8000/api/v1/episodes/cc1ad449-9cad-4a40-a150-652db0b7dc7a/workflow/completion-readiness | jq '{status,failed_checks}'
    nvidia-smi --query-gpu=memory.used,power.limit,temperature.gpu --format=csv,noheader

Run focused tests while implementing:

    .venv/bin/pytest -q backend/tests/test_comfyui_service.py
    .venv/bin/pytest -q backend/tests/test_timeline_service.py
    .venv/bin/pytest -q backend/tests/test_render_service.py
    .venv/bin/pytest -q backend/tests/test_api.py -k 'timeline or seated or visual'
    npm --prefix frontend test -- --run
    npm --prefix frontend run build

Before every commit:

    .venv/bin/ruff check backend tests scripts
    .venv/bin/pytest -q
    npm --prefix frontend test -- --run
    npm --prefix frontend run build
    docker compose -f compose.yaml config --quiet
    git diff --check

After pushing, inspect the exact head and CI rather than assuming deployment:

    git push origin main
    gh run list --repo B1-Mordred/DialectiCore --branch main --limit 3
    gh run watch RUN_ID --repo B1-Mordred/DialectiCore --exit-status
    test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
    test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | cut -f1)"

## Validation and Acceptance

The character gate passes only when all six new seated masters are independently
linked to the canonical references, visibly compatible in seated scale, within
the chosen eye/shoulder tolerance, sufficiently high resolution for animation,
cleanly matted, and approved through the UI. Representative speaking clips must
show improved mouth and subtle head/expression behavior without identity drift
or obvious blocking at normal 1080p viewing distance. No run may cause CUDA OOM,
new swap-out, unsafe RAM pressure, or bypass B1's lease.

The camera gate passes only when every speaker is the largest face in their
medium shot, their face center lies in the central 45-55 percent of frame width,
their complete head and shoulders are visible, the desk still establishes a
seated setting, and no neighbor is more prominent. The six-speaker comparison
must be visible in the UI.

The B-roll gate passes only when a real video can be added and independently
trimmed on a parallel lane, plays on the rear screen during uninterrupted
dialogue, fills the measured quadrilateral without stretching, is naturally
occluded by foreground characters, and can make a smooth fullscreen round trip
without repeated, skipped, or reset source frames. Missing source/license
metadata must not block any stage.

The integrated gate passes only when a browser user can load the qualification
timeline, inspect and edit all required lanes, save it, render a preview, review
the actual output, and observe no console errors. The full v2 gate additionally
requires all 21 speaking turns, complete dialogue/captions, deterministic QC,
new final artifacts and package, restart persistence, recovery validation,
green CI, exact source provenance, and unchanged v1 hashes.

## Idempotence and Recovery

All v2 media and timeline operations use new asset IDs and versioned metadata.
Never overwrite v1 files or reactivate replaced historical assets. A failed B1
job may be synchronized and retried through the existing managed API only after
its terminal state is known. Qualification runs must be bounded and safe to
repeat with explicit asset IDs and attempt metadata.

Before database-mutating v2 production, create and validate a named pre-v2
backup through the existing authenticated backup API. Keep the already
validated v1 archive. Restore only with API and workers stopped, and validate
with `apply=false` before any real restore. Source rollback uses a normal inverse
commit or explicit patch; never reset the repository destructively. Restart only
the affected DialectiCore user unit. Do not stop or modify unrelated services,
TubeFactory, B1 language runtimes, or DialectiCore dependencies not implicated
by the change.

## Artifacts and Notes

- v1 final render: `storage/object-store/dialecticore/renders/cc1ad449-9cad-4a40-a150-652db0b7dc7a/3a938a75-5b9a-4311-869c-189806673c6f.mp4`.
- v1 final SHA-256: `1837df46318eba0bd0a21dc60c0d97b5c0236423476401058f3bdfd0278b3218`.
- v1 timeline asset: `6299cb36-8fae-4b82-97b4-1e7e5f5c2909`.
- v1 validated recovery archive SHA-256: `d388802ab664dba3edfa75310b2f38ec8ddf6e426d8235f9ce4731f671d404bb`.
- v2 reports and qualification outputs belong under `output/production-v2`; large runtime artifacts remain ignored by Git, while concise schemas and reports required for reproducibility may be committed under `docs` or this plan.

## Interfaces and Dependencies

Timeline update continues through `PUT /api/v1/episodes/{episode_id}/timeline`
using `TimelineUpdateRequest`, with a backward-compatible versioned JSON body.
The new track clip contract must contain stable clip ID, track kind,
`start_ms`, `end_ms`, optional `source_start_ms` and `source_end_ms`, asset ID,
and kind-specific data. B-roll presentation clips reference a B-roll content
clip and contain ordered display-state keyframes. Optional source URL, license,
attribution, evidence, and checksum fields are accepted but not required.

B1 media remains available only through authenticated managed job, upload,
artifact, approval, load, and cancellation routes. The P40 retains B1's sole GPU
lease, 125 W cap, and internal-only runtime. FFmpeg/ffprobe perform probing,
lossless intermediate composition, perspective transforms, transition
rendering, metrics, and final H.264/AAC delivery. The Web UI must preserve its
current authenticated API client and existing episode/timeline compatibility.

Plan update note (2026-08-15 06:22Z): Created the v2 implementation plan from
the user's approved scale, resolution/animation, speaker-framing, and parallel
B-roll requirements plus a live source/runtime audit. The plan deliberately
gates full regeneration behind measured character and integrated prototypes so
the completed v1 remains a safe rollback.

Plan update note (2026-08-15 07:10Z): Recorded the six-character v1 quality
baseline, current B1's lossless RGBA PNG acceptance, removal of the historical
JPEG transport workaround, focused passing tests, and the measured 192x166 face
resolution and blocking spread that the high-resolution prototype must beat.

Plan update note (2026-08-15 09:27Z): Recorded the human review of qualification
revision 1 and the resulting revision 2 corrections. DeepSeek now uses useful
alpha-body scale rather than equal canvas scale, every character overlaps the
foreground desk by 12px, and B-roll presentation transitions use a bounded,
editable duration and segment-local eased clock. Revision 1 remains immutable
and superseded; full episode production remains blocked on review of revision 2.
