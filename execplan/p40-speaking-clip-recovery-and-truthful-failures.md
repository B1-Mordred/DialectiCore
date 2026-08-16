# Recover P40 speaking clips and preserve truthful provider failures

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

The current episode must be able to produce its 21 seated speaking clips through
B1's scheduler-managed P40 lane. A transient idle CUDA footprint must not make the
managed recovery hook reject an otherwise free GPU, and DialectiCore must show the
real B1 failure whenever a native directed video fails. The observable acceptance
gate is one successfully submitted, completed, synchronized, and structurally
qualified speaking clip; only then may the other 20 failed clips be retried.

## Progress

- [x] (2026-08-16 00:20Z) Diagnosed 31 B1 attempts failing at the `lan-p40-media` recovery hook and 21 current DialectiCore speaking assets showing a masked coverage error.
- [x] (2026-08-16 00:23Z) Confirmed the P40 is idle at about 233 MiB VRAM and traced the manager's brittle 256 MiB recovery threshold.
- [x] (2026-08-16 00:24Z) Added deterministic manager recovery stability/margin behavior; all seven manager tests pass.
- [x] (2026-08-16 00:27Z) Preserved bounded structured recovery evidence in B1; all 1,630 unit tests and complete local quality gates pass.
- [x] (2026-08-16 00:24Z) Prevented DialectiCore fallback conversion for native directed visuals; all 56 ComfyUI service tests and Ruff pass.
- [x] (2026-08-16 00:24Z) Deployed only media-manager v0.1.3 and proved authenticated recovery HTTP 200 with three stable 233 MiB samples.
- [x] (2026-08-16 00:31Z) Deployed B1 source commit `f73dc16` as control-plane image `sha256:68ed6a4389fbe7682933cf0157454cf0aa461cb2c3a3e4c9c10254326c0f02c7` after the existing lease expired.
- [x] (2026-08-16 00:37Z) Retried and qualified exactly one current speaking asset end to end as B1 job `job_7851d2d303d54fae83c25047d7df535f`.
- [x] (2026-08-16 01:09Z) Retried the remaining 20 current speaking assets only after the single-clip gate passed; all 20 completed serially with zero failures.
- [x] (2026-08-16 01:15Z) Audited all 21 current clips, proved idle recovery, removed the temporary admission override, and restored the normal B1 control-plane policy.
- [x] (2026-08-16 01:39Z) Committed and pushed both evidence updates, observed both CI workflows complete successfully, and verified local/remote commit equality.

## Surprises & Discoveries

- Observation: the visible coverage failure is secondary, not the provider failure.
  Evidence: B1 PostgreSQL records report `gpu_runner_error` with `lan-p40-media runtime recover hook returned HTTP 503`, while DialectiCore's sync fallback converts the failed result into a completed local fallback before native coverage validation rejects it.
- Observation: the nominal idle limit is only 23 MiB above the measured steady P40 footprint.
  Evidence: the manager uses 256 MiB and the unloaded host reports approximately 233 MiB.
- Observation: the first authenticated recovery with the new manager completed in about three seconds without loading either backend.
  Evidence: HTTP 200 reported samples `[233,233,233]`, zero loaded ComfyUI models, and a null resident MuseTalk model.
- Observation: B1's 60-job hourly owner limit correctly blocked the first gate submission after the failed retry burst.
  Evidence: admission returned `owner_rate_limit`, current 60, before creating GPU work. A temporary database policy override raises only the hourly ceiling to 100 for this bounded 21-job recovery; queue and active limits remain 20 and 3, and the override will be removed afterward.
- Observation: the long-running MuseTalk container had lost CUDA access independently of the manager fix.
  Evidence: its first admitted gate failed with `slow_conv2d_cpu not implemented for Half`; in-container `torch.cuda.is_available()` was false and NVML returned `Unknown Error`. Recreating only `media-lipsync` restored one Tesla P40 device and the next gate succeeded.
- Observation: truthful new provider results can inherit an obsolete native-camera rejection flag from a previously masked result.
  Evidence: the gate's real `lipsync_runtime_error` retained `native_camera_coverage_rejected=true` until `_apply_visual_result` was taught to clear the stale flag; the regression and all 56 ComfyUI service tests pass.

## Decision Log

- Decision: keep B1 as the sole GPU scheduler and change only its existing managed runtime hooks.
  Rationale: no direct backend exposure, independent service, or scheduler bypass is needed.
  Date/Author: 2026-08-16 / Codex.
- Decision: use a bounded baseline margin plus consecutive stable samples instead of a single exact VRAM sample.
  Rationale: a persistent model allocation remains far above the idle envelope, while small driver and CUDA-context fluctuations should not cause false recovery failures.
  Date/Author: 2026-08-16 / Codex.
- Decision: gate the batch retry on one representative clip.
  Rationale: this limits duplicate work and proves recovery, B1 execution, synchronization, media materialization, and native camera evidence together.
  Date/Author: 2026-08-16 / Codex.

## Outcomes & Retrospective

The recovery is complete. Manager image
`b1-ai-hub/p40-media-manager:v0.1.3@sha256:bec3fb35922b8e41aa81e986380d9ae95ada8b690e2a7733b7a4f651f5451baa`
accepts the measured idle state only after three stable samples, and B1 control
plane source commit `f73dc16db43da6e360448773a424d8e270bf2322`
preserves bounded failure evidence. DialectiCore commits `12e0db4` and `a4eba9b`
keep native provider failures truthful and clear obsolete camera rejection state.

The gate plus all 20 serial recovery jobs completed. DialectiCore now has exactly
21 current `video_primary` assets, all completed and none with stale camera
rejection metadata. A filesystem and ffprobe audit passed 21/21: SHA-256 matched,
all clips are H.264/AAC at 1024x576 and 12 fps, duration matches the asset record,
and all carry native scene-camera, intended-speaker, preserved-rear-screen, and
audio-driven seated lip-sync evidence. The camera distribution is one
establishing wide, five panel two-shots, and 15 speaker-medium shots. Face heights
are 111 pixels for two-shots and 140-141 pixels for medium shots.

Post-batch authenticated recovery returned HTTP 200 with VRAM samples
`[233,233,233]`, no loaded ComfyUI models, and no resident MuseTalk model. GPU
utilization was 0% and the 125 W cap remained applied. The temporary owner hourly
limit used to move past the earlier failed-attempt burst was deleted; the B1
control plane was then recreated alone and returned healthy, restoring its normal
environment-backed 60-job default.

## Context and Orientation

DialectiCore's managed visual adapter is
`backend/app/services/comfyui_service.py`. `sync_visual_results` polls B1 and may
produce local fallbacks; native seated-panel videos are required to retain real
provider results and native camera evidence. The P40 manager is
`/opt/b1-p40-worker/deploy/media-manager/media_manager.py` and is reached only by
B1 over the internal worker network. B1's lifecycle client is
`/opt/b1-ai-hub-source/services/control-plane/app/executor.py`.

The live episode is `9d145344-82c9-46cc-b4c1-661d95f0bf56`. It has an approved,
Claude-height-normalized panel keyframe and seven ready rear-screen assets. Its 21
current `video_primary` assets are failed and retryable.

## Plan of Work

First add unit tests and the smallest implementation changes at each boundary.
The media manager will require several consecutive samples below a measured idle
envelope composed of its existing baseline plus a new bounded margin, and will
return the sampled VRAM evidence. B1 will include a bounded, sanitized response
summary when an authenticated runtime hook returns an error. DialectiCore will
apply fallback-on-failure only to visuals that are not native directed assets.

Build and deploy only the P40 media manager and, if required for detailed B1
evidence, only the B1 control plane. Call the authenticated recovery hook through
the managed path and verify a successful idle result without affecting unrelated
models. Select one failed speaking asset, use the existing retry API, and observe
the public B1 job through terminal completion and automatic DialectiCore sync.
Inspect its MP4, metadata, native panel coverage, audio, and mouth motion. Only on
success, selectively retry the other 20 failed current assets.

## Concrete Steps

From `/srv/DialectiCore`, `/opt/b1-ai-hub-source`, and `/opt/b1-p40-worker` run the
focused pytest/unittest commands for the edited files. Build immutable Docker
images, capture their digests, update the inspectable Compose configuration, and
force-recreate only the affected service. Use the existing authenticated B1 and
DialectiCore APIs for recovery and retry; never call ComfyUI or MuseTalk directly.

## Validation and Acceptance

Acceptance requires a manager unit test proving transient above-baseline samples
settle successfully and a persistent allocation fails with detailed samples; a B1
test proving an HTTP 503 response carries bounded hook evidence; and a DialectiCore
test proving a failed native seated-panel result remains failed with the original
provider category/message. Live acceptance requires HTTP 200 recovery at idle,
one completed speaking clip with native `studio_panel` evidence and valid A/V, then
all remaining retries submitted without duplicate active jobs. GPU scheduling,
internal networking, authentication, and unrelated services must remain unchanged.

## Idempotence and Recovery

Unit tests and idle recovery are repeatable. Recreating only `media-manager` does
not stop ComfyUI or MuseTalk. The previous image digest and threshold remain the
rollback target. Failed DialectiCore assets must be retried only through the
existing cancellation/retry-aware service path; never rewrite their remote job IDs
manually. If the single clip fails, stop before the batch and preserve its exact B1
and local evidence.

## Artifacts and Notes

Initial evidence: idle P40 usage approximately 233 MiB; current threshold 256 MiB;
31 failed B1 attempts; 21 current failed `video_primary` assets. The approved panel
checksum is `7bbcfb229523c65757b837beefcb6f5cbe81ffda156190ce062ae33bdc300f95`.

P40 manager v0.1.3 image digest is
`sha256:bec3fb35922b8e41aa81e986380d9ae95ada8b690e2a7733b7a4f651f5451baa`.
B1 evidence source commit is `f73dc161e545bdd4e2bf65137a2b1e625306375d`.

The single-clip gate is DialectiCore asset
`344e4d35-770b-4c7a-a66b-6ec3e3a5315f`, B1 job
`job_7851d2d303d54fae83c25047d7df535f`, and SHA-256
`f45c0ff496a2be5344ae8a397647354017888cf5c4b0a66b6190d3dcb2f87bca`.
It is H.264/AAC, 1024x576, 12 fps, 8.75 seconds, with 105 unique decoded
frames. Its mouth crop changed above a 0.1 luma-difference threshold in 103 of
104 transitions. B1 recorded a 141-pixel speaker face, native speaker-medium
composition, ChatGPT as the framed participant, and preserved rear screen.

## Interfaces and Dependencies

No new port, credential, model, GPU runtime, dependency, or database migration is
introduced. Existing interfaces are the manager's authenticated
`/b1/runtime/recover`, B1's job executor and public media API, and DialectiCore's
managed visual retry/sync APIs.

Plan update note (2026-08-16): created from live B1, P40, and DialectiCore evidence
before implementation; batch retry is explicitly gated by one qualified clip.

Plan update note (2026-08-16 00:29Z): recorded passing source gates and live manager
recovery; B1 control-plane activation waits for an existing scheduler lease to
expire rather than disrupting its owner.

Plan update note (2026-08-16 00:39Z): recorded the bounded admission override,
stale CUDA container recovery, successful single-clip gate, stale-metadata cleanup,
and the admitted serial batch.

Plan update note (2026-08-16 01:15Z): recorded 20/20 batch completion, the 21/21
media and metadata audit, stable idle recovery, and removal of the temporary
admission override.

Plan update note (2026-08-16 01:39Z): DialectiCore CI run `31919203276` and B1
CI run `31919202106` completed successfully. DialectiCore commit `2663d45` and B1
commit `0f48da2` matched their respective remote branch heads before this final
plan-only closure update.
