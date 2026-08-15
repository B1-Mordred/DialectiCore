# Recover and complete the P40 talk-show media workflow

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

DialectiCore should turn the approved six-character P40 studio panel and the
complete approved transcript into a reproducible, YouTube-worthy talk-show
episode. Wide shots establish the panel, medium shots show the active speaker's
audio-driven mouth motion, listening/reaction coverage prevents a succession of
static talking heads, and source-bound B-roll appears on the rear studio screen
where it helps the discussion. A person must be able to reproduce the same
workflow through the Web UI, inspect a technically passing full preview, and
obtain the final video, subtitles, thumbnail, delivery package, and production
manifest without calling a worker backend directly. The user has delegated
editorial approval decisions to Codex, but deterministic QC, provenance, B1 GPU
scheduling, and publication safety gates remain binding.

## Progress

- [x] (2026-08-14 04:15Z) Audited live services, current episode state, P40 artifacts, and the latest rejected full render.
- [x] (2026-08-14 04:23Z) Queried all 19 DialectiCore-submitted visual jobs directly through the authenticated B1 API: 11 completed and 8 failed.
- [x] (2026-08-14 04:29Z) Reconciled exactly 19 terminal B1 results without resubmission: 11 completed, 8 failed, zero fallbacks, zero running.
- [x] (2026-08-14 04:35Z) Structurally qualified all nine recovered MuseTalk clips and both recovered generation artifacts; Mistral was strong, ChatGPT/Gemini were subtle but real, and all four Claude clips were effectively static.
- [x] (2026-08-14 04:50Z) Added and deployed authenticated declared-region MuseTalk placement plus a measured low-motion audio-mouth fallback; a formerly failed DeepSeek turn completed and the Claude v2 gate passed without the rejected v1 artifact.
- [x] (2026-08-14 05:14Z) Replaced the unusable identical SD1.5 rear-screen outputs with deterministic, transcript-derived PNG cards accepted by B1's managed image ingress.
- [x] (2026-08-14 05:15Z) Completed and synchronized the six affected Claude, DeepSeek, and Grok speaking turns; all passed structural and mouth-motion qualification.
- [x] (2026-08-14 05:36Z) Completed a desk-safe Mistral medium shot without an embedded wall card; its measured mouth-motion score is `5.046848`.
- [x] (2026-08-14 05:47Z) Persisted qualification timeline `90ea9010-abdd-4b40-af92-f967457b5ce4`, an exact contiguous slice of turns 2-5 with Grok, Mistral, Claude, and DeepSeek.
- [x] (2026-08-14 06:00Z) Corrected the render lower-third coordinate bug and produced inspected 50.58-second preview `9e2a615a-fea6-4605-b0fc-fe22c56659a2` at 1280x720.
- [x] (2026-08-14 06:08Z) Preserved the episode pause, marked the preview pending approval, passed 153 focused DialectiCore tests and all 1,621 B1 backend tests, and stopped before a full render.
- [x] (2026-08-14 19:10Z) Re-audited the live API, database, workers, queues, B1 endpoints, and qualification artifact; confirmed zero active media jobs and a healthy managed media route.
- [x] (2026-08-14 19:18Z) Identified that the four-turn qualification artifact is incorrectly labeled `review_scope=full_timeline` and therefore fails selectable-caption and studio-context QC despite being a bounded slice.
- [x] (2026-08-14 19:48Z) Added explicit full/qualification render scope, fixed native seated-panel studio-context accounting, and corrected subtitle QC so trailing audio silence is not mislabeled as timing drift; focused suites pass.
- [x] (2026-08-14 19:50Z) Completed and synchronized all 21 primary speaking visuals, generated a passing 134-cue word-timed VTT, and rebuilt timeline `fc435b43-2f95-4439-8ba3-f6d1a23768fd` with seven bounded physical rear-screen inserts; timeline QC passes.
- [x] (2026-08-14 21:22Z) Rendered and visually reviewed the complete 6:04 episode, approved the current preview, and promoted a technically passing 1920x1080 final render with seven physical rear-screen B-roll inserts.
- [x] (2026-08-14 22:05Z) Produced the current subtitle sidecar, non-black representative thumbnail, YouTube delivery package, compact production manifest, and mock dry-run publication record through persisted workflow APIs.
- [x] (2026-08-15 04:20Z) Replayed the archived episode through the Web UI after restarting API, Web UI, render worker, and workflow worker; all five delivery artifacts were visible and downloadable with zero browser-console errors.
- [x] (2026-08-15 05:16Z) Fixed mutable worker-state backup capture, moved large backup operations off the API event loop, passed 289 relevant tests, and validated a fresh 2.67 GB recovery archive covering 5,455 database records and all 660 selected files.
- [x] (2026-08-15 05:54Z) Established the dedicated canonical DialectiCore repository at `B1-Mordred/DialectiCore`, committed and pushed the source-only production platform, and obtained fully green GitHub CI run `31867941602` on revision `bc889aeb274ac57c5d4b28efb9f607931e22d200` (Compose, frontend tests/build, Ruff, and all 798 backend tests).

## Surprises & Discoveries

- Observation: DialectiCore reports 19 submitted visual jobs, but B1 reports all
  19 as terminal: 11 completed and 8 failed.
  Evidence: authenticated `GET /v1/media/jobs/{job_id}` responses on 2026-08-14.
- Observation: the current canonical broadcast transcript is approved even
  though the older stored run-until-blocked summary still asks for transcript
  approval.
  Evidence: canonical transcript `464f9f0a-246a-42ea-9fdf-4eb76746297a`
  has status `approved` and approval `25387176-3ad6-4e2e-acca-ad5e40c77c52`.
- Observation: the episode is intentionally paused with no current workflow ID,
  so normal stage orchestration does not synchronize the terminal media jobs.
  Evidence: `workflow_control.paused=true`, pause actor
  `codex-seated-revision`, and `current_workflow_id=null`.
- Observation: `/srv/DialectiCore` began as a deployed source workspace without
  Git metadata, while the user intentionally created a separate empty
  `B1-Mordred/DialectiCore` repository and required the older TubeFactory
  project to remain unchanged.
  Evidence: the workspace was initialized independently on `main`; production
  data, secrets, scratch outputs, and model benchmarks are ignored, and no file
  under `/srv/TubeFactory` was modified.
- Observation: selective synchronization preserved the intentional episode
  pause and created no replacement jobs.
  Evidence: the `visual.jobs.synced` audit event records 11 completed, 8 failed,
  0 fallback, 0 running, while `workflow_control.paused=true` and
  `current_workflow_id=null` remain unchanged.
- Observation: terminal success did not imply usable lip motion. All four Claude
  clips measured only `0.001` mean mouth-region frame difference, while Mistral
  measured `0.865-0.887`; ChatGPT/Gemini measured about `0.132`.
  Evidence: declared face-region FFmpeg `tblend=difference,signalstats` analysis
  plus six-frame face contact strips from every recovered clip.
- Observation: sending B1's authenticated declared face rectangle to MuseTalk
  fixes hard `scene_face_not_detected` failures, but non-human faces can still
  produce nearly static lips.
  Evidence: DeepSeek job `job_85187d3801694268b4063a659b550ac7`
  completed with `musetalk_face_region_source=declared_scene_face_region`, but
  measured only `0.013` mean mouth motion.
- Observation: the first procedural mouth prototype produced an unacceptable
  oversized dark bar and was rejected before batch use. The tightened v2 gate
  is visually bounded and raises Claude from `0.001` to `0.081` mean motion.
  Evidence: Claude job `job_cecc8367a4544d8996412d66843f0034`, artifact
  checksum `sha256:8edead5db9ef7b988d778753c7daa20b0e7f85ee326bd381c7a7e7bb682ca0e0`.
- Observation: both older and current SD1.5 B-roll jobs returned the same
  unrelated grey-floor/yellow-pillar image for different transcript prompts;
  five later jobs failed before inference with connection errors.
  Evidence: byte-identical recovered B-roll artifacts and terminal B1 job
  responses. Retrying that model would not provide source-bound coverage.
- Observation: deterministic SVG wall-screen cards rendered correctly locally
  but B1 rejected them as `application/octet-stream` with HTTP 415. Opaque PNG
  rasterization preserves the same design and is accepted by managed ingress.
  Evidence: failed Mistral submission followed by seven completed PNG card
  assets using `dialecticore-wall-screen-card-png/v1`.
- Observation: after an expired idle lease, persisted media jobs remained queued
  until only the B1 control-plane container was restarted; the jobs then resumed
  sequentially under the normal lease with no direct worker intervention.
  Evidence: six-job batch state and B1 lease/runtime observations.
- Observation: the repaired batch's measured mouth-region motion ranges from
  `0.046997` to `0.398691`; Claude's three formerly static turns now measure
  `0.103375-0.112472`. A six-frame contact sheet shows intact panel composition.
  Evidence: local FFmpeg `tblend=difference,signalstats` measurements on the
  exact synchronized MP4s.
- Observation: embedding an opaque rectangular wall-screen card into the native
  panel source covers foreground characters and the desk because B1 does not
  receive a depth or foreground mask for that operation.
  Evidence: rejected Mistral close and medium jobs using the same card both
  placed the rectangle over the seated panel. The clean no-card medium preserves
  the table and all visible panelists.
- Observation: `speaker_close_up` was too aggressive for this scene even when
  the correct face was tracked; it removed the desk and made participants look
  like floating heads. Native `speaker_medium` retains useful facial scale and
  the seated context.
  Evidence: side-by-side extracted frames from the rejected close shots and
  accepted Mistral medium job `job_117c16c834e44e2eb93eff1ba6195cdb`.
- Observation: B1's native camera height used Python rounding at an exact
  half-pixel boundary, reducing one Mistral crop below the 220-pixel provider
  minimum. Flooring to the nearest even height satisfies the declared provider
  contract without changing framing strategy.
  Evidence: new boundary regression in `tests/unit/test_executor.py` and the
  completed Mistral medium job.
- Observation: an idle runtime recover-hook HTTP 503 escaped B1's persistent
  job loop and terminated the runner, leaving submitted jobs dormant until a
  control-plane restart.
  Evidence: control-plane traceback through `unload_expired_idle_runtime`; the
  repair retains model state, records `idle_unload_failed`, releases the lease,
  and passes the complete 1,621-test B1 backend suite.
- Observation: DialectiCore's first qualification render had speaker labels at
  the bottom but black backing rectangles at the top edge. In FFmpeg `drawbox`,
  `h` denotes the box height; input height is `ih`.
  Evidence: changing `y=h-126` to `y=ih-126`, a focused regression test, and
  four representative frames from the corrected preview.
- Observation: B1's native `speaker_medium` output is a tight three-character
  crop with no physical rear screen visible, so compositing a card there would
  create a floating overlay rather than an in-world display.
  Evidence: three-frame contact sheet from completed Gemini asset
  `b02a47ea-78af-4a27-9325-c76f89d9e552`.
- Observation: the approved wide panel plate has a measurable blue rear-screen
  safe region above all six heads. A 50% by 23% insert at normalized position
  `(0.18, 0.19)` remains inside that region at 1280x720.
  Evidence: inspected composite against studio panel asset
  `b18130f9-0ff6-40e7-9d57-0310978c6dc1`.
- Observation: Voicebox word timestamps correctly end on the final spoken word,
  while WAV assets retain 20-681 ms of trailing silence. Comparing the last
  caption end to the full WAV duration falsely failed an otherwise exact VTT.
  Evidence: subtitle QC for asset `762144e5-be53-4445-854d-d224e3351698` and
  the passing regenerated asset `7460ebb8-56df-4d13-8ca7-1e333f77fc4c`.
- Observation: sampling the first frame for the automatic thumbnail selected
  the program fade and produced an almost black JPEG even though the render was
  healthy.
  Evidence: the corrected thumbnail samples the midpoint of the first physical
  rear-screen insert at 103.361 seconds, records mean luma 31, and passes the
  new near-black gate.
- Observation: production manifests recursively embedded earlier production
  manifests, growing API/database payloads from tens of megabytes to hundreds
  of megabytes and a multi-gigabyte SQLite WAL.
  Evidence: current manifest generation excludes manifest assets from the
  ordinary asset list, summarizes history, caps oversized generation metadata,
  and produced current manifest asset `78a47e9a-7c9d-43a0-a08b-6ea19b490a8e`.
- Observation: backup creation hashed live worker-lease JSON and opened it a
  second time while creating the tar archive; a lease rewrite between reads made
  the resulting archive fail its own checksum validation.
  Evidence: the 2026-08-15 04:50Z archive fails on
  `runtime-state/worker-leases/render-worker.json`; the immutable-snapshot
  regression deliberately rewrites the live file and still validates.
- Observation: synchronous compression and restore validation ran directly in
  asynchronous API handlers, making the single API process unresponsive during
  a multi-minute full backup.
  Evidence: after moving these calls to `asyncio.to_thread`, completion
  readiness responded in 1.33 seconds while all 2.67 GB of the replacement
  archive were being validated.

## Decision Log

- Decision: Reconcile before retrying or resuming.
  Rationale: B1 has completed artifacts that DialectiCore has not downloaded;
  resubmission would waste GPU time and could create conflicting revisions.
  Date/Author: 2026-08-14 / Codex.
- Decision: Keep the episode paused while repairing media.
  Rationale: the pause protects the approved manual panel revision from legacy
  automatic visual orchestration and prevents an invalid full render.
  Date/Author: 2026-08-14 / Codex.
- Decision: Use wide panel shots only for establishing/listening coverage and
  require medium or close active-speaker shots for visible lip motion.
  Rationale: the approved six-seat panel has only 25-43 source pixels of face
  height per participant; that is below a useful lip-readability threshold.
  Date/Author: 2026-08-14 / Codex.
- Decision: Do not accept a static fallback for failed speaking turns.
  Rationale: a static card would falsely satisfy completion while violating the
  requested audio-driven character performance.
  Date/Author: 2026-08-14 / Codex.
- Decision: Use the declared-region audio-mouth v2 path only when measured
  MuseTalk mouth motion is below `0.035`; retain native MuseTalk output above
  the threshold and record every fallback in artifact metadata.
  Rationale: the gate preserves good Mistral/ChatGPT/Gemini animation while
  making stylized robotic faces visibly audio-driven without a false success.
  Date/Author: 2026-08-14 / Codex.
- Decision: Replace `wall_screen_broll` generation with deterministic,
  transcript-derived PNG topic cards while leaving other B-roll roles alone.
  Rationale: the configured weak generative model ignored prompt differences;
  source-bound cards are reproducible, readable, render-ready, and valid B1
  scene inputs without inventing unsupported visual claims.
  Date/Author: 2026-08-14 / Codex.
- Decision: Keep source-bound wall-screen PNGs as separate editorial assets and
  do not embed them into lipsync inputs until the compositor has a validated
  rear-screen quad plus foreground/depth mask.
  Rationale: an opaque scene input is reproducible but cannot distinguish the
  rear display from foreground characters, so embedding it destroys the very
  desk composition the workflow must preserve.
  Date/Author: 2026-08-14 / Codex.
- Decision: Use `speaker_medium` as the active-speaker default for this seated
  panel and retire planned close shots during replan.
  Rationale: measured medium shots retain both visible speech and the spatial
  cue that the characters are seated at the table.
  Date/Author: 2026-08-14 / Codex.
- Decision: permit a paused episode to render only an explicitly identified,
  contiguous qualification-preview timeline when the persisted request sets
  `allow_paused_episode=true`; final renders remain blocked.
  Rationale: this enables human review without resuming automatic orchestration
  or weakening the production pause gate.
  Date/Author: 2026-08-14 / Codex.
- Decision: continue past the former human-review stopping point under delegated
  editorial authority, while never converting a failing deterministic QC result
  into an approval.
  Rationale: the user explicitly requested autonomous completion of the whole
  episode and UI-reproducible workflow. Approval can therefore be performed by
  Codex only after the same evidence a human reviewer would require is present.
  Date/Author: 2026-08-14 / Codex.
- Decision: treat the existing four-turn render as a qualification slice, not a
  full-timeline preview.
  Rationale: its timeline contains four contiguous turns and is 50.58 seconds,
  while the episode target is four minutes and the persisted QC expects the
  full program. Honest scope metadata is required before its evidence can be
  used to promote the shot grammar.
  Date/Author: 2026-08-14 / Codex.
- Decision: present rear-screen B-roll as a short wide cutaway between two
  source-timed speaker pieces, rather than overlaying it on a medium shot.
  Rationale: this preserves visible lip-sync before and after the insert, keeps
  the media physically inside the studio screen, and avoids covering faces or
  the desk. The normal timeline API now emits this deterministic three-piece
  grammar for seven source-bound cards.
  Date/Author: 2026-08-14 / Codex.
- Decision: subtitle overrun is a synchronization error; trailing audio silence
  after the final word is not.
  Rationale: extending captions into silence is less accurate, while the prior
  absolute end-time comparison rejected valid word-level timing.
  Date/Author: 2026-08-14 / Codex.
- Decision: choose the thumbnail seek point from representative timeline
  content and reject near-black automatic thumbnails.
  Rationale: a technically valid JPEG from a fade is not a usable YouTube
  thumbnail; the first rear-screen cutaway is deterministic and editorially
  representative.
  Date/Author: 2026-08-14 / Codex.
- Decision: store manifest history as compact summaries and exclude production
  manifests from ordinary asset serialization.
  Rationale: manifests must describe production provenance without recursively
  containing themselves or making normal API reads operationally unsafe.
  Date/Author: 2026-08-14 / Codex.
- Decision: snapshot the small mutable runtime-state tree before computing its
  manifest and archive entries, while continuing to stream large media objects.
  Rationale: the manifest and archive must represent exactly the same lease
  bytes without copying 7.26 GB of media into memory or a second staging tree.
  Date/Author: 2026-08-15 / Codex.
- Decision: execute backup listing, creation, and validation in worker threads
  behind the unchanged authenticated API.
  Rationale: filesystem compression and hashing are blocking work; keeping them
  off the event loop preserves the UI and read APIs without introducing a new
  service or bypass.
  Date/Author: 2026-08-15 / Codex.
- Decision: make `B1-Mordred/DialectiCore` the canonical source repository and
  leave `/srv/TubeFactory` as its existing independent project.
  Rationale: DialectiCore is a rebranded and now independently maintained
  production platform; a source-only repository preserves its implementation
  and CI without importing runtime media, secrets, databases, or rewriting the
  history and working tree of TubeFactory.
  Date/Author: 2026-08-15 / Codex and user.

## Outcomes & Retrospective

The user-visible production objective is now operationally demonstrated by one
complete episode, “KI-Rechenzentren: Strom, Wasser und Wachstum”
(`cc1ad449-9cad-4a40-a150-652db0b7dc7a`). All 21 speaking turns use qualified
audio-driven character media. The current 36-segment timeline contains seven
short source-bound B-roll inserts composited into the physical rear studio
screen between lip-synced speaker pieces. Final render asset
`37fa74da-820c-49c7-96c2-d973dc6efb46` is 364.300 seconds of H.264 1920x1080 at
30 fps with stereo 48 kHz AAC, zero measured A/V offset, and SHA-256
`1837df46318eba0bd0a21dc60c0d97b5c0236423476401058f3bdfd0278b3218`.
Deterministic final QC `b51e55e8-3193-4975-a25b-03469b94cb0a` passes.

The normal workflow also produced a selectable German VTT, representative
thumbnail asset `9615fb6d-e9e4-4de8-b3a1-46aaac2fd6cd`, YouTube package asset
`ece9498a-9f74-4e81-9cea-f555168fe50a`, compact manifest asset
`78a47e9a-7c9d-43a0-a08b-6ea19b490a8e`, and a non-publishing mock dry run. A
browser user can open the archived completed episode, inspect/edit its 36-scene
timeline, see all five delivery artifacts, and download them. Restarting all
four DialectiCore user units preserved the completed state, artifact identifiers,
hashes, and readiness pass. A fresh recovery archive subsequently validated all
5,455 database records, 643 object files, and 17 runtime files.

The episode is YouTube-worthy as a clearly AI-produced pilot: technically strong
and editorially coherent, with visible speech, seated characters, coherent
studio continuity, and useful visual variation. It is not photoreal broadcast
television. The remaining aesthetic ceiling is restrained/repetitive body
language, limited facial nuance, and occasional cutout/depth overlap—not output
resolution, A/V synchronization, or missing workflow stages. The relevant
backend suite passes 289 tests with only the existing Starlette/httpx deprecation
warning. B1's managed media changes remain scheduler-controlled and retain their
previous green canonical CI evidence. DialectiCore now has independent canonical
source provenance at `https://github.com/B1-Mordred/DialectiCore`; the initial
production-platform commit and Python 3.12 compatibility correction are pushed
on `main`, with source-only ignore boundaries and GitHub CI covering Ruff, the
full backend suite, frontend tests/build, and production Compose validation.
Clean-run workspace dependencies discovered by the first CI attempts were
removed from the affected health tests; run `31867941602` is fully green on
revision `bc889aeb274ac57c5d4b28efb9f607931e22d200`.

## Context and Orientation

The working directory is `/srv/DialectiCore`. The development API is a user
service running `backend/app/main.py` against `dialecticore-dev.db`; the workflow
and render workers run `backend/app/workflows/worker_placeholder.py`. Visual
planning, submission, and synchronization live primarily in
`backend/app/services/comfyui_service.py` and the episode routes in
`backend/app/api/routes.py`.

DialectiCore never calls the P40 backends directly. It submits authenticated
jobs to `https://api.ai.b1.germering/v1/media/jobs`. B1 owns the sole GPU lease
and routes `studio-seated-character-p40` and `talking-head-lipsync` to the
internal P40 media runtime. `studio-panel-shot` is a CPU-only B1 runtime.

The target episode is `cc1ad449-9cad-4a40-a150-652db0b7dc7a`. Its approved
studio panel is asset `b18130f9-0ff6-40e7-9d57-0310978c6dc1`. The old rejected
full render is asset `fa33e7ba-8ec1-43f0-abee-e671a653e3d1`; it predates the
P40 panel work and must not be treated as current acceptance evidence.

## Plan of Work

The media-recovery and production milestones are complete. Reconciliation first
recovered all terminal B1 results without duplicate work. Visual qualification
then promoted only correct-speaker clips with measurable audio-driven motion;
declared face regions and a bounded low-motion fallback recovered stylized faces
that MuseTalk could not track reliably. Deterministic, transcript-derived screen
cards replaced a weak image model that ignored prompts.

The production milestone constructed the normal persisted timeline grammar:
speaker medium, optional wide rear-screen insert, then speaker medium. It
generated the full preview, delegated approval, final render, captions,
thumbnail, delivery package, production manifest, and mock publication using API
and UI-visible workflow state rather than an out-of-band FFmpeg artifact.

The lifecycle milestone fixed recursive manifest growth, restarted all services,
replayed the completed episode through the browser, and validated a full recovery
archive. Remaining work is provenance-only: search systemd, deployment metadata,
documentation, and adjacent workspaces for the canonical DialectiCore Git clone.
If it exists, transplant the reviewed patch into that clean repository, run the
same tests, commit, push, watch CI, and confirm deployed/source SHAs. Do not
initialize a new repository in the deployed workspace or claim source provenance
without evidence.

## Concrete Steps

Run validation from `/srv/DialectiCore`:

    curl -fsS http://127.0.0.1:8000/api/v1/episodes/cc1ad449-9cad-4a40-a150-652db0b7dc7a/workflow/completion-readiness
    ffprobe -v error -show_streams -show_format storage/object-store/dialecticore/renders/cc1ad449-9cad-4a40-a150-652db0b7dc7a/3a938a75-5b9a-4311-869c-189806673c6f.mp4
    .venv/bin/pytest -q backend/tests/test_backup_service.py backend/tests/test_render_service.py backend/tests/test_timeline_service.py backend/tests/test_subtitle_service.py backend/tests/test_comfyui_service.py backend/tests/test_api.py

The validated recovery archive is
`dialecticore-backup-20260815T050652Z-completed-youtube-episode-runtime-snapshot-20260815.tar.gz`.
Repeat its non-mutating restore validation through
`POST /api/v1/system/backups/restore` with `apply=false` and all three restore
scopes enabled. A successful response reports 5,455 records, 643 object files,
17 runtime files, and matching checksums for every file.

For browser acceptance, open `http://127.0.0.1:5173`, enable “Show archive,”
select the exact episode UUID above, and verify `COMPLETED`, “Delivered,” a
36-scene editable timeline, passing readiness, and five usable delivery
artifacts. Do not invoke direct B1 worker or FFmpeg generation paths from the UI.

## Validation and Acceptance

Acceptance requires all 21 speaking turns to resolve to completed, qualified
audio-driven visual assets; seven B-roll inserts to appear only inside the rear
screen safe region; the full timeline to be contiguous; and final render QC to
pass current-artifact, caption, studio-context, duration, codec, and A/V offset
checks. The render must be 1920x1080, contain H.264 video and stereo AAC audio,
and be inspectable rather than merely exist on disk.

The Web UI must expose the archived completed episode, its editable timeline,
readiness, preview/final approval state, and all delivery downloads without
console errors. Restarting the API, UI, render worker, and workflow worker must
preserve those identifiers and hashes. Backup acceptance requires a dry-run
restore to verify every selected archive member while normal API reads remain
responsive. Source-control acceptance remains separate and unproven until the
canonical Git repository, pushed commit, CI result, and deployed SHA are known.

## Idempotence and Recovery

B1 media submission is idempotent per DialectiCore asset ID, but reconciliation
must still avoid calling generation routes. SQLite is backed up with its live
WAL state through the SQLite backup API before synchronization. If a service
change fails, restore only the affected source file from its explicit backup or
apply the inverse patch, restart only the relevant DialectiCore or B1 service,
and leave the episode paused. Never restore the whole database over a running
API; stop the DialectiCore API/workers first if database recovery is required.
Full backups first copy the small mutable runtime-state tree into a temporary
snapshot. A vanished ephemeral file is omitted; any other read error aborts the
backup. Restore validation is safe to repeat because `apply=false` changes no
database, media, or runtime file, but records a validation audit event. The old
04:50Z archive is intentionally retained as evidence of the checksum-race defect
and must not be selected for recovery.

## Artifacts and Notes

- P40 qualification: `/srv/DialectiCore/.b1-media-stage.XhEdfH/dialecticore-p40-media-final-config.md`
- Managed-worker plan: `/srv/DialectiCore/.b1-media-stage.XhEdfH/p40-managed-media-worker.md`
- Approved panel: `storage/object-store/dialecticore/visual/cc1ad449-9cad-4a40-a150-652db0b7dc7a/de/studio_panel_keyframe/b18130f9-0ff6-40e7-9d57-0310978c6dc1.png`
- Rejected old render: `storage/object-store/dialecticore/renders/cc1ad449-9cad-4a40-a150-652db0b7dc7a/9325e34b-034b-4781-8905-affad3ac34c9.mp4`
- Pre-reconciliation database backup:
  `storage/backups/dialecticore-dev.pre-p40-media-sync-20260814T0428Z.db`,
  SHA-256 `74bb9d1301f586a95239bbb5db6cdb7f9d8c3743d4fbe8204d46cd18562050a9`.
- Active P40 lipsync image after the accepted v2 gate:
  `b1-ai-hub-lipsync@sha256:ef6f1067fdafb0bd5f23820b84a64d1db1e200055d766666f5885c14b3b5f0d4`.
- Reproducible P40 overlay source:
  `/opt/b1-p40-worker/source/media-lipsync-declared-mouth-v2`.
- Final render:
  `storage/object-store/dialecticore/renders/cc1ad449-9cad-4a40-a150-652db0b7dc7a/3a938a75-5b9a-4311-869c-189806673c6f.mp4`.
- YouTube package:
  `storage/object-store/dialecticore/exports/cc1ad449-9cad-4a40-a150-652db0b7dc7a/5d7a99b4-245d-4a27-8087-ab72c1f682fe.youtube.zip`.
- Current thumbnail:
  `storage/object-store/dialecticore/thumbnails/cc1ad449-9cad-4a40-a150-652db0b7dc7a/afd8fe5b-fc67-4ac3-acb0-d8dc17a78ad2.jpg`.
- Validated full recovery archive:
  `storage/backups/dialecticore-backup-20260815T050652Z-completed-youtube-episode-runtime-snapshot-20260815.tar.gz`,
  SHA-256 `d388802ab664dba3edfa75310b2f38ec8ddf6e426d8235f9ce4731f671d404bb`.

## Interfaces and Dependencies

The work depends on DialectiCore's selective visual generation/synchronization
API, the persisted `asset_records` payload contract, B1's authenticated
`GET /v1/media/jobs/{job_id}` and artifact download routes, the P40 managed
media aliases, local object storage, FFmpeg/ffprobe, and the existing timeline
and render services. No new direct P40 endpoint or independent scheduler is
permitted.

Plan update note (2026-08-14): Initial plan created from the live audit and B1
terminal job inventory so reconciliation precedes repair or regeneration.

Plan update note (2026-08-14 04:29Z): Marked reconciliation complete and
recorded its exact audit counts, unchanged pause state, and database backup.

Plan update note (2026-08-14 04:50Z): Recorded per-speaker motion qualification,
the declared-region detector repair, the rejected v1 mouth prototype, and the
accepted bounded v2 Claude gate. Remaining affected turns still require bounded
regeneration before the preview can be assembled.

Plan update note (2026-08-14 05:15Z): Recorded the B1 runner liveness recovery,
the source-bound PNG wall-screen decision, and terminal qualification of all six
repaired speaking clips. Only the Mistral bridge clip remains before timeline
assembly and preview rendering.

Plan update note (2026-08-14 06:08Z): Recorded the desk-safe Mistral gate,
native-camera rounding repair, managed-runner idle-hook hardening, explicit
paused qualification-preview contract, lower-third coordinate fix, corrected
preview evidence, complete B1 test result, and the intentional stop at pending
human approval.

Plan update note (2026-08-14 07:11Z): Recorded canonical B1 commits and the
fully green CI result after current vulnerability data required digest-pinned
Python 3.12.14 and Nginx 1.30.4 security-base refreshes. DialectiCore remains at
the human preview approval gate.

Plan update note (2026-08-14 19:18Z): Expanded the plan from a bounded preview
recovery into the complete YouTube episode requested by the user, recorded
delegated editorial authority, and made the current qualification-scope/QC
mismatch the next implementation milestone.

Plan update note (2026-08-15 05:20Z): Replaced the stale qualification-only
outcome with the completed 6:04 episode, final delivery artifacts, restart and UI
acceptance, recursive-manifest repair, immutable runtime backup snapshot, live
API responsiveness evidence, and fully validated replacement recovery archive.

Plan update note (2026-08-15 05:39Z): Recorded the user's dedicated canonical
`B1-Mordred/DialectiCore` repository, the deliberate separation from the
unchanged TubeFactory project, source-only repository boundaries, pushed
production commits, and GitHub CI coverage.

Plan update note (2026-08-15 05:54Z): Recorded fully green GitHub CI run
`31867941602` on source revision
`bc889aeb274ac57c5d4b28efb9f607931e22d200`: Compose and frontend passed, and
the clean Python 3.12 backend job passed Ruff plus all 798 tests after its two
workspace-dependent health tests were made self-contained.
