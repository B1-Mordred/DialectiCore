# Quality Control

QC results use pass/warning/fail severity. The current scaffold includes the
quality result type and endpoint surface.

Implemented in Increment 1:

- `discussion_minimum_structure`
- `discussion_duration_control`
- `transcript_semantic_fidelity`

`discussion_minimum_structure` verifies that the session has enough turns,
contains host moderation, includes the configured speakers, and covers every
required topic dimension. Coverage is tracked deterministically from accepted
turn text and claims as `discussion_turn_coverage.v1` metadata, then rebuilt
after transcript regeneration or exclusion edits so missing dimensions remain
visible before approval and delivery completion.

The transcript semantic check is deterministic. It verifies source turn links,
source turn existence, speaker attribution, non-empty non-excluded text, and
added-claim detection by comparing transcript-turn claims against the linked
discussion turn. Excluded turns produce warnings rather than failures so a
producer can intentionally remove a turn before approval.

Transcript approval is blocked when the canonical broadcast transcript has a
failing semantic-fidelity QC result. Generated visual media, render, and final
media checks are implemented in later increments.

The duration-control check records estimated duration, target, minimum, maximum,
maximum monologue seconds, and any turns shortened by hard duration controls.
Exceeding maximum duration or monologue duration is a failure; being under the
minimum duration or applying deterministic shortening is a warning.

Implemented in Increment 2 groundwork:

- `localized_transcript_semantic_fidelity`
- `audio_asset_plan_completeness`
- `audio_generation_completeness`
- `audio_media_integrity`
- `subtitle_generation_completeness`

Localized transcript semantic-fidelity QC verifies turn coverage, source-link
preservation, speaker attribution, playable text, pronunciation markup, and the
episode language policy. When `allow_new_claims=false`, localized claim records
are compared with the linked canonical turn's claim records and explicit added
claims fail with `localized_new_claim_detected`.

Audio media integrity verifies generated audio asset metadata for storage,
duration plausibility, audio MIME format, sample rate, expected language,
voice-profile consistency, clipping markers, excessive silence, loudness
deviation, true-peak ceiling, and word-timestamp bounds. Metadata-detected hard
media defects are failures; missing sample-rate, missing channel count, probe
warnings, loudness deviation, or true-peak limit overage are warnings. When an
asset uses a configured object-storage URI (`object://` for local storage or
`s3://` for S3-compatible storage), QC probes the actual stored audio or S3
probe cache first and uses measured duration, MIME type, sample rate, channel
count, peak dBFS, RMS dBFS, integrated LUFS, loudness range, true peak,
normalization gain, silence ratio, and clipping detection ahead of
provider-supplied hints. FFmpeg `loudnorm` output is preferred for integrated
loudness analysis; stored WAV assets fall back to RMS-estimated loudness when
FFmpeg is unavailable. External remote URIs without a stored local object still
fall back to provider metadata. QC details record how many assets were probed,
waveform-analyzed, loudness-analyzed, and downloaded from remote result URLs.

Audio media integrity also validates phoneme/viseme timing tracks used by later
lip-sync and styled subtitle workflows. Provider phoneme timings are preferred;
estimated fallback tracks derived from word timings are accepted but counted
separately. Missing timing is a warning, while malformed timing bounds,
non-monotonic starts, or viseme/phoneme count mismatches are failures.

Subtitle completeness verifies every playable transcript turn is covered by at
least one cue, cue text is non-empty, source discussion-turn links are retained,
timings do not overlap, line lengths stay bounded, and cue/audio sync drift does
not exceed the episode's `block_on_sync_error_ms` threshold. Completed audio
asset word timestamps are used for finer-grained cue timing when available.
Missing audio falls back to deterministic estimated timing and produces warning
QC.

Implemented in Increment 3 groundwork:

- `visual_asset_plan_completeness`
- `visual_generation_completeness`

Visual asset plan completeness verifies that every playable transcript turn has
a participant visual profile, that each visual profile references an enabled
primary ComfyUI workflow, and that one active primary video asset exists per
playable turn. QC details include required visual turn count, planned primary
visual asset count, planned B-roll asset count, required/planned reusable
reaction-loop counts, planned reusable studio-scene count, shot-planned turn
count, citation-overlay turn/card counts, missing primary turn IDs, missing
reusable participant IDs, missing shot plan turn IDs, missing citation overlay
turn IDs, and issue/failure/warning counts. Missing primary visual coverage or
required citation overlay cards is a failure. Missing reusable reaction loops or
the reusable studio scene are also failures when their workflows are enabled,
because talk-show timelines depend on those assets for reaction and wide-shot
coverage; missing shot-plan coverage remains a warning. This check validates
planning coverage only; generated
media integrity is handled by the separate visual media integrity check.

Visual generation completeness verifies the visual job lifecycle after ComfyUI
submission, sync, or cancellation. It reports checked, completed, submitted,
failed, stored, probed, render-ready, render-suitable, and fallback visual asset
counts, including planned/completed citation-card counts. Failed visual jobs and
completed assets without storage are failures. Completed assets without
checksums, deterministic mock placeholder assets, fallback SVG cards, visual
probe warnings, completed non-placeholder assets without dimensions, and
non-render-suitable assets are warnings. PNG/JPEG dimensions are read from file
headers, SVG fallback and citation-card dimensions are parsed from the SVG
document, and video metadata uses `ffprobe` when available. Video probes validate
that a video stream exists and that dimensions, duration, FPS, codec, pixel
format, and exact or estimated frame-count evidence are present and coherent.

Visual media integrity is a rerunnable QC check for generated visual assets. It
checks completion, storage, checksums, media probe evidence, render suitability,
expected dimensions, PNG pixel evidence, SVG structural evidence, video FPS,
video duration, video probe integrity, primary-turn duration alignment with
completed audio, lip-sync readiness from audio phoneme timing, measured lip-sync
offsets, visual style metadata, character identity/style consistency against the
plan-time profile snapshot, configured workflow/endpoint references, fallback
use, and placeholder use. Non-completed selected assets and completed assets
without storage are failures. Placeholder media, fallback media, missing probes,
dimension/FPS or duration mismatches, missing or contradictory video probe
evidence, very low-detail, mostly dark, or mostly transparent PNGs, low-detail
SVGs, missing lip-sync readiness, identity/style mismatches, and missing style
metadata are warnings. The check records `visual_media_integrity` details,
including pixel analyzed and pixel warning counts, video probe warning/invalid
counts, measured/max/average lip-sync offsets, identity/style warning counts,
citation-card checked/render-suitable counts, and `visual.qc.completed` audit
events.

Timeline integrity validates the first Increment 4 `EpisodeTimeline` asset. It
checks that each playable transcript turn has one segment, that segment timing is
continuous, that completed dialogue audio and primary video are linked, that a
fallback visual is available when primary video is missing, that subtitle assets
are attached, and that the total timeline duration is positive and inside the
episode maximum. Missing dialogue audio or a primary video without fallback is a
failure. Missing subtitles, timing gaps, primary video with fallback available,
and over-maximum duration are warnings. The check records
`timeline_integrity` details, including cited segment and linked citation
overlay segment counts, and `timeline.qc.completed` audit events.

Render integrity validates FFmpeg preview and final render artifacts. The check
confirms that the render asset has storage and checksum evidence, that FFprobe
can read the MP4, that dimensions match the selected render preset, that FPS and
duration are within tolerance, and that audio is normalized to the preset sample
rate. Preview jobs are checked against the 30-second preview cap; final jobs are
checked against the full timeline duration. Failures block missing storage,
missing checksums, missing duration, or dimension mismatches. Probe warnings,
FPS mismatches, duration drift, and audio sample-rate mismatches are warnings.
The render manifest also snapshots the source assets that fed the render.
Render QC fails with `render_source_asset_missing` when a manifest source asset
no longer exists on the episode, and fails with `render_source_asset_stale` when
the current source asset has different source entity links, status, storage URI,
media metadata, checksum, or render-readiness state from the manifest snapshot.
Completion readiness exposes stale or missing render-source inputs through
stage-specific preview/final render source-asset blockers.
When timeline segments contain claim citations, the render manifest must include
`evidence_lineage` resolving cited evidence refs to the active evidence pack;
missing evidence-pack lineage or unresolved source refs are failures. QC details
include evidence citation, source, and unresolved ref counts. Render QC also
checks scene-composition evidence: a timeline with segments must produce
composition segments, and planned citation overlay assets must be counted as
composited overlays. Details include composition mode, composition segment
count, citation overlay count, composited overlay count, resolved overlay
count, resolved media counts, layout and transition policy names, motion
primitive names/counts, advanced layout counts, split-screen scene counts,
focus-shift scene counts, cross-scene transition counts, rendered xfade counts,
cross-scene renderer mode, rendered layer transform counts, rendered transform
names, rendered scale/opacity keyframe counts, rendered easing curve
names/counts, rendered mask names/counts, non-rectangular mask counts, layer
mask renderer mode, and layer motion renderer mode.
Render jobs record `render_preview_integrity` or `render_final_integrity` details
and `render.qc.completed` audit events.

Thumbnail integrity validates FFmpeg-extracted JPEG thumbnails. It checks
storage, checksum, probe availability, dimensions, and whether the thumbnail
matches the source render dimensions. Missing storage, checksum, or dimensions
are failures; probe warnings or render-dimension drift are warnings. Thumbnail
jobs record `thumbnail_integrity` details and `thumbnail.qc.completed` audit
events.

YouTube package integrity validates exported delivery ZIPs. It checks package
storage, checksum, manifest inclusion, MP4 inclusion, whether the package uses a
final render, thumbnail inclusion, and subtitle inclusion. Missing storage,
checksum, manifest, or video are failures. Preview-render exports remain
warnings for explicit non-production test packages. Missing thumbnail or
subtitle source material remains visible as a warning, while packages that omit
an available checked thumbnail or render-linked generated subtitle asset fail
package QC and block production completion, live readiness, and workflow
delivery handoff. If cited claims are present, the package
manifest must preserve resolved evidence lineage from the render manifest;
missing lineage or unresolved evidence refs are failures. Package exports record
`youtube_package_integrity` details and
`youtube.package.qc.completed` audit events.

Implemented in Increment 5 groundwork:

- `evidence_pack_integrity`
- `claim_citation_integrity`

Evidence pack integrity validates stored research assets. It checks that the
evidence pack has object storage, checksum evidence, research sub-questions,
source index entries, fact-check rules, and internally valid source references.
Unknown source references are failures. Missing fact-check rules or having only
configuration-derived sources are warnings, because the configuration pack is
useful for workflow grounding but is not a substitute for retrieved external
research. Supplied manual/external sources and successfully fetched explicit URL
retrieval targets remove the retrieved-source warning when they are accepted
into the source index; their score factors and content checksums are retained
in source metadata. Retrieval attempts, successes, skipped targets, and fetch
failures are retained in the evidence asset `retrieval_tool_log`. Source
rankings are required when retrieved sources exist. If a multi-source evidence
pack has no strong external source, or contains stale external source material,
QC records warnings with ranking/source-policy details. Multi-source evidence
packs also retain cross-source summary evidence with agreement, conflict, and
relationship-cluster counts so reviewers can distinguish corroborated claims
from disputed factual ground. Those relationships include deterministic
shared-term/stance matches and claim-facet matches across source-grounded
subject/relation/object structures. Evidence-pack QC also reports deterministic
relationship/quantity facet counts plus facet agreement/conflict counts, giving
reviewers a structured view of extracted factual material alongside the original
source-linked sentences.
It also reports deterministic causal context and scope qualifier counts from
`deterministic_causal_scope_context_v1`, so reviewers can see which source
claims have explicit cause/effect evidence or contextual applicability
qualifiers.

Claim support group counts summarize how many extracted source-claim groups are
corroborated by multiple sources, disputed across sources, or currently backed
by only one source.
When the trusted external advanced extractor is enabled, evidence-pack QC also
reports extraction attempts, successful responses, accepted source-bound claims,
and rejected untrusted claims. Claims returned without the current source ID are
not inserted into the evidence pack and produce a warning.

Source review integrity records the `human_source_review_v1` human-review state
for the active evidence pack. It reports reviewed, approved, rejected,
needs-revision, reviewed-external, and unreviewed-external source counts, and
warns when external sources remain unreviewed or when a reviewer rejects or
flags a source for revision.

Claim citation integrity checks transcript claims against the selected evidence
pack. Claims with unknown evidence refs are failures. Factual, supported,
verified, or statistical claims without source refs are unsupported and fail when
the episode requires source links or blocks unsupported high-impact claims.
Uncited opinions and predictions are warnings rather than failures when source
links are not required. The result records claim counts, cited claim counts,
unsupported claim counts, invalid evidence refs, source counts, and
`research.claim_qc.completed` audit evidence.
