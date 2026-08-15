from __future__ import annotations

import re
from datetime import UTC, datetime

from app.domain.enums import EpisodeStatus, QualitySeverity, TranscriptType
from app.domain.schemas import (
    Approval,
    AuditEvent,
    Claim,
    Episode,
    LocalizationRequest,
    QualityResult,
    TranscriptTurn,
    TranscriptVersion,
)


class LocalizationService:
    def create_language_variants(
        self,
        episode: Episode,
        request: LocalizationRequest,
    ) -> Episode:
        canonical = self._approved_canonical_transcript(episode)
        targets = self._target_languages(episode, request)
        if not targets:
            raise ValueError("episode definition has no non-canonical language outputs")

        episode.status = EpisodeStatus.localizing
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.localizing.value},
            )
        )

        created_languages: list[str] = []
        for language, mode in targets:
            existing = self._existing_localized_transcript(episode, language)
            if existing and not request.regenerate:
                continue
            localized = self._create_localized_transcript(
                canonical,
                language,
                mode,
                allow_new_claims=episode.definition.languages.allow_new_claims,
            )
            episode.transcripts.append(localized)
            episode.quality_results.append(
                self._localized_transcript_qc(episode, canonical, localized, mode)
            )
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="localization.transcript.created",
                    actor=request.user_id or "system",
                    details={
                        "language": language,
                        "mode": mode,
                        "transcript_version_id": str(localized.id),
                        "parent_version_id": str(canonical.id),
                    },
                )
            )
            episode.approvals.append(
                Approval(
                    episode_id=episode.id,
                    stage="localized_transcript_review",
                    target_type="transcript_version",
                    target_id=str(localized.id),
                    decision="pending",
                    comment=(
                        f"Manual {language} transcript approval blocks localized "
                        "media production."
                    ),
                )
            )
            created_languages.append(language)

        episode.status = EpisodeStatus.ready
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.ready.value},
            )
        )
        episode.updated_at = datetime.now(UTC)
        if not created_languages and not request.regenerate:
            raise ValueError("requested localized transcripts already exist")
        return episode

    def _approved_canonical_transcript(self, episode: Episode) -> TranscriptVersion:
        canonical_id = episode.canonical_transcript_version_id
        if canonical_id is None:
            raise ValueError("episode has no canonical transcript")
        canonical = next(
            (transcript for transcript in episode.transcripts if transcript.id == canonical_id),
            None,
        )
        if canonical is None:
            raise ValueError("canonical transcript not found")
        if canonical.status != "approved":
            raise ValueError("canonical transcript must be approved before localization")
        return canonical

    def _target_languages(
        self,
        episode: Episode,
        request: LocalizationRequest,
    ) -> list[tuple[str, str]]:
        configured = {
            output.language: output.mode
            for output in episode.definition.languages.outputs
            if output.language != episode.source_language or output.mode != "canonical"
        }
        requested = request.languages
        if requested is None:
            return sorted(configured.items())
        targets: list[tuple[str, str]] = []
        for language in requested:
            mode = configured.get(language)
            if mode is None:
                raise ValueError(f"language {language} is not configured for this episode")
            targets.append((language, mode))
        return targets

    def _existing_localized_transcript(
        self,
        episode: Episode,
        language: str,
    ) -> TranscriptVersion | None:
        return next(
            (
                transcript
                for transcript in episode.transcripts
                if transcript.type == TranscriptType.localized and transcript.language == language
            ),
            None,
        )

    def _create_localized_transcript(
        self,
        canonical: TranscriptVersion,
        language: str,
        mode: str,
        *,
        allow_new_claims: bool,
    ) -> TranscriptVersion:
        localized = TranscriptVersion(
            episode_id=canonical.episode_id,
            type=TranscriptType.localized,
            language=language,
            parent_version_id=canonical.id,
            status="pending_review",
            semantic_fidelity_score=1,
            localization_metadata={
                "schema_version": "transcript_localization_metadata.v1",
                "mode": mode,
                "source_language": canonical.language,
                "target_language": language,
                "source_transcript_version_id": str(canonical.id),
                "localization_adapter": "deterministic_scaffold",
                "source_bound": True,
                "claim_policy": (
                    "allow_target_language_claims"
                    if allow_new_claims
                    else "preserve_source_claims"
                ),
                "allows_new_claims": allow_new_claims,
                "supports_dubbing": True,
                "supports_video_reperformance": mode == "localized_reperformance",
                "requires_human_review": True,
            },
        )
        localized.turns = [
            TranscriptTurn(
                transcript_version_id=localized.id,
                source_discussion_turn_ids=turn.source_discussion_turn_ids,
                speaker_participant_id=turn.speaker_participant_id,
                text=self._mock_localize_text(turn.text, language, mode),
                edit_type=f"localized_{mode}",
                semantic_difference_score=turn.semantic_difference_score,
                claims=turn.claims,
                pronunciation_markup=self._pronunciation_markup(turn.text, language),
                status=turn.status,
            )
            for turn in canonical.turns
        ]
        return localized

    def _mock_localize_text(self, text: str, language: str, mode: str) -> str:
        if not text:
            return text
        return text if language == "en" else f"[{language} {mode}] {text}"

    def _pronunciation_markup(self, text: str, language: str) -> str | None:
        if not text:
            return None
        escaped = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        return f'<speak xml:lang="{language}">{escaped}</speak>'

    def _localized_transcript_qc(
        self,
        episode: Episode,
        canonical: TranscriptVersion,
        localized: TranscriptVersion,
        mode: str,
    ) -> QualityResult:
        canonical_turns = {
            tuple(str(turn_id) for turn_id in turn.source_discussion_turn_ids): turn
            for turn in canonical.turns
        }
        failures: list[dict] = []
        warnings: list[dict] = []
        allow_new_claims = episode.definition.languages.allow_new_claims
        semantic_fidelity_threshold = episode.definition.languages.semantic_fidelity_threshold

        if len(localized.turns) != len(canonical.turns):
            failures.append(
                {
                    "issue": "turn_count_mismatch",
                    "canonical_turn_count": len(canonical.turns),
                    "localized_turn_count": len(localized.turns),
                }
            )

        for localized_turn in localized.turns:
            if not localized_turn.source_discussion_turn_ids:
                failures.append(
                    {
                        "transcript_turn_id": str(localized_turn.id),
                        "issue": "missing_source_turn_link",
                    }
                )
            if localized_turn.status != "excluded" and not localized_turn.text.strip():
                failures.append(
                    {
                        "transcript_turn_id": str(localized_turn.id),
                        "issue": "empty_non_excluded_turn",
                    }
                )
            source_key = tuple(
                str(turn_id) for turn_id in localized_turn.source_discussion_turn_ids
            )
            source = canonical_turns.get(source_key)
            if source and localized_turn.speaker_participant_id != source.speaker_participant_id:
                failures.append(
                    {
                        "transcript_turn_id": str(localized_turn.id),
                        "issue": "speaker_attribution_mismatch",
                    }
                )
            if source and not allow_new_claims:
                new_claims = self._new_claims(source.claims, localized_turn.claims)
                for claim in new_claims:
                    failures.append(
                        {
                            "transcript_turn_id": str(localized_turn.id),
                            "issue": "localized_new_claim_detected",
                            "claim_text": claim.text,
                            "claim_type": claim.claim_type,
                            "source_claim_count": len(source.claims),
                            "localized_claim_count": len(localized_turn.claims),
                        }
                    )
            if localized_turn.status != "excluded" and localized_turn.pronunciation_markup is None:
                warnings.append(
                    {
                        "transcript_turn_id": str(localized_turn.id),
                        "issue": "missing_pronunciation_markup",
                    }
                )

        if failures:
            severity = QualitySeverity.fail
        elif warnings:
            severity = QualitySeverity.warning
        else:
            severity = QualitySeverity.pass_
        score = max(0.0, 1.0 - (0.2 * len(failures)) - (0.03 * len(warnings)))
        localized.semantic_fidelity_score = score
        return QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id=str(localized.id),
            check_type="localized_transcript_semantic_fidelity",
            severity=severity,
            status=severity.value,
            score=score,
            details={
                "language": localized.language,
                "mode": mode,
                "localization_metadata": localized.localization_metadata,
                "allow_new_claims": allow_new_claims,
                "semantic_fidelity_threshold": semantic_fidelity_threshold,
                "turn_count": len(localized.turns),
                "failure_count": len(failures),
                "warning_count": len(warnings),
                "failures": failures,
                "warnings": warnings,
            },
        )

    def _new_claims(self, source_claims: list[Claim], localized_claims: list[Claim]) -> list[Claim]:
        source_claim_texts = {self._normalized_claim_text(claim.text) for claim in source_claims}
        return [
            claim
            for claim in localized_claims
            if self._normalized_claim_text(claim.text)
            and self._normalized_claim_text(claim.text) not in source_claim_texts
        ]

    def _normalized_claim_text(self, value: str) -> str:
        return re.sub(r"\s+", " ", value.casefold()).strip()
