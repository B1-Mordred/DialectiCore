# Timeline and Rendering

Increment 4 now has the first `EpisodeTimeline` JSON path. The timeline builder
turns a transcript plus completed dialogue audio, primary visual assets, optional
B-roll/reaction/studio media, fallback visuals, and subtitle assets into a
versioned timeline asset stored through the configured object store.

Current timeline assets use MIME type
`application/vnd.dialecticore.timeline+json` and are stored as normal
`AssetType.timeline` records. The JSON includes:

- `schema_version: episode_timeline.v1`
- media dimensions, frame rate, aspect ratio, and subtitle mode
- tracks for `video_primary`, `video_secondary`, `audio_dialogue`,
  `audio_music`, `audio_effects`, `captions`, `graphics`, `citations`, and
  `chapters`
- one segment per playable transcript turn with source turn ID, speaker ID,
  start/end timing, audio asset ID, primary video asset ID, secondary B-roll
  asset ID, reaction visual asset ID, studio scene asset ID, fallback video
  asset ID, ordered `visual_layers`, subtitle asset ID, camera transition,
  deterministic lower-third graphics, citation references, and citation overlay
  asset IDs when generated
- checksum/status/storage fingerprints for linked audio, primary visual,
  B-roll, reaction-loop, studio-scene, fallback, subtitle, and citation overlay
  assets
- editable metadata with `editable: true` and an `edit_version`

`POST /api/v1/episodes/{episode_id}/timeline/build` builds a timeline for the
target approved transcript and writes a checksummed timeline asset. Direct
timeline-build requests with an explicit transcript ID still require that
transcript to be approved, so pending transcript-review drafts cannot enter the
render timeline. `GET
/api/v1/episodes/{episode_id}/timeline` returns the latest active timeline asset
and JSON payload plus a typed `timeline_entity` view containing the required
entity fields: `id`, `episode_id`, `language`, `version`, `status`,
`duration_ms`, `timeline_json`, and `created_at`. The same entity view is stored
in timeline asset metadata. `PUT /api/v1/episodes/{episode_id}/timeline`
persists an edited timeline as a new active asset and marks the previous
timeline asset as `replaced`, so edits do not require regenerating the
discussion.
Manual replacement of a failed or corrected non-timeline asset is available
through `POST /api/v1/episodes/{episode_id}/assets/{asset_id}/replace`. The
replacement keeps the original source entity link, marks the old asset as
`replaced`, records `manual_asset_replacement.v1` metadata, and rewrites active
timeline asset IDs that referenced the old asset so later renders use the
operator-provided media without rebuilding the discussion.

The Web UI now includes a scene-based timeline editor for the latest completed
timeline asset. Editors can select timeline scenes, adjust `start_ms`/`end_ms`,
change the camera transition, and choose the scene visual role. Saving writes a
new timeline asset through `PUT /api/v1/episodes/{episode_id}/timeline`, bumps
`edit_version`, normalizes each scene `duration_ms` from the edited timing, and
records timeline edit audit/QC evidence.

Timeline integrity QC records `timeline_integrity` results with segment,
playable-turn, chapter, duration, missing audio, missing primary video, missing
fallback video, subtitle-linked, timing-gap, failure, warning, and issue counts.
It also reports how many cited segments exist and how many have linked citation
overlay assets. Segment media fingerprints are compared with current asset
records so replaced, missing, mutated, or no-longer-render-ready media fail with
`timeline_stale_media_fingerprint` before rendering or completion can reuse a
stale timeline. When a primary visual asset carries a `shot_plan` with reusable
reaction-loop or studio-scene asset IDs, timeline QC requires the segment to
link those exact completed render-ready assets. Missing or still-planned
shot-planned reusable character/studio media is a failure, because the talk show
timeline would otherwise silently lose the reusable cast animation or show set
that was planned for render.
Missing dialogue audio is a failure because rendering cannot produce a complete
dialogue track. Missing primary video is a warning when a fallback video asset is
available, otherwise it is a failure. Missing subtitles are warnings.

Render presets are exposed at `GET /api/v1/render-presets`. The initial presets
are YouTube 1080p, YouTube 1440p, YouTube 4K, preview low-bitrate, audio-only,
and short promotional clip. Each preset declares output resolution, frame rate,
video bitrate, audio bitrate, 48 kHz audio normalization target, pixel format,
container, codec, and render scope.

`POST /api/v1/episodes/{episode_id}/renders` creates FFmpeg-backed preview or
final render artifacts from an active timeline. Preview jobs use
`timeline_scene_composite_preview` and cap the scene render at 30 seconds for
fast review. Final jobs use `timeline_scene_composite_final` and render the full
timeline duration. The renderer builds a segment-scoped FFmpeg composition from
timeline timing, lower-third graphics, source asset links, visual layer
metadata, and citation overlay links. It normalizes resolved image/video visual
assets into timeline-ordered scene plates. When a segment provides multiple
renderable visual layers, FFmpeg composes them through a deterministic
role-aware layout policy: studio scene as the base when available, primary
talking-head media as the main focal region, B-roll as a contextual panel, and
participant reaction media as either split-screen focus or secondary reaction
context. Layout policies now distinguish basic full-frame shots from advanced
focus-with-context and split-screen compositions, record focus roles and safe
area rules, and feed those slot choices into the visual plate overlay geometry.
Timeline `camera_transition` values are resolved into named transition
policies, cross-scene flags, motion primitive names, layer animation cues, and
simple per-scene fade-in effects for non-cut transitions. Adjacent scene plates
with cross-scene transition policies are composed through FFmpeg `xfade`
boundaries; incoming plates are extended by the transition duration so the final
visual plate keeps the timeline duration. Overlay layers with rendered motion
policies use FFmpeg per-frame `overlay`, `scale`, and alpha `fade` expressions,
so B-roll slides and reaction/focus overlays can move, scale, and fade from
their entry states into the resolved layout slot rather than appearing at a
static coordinate. `source_reveal` transitions now add a B-roll arc-reveal path
with an `ease_in_out` curve and diamond alpha mask, while
`speaker_spotlight` transitions add a primary-speaker bounce path with an
`ease_out_back` curve and circular alpha mask. Non-full-frame visual layers
continue to carry deterministic rounded-rectangle alpha masks when no explicit
geometric transition mask is selected, and the FFmpeg overlay stream applies
those masks before compositing the layer.
If no usable source media is available, the renderer uses generated fallback
scene backgrounds while preserving source references in the manifest.
It assembles a 48 kHz dialogue track in timeline order from resolved
per-segment audio assets, inserting generated silence only for timing gaps or
segments whose audio object is unavailable to the renderer. It also parses
linked subtitle asset cues from stored VTT/SRT text or embedded subtitle
metadata and burns matching caption text into each scene during the cue timing
window. When a segment has citation overlay asset IDs, the rendered video
contains an evidence overlay card region during that segment and the render
manifest records how many citation overlays were planned, resolved from object
storage, and composited.
When normalized visual source media is not locally available to the renderer,
it uses deterministic generated scene backgrounds while preserving the source
asset references in the manifest. When timeline segments carry claim citations,
the manifest also includes
`evidence_lineage` with the active evidence-pack asset ID/checksum, referenced
source IDs, source metadata, citation-to-segment links, unresolved evidence
refs, and retrieval tool-log summary.
The render asset stores:

- the MP4 object URI and checksum
- a separate render manifest object URI and checksum
- a checksum-bound snapshot of every source timeline, evidence, audio, visual,
  subtitle, fallback, and citation-overlay asset used by the render, including
  source entity links, status, storage URI, media metadata, checksum, and
  render-readiness state
- FFprobe media evidence, including duration, dimensions, FPS, video/audio
  codecs, audio sample rate, channels, and byte size
- render type, preset ID, timeline asset ID, source asset count, and
  normalization targets
- scene composition evidence, including segment count, resolved media counts,
  resolved visual plate counts, composited visual overlay layer counts,
  generated visual fallback counts, layout policy names, transition policy
  names, animated scene counts, motion primitive names/counts, advanced layout
  counts, split-screen/focus-shift/cross-scene counts, rendered xfade counts,
  cross-scene renderer mode, rendered layer transform names/counts, rendered
  scale/opacity keyframe counts, rendered easing curve names/counts, rendered
  mask names/counts, non-rectangular mask counts, layer mask renderer mode,
  layer motion renderer mode,
  per-layer layout slots, per-layer animation cues, dialogue audio layer counts,
  silent fallback counts, subtitle track counts, burned-in caption cue counts,
  citation overlay counts, and composited overlay counts

Render QC records `render_preview_integrity` for preview jobs and
`render_final_integrity` for final jobs. It validates storage and checksum
presence, FFprobe availability, expected dimensions, FPS, duration, and 48 kHz
audio sample rate. Final render QC also records the episode target/min/max
runtime in milliseconds and fails when the measured final render duration falls
outside the configured episode duration bounds. If timeline citations exist,
render QC also validates that the manifest resolves those evidence refs to the
active evidence pack. If
timeline citation overlay assets exist, render QC verifies that the composition
plan includes them as composited overlays. Linked subtitle tracks with no
parsable burn-in cues are reported as render QC warnings. The API also exposes `GET
/api/v1/episodes/{episode_id}/renders` to list render assets for an episode.
Render QC compares the render manifest's source-asset snapshots with the
episode's current asset records. Missing source assets fail as
`render_source_asset_missing`; changed source entity links, status, storage,
media metadata, checksums, or render-readiness state fail as
`render_source_asset_stale`, so preview/final approval and completion cannot
reuse a render whose inputs have drifted after rendering.
Completion readiness surfaces those render-source failures as
`preview_render_source_assets_stale` or `final_render_source_assets_stale`.
Every completed preview render creates a targeted pending
`preview_render_review` approval and records `approval.required` audit evidence.
Final rendering is gated on an approved preview for the same timeline unless an
API caller explicitly sets `allow_unapproved_preview=true` for a nonstandard
operator path. Every completed final render also creates a targeted pending
`final_render_review` approval and records `approval.required` audit evidence.
Approving either record requires the latest matching render QC row to exist and
to be non-failing, then marks the render metadata as approved. Rejecting either
record marks the render asset as rejected so a later render can be produced
without packaging the declined artifact.

`POST /api/v1/episodes/{episode_id}/thumbnails/generate` extracts a JPEG
thumbnail from the latest completed render, or from a supplied render asset ID.
The thumbnail is stored as a normal `thumbnail` asset linked back to the render
asset and records `thumbnail_integrity` QC with storage, checksum, probe, and
dimension evidence.

`POST /api/v1/episodes/{episode_id}/youtube-package/export` creates a
checksummed ZIP delivery package from the latest approved final render. Package
export refuses unapproved final renders; preview exports remain available only
through the explicit non-production `allow_preview_render` request flag. The
package contains `youtube-package.json`, the rendered MP4, the matching thumbnail
when available, and subtitle files from stored subtitle objects or embedded
subtitle text metadata. The package manifest preserves the render manifest's
`evidence_lineage` block so delivery artifacts remain auditable back to source
material. Package QC records `youtube_package_integrity` with included file
counts, subtitle file counts, chapter counts, evidence citation/source counts,
unresolved evidence refs, and delivery component evidence. Missing source
thumbnails or subtitles are warnings, but omitting an available checked
thumbnail or render-linked subtitle asset from the final package is a package QC
failure. Omitting or changing chapter entries from a chaptered timeline is also
a package QC failure.

`POST /api/v1/episodes/{episode_id}/production-manifest` stores a
checksummed `production_manifest.v1` JSON asset for a completed delivery package.
It requires the latest `youtube_package_integrity` QC for that package to be
present and non-failing, then ties the final package back to the render asset,
render manifest, timeline asset and segment list, source assets, QC results,
approvals, workflow/audit counts, package-linked publish jobs, transcript
localization metadata, per-turn transcript-to-discussion lineage, and preserved
evidence lineage. The manifest timeline block includes normalized chapter
entries with title, start milliseconds, YouTube timecode, source turn, and
segment identifiers. Timeline segments and manifest timeline segments retain the
transcript turn ID plus `source_discussion_turn_ids`, preserving the canonical
discussion-turn chain for localized dialogue, audio, video, subtitles, timeline,
render, and delivery package review. Timeline QC fails with
`timeline_segment_missing_source_discussion_turn_links` or
`timeline_segment_source_discussion_turn_link_mismatch` when a segment for a
transcript turn with canonical source links drops or changes those IDs. Manifest
timeline segments retain first-class secondary visual, reaction-loop, and
studio-scene asset IDs in addition to the ordered visual layer list. The top-level
`talkshow_visuals` block records expected, linked, and missing shot-planned
reusable reaction/studio segment counts so the final audit artifact proves
whether planned cast animation and set scenes survived through
timeline/render/package handoff. Completion readiness, system health, and live
publishing treat manifests with reusable reaction/studio segment links but no
ready `talkshow_visuals` block as invalid. The embedded
`delivery_package.asset_id` is required and must
match the selected export package before completion, health, or live-publish
readiness can treat the manifest as valid. If the manifest timeline includes
chapters, the embedded delivery-package manifest must include matching chapter
entries before those same readiness surfaces treat it as valid. Each asset entry
also includes
normalized audit fields for creation/update time, source turn/evidence
references, reproducibility metadata,
retry history, manual edits, and latest approval state, while preserving
redacted generation metadata for audit context. Asset generation metadata,
model endpoint capabilities, QC-result details, and package-linked publish-job
result and delivery snapshots are recursively redacted for token, secret,
password, API-key, authorization, and credential fields before they are
embedded in the manifest, including common camelCase/PascalCase variants such
as `accessToken`, `clientSecret`, and `apiKey`. The Web UI can
generate the manifest once a package exists; regeneration marks the previous
manifest for that package as `replaced` so auditors can see manifest history.

`POST /api/v1/episodes/{episode_id}/publish` creates an auditable publish job
from a completed package. The default `mock-youtube` target is a dry-run adapter:
it records the delivery payload that would be sent to YouTube, including title,
description, tags, language, package checksum, video/thumbnail URIs, chapters,
subtitle count, and evidence lineage, but it does not upload to a live account.
HTTP delivery targets can also be configured for live package delivery; they
post the same delivery payload and, when available, the package ZIP to the
target's configured delivery endpoint. Non-mock non-dry-run delivery requires a
non-failing `youtube_package_integrity` QC result and completed
`production_manifest` asset for the selected package before any external request
is opened, and the payload carries the manifest asset ID, URI, checksum, and
schema version for the receiving delivery service. The seeded disabled
`youtube-resumable`
target shows the live YouTube Data API configuration shape. When enabled with an
OAuth access-token credential reference or refresh-token credential references,
the `youtube_resumable` adapter resolves an access token, records safe OAuth
source/refresh metadata, reads `video/render.*` from the exported package ZIP,
initiates a resumable YouTube upload session, records only presence evidence for
the returned session URI, uploads the video bytes, and records the returned
video ID/watch URL. It then uploads the packaged thumbnail through YouTube's thumbnail
endpoint and each packaged subtitle file through YouTube's captions endpoint,
persisting per-entry status, safe endpoint path/query-key evidence, response
payload, content type, byte size, and aggregate caption counts in the publish
job and delivery QC.
Publish jobs are persisted in the episode payload, exposed through `GET
/api/v1/episodes/{episode_id}/publish-jobs`, audited with `publisher.job.*`
events, and checked with `publish_delivery_integrity` QC. Failing package QC
blocks publishing, while missing package QC and missing production manifests
block live delivery.
The publishing worker remains dry-run by default. It can submit live automated
jobs only when `DIALECTICORE_PUBLISHER_AUTOMATED_LIVE_ENABLED=true` and the
enabled target explicitly sets `capabilities.automated_live_publish=true`; the
worker creates a production manifest between package export and publish, then
records production-manifest, live-job, and dry-run-job counts in heartbeat
evidence.

Current Increment 2 groundwork can already create transcript-linked WebVTT or
SRT subtitle assets with cue provenance, word-timestamp segmentation when audio
timing metadata is available, normalized provider or estimated phoneme/viseme
tracks for later lip-sync and styled subtitle workflows, and subtitle sync QC.

Remaining Increment 4 work:

- No known YouTube delivery implementation gaps remain beyond live-provider
  hardening and operator credential provisioning.
