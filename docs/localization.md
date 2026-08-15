# Localization

Localization is part of Increment 2. Required modes:

- Direct translation and dubbing.
- Localized re-performance.
- Independent target-language discussion.

Localized turns must retain source links and pass new-claim detection.

Current implementation:

- Language profiles are persisted as a Web UI/API managed catalog with BCP 47
  tags, native names, default localization modes, subtitle direction,
  line-breaking policy, voice defaults, audit events, and backup/restore
  coverage.
- Episode definitions can declare multiple language outputs.
- The Web UI editor accepts comma-separated output languages and keeps `en` as
  the canonical source output.
- `POST /api/v1/episodes/{episode_id}/localize` runs after canonical transcript
  approval and creates `localized` transcript versions for configured
  non-canonical outputs.
- Each localized transcript turn preserves source discussion-turn links,
  speaker attribution, claims, excluded-turn status, and pronunciation markup.
- Each localized transcript records `transcript_localization_metadata.v1` with
  the localization mode, source/target languages, parent transcript ID,
  adapter identity, source-bound claim policy, human-review requirement, and
  whether the transcript is suitable for dubbing and localized video
  re-performance.
- Audio, subtitle, and visual asset plans copy the transcript type and
  localization metadata into asset generation metadata, giving downstream
  Voicebox, subtitle, ComfyUI, timeline, and render stages a stable
  mode/provenance contract.
- `localized_transcript_semantic_fidelity` QC checks source links, turn count,
  speaker attribution, playable text, pronunciation markup, the configured
  semantic-fidelity threshold, and the new-claim policy. When
  `allow_new_claims=false`, any localized claim record not present on the
  source canonical turn fails with `localized_new_claim_detected`. QC details
  include the same localization metadata so reviewers can see whether they are
  approving direct translation/literal output or localized re-performance.
- Each created localized transcript receives a pending
  `localized_transcript_review` approval targeted at that transcript version.
  The approval decision endpoint marks the localized version `approved` or
  `rejected`; failing localized semantic-fidelity QC blocks approval.
- Production completion readiness includes `localized_output_readiness.v1` and
  blocks `COMPLETED` while configured non-canonical output languages are
  missing, not approved, missing localized semantic-fidelity QC, or failing that
  QC.
- Subtitles can be generated from approved canonical or localized transcripts
  after audio generation. Direct subtitle-generation requests with an explicit
  transcript ID still require that transcript to be approved. Subtitle cues
  retain transcript-turn provenance and therefore keep the source discussion-turn
  chain available for review.
- Audit events record stage transitions and created localized transcript
  versions.

The current localizer is deterministic scaffold behavior. It is intentionally
source-bound, mode-preserving, and claim-policy aware, and its output can be
used by the system for localized audio/subtitle/visual production once the
localized transcript has passed QC and human approval. Remote/provider-backed
translation adapters and independent target-language discussions remain future
Increment 2 work.
