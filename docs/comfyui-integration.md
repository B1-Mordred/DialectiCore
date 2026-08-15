# ComfyUI Integration

ComfyUI is modeled as a remote visual generation service. The current Increment
3 foundation implements persisted endpoint administration, versioned workflow
registry records, visual profiles, mock capability checks, visual asset planning,
remote job submission/sync/cancellation scaffolding, object-storage writes, and
visual QC.

Persisted records:

- `comfyui_endpoint_records` store adapter type, base URL, credential reference,
  timeout, concurrency, retry policy, enabled state, capabilities, and health
  status. The Web UI can create, edit, delete, and health-check these records
  while preserving credential references instead of raw secrets.
- `comfyui_workflow_records` store versioned API workflow templates, workflow
  type, output asset type, default parameters, prompt templates, and the target
  ComfyUI endpoint. The Web UI can create, edit, enable, disable, and delete
  workflow records, including the structured API workflow, prompt template, and
  default parameter JSON used by the adapter.
- `visual_profile_records` store character-level visual direction and workflow
  assignments. The Web UI can create, edit, enable, disable, and delete visual
  profiles with typed portrait, full-body, and wardrobe reference images,
  style, negative prompt, seed, wardrobe, and primary/reaction/B-roll workflow
  bindings. Participant profiles may reference a `visual_profile_id`. Operators
  can upload PNG, JPEG, or WebP character reference images from the Web UI. The
  backend stores them in object storage, updates `reference_images`, keeps one
  portrait and one full-body slot, allows multiple optional wardrobe references,
  keeps the legacy `reference_image_uri` portrait slot, audits uploads with safe
  checksum/size evidence, and exposes
  `GET /api/v1/visual-profiles/{profile_id}/reference-images/{reference_type}/download`
  for provider/operator download of the stored material. For wardrobe, callers
  may add `?uri={stored_object_uri}` to download a specific wardrobe reference;
  without it, the latest wardrobe reference is returned for backwards
  compatibility. Operators can remove a typed reference association with
  `DELETE /api/v1/visual-profiles/{profile_id}/reference-images/{reference_type}`;
  wardrobe removal can likewise target a specific stored URI. This unlinks the
  portrait/full-body/wardrobe association while retaining the stored object for
  audit evidence. Planned primary/reaction/B-roll visual assets copy both
  object-storage URIs and API download URLs into ComfyUI prompt inputs,
  including `wardrobe_reference_images` and
  `wardrobe_reference_image_download_urls` when multiple wardrobe references
  exist.
  Timeline video scenes and render-composition evidence also expose character
  reference material at the segment and visual-layer level.
  The Character Setup page also renders per-character readiness badges for
  model, voice, portrait, and full-body bindings so operators can spot native
  visual setup gaps before starting a pilot run.
- Show-level scene references are separate from character material.
  `POST /api/v1/show-media/scene-reference-image` stores a PNG, JPEG, or WebP
  studio/set reference through object storage and returns the
  `scene_reference_image_uri` that the Web UI writes into the episode media
  definition. Planned B-roll and studio visual prompt inputs then receive that
  show scene URI through `media.scene_reference_image_uri`. Providers and
  operators can download the stored scene material through
  `GET /api/v1/show-media/scene-reference-image/download?uri={scene_reference_image_uri}`;
  the endpoint is constrained to `show-media/scene-reference-images/` objects.

The default scaffold includes a deterministic `mock-comfyui` endpoint plus
talking-head, reaction, B-roll, studio-wide, image-edit, and image-upscale
workflow records. The mock health check reports prompt/history/queue/image/video
capabilities without calling an external service. Default native ComfyUI graph
records now use B1-compatible core node classes where possible:
`CheckpointLoaderSimple`, `EmptyLatentImage`, `KSampler`, `VAEDecode`,
`CreateVideo`, and `SaveVideo` for talking-head, reaction, and studio-wide
video placeholders, and `SaveImage` for B-roll image output. Each default preset
includes explicit node input bindings for positive/negative prompts,
dimensions, seed, sampler settings, computed frame count as image batch size,
and FPS where applicable.

B1 media families are recorded in workflow `default_parameters` so operators can
route or replace workflows without guessing:

| Workflow | B1 preset | Intended use |
| --- | --- | --- |
| `workflow-topic-broll-v1` | `image-default` | SD 1.5 text-to-image B-roll/stills |
| `workflow-image-edit-v1` | `image-edit` | SD 1.5 image edit/inpaint, disabled until selected |
| `workflow-image-upscale-v1` | `image-upscale` | Real-ESRGAN x4plus, disabled until selected |
| `workflow-studio-wide-v1` | `video-text` | Wan 2.1 T2V 1.3B studio/set shots |
| `workflow-talking-head-v1` | `talking-head-lipsync` | MuseTalk audio-driven portrait lip-sync through the B1 managed-media API |
| `workflow-studio-seated-character-p40-v2` | `studio-seated-character-p40` | Scheduler-managed native 1280x720 P40 seated character plate |
| `workflow-studio-panel-shot-v1` | `studio-panel-shot` | CPU-only deterministic panel compositing; no GPU lease |
| `workflow-reaction-v1` | `video-image` | Wan 2.1 VACE 1.3B image/video-conditioned character shots |

When `managed_b1_media_api=true`, DialectiCore submits visual work to the B1 API
hub instead of the native ComfyUI `/prompt` compatibility route. The adapter
posts `modality`, `operation`, `model`, and normalized visual input metadata to
`POST https://api.ai.b1.germering/v1/media/jobs` with an idempotency key derived
from the DialectiCore asset ID. The submitted asset stores the B1 job ID,
managed API base, redacted request payload, and prompt context for audit. Sync
uses `GET /v1/media/jobs/{job_id}` and downloads completed `/artifacts/...`
results back into DialectiCore object storage with the same bearer credential
and B1 CA verification. Native `/prompt` remains available for workflows without
the managed flag and for normal external ComfyUI clients.

The Web UI also includes a B1 Native ComfyUI endpoint preset for
`https://comfy.ai.b1.germering`. It stores `credential_reference=env:B1_API_KEY`
and B1 native-route capability metadata without storing the bearer token.
Endpoint health probes `/object_info` with bearer authentication and the
configured B1 CA certificate, then performs a non-rendering `/prompt` admission
probe with an empty prompt. The health record stores safe readiness evidence for
object metadata, scheduler-aware native routes, WebSocket support, credential
resolution, CA file availability, and prompt admission. When
`remote_nodes_api_base` is configured, the same health check also probes
`GET https://api.ai.b1.germering/v1/models` without starting a render. It records
`managed_media_required_presets`, `managed_media_available_presets`,
`managed_media_missing_presets`, `managed_media_model_count`, and
`managed_media_catalog_ready` so the pilot readiness gate can fail native visual
production if a selected managed workflow asks for a B1 alias that the API hub
does not currently advertise. A native endpoint can therefore report readable
node metadata while still being unhealthy for production if `/prompt` is blocked
by write-scope admission or B1 hardware resource policy, or if required managed
media aliases are missing from the API hub catalog. The CA bootstrap action
downloads the public B1 root certificate from
`https://comfy.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt` without endpoint
authorization, verifies the configured SHA-256 when present, and stores the
certificate under runtime-state certificates for read-only app roots.
If the admission probe reports `hardware_resource_policy`, operators should
free or restart the conflicting workload on the B1 appliance and rerun endpoint
health. DialectiCore only records and surfaces the admission failure; the native
gateway can reject direct ComfyUI memory-clear compatibility routes such as
`/free`, so automated local cleanup is not assumed.

Catalog readiness is intentionally not treated as proof that the B1 GPU runner
can complete media jobs. The production test report includes
`media_readiness.managed_media_execution`, derived from actual episode visual
assets submitted through the managed B1 media adapter. It reports whether
managed media was required, completed artifact counts, submitted/running counts,
failed counts, fallback counts, model/operation aliases, and compact failure
samples such as `gpu_runner_error`/`ValueError` without storing tracebacks or
raw provider bodies in the report summary. A configured managed workflow with
no managed assets is reported as `not_attempted`, not as ready. This lets
operators distinguish "B1 advertises `image-default`/`video-image`" from "B1
produced downloadable artifacts for this talkshow."
The companion `media_readiness.managed_media_operator_action` gives the next
operator step, such as running the B1 managed-media smoke, syncing in-flight
managed jobs, fixing the B1 runner before retrying visuals, or confirming that
managed media execution is ready.

Remote clients can use the appliance in two distinct ways:

- Native ComfyUI clients should use `https://comfy.ai.b1.germering` with
  `Authorization: Bearer <B1 API key>`. The preserved native routes include
  `/object_info`, `/models`, `/system_stats`, `/prompt`, `/history`, `/queue`,
  `/interrupt`, `/upload/image`, `/upload/mask`, `/view`, and `/ws`. Read-only
  clients need `jobs:read`; prompt submission, queue mutation, interrupt, and
  upload routes also need `jobs:write`. Native prompt submissions are
  scheduler-aware and return the real ComfyUI `prompt_id` after the shared GPU
  lease is acquired.
- External ComfyUI instances that should call B1-hosted LLM, image, video, TTS,
  STT, or embedding models should install
  `/home/mordred/localAIcentre/integrations/comfyui-b1-remote-nodes` into that
  instance's `custom_nodes/` directory and configure
  `B1_AI_HUB_API_BASE=https://api.ai.b1.germering`,
  `B1_AI_HUB_CA_FILE`, `B1_AI_HUB_API_KEY_FILE`, and
  `B1_AI_HUB_DOWNLOAD_DIR`. Those nodes use the B1 API hub endpoint, not the
  native ComfyUI endpoint.

Legacy unauthenticated clients fixed to `http://host:8188` require the separate
legacy Comfy profile and a narrow `B1_LEGACY_COMFY_ALLOW_CIDRS` allowlist. That
path still routes through the scheduler-aware proxy rather than directly to
ComfyUI.

`POST /api/v1/episodes/{episode_id}/visual-assets/plan` creates planned visual
asset placeholders for the selected canonical or localized transcript. It adds
one primary video asset for every playable transcript turn, optional B-roll
assets when the episode media settings request B-roll, one reusable studio scene
asset per episode/language, and one reusable reaction/listening loop per active
participant when the visual profile has an enabled reaction workflow. Planned
turn assets carry shot-plan metadata that references reusable reaction/studio
assets, optional B-roll, transition type, subtitle style, and citation-overlay
requirements. When cited transcript turns exist and citation cards are enabled,
the planner also creates `citation_card` overlay assets linked to the transcript
turn, evidence refs, claim text, and resolved source metadata. Planned remote
assets carry ComfyUI endpoint/workflow IDs, visual profile IDs, shot role,
prompt inputs, expected character/style snapshots, seed/reference/style
metadata, and a fallback policy for later execution.

`visual_asset_plan_completeness` QC verifies that every playable transcript turn
has a participant visual profile, an enabled primary workflow, and an active
primary visual asset. It also verifies that cited turns have planned citation
overlay cards when the episode media policy enables them. When reaction or
studio-wide workflows are enabled, missing reusable reaction loops or the
reusable studio scene fail the plan QC because downstream timeline composition
uses those assets for listening cutaways and wide-shot coverage. QC reports reusable
reaction loop, studio scene, shot-planned turn, and citation-card counts,
warning when reusable or shot-plan coverage is missing.

`POST /api/v1/episodes/{episode_id}/visual-assets/generate` submits planned
visual assets to the configured workflow. Remote ComfyUI endpoints receive a
normalized `/prompt` request containing the workflow API JSON plus asset,
transcript-turn, visual-role, shot-type, and prompt-input metadata. Returned
`prompt_id` or `job_id` values are stored as `remote_job_id`.
Citation overlay cards are generated locally as deterministic SVG assets because
they are source-bound production graphics, not model-generated imagery. They are
stored through the same object-storage path as remote results and include SVG
probe metadata, checksums, evidence refs, source titles/URIs, and static-image
duration metadata.

`POST /api/v1/episodes/{episode_id}/visuals/produce` is the producer shortcut
for the same path after transcript approval and per-turn audio completion. It
plans missing visual assets, generates or submits them through each character's
configured visual profile/workflow, and immediately records visual media QC. The
explicit plan/generate/sync/QC routes remain available for selective retry, job
recovery, and operator review, but explicit transcript IDs must still refer to
approved transcripts before visual production can start.

Before submission, the API workflow JSON is deep-copied and patched from
resolved prompt inputs. Workflow records may define explicit
`prompt_template.node_input_bindings` or `default_parameters.node_input_bindings`
using paths such as `6.inputs.text: positive_prompt` and
`9.inputs.seed: seed`. Seeded video presets additionally bind sampler controls,
motion bucket, camera motion, lighting preset, FPS, and computed frame count;
the B-roll preset binds shot style, composition, and lighting preset metadata.
When explicit bindings are absent, common ComfyUI input names such as `text`,
`prompt`, `positive`, `negative`, `width`, `height`, `fps`, and `seed` are
patched conservatively. Common character-reference names are also recognized:
`reference_image_uri`, `character_reference_image`,
`portrait_reference_image_uri`, `full_body_reference_image_uri`,
`wardrobe_reference_image_uri`, and `show_scene_reference_image_uri` receive the
corresponding visual-profile or show-scene object-storage URI when present.
Workflow inputs named `reference_image_download_url`,
`portrait_reference_image_download_url`,
`full_body_reference_image_download_url`, or
`wardrobe_reference_image_download_url` receive the matching character API
download path when the reference exists. Inputs named
`show_scene_reference_image_download_url` or
`show_scene_reference_download_url` receive the matching show-scene API download
path. The asset metadata records `resolved_prompt_inputs` and
`workflow_patch_bindings` for auditability.

`POST /api/v1/episodes/{episode_id}/visual-assets/sync` polls submitted or
running jobs through `job_status_path_template` or `history_path_template`,
defaulting to `/history/{job_id}`. Provider payloads can return
`video_base64`, `image_base64`, `media_base64`, `result_base64`, direct storage
URIs, or same-origin result URLs. Returned bytes are written to the configured
object store and recorded with storage backend, object key, checksum, size, and
path/cache evidence.

Stored visual outputs are probed when possible. PNG and JPEG dimensions are read
from file headers without extra dependencies. Deterministic SVG fallback cards
are parsed for dimensions from `width`/`height` or `viewBox`. Video assets are
probed with `ffprobe` when available. Probe evidence is recorded in
`generation_metadata.media_probe`, including MIME type, width, height, duration,
FPS, frame count, byte size, probe tool, warnings, render-readiness, and
`video_analysis` metadata for codec, pixel format, container/stream bitrate,
exact or estimated frame-count source, and critical warnings.

Visual generation and sync requests default `fallback_on_failure` to `true`.
When a remote submission fails or a provider reports a failed job, the service
stores a deterministic SVG citation card or fallback still in object storage.
Fallback assets preserve the original source turn, duration, workflow metadata,
provider failure metadata when present, and `fallback_visual` audit metadata.
QC records fallback use as a warning while keeping the asset render-suitable for
later timeline composition.

`POST /api/v1/episodes/{episode_id}/visual-assets/qc` reruns visual media
integrity checks without resubmitting jobs. It records completion, storage,
checksum, probe, render-suitability, dimension, PNG pixel evidence, SVG
structural evidence, video probe integrity, FPS, duration, lip-sync readiness,
measured lip-sync offset, character identity/style consistency against the
plan-time profile snapshot, workflow/endpoint reference, placeholder, and
fallback evidence in `visual_media_integrity` QC plus video-probe,
citation-card checked/render-suitable counts and `visual.qc.completed` audit
events.

`POST /api/v1/episodes/{episode_id}/visual-assets/cancel` cancels submitted or
running remote jobs through `job_cancel_path_template` or
`cancellation_path_template`, defaulting to `DELETE /queue/{job_id}`. The default
API behavior resets cancelled assets to `planned` so they can be retried. Remote
cancellation responses are recursively redacted before they are persisted in
asset metadata.

The deterministic mock ComfyUI adapter stores SVG still assets with
`deterministic_mock_visual`, `render_ready`, and static-duration metadata. This
lets the platform verify job metadata, object storage, retries, audit events,
worker sync, visual QC, timeline construction, and render handoff without
bundling a local ComfyUI server. Non-renderable remote placeholders still set
`render_ready: false` and produce warning QC when they appear.

Remaining Increment 3 work:

- No known Increment 3 implementation gaps remain in the current plan.
- Next work continues Increment 4 rendering, layout, thumbnail, publishing, and
  production hardening.
