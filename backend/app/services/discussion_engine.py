from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote
from uuid import UUID

from app.core.config import Settings
from app.domain.enums import AssetType, EpisodeStatus, ParticipantType, TranscriptType, TurnType
from app.domain.schemas import (
    Approval,
    AuditEvent,
    DiscussionSession,
    DiscussionTurn,
    Episode,
    ParticipantMemory,
    ParticipantProfile,
    ProductionStatus,
    QualityResult,
    SpeakerBalance,
    TranscriptTurn,
    TranscriptVersion,
)
from app.services.model_gateway import ModelGateway, TurnContext, safe_provider_response_payload
from app.services.production_control_service import ProductionControlService


class DiscussionEngine:
    def __init__(self, gateway: ModelGateway, settings: Settings) -> None:
        self.gateway = gateway
        self.settings = settings
        self.production_control = ProductionControlService()

    async def run(self, episode: Episode) -> Episode:
        self._validate_participants(episode)
        self._ensure_research_allows_discussion(episode)
        episode.status = EpisodeStatus.preparing_discussion
        self.production_control.record_stage(
            episode,
            EpisodeStatus.preparing_discussion,
            "discussion_engine.run",
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                details={"stage": EpisodeStatus.preparing_discussion},
            )
        )
        session = self._new_session(episode)
        episode.discussion_session = session
        episode.status = EpisodeStatus.discussing
        self.production_control.record_stage(
            episode,
            EpisodeStatus.discussing,
            "discussion_engine.run",
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                details={"stage": EpisodeStatus.discussing},
            )
        )

        latest_host_instruction = (
            self._post_primer_bridge_seed_instruction(episode)
            if episode.definition.media.opening.enabled
            else self._opening_instruction(episode)
        )
        evidence_summary = self._evidence_summary(episode)
        available_evidence_refs = self._available_evidence_refs(episode)
        while True:
            remaining = max(
                episode.maximum_duration_seconds - session.estimated_duration_seconds,
                0,
            )
            if remaining <= 0:
                break
            if remaining < self._minimum_turn_duration_seconds():
                break
            next_turn = self._next_turn_decision(
                episode,
                session,
                latest_host_instruction,
                remaining,
            )
            if next_turn is None:
                break
            participant, turn_type, phase, selection = next_turn
            sequence_number = len(session.turns) + 1
            session.phase = phase
            tool_results, tool_log = self._discussion_tool_results(
                episode,
                participant,
                latest_host_instruction,
                phase,
            )
            coverage_guidance = self._coverage_guidance(episode, session, phase)
            remaining_turns = max(
                1,
                int(selection.get("minimum_remaining_turns_after_selection") or 0) + 1,
            )
            allowed_seconds = self._turn_duration_allowance(
                episode,
                session,
                remaining_turns,
            )
            if turn_type == TurnType.post_primer_bridge:
                allowed_seconds = self._post_primer_bridge_duration_allowance(
                    episode,
                    remaining,
                )
            turn_contract = self._turn_contract(
                episode,
                session,
                participant,
                turn_type,
                phase,
                selection,
                allowed_seconds,
            )
            interaction = self._interaction_metadata(
                session,
                participant,
                turn_type,
                phase,
                selection,
                turn_contract,
            )
            turn_instruction = self._instruction_with_coverage_guidance(
                latest_host_instruction,
                coverage_guidance,
            )
            turn_instruction = self._instruction_with_turn_contract(
                turn_instruction,
                turn_contract,
            )
            context = TurnContext(
                central_question=episode.central_question,
                phase=phase,
                latest_host_instruction=turn_instruction,
                public_transcript=[
                    f"{turn.speaker_participant_id}: {turn.spoken_text}"
                    for turn in session.turns[-8:]
                ],
                remaining_seconds=remaining,
                private_memory=session.memories[participant.id],
                required_dimensions=episode.definition.topic.required_dimensions,
                evidence_summary=evidence_summary,
                available_evidence_refs=available_evidence_refs,
                tool_results=tool_results,
                discussion_intensity=self._discussion_intensity(episode),
            )
            endpoint = self._endpoint_for(episode, participant.model_endpoint_id)
            response = await self.gateway.generate_turn(endpoint, participant, context)
            structured, citation_metadata = self._sanitize_structured_evidence_refs(
                response.structured,
                allowed_evidence_refs=available_evidence_refs,
            )
            structured, duration_metadata, status = self._apply_duration_controls(
                structured,
                allowed_seconds,
                episode.definition.format.maximum_monologue_seconds,
            )
            turn = DiscussionTurn(
                discussion_session_id=session.id,
                sequence_number=sequence_number,
                speaker_participant_id=participant.id,
                turn_type=turn_type,
                spoken_text=structured.spoken_text,
                intent=structured.intent,
                responding_to_turn_id=self._responding_to_turn_id_or_contract_question(
                    session,
                    structured.responding_to,
                    turn_contract,
                    fallback_target_turn_id=next(
                        iter(interaction.get("target_turn_ids", [])), None
                    ),
                ),
                estimated_duration_seconds=self._estimate_duration(structured.spoken_text),
                structured_output=structured,
                raw_provider_response=self._safe_raw_provider_response(response.raw),
                generation_metadata={
                    **response.metadata,
                    **citation_metadata,
                    **duration_metadata,
                    "speaker_selection": selection,
                    "tool_usage": tool_log,
                    "coverage_guidance": coverage_guidance,
                    "turn_contract": turn_contract,
                    "interaction": interaction,
                },
                status=status,
            )
            session.turns.append(turn)
            covered_dimensions = self._update_coverage_state(episode, session, turn)
            turn.generation_metadata["coverage"] = {
                "schema_version": "discussion_turn_coverage.v1",
                "covered_dimensions": covered_dimensions,
                "coverage_state_after_turn": dict(session.coverage_state),
            }
            session.estimated_duration_seconds += turn.estimated_duration_seconds
            self._update_memory(session, participant.id, response.structured.private_memory_update)
            self._update_balance(session, participant.id, turn, sequence_number)
            self._update_controller_state(
                session,
                participant.id,
                turn,
                selection,
                tool_log,
            )
            if participant.participant_type == ParticipantType.host:
                latest_host_instruction = turn.spoken_text
            if citation_metadata:
                episode.audit_events.append(
                    AuditEvent(
                        episode_id=episode.id,
                        event_type="discussion.citation_refs.sanitized",
                        details={
                            "turn_id": str(turn.id),
                            "sequence_number": sequence_number,
                            "speaker_participant_id": participant.id,
                            **citation_metadata["citation_ref_sanitization"],
                        },
                    )
                )
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="discussion.turn.created",
                    details={
                        "turn_id": str(turn.id),
                        "sequence_number": sequence_number,
                        "speaker_participant_id": participant.id,
                        "phase": phase,
                    },
                )
            )
            if tool_log["tool_call_count"]:
                episode.audit_events.append(
                    AuditEvent(
                        episode_id=episode.id,
                        event_type="discussion.tools.used",
                        details={
                            "turn_id": str(turn.id),
                            "sequence_number": sequence_number,
                            "speaker_participant_id": participant.id,
                            "tool_policy_id": participant.tool_policy_id,
                            "tool_call_count": tool_log["tool_call_count"],
                            "result_count": tool_log["result_count"],
                        },
                    )
                )
            if session.estimated_duration_seconds >= episode.maximum_duration_seconds:
                break

        session.status = "completed"
        session.ended_at = datetime.now(UTC)
        episode.status = EpisodeStatus.transcript_qc
        self.production_control.record_stage(
            episode,
            EpisodeStatus.transcript_qc,
            "discussion_engine.run",
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                details={"stage": EpisodeStatus.transcript_qc},
            )
        )
        raw_transcript = self._create_transcript(episode, TranscriptType.raw)
        broadcast_transcript = self._create_transcript(
            episode,
            TranscriptType.broadcast,
            parent_version_id=raw_transcript.id,
        )
        episode.transcripts.extend([raw_transcript, broadcast_transcript])
        episode.canonical_transcript_version_id = broadcast_transcript.id
        episode.quality_results.append(self._discussion_qc(episode))
        episode.quality_results.append(self._conversation_quality_qc(episode))
        episode.quality_results.append(self._duration_qc(episode))
        episode.quality_results.append(self._transcript_semantic_qc(episode, broadcast_transcript))
        episode.approvals.append(
            Approval(
                episode_id=episode.id,
                stage="transcript_review",
                target_type="transcript_version",
                target_id=str(broadcast_transcript.id),
                decision="pending",
                comment="Manual transcript approval blocks downstream production.",
            )
        )
        episode.status = EpisodeStatus.transcript_review
        self.production_control.record_stage(
            episode,
            EpisodeStatus.transcript_review,
            "discussion_engine.run",
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                details={"stage": EpisodeStatus.transcript_review},
            )
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="approval.required",
                details={"stage": "transcript_review"},
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def reopen_approved_transcript_review(
        self,
        episode: Episode,
        user_id: str | None = None,
        comment: str | None = None,
    ) -> Episode:
        """Fork the approved canonical transcript into a new editable revision."""
        if episode.status in {EpisodeStatus.completed, EpisodeStatus.cancelled}:
            raise ValueError("completed or cancelled episodes cannot reopen transcript review")
        self._require_session(episode)

        canonical = next(
            (
                transcript
                for transcript in episode.transcripts
                if transcript.id == episode.canonical_transcript_version_id
            ),
            None,
        )
        if canonical is None:
            raise ValueError("episode has no canonical transcript")
        if canonical.status != "approved":
            raise ValueError("only an approved transcript can be reopened for editing")

        self._append_broadcast_revision(
            episode,
            edit_reason="approved_transcript_reopened",
            actor=user_id,
            details={
                "reopened_transcript_version_id": str(canonical.id),
                "comment": comment,
            },
        )
        self.production_control.record_stage(
            episode,
            EpisodeStatus.transcript_review,
            "discussion.approved_transcript_reopened",
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="transcript.approved_revision.reopened",
                actor=user_id or "system",
                details={
                    "reopened_transcript_version_id": str(canonical.id),
                    "editable_transcript_version_id": str(
                        episode.canonical_transcript_version_id
                    ),
                    "comment": comment,
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    async def regenerate_turn(
        self,
        episode: Episode,
        turn_id: UUID,
        user_id: str | None = None,
        comment: str | None = None,
    ) -> Episode:
        self._ensure_transcript_review_editable(episode)
        session = self._require_session(episode)
        index, turn = self._turn_by_id(session, turn_id)
        if turn.status == "excluded":
            raise ValueError("excluded turns cannot be regenerated")

        participant = self._participant_by_id(episode, turn.speaker_participant_id)
        endpoint = self._endpoint_for(episode, participant.model_endpoint_id)
        latest_host_instruction = (
            self._post_primer_bridge_seed_instruction(episode)
            if turn.turn_type == TurnType.post_primer_bridge
            else self._latest_host_instruction_before(episode, index)
        )
        phase = (
            "post_primer_bridge"
            if turn.turn_type == TurnType.post_primer_bridge
            else session.phase
        )
        public_transcript = [
            f"{prior.speaker_participant_id}: {prior.spoken_text}"
            for prior in session.turns[max(0, index - 8) : index]
            if prior.status != "excluded"
        ]
        tool_results, tool_log = self._discussion_tool_results(
            episode,
            participant,
            latest_host_instruction,
            phase,
        )
        allowed_seconds = min(
            episode.definition.format.maximum_monologue_seconds,
            max(
                1.0,
                episode.maximum_duration_seconds
                - (session.estimated_duration_seconds - turn.estimated_duration_seconds),
            ),
        )
        turn_contract: dict | None = None
        if turn.turn_type == TurnType.post_primer_bridge:
            allowed_seconds = self._post_primer_bridge_duration_allowance(
                episode,
                max(
                    episode.maximum_duration_seconds
                    - (session.estimated_duration_seconds - turn.estimated_duration_seconds),
                    0,
                ),
            )
            turn_contract = self._turn_contract(
                episode,
                session,
                participant,
                turn.turn_type,
                phase,
                {},
                allowed_seconds,
            )
            latest_host_instruction = self._instruction_with_turn_contract(
                latest_host_instruction,
                turn_contract,
            )
        context = TurnContext(
            central_question=episode.central_question,
            phase=phase,
            latest_host_instruction=latest_host_instruction,
            public_transcript=public_transcript,
            remaining_seconds=max(
                episode.maximum_duration_seconds - session.estimated_duration_seconds,
                0,
            ),
            private_memory=session.memories.get(
                participant.id,
                ParticipantMemory(
                    discussion_session_id=session.id,
                    participant_id=participant.id,
                ),
            ),
            required_dimensions=episode.definition.topic.required_dimensions,
            evidence_summary=self._evidence_summary(episode),
            available_evidence_refs=self._available_evidence_refs(episode),
            tool_results=tool_results,
            discussion_intensity=self._discussion_intensity(episode),
        )
        response = await self.gateway.generate_turn(endpoint, participant, context)
        structured, citation_metadata = self._sanitize_structured_evidence_refs(
            response.structured,
            allowed_evidence_refs=context.available_evidence_refs,
        )
        structured, duration_metadata, status = self._apply_duration_controls(
            structured,
            allowed_seconds,
            episode.definition.format.maximum_monologue_seconds,
        )
        history = list(turn.generation_metadata.get("regeneration_history", []))
        history.append(
            {
                "regenerated_at": datetime.now(UTC).isoformat(),
                "previous_text": turn.spoken_text,
                "previous_status": turn.status,
                "previous_raw_provider_response": self._safe_raw_provider_response(
                    turn.raw_provider_response
                ),
                "user_id": user_id,
                "comment": comment,
            }
        )
        turn.spoken_text = structured.spoken_text
        turn.intent = structured.intent
        turn.estimated_duration_seconds = self._estimate_duration(structured.spoken_text)
        turn.structured_output = structured
        turn.raw_provider_response = self._safe_raw_provider_response(response.raw)
        turn.generation_metadata = {
            **response.metadata,
            **citation_metadata,
            **duration_metadata,
            "regeneration_history": history,
            "tool_usage": tool_log,
            **({"turn_contract": turn_contract} if turn_contract is not None else {}),
        }
        turn.status = (
            "regenerated_duration_adjusted"
            if status == "duration_adjusted"
            else "regenerated"
        )
        self._update_memory(session, participant.id, response.structured.private_memory_update)
        if citation_metadata:
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="discussion.citation_refs.sanitized",
                    details={
                        "turn_id": str(turn.id),
                        "speaker_participant_id": participant.id,
                        "regenerated": True,
                        **citation_metadata["citation_ref_sanitization"],
                    },
                )
            )
        self._refresh_session_state(episode)
        self._append_tool_log_to_controller_state(session, tool_log)
        episode.quality_results.append(self._discussion_qc(episode))
        episode.quality_results.append(self._conversation_quality_qc(episode))
        episode.quality_results.append(self._duration_qc(episode))
        self._append_broadcast_revision(
            episode,
            edit_reason="turn_regenerated",
            actor=user_id,
            details={"turn_id": str(turn_id), "comment": comment},
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="transcript.turn.regenerated",
                actor=user_id or "system",
                details={"turn_id": str(turn_id), "comment": comment},
            )
        )
        if tool_log["tool_call_count"]:
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="discussion.tools.used",
                    actor=user_id or "system",
                    details={
                        "turn_id": str(turn.id),
                        "speaker_participant_id": participant.id,
                        "tool_policy_id": participant.tool_policy_id,
                        "tool_call_count": tool_log["tool_call_count"],
                        "result_count": tool_log["result_count"],
                        "regenerated": True,
                    },
                )
            )
        episode.updated_at = datetime.now(UTC)
        return episode

    def add_post_primer_bridge_draft(
        self,
        episode: Episode,
        user_id: str | None = None,
        comment: str | None = None,
    ) -> Episode:
        """Turn the existing moderator opening into a reviewable primer hand-off."""
        if not episode.definition.media.opening.enabled:
            raise ValueError("a post-primer bridge requires an enabled topic primer")
        if episode.status in {EpisodeStatus.completed, EpisodeStatus.cancelled}:
            raise ValueError("completed or cancelled episodes cannot be revised")

        session = self._require_session(episode)
        if any(
            turn.turn_type == TurnType.post_primer_bridge and turn.status != "excluded"
            for turn in session.turns
        ):
            raise ValueError("the discussion already contains a post-primer host bridge")
        host = next(
            (
                participant
                for participant in self._active_participants(episode)
                if participant.participant_type == ParticipantType.host
            ),
            None,
        )
        if host is None:
            raise ValueError("the episode has no moderator to use for a post-primer bridge")
        opening_turn = next(
            (
                turn
                for turn in sorted(session.turns, key=lambda item: item.sequence_number)
                if turn.speaker_participant_id == host.id and turn.status != "excluded"
            ),
            None,
        )
        if opening_turn is None:
            raise ValueError("the discussion has no usable moderator opening to convert")

        original_turn_type = opening_turn.turn_type.value
        opening_turn.turn_type = TurnType.post_primer_bridge
        opening_turn.generation_metadata = {
            **opening_turn.generation_metadata,
            "post_primer_bridge_retrofit": {
                "created_at": datetime.now(UTC).isoformat(),
                "user_id": user_id,
                "comment": comment,
                "original_turn_type": original_turn_type,
            },
        }
        self._refresh_session_state(episode)
        episode.quality_results.append(self._discussion_qc(episode))
        episode.quality_results.append(self._conversation_quality_qc(episode))
        episode.quality_results.append(self._duration_qc(episode))
        self._append_broadcast_revision(
            episode,
            edit_reason="post_primer_bridge_retrofitted",
            actor=user_id,
            details={"turn_id": str(opening_turn.id), "comment": comment},
        )
        superseded_render_approvals = 0
        for approval in episode.approvals:
            if (
                approval.decision == "pending"
                and approval.stage in {"preview_render_review", "final_render_review"}
            ):
                approval.decision = "rejected"
                approval.comment = "Superseded by a post-primer host bridge transcript revision."
                approval.user_id = user_id
                superseded_render_approvals += 1
        self.production_control.record_stage(
            episode,
            EpisodeStatus.transcript_review,
            "discussion.post_primer_bridge_retrofitted",
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="transcript.post_primer_bridge.retrofitted",
                actor=user_id or "system",
                details={
                    "turn_id": str(opening_turn.id),
                    "comment": comment,
                    "superseded_render_approval_count": superseded_render_approvals,
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def _safe_raw_provider_response(self, raw: dict) -> dict:
        safe_raw = safe_provider_response_payload(raw)
        return safe_raw if isinstance(safe_raw, dict) else {}

    def exclude_turn(
        self,
        episode: Episode,
        turn_id: UUID,
        user_id: str | None = None,
        comment: str | None = None,
    ) -> Episode:
        self._ensure_transcript_review_editable(episode)
        session = self._require_session(episode)
        _, turn = self._turn_by_id(session, turn_id)
        turn.status = "excluded"
        turn.generation_metadata = {
            **turn.generation_metadata,
            "excluded_at": datetime.now(UTC).isoformat(),
            "excluded_by": user_id,
            "exclusion_comment": comment,
        }
        self._refresh_session_state(episode)
        episode.quality_results.append(self._discussion_qc(episode))
        episode.quality_results.append(self._conversation_quality_qc(episode))
        episode.quality_results.append(self._duration_qc(episode))
        self._append_broadcast_revision(
            episode,
            edit_reason="turn_excluded",
            actor=user_id,
            details={"turn_id": str(turn_id), "comment": comment},
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="transcript.turn.excluded",
                actor=user_id or "system",
                details={"turn_id": str(turn_id), "comment": comment},
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def edit_turn_text(
        self,
        episode: Episode,
        turn_id: UUID,
        text: str,
        user_id: str | None = None,
        comment: str | None = None,
    ) -> Episode:
        """Apply a human-authored turn correction and reopen transcript review."""
        self._ensure_transcript_review_editable(episode)
        session = self._require_session(episode)
        _, turn = self._turn_by_id(session, turn_id)
        if turn.status == "excluded":
            raise ValueError("excluded turns cannot be edited")

        edited_text = " ".join(text.split())
        if not edited_text:
            raise ValueError("turn text must contain visible characters")

        history = list(turn.generation_metadata.get("manual_edit_history", []))
        history.append(
            {
                "edited_at": datetime.now(UTC).isoformat(),
                "previous_text": turn.spoken_text,
                "previous_status": turn.status,
                "user_id": user_id,
                "comment": comment,
            }
        )
        turn.spoken_text = edited_text
        turn.estimated_duration_seconds = self._estimate_duration(edited_text)
        # Claims came from the model's previous wording and must not be presented
        # as evidence for a human-authored replacement without a fresh review.
        turn.structured_output = turn.structured_output.model_copy(
            update={"spoken_text": edited_text, "claims": []}
        )
        turn.generation_metadata = {
            **turn.generation_metadata,
            "manual_edit_history": history,
            "manual_claim_review_required": True,
        }
        turn.status = "manually_edited"

        self._refresh_session_state(episode)
        episode.quality_results.append(self._discussion_qc(episode))
        episode.quality_results.append(self._conversation_quality_qc(episode))
        episode.quality_results.append(self._duration_qc(episode))
        self._append_broadcast_revision(
            episode,
            edit_reason="turn_manually_edited",
            actor=user_id,
            details={"turn_id": str(turn_id), "comment": comment},
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="transcript.turn.manually_edited",
                actor=user_id or "system",
                details={
                    "turn_id": str(turn_id),
                    "comment": comment,
                    "claims_cleared_for_review": True,
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def status(self, episode: Episode) -> ProductionStatus:
        session = episode.discussion_session
        workflow_control = dict(episode.workflow_control or {})
        return ProductionStatus(
            episode_id=episode.id,
            status=episode.status,
            current_stage=episode.status.value,
            workflow_paused=workflow_control.get("paused") is True,
            workflow_cancelled=(
                episode.status == EpisodeStatus.cancelled
                or workflow_control.get("cancelled") is True
            ),
            retry_available=(
                episode.status == EpisodeStatus.failed
                or any(asset.status == "failed" for asset in episode.assets)
            ),
            workflow_control=workflow_control,
            current_discussion_phase=session.phase if session else None,
            turn_count=len(session.turns) if session else 0,
            estimated_duration_seconds=session.estimated_duration_seconds if session else 0,
            target_duration_seconds=episode.target_duration_seconds,
            speaker_balance=session.speaker_balance_state if session else {},
            awaiting_approval=any(approval.decision == "pending" for approval in episode.approvals),
        )

    def _new_session(self, episode: Episode) -> DiscussionSession:
        session = DiscussionSession(
            episode_id=episode.id,
            status="running",
            started_at=datetime.now(UTC),
            coverage_state={
                dimension: False
                for dimension in episode.definition.topic.required_dimensions
            },
            speaker_balance_state={
                profile.id: SpeakerBalance() for profile in self._active_participants(episode)
            },
            controller_state={
                "discussion_intensity": self._discussion_intensity(episode),
                "interaction_policy": "sharp_but_civil_v1",
            },
        )
        session.memories = {
            profile.id: ParticipantMemory(
                discussion_session_id=session.id,
                participant_id=profile.id,
            )
            for profile in self._active_participants(episode)
        }
        return session

    def _ensure_research_allows_discussion(self, episode: Episode) -> None:
        if any(
            approval.stage == "research_review" and approval.decision == "pending"
            for approval in episode.approvals
        ):
            raise ValueError("pending research approval blocks discussion")
        if any(
            approval.stage == "research_review" and approval.decision == "rejected"
            for approval in episode.approvals
        ):
            raise ValueError("rejected research approval blocks discussion")
        if not episode.definition.research.enabled:
            return
        if self._latest_evidence_pack(episode) is None:
            raise ValueError("research-enabled episodes require an evidence pack")

    def _evidence_summary(self, episode: Episode) -> list[str]:
        pack = self._latest_evidence_pack(episode)
        if not pack:
            return []
        summary: list[str] = []
        for key in ("verified_facts", "supported_claims", "uncertain_claims", "disputed_claims"):
            claims = pack.get(key, [])
            if not isinstance(claims, list):
                continue
            for claim in claims[:4]:
                if not isinstance(claim, dict):
                    continue
                text = claim.get("text")
                refs = claim.get("evidence_refs", [])
                if isinstance(text, str):
                    ref_text = ", ".join(refs) if isinstance(refs, list) else ""
                    summary.append(f"{key}: {text} [{ref_text}]")
                if len(summary) >= 8:
                    return summary
        return summary

    def _available_evidence_refs(self, episode: Episode) -> list[str]:
        pack = self._latest_evidence_pack(episode)
        if not pack:
            return []
        source_index = pack.get("source_index", [])
        if not isinstance(source_index, list):
            return []
        return [
            source["id"]
            for source in source_index
            if isinstance(source, dict) and isinstance(source.get("id"), str)
        ]

    def _sanitize_structured_evidence_refs(
        self,
        structured,
        *,
        allowed_evidence_refs: list[str],
    ):
        allowed = set(allowed_evidence_refs)
        sanitized_claims = []
        stripped_refs: list[str] = []
        downgraded_claim_count = 0
        changed = False
        for claim in structured.claims:
            original_refs = list(claim.evidence_refs)
            kept_refs = [ref for ref in original_refs if ref in allowed]
            removed_refs = [ref for ref in original_refs if ref not in allowed]
            if removed_refs:
                stripped_refs.extend(removed_refs)
                changed = True
            claim_type = claim.claim_type
            if claim_type == "supported" and not kept_refs:
                claim_type = "opinion"
                downgraded_claim_count += 1
                changed = True
            if kept_refs != original_refs or claim_type != claim.claim_type:
                sanitized_claims.append(
                    claim.model_copy(
                        update={
                            "claim_type": claim_type,
                            "evidence_refs": kept_refs,
                        }
                    )
                )
            else:
                sanitized_claims.append(claim)
        if not changed:
            return structured, {}
        sanitized = structured.model_copy(update={"claims": sanitized_claims})
        return sanitized, {
            "citation_ref_sanitization": {
                "schema_version": "discussion_citation_ref_sanitization.v1",
                "policy": "only_episode_evidence_pack_source_ids_may_be_cited",
                "allowed_evidence_ref_count": len(allowed),
                "stripped_evidence_refs": sorted(set(stripped_refs)),
                "stripped_evidence_ref_count": len(stripped_refs),
                "downgraded_claim_count": downgraded_claim_count,
            }
        }

    def _discussion_tool_results(
        self,
        episode: Episode,
        participant: ParticipantProfile,
        latest_host_instruction: str,
        phase: str,
    ) -> tuple[list[dict], dict]:
        policy_id = (participant.tool_policy_id or "no_tools").strip()
        log = {
            "schema_version": "discussion_tool_usage.v1",
            "policy_id": policy_id,
            "participant_id": participant.id,
            "phase": phase,
            "tool_call_count": 0,
            "result_count": 0,
            "cost_units": 0,
            "time_limit_ms": 250,
            "max_results": 4,
            "calls": [],
        }
        if policy_id in {"", "no_tools"}:
            return [], log
        allowed_tools = self._allowed_discussion_tools(policy_id)
        results: list[dict] = []
        if "evidence_pack_lookup" in allowed_tools:
            call_started = datetime.now(UTC)
            lookup_results = self._evidence_pack_lookup_results(
                episode,
                query=" ".join(
                    [
                        episode.central_question,
                        latest_host_instruction,
                        participant.perspective,
                        participant.expertise,
                    ]
                ),
                max_results=4,
            )
            calls = list(log["calls"])
            calls.append(
                {
                    "tool_name": "evidence_pack_lookup",
                    "status": "completed",
                    "started_at": call_started.isoformat(),
                    "completed_at": datetime.now(UTC).isoformat(),
                    "result_count": len(lookup_results),
                    "cost_units": 0,
                    "query_terms": sorted(self._keywords(latest_host_instruction))[:8],
                }
            )
            log["calls"] = calls
            log["tool_call_count"] = int(log["tool_call_count"]) + 1
            results.extend(lookup_results)
        log["result_count"] = len(results)
        return results, log

    def _allowed_discussion_tools(self, policy_id: str) -> set[str]:
        if policy_id in {"evidence_pack_lookup", "evidence_lookup"}:
            return {"evidence_pack_lookup"}
        if policy_id in {"source_grounded_tools", "research_tools"}:
            return {"evidence_pack_lookup"}
        return set()

    def _evidence_pack_lookup_results(
        self,
        episode: Episode,
        query: str,
        max_results: int,
    ) -> list[dict]:
        pack = self._latest_evidence_pack(episode)
        if not pack:
            return []
        source_by_id = {
            source.get("id"): source
            for source in pack.get("source_index", [])
            if isinstance(source, dict) and isinstance(source.get("id"), str)
        }
        query_terms = self._keywords(query)
        candidates: list[tuple[float, dict]] = []
        for section in (
            "verified_facts",
            "supported_claims",
            "uncertain_claims",
            "disputed_claims",
        ):
            claims = pack.get(section, [])
            if not isinstance(claims, list):
                continue
            for claim in claims:
                if not isinstance(claim, dict) or not isinstance(claim.get("text"), str):
                    continue
                claim_terms = self._keywords(claim["text"])
                refs = [ref for ref in claim.get("evidence_refs", []) if ref in source_by_id]
                score = float(len(query_terms & claim_terms)) + (0.25 if refs else 0.0)
                if score <= 0 and candidates:
                    continue
                candidates.append(
                    (
                        score,
                        {
                            "tool_name": "evidence_pack_lookup",
                            "section": section,
                            "claim_text": claim["text"][:500],
                            "claim_type": claim.get("claim_type") or section,
                            "confidence": claim.get("confidence"),
                            "evidence_refs": refs[:4],
                            "sources": [
                                {
                                    "id": ref,
                                    "title": source_by_id[ref].get("title"),
                                    "uri": source_by_id[ref].get("uri"),
                                    "source_type": source_by_id[ref].get("source_type"),
                                }
                                for ref in refs[:4]
                            ],
                        },
                    )
                )
        candidates.sort(key=lambda item: (-item[0], item[1]["section"], item[1]["claim_text"]))
        return [item for _score, item in candidates[:max_results]]

    def _latest_evidence_pack(self, episode: Episode) -> dict | None:
        asset = next(
            (
                item
                for item in reversed(episode.assets)
                if item.asset_type == AssetType.evidence_pack and item.status == "completed"
            ),
            None,
        )
        if asset is None:
            return None
        metadata_pack = asset.generation_metadata.get("evidence_pack")
        if isinstance(metadata_pack, dict):
            return metadata_pack
        if asset.storage_uri is None:
            return None
        path: Path | None = None
        if asset.storage_uri.startswith(f"object://{self.settings.object_storage_bucket}/"):
            key = asset.storage_uri.removeprefix(f"object://{self.settings.object_storage_bucket}/")
            path = (
                Path(self.settings.object_storage_local_path).expanduser()
                / self.settings.object_storage_bucket
                / unquote(key)
            )
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _opening_instruction(self, episode: Episode) -> str:
        opening = episode.definition.media.opening
        if not opening.enabled:
            return "Open the discussion with the central question."
        moderator = next(
            participant
            for participant in self._active_participants(episode)
            if participant.participant_type == ParticipantType.host
        )
        introductions = []
        introduce_participants = opening.post_primer_bridge.introduce_participants
        if introduce_participants is None:
            introduce_participants = opening.introduce_participants
        if introduce_participants:
            introductions = [
                (
                    f"{participant.display_name}: {participant.expertise}; "
                    f"perspective {participant.perspective}"
                )
                for participant in self._active_participants(episode)
                if participant.id != moderator.id
            ]
        source_references = [
            reference.strip()
            for reference in opening.source_references
            if reference.strip()
        ]
        parts = [
            "Create an evidence-led topic primer before the panel discussion, not a panel teaser.",
            "Onboard a general audience: explain what the topic is, the verified current context, "
            "and why the central question matters before anyone takes a position.",
            "Use only evidence-pack sources for factual framing. Attribute source material when "
            "relevant, never invent citations, and do not resolve the panel's debate yourself.",
            "Assume the primer will play over source visuals such as official material, credited "
            "reporting, screenshots, or licensed footage supplied by the producer.",
        ]
        if opening.narration_brief.strip():
            parts.append(f"Producer narration brief: {opening.narration_brief.strip()}")
        if source_references:
            parts.append(
                "Producer visual/source references (attribute them when relevant): "
                + "; ".join(source_references[:6])
            )
        if introductions:
            parts.append(
                "The post-primer host bridge should introduce these participants concisely before "
                "inviting their opening positions: "
                + " | ".join(introductions)
            )
        return "\n".join(parts)

    def _post_primer_bridge_seed_instruction(self, episode: Episode) -> str:
        return (
            "The separately produced topic primer has already completed and played immediately "
            "before this turn. You are now the host's first live studio voice. Continue from the "
            "primer into the panel discussion; never write, describe, or replace the primer itself."
        )

    def _post_primer_bridge_context(self, episode: Episode) -> str:
        production_state = episode.workflow_control.get("primer_production")
        script = (
            str(production_state.get("script") or "")
            if isinstance(production_state, dict)
            else ""
        )
        primer_closing_line = self._final_spoken_sentence(script)
        if not primer_closing_line:
            primer_closing_line = episode.definition.media.opening.narration_brief.strip()

        guests = [
            (
                participant.display_name.strip(),
                self._post_primer_guest_bio_source(participant),
            )
            for participant in self._active_participants(episode)
            if participant.participant_type != ParticipantType.host
            and participant.display_name.strip()
        ]
        continuity = [
            " Bridge continuity context:",
            (
                "The last line the viewer heard in the topic primer was: "
                f'"{primer_closing_line}"'
                if primer_closing_line
                else "The topic primer has ended; continue directly from its central question."
            ),
            "Treat that as immediately preceding this turn. Do not quote it, restate its facts, "
            "or restart with a generic welcome.",
        ]
        if guests:
            continuity.append(
                "Locked on-screen guest roster, in the required naming order, with the only "
                "permitted bio source for each guest: "
                + " | ".join(f"{name}: {bio}" for name, bio in guests)
                + ". These are the only people who may be introduced or addressed by name."
            )
        else:
            continuity.append(
                "There is no confirmed guest roster. Do not invent or introduce any guests."
            )
        return " ".join(continuity)

    @staticmethod
    def _post_primer_guest_bio_source(participant: ParticipantProfile) -> str:
        perspective = participant.perspective.strip()
        expertise = participant.expertise.strip()
        source = "; ".join(part for part in (perspective, expertise) if part)
        return source[:600] or "configured panel perspective"

    @staticmethod
    def _final_spoken_sentence(script: str) -> str:
        sentences = re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", script.strip())
        return sentences[-1].strip() if sentences else ""

    def _turn_plan(
        self, episode: Episode
    ) -> list[tuple[int, ParticipantProfile, TurnType, str]]:
        participants = self._active_participants(episode)
        host = next(p for p in participants if p.participant_type == ParticipantType.host)
        panelists = [p for p in participants if p.participant_type != ParticipantType.host]
        opening_turn_type = self._initial_host_turn_type(episode)
        plan: list[tuple[int, ParticipantProfile, TurnType, str]] = [
            (1, host, opening_turn_type, opening_turn_type.value)
        ]
        seq = 2
        for participant in panelists:
            plan.append((seq, participant, TurnType.opening_position, "opening_positions"))
            seq += 1
        plan.append((seq, host, TurnType.question, "main_discussion"))
        seq += 1
        for participant in self._balanced_order(panelists):
            plan.append((seq, participant, TurnType.rebuttal, "main_discussion"))
            seq += 1
        plan.append((seq, host, TurnType.question, "focused_challenge"))
        seq += 1
        for participant in reversed(panelists):
            plan.append((seq, participant, TurnType.closing_statement, "closing_statements"))
            seq += 1
        plan.append((seq, host, TurnType.host_synthesis, "host_synthesis"))
        return plan

    def _balanced_order(self, participants: list[ParticipantProfile]) -> list[ParticipantProfile]:
        return sorted(
            participants,
            key=lambda p: (p.participant_type != ParticipantType.fact_checker, p.id),
        )

    def _next_turn_decision(
        self,
        episode: Episode,
        session: DiscussionSession,
        latest_host_instruction: str,
        remaining_seconds: float,
    ) -> tuple[ParticipantProfile, TurnType, str, dict] | None:
        participants = self._active_participants(episode)
        discussion_intensity = self._discussion_intensity(episode)
        host = next(p for p in participants if p.participant_type == ParticipantType.host)
        panelists = [p for p in participants if p.participant_type != ParticipantType.host]
        sequence_number = len(session.turns) + 1
        opening_count = sum(
            1 for turn in session.turns if turn.turn_type == TurnType.opening_position
        )
        if sequence_number == 1:
            opening_turn_type = self._initial_host_turn_type(episode)
            return (
                host,
                opening_turn_type,
                opening_turn_type.value,
                self._fixed_speaker_selection(
                    sequence_number,
                    host,
                    "host opens the discussion",
                    self._minimum_remaining_cast_turns_after_selection(
                        participants,
                        session,
                        host.id,
                        discussion_intensity=discussion_intensity,
                    ),
                ),
            )
        if opening_count < len(panelists):
            participant = panelists[opening_count]
            return (
                participant,
                TurnType.opening_position,
                "opening_positions",
                self._fixed_speaker_selection(
                    sequence_number,
                    participant,
                    "each participant gives one opening position",
                    self._minimum_remaining_cast_turns_after_selection(
                        participants,
                        session,
                        participant.id,
                        discussion_intensity=discussion_intensity,
                    ),
                ),
            )

        host_question_count = sum(
            1 for turn in session.turns if turn.turn_type == TurnType.question
        )
        dynamic_rebuttal_count = sum(
            1
            for turn in session.turns
            if turn.turn_type in {TurnType.rebuttal, TurnType.clarification}
        )
        closing_speakers = {
            turn.speaker_participant_id
            for turn in session.turns
            if turn.turn_type == TurnType.closing_statement
        }
        if host_question_count == 0:
            return (
                host,
                TurnType.question,
                "main_discussion",
                self._fixed_speaker_selection(
                    sequence_number,
                    host,
                    "host sets up the main discussion after opening positions",
                    self._minimum_remaining_cast_turns_after_selection(
                        participants,
                        session,
                        host.id,
                        discussion_intensity=discussion_intensity,
                    ),
                ),
            )
        if dynamic_rebuttal_count < len(panelists):
            minimum_cast_turns = self._minimum_remaining_cast_turns_after_selection(
                participants,
                session,
                None,
                discussion_intensity=discussion_intensity,
            )
            return self._score_speaker_selection(
                episode,
                session,
                panelists,
                TurnType.rebuttal,
                "main_discussion",
                latest_host_instruction,
                remaining_seconds,
                sequence_number,
                minimum_remaining_turns_after_selection=minimum_cast_turns,
            )
        if host_question_count == 1:
            return (
                host,
                TurnType.question,
                "focused_challenge",
                self._fixed_speaker_selection(
                    sequence_number,
                    host,
                    "host focuses unresolved disagreements before closing",
                    self._minimum_remaining_cast_turns_after_selection(
                        participants,
                        session,
                        host.id,
                        discussion_intensity=discussion_intensity,
                    ),
                ),
            )
        if (
            discussion_intensity == "high"
            and self._cross_examination_turn_count(session) < 2
        ):
            minimum_cast_turns = self._minimum_remaining_cast_turns_after_selection(
                participants,
                session,
                None,
                discussion_intensity=discussion_intensity,
                cross_examination_turn_selected=True,
            )
            participant, selected_turn_type, selected_phase, selection = (
                self._score_speaker_selection(
                    episode,
                    session,
                    panelists,
                    TurnType.clarification,
                    "cross_examination",
                    latest_host_instruction,
                    remaining_seconds,
                    sequence_number,
                    minimum_remaining_turns_after_selection=minimum_cast_turns,
                )
            )
            selection["selection_reason"] = (
                "highest deterministic controller score for a direct cross-examination reply"
            )
            return participant, selected_turn_type, selected_phase, selection
        closing_candidates = [
            participant for participant in panelists if participant.id not in closing_speakers
        ]
        if closing_candidates:
            minimum_closing_turns = self._minimum_remaining_cast_turns_after_selection(
                participants,
                session,
                None,
                discussion_intensity=discussion_intensity,
            )
            return self._score_speaker_selection(
                episode,
                session,
                closing_candidates,
                TurnType.closing_statement,
                "closing_statements",
                latest_host_instruction,
                remaining_seconds,
                sequence_number,
                minimum_remaining_turns_after_selection=minimum_closing_turns,
            )
        if not any(turn.turn_type == TurnType.host_synthesis for turn in session.turns):
            return (
                host,
                TurnType.host_synthesis,
                "host_synthesis",
                self._fixed_speaker_selection(
                    sequence_number,
                    host,
                    "host closes with agreements, disagreements, and uncertainty",
                    0,
                ),
            )
        return None

    def _minimum_remaining_cast_turns_after_selection(
        self,
        participants: list[ParticipantProfile],
        session: DiscussionSession,
        selected_participant_id: str | None,
        *,
        discussion_intensity: str = "medium",
        cross_examination_turn_selected: bool = False,
    ) -> int:
        """Return the remaining mandatory turns after the current selection.

        The original reservation only made sure that every participant spoke once.
        That allowed long early turns to consume the time needed for the focused
        challenge, closing positions, and the host synthesis. These are structural
        obligations of a panel discussion, independent of its subject.
        """
        panelist_count = sum(
            1
            for participant in participants
            if participant.participant_type != ParticipantType.host
        )
        opening_count = sum(
            1 for turn in session.turns if turn.turn_type == TurnType.opening_position
        )
        host_question_count = sum(
            1 for turn in session.turns if turn.turn_type == TurnType.question
        )
        dynamic_rebuttal_count = sum(
            1
            for turn in session.turns
            if turn.turn_type in {TurnType.rebuttal, TurnType.clarification}
        )
        closing_count = sum(
            1 for turn in session.turns if turn.turn_type == TurnType.closing_statement
        )
        host_synthesis_count = sum(
            1 for turn in session.turns if turn.turn_type == TurnType.host_synthesis
        )
        cross_examination_count = self._cross_examination_turn_count(session)
        required_cross_examination_turns = 2 if discussion_intensity == "high" else 0

        selected_is_host = selected_participant_id and any(
            participant.id == selected_participant_id
            and participant.participant_type == ParticipantType.host
            for participant in participants
        )
        opening_after = opening_count + (0 if selected_is_host else 1)
        host_questions_after = host_question_count + (1 if selected_is_host else 0)
        rebuttals_after = dynamic_rebuttal_count + (0 if selected_is_host else 1)
        closings_after = closing_count + (0 if selected_is_host else 1)
        cross_examinations_after = cross_examination_count + int(
            cross_examination_turn_selected
        )

        if opening_count < panelist_count:
            return (
                max(0, panelist_count - opening_after)
                + 1  # first host question
                + panelist_count  # main-discussion responses
                + 1  # focused host question
                + required_cross_examination_turns  # direct replies at high intensity
                + panelist_count  # closing positions
                + 1  # host synthesis
            )
        if host_question_count == 0:
            return (
                panelist_count
                + 1
                + required_cross_examination_turns
                + panelist_count
                + 1
            )
        if dynamic_rebuttal_count < panelist_count:
            return (
                max(0, panelist_count - rebuttals_after)
                + (1 if host_questions_after < 2 else 0)
                + max(0, required_cross_examination_turns - cross_examinations_after)
                + panelist_count
                + 1
            )
        if host_question_count == 1:
            return required_cross_examination_turns + panelist_count + 1
        if cross_examination_count < required_cross_examination_turns:
            return (
                max(0, required_cross_examination_turns - cross_examinations_after)
                + panelist_count
                + 1
            )
        if closing_count < panelist_count:
            return max(0, panelist_count - closings_after) + 1
        return max(0, 1 - host_synthesis_count)

    def _fixed_speaker_selection(
        self,
        sequence_number: int,
        participant: ParticipantProfile,
        reason: str,
        minimum_remaining_turns_after_selection: int,
    ) -> dict:
        return {
            "schema_version": "speaker_selection.v1",
            "policy": "deterministic_discussion_controller_v1",
            "sequence_number": sequence_number,
            "selected_participant_id": participant.id,
            "selection_reason": reason,
            "minimum_remaining_turns_after_selection": minimum_remaining_turns_after_selection,
            "candidate_scores": [
                {
                    "participant_id": participant.id,
                    "score": 0.0,
                    "score_components": {"fixed_role_turn": 0.0},
                }
            ],
            "addressed_question_ids": [],
        }

    def _score_speaker_selection(
        self,
        episode: Episode,
        session: DiscussionSession,
        candidates: list[ParticipantProfile],
        turn_type: TurnType,
        phase: str,
        latest_host_instruction: str,
        remaining_seconds: float,
        sequence_number: int,
        minimum_remaining_turns_after_selection: int,
    ) -> tuple[ParticipantProfile, TurnType, str, dict]:
        open_questions = self._open_controller_questions(session)
        last_speaker_id = session.turns[-1].speaker_participant_id if session.turns else None
        max_turns = max(
            (
                session.speaker_balance_state.get(candidate.id, SpeakerBalance()).total_turns
                for candidate in candidates
            ),
            default=0,
        )
        scored: list[tuple[float, ParticipantProfile, dict]] = []
        for candidate in candidates:
            balance = session.speaker_balance_state.get(candidate.id, SpeakerBalance())
            addressed_questions = self._questions_addressed_to(open_questions, candidate.id)
            components = {
                "relevance_to_current_question": self._keyword_overlap_score(
                    latest_host_instruction,
                    [candidate.perspective, candidate.expertise, phase],
                ),
                "unresolved_disagreement_weight": self._unresolved_disagreement_score(
                    session,
                    candidate.id,
                ),
                "requested_response_weight": 4.0 if addressed_questions else 0.0,
                "expertise_match": self._keyword_overlap_score(
                    " ".join(episode.definition.topic.required_dimensions),
                    [candidate.expertise, candidate.perspective],
                ),
                "underrepresented_speaker_bonus": max(0.0, float(max_turns - balance.total_turns)),
                "recent_speaking_penalty": -3.0 if candidate.id == last_speaker_id else 0.0,
                "repetition_risk": self._repetition_penalty(session, candidate.id),
                "duration_overrun_risk": self._duration_overrun_penalty(
                    episode,
                    remaining_seconds,
                    minimum_remaining_turns_after_selection,
                ),
            }
            score = round(sum(components.values()), 4)
            scored.append(
                (
                    score,
                    candidate,
                    {
                        "participant_id": candidate.id,
                        "score": score,
                        "score_components": components,
                        "addressed_question_ids": [
                            str(item.get("question_id")) for item in addressed_questions
                        ],
                    },
                )
            )
        scored.sort(
            key=lambda item: (
                -item[0],
                item[1].participant_type != ParticipantType.fact_checker,
                item[1].id,
            )
        )
        selected = scored[0][1]
        selected_entry = scored[0][2]
        selection = {
            "schema_version": "speaker_selection.v1",
            "policy": "deterministic_discussion_controller_v1",
            "sequence_number": sequence_number,
            "phase": phase,
            "turn_type": turn_type.value,
            "selected_participant_id": selected.id,
            "selection_reason": "highest deterministic controller score",
            "minimum_remaining_turns_after_selection": minimum_remaining_turns_after_selection,
            "candidate_scores": [entry for _, _, entry in scored],
            "addressed_question_ids": selected_entry["addressed_question_ids"],
        }
        return selected, turn_type, phase, selection

    def _questions_addressed_to(
        self,
        questions: list[dict],
        participant_id: str,
    ) -> list[dict]:
        return [
            question
            for question in questions
            if str(question.get("participant_id") or "").lower()
            in {participant_id.lower(), "all", "panel", "*"}
        ]

    def _open_controller_questions(self, session: DiscussionSession) -> list[dict]:
        questions = session.controller_state.get("pending_questions", [])
        if not isinstance(questions, list):
            return []
        return [
            question
            for question in questions
            if isinstance(question, dict) and question.get("status", "open") == "open"
        ]

    def _keyword_overlap_score(self, text: str, fields: list[str]) -> float:
        haystack = self._keywords(" ".join(fields))
        if not haystack:
            return 0.0
        needles = self._keywords(text)
        if not needles:
            return 0.0
        return float(len(haystack & needles)) / max(1.0, float(len(needles)))

    def _keywords(self, value: str) -> set[str]:
        tokens = []
        normalized = self._normalize_keyword_text(value)
        for token in (
            normalized.replace("/", " ").replace("-", " ").replace("_", " ").split()
        ):
            cleaned = "".join(character for character in token if character.isalnum())
            if len(cleaned) >= 4:
                tokens.append(cleaned)
        return set(tokens)

    def _normalize_keyword_text(self, value: str) -> str:
        transliterated = (
            value.lower()
            .replace("ä", "ae")
            .replace("ö", "oe")
            .replace("ü", "ue")
            .replace("ß", "ss")
        )
        return "".join(
            character
            for character in unicodedata.normalize("NFKD", transliterated)
            if not unicodedata.combining(character)
        )

    def _coverage_guidance(
        self,
        episode: Episode,
        session: DiscussionSession,
        phase: str,
    ) -> dict:
        required_dimensions = list(episode.definition.topic.required_dimensions)
        uncovered = [
            dimension
            for dimension in required_dimensions
            if not session.coverage_state.get(dimension)
        ]
        if not uncovered:
            return {
                "schema_version": "discussion_coverage_guidance.v1",
                "phase": phase,
                "required_dimensions": required_dimensions,
                "uncovered_dimensions_before_turn": [],
                "instruction": None,
            }
        instruction = (
            "Coverage requirement: explicitly address the still-uncovered discussion "
            f"dimension(s): {', '.join(uncovered)}. Use the exact dimension label or "
            "a clear synonym in the spoken answer or in a structured claim, and keep "
            "the point tied to the available evidence or clearly marked opinion."
        )
        return {
            "schema_version": "discussion_coverage_guidance.v1",
            "phase": phase,
            "required_dimensions": required_dimensions,
            "uncovered_dimensions_before_turn": uncovered,
            "instruction": instruction,
        }

    def _instruction_with_coverage_guidance(self, instruction: str, guidance: dict) -> str:
        coverage_instruction = guidance.get("instruction")
        if not coverage_instruction:
            return instruction
        return f"{instruction}\n\n{coverage_instruction}"

    def _turn_contract(
        self,
        episode: Episode,
        session: DiscussionSession,
        participant: ParticipantProfile,
        turn_type: TurnType,
        phase: str,
        selection: dict,
        allowed_seconds: float,
    ) -> dict:
        """Build a compact, subject-neutral brief for one turn.

        A host instruction alone is not sufficiently precise once the controller
        selects a participant to answer a particular question. The contract gives
        each model a clear contribution shape, recipient-specific question text,
        and a generation-time word budget, avoiding lossy post-generation cuts.
        """
        effective_words_per_second = (
            self.settings.words_per_second * self.settings.discussion_duration_audio_safety_factor
        )
        max_words = max(8, int(allowed_seconds * effective_words_per_second * 0.82))
        question_ids = {str(item) for item in selection.get("addressed_question_ids", [])}
        addressed_questions = [
            question
            for question in self._open_controller_questions(session)
            if str(question.get("question_id")) in question_ids
        ]
        bridge = episode.definition.media.opening.post_primer_bridge
        bridge_target_seconds = self._post_primer_bridge_target_seconds(episode)
        bridge_brief = bridge.editorial_brief.strip()
        instructions = {
            TurnType.host_opening: (
                "Open the panel discussion. The separately produced primer has already supplied "
                "the factual orientation, so do not repeat it. State the central tension in one "
                "sentence and invite the first participant to take a position."
            ),
            TurnType.post_primer_bridge: (
                "The topic primer has just ended and this is the first live studio voice. "
                "Deliver a fluent spoken hand-off into the panel, not a new standalone opening. "
                "Acknowledge the central tension without recapping the primer, then open the "
                "discussion with one specific question. Keep this to a natural hand-off of about "
                f"{bridge_target_seconds} seconds; do not narrate source evidence, repeat the "
                "primer, or answer for the panel."
            ),
            TurnType.opening_position: (
                "State one provisional position and one criterion that could change it. Add a "
                "concrete distinction; do not list every dimension or summarize the whole topic."
            ),
            TurnType.question: (
                "Connect two prior positions, expose the most decision-relevant disagreement, and "
                "ask one concise, answerable question to a named participant or the panel. "
                "Do not recap."
            ),
            TurnType.rebuttal: (
                "Advance the discussion by answering the active challenge, referring to a concrete "
                "prior position, and adding either a trade-off, counterexample, criterion, or "
                "revision. "
                "Do not restart the topic or merely restate an opening position."
            ),
            TurnType.clarification: (
                "Answer the point at issue directly before extending it. Name the claim you "
                "accept, reject, or revise; distinguish evidence from judgment; and say what "
                "would resolve the remaining uncertainty."
            ),
            TurnType.closing_statement: (
                "Give a final conditional recommendation: state the strongest conclusion, "
                "the condition under which it holds, and the main unresolved trade-off. "
                "Do not repeat the full debate."
            ),
            TurnType.host_synthesis: (
                "Close the discussion by naming the strongest agreement, the central unresolved "
                "disagreement, and a practical next step. Do not introduce a new argument."
            ),
        }
        if turn_type == TurnType.post_primer_bridge and bridge_brief:
            instructions[turn_type] += f" Producer bridge brief: {bridge_brief}"
        if turn_type == TurnType.post_primer_bridge:
            instructions[turn_type] += self._post_primer_bridge_context(episode)
        introduce_participants = bridge.introduce_participants
        if introduce_participants is None:
            introduce_participants = episode.definition.media.opening.introduce_participants
        if turn_type == TurnType.post_primer_bridge and introduce_participants:
            instructions[turn_type] += (
                " Use exactly four short spoken sentences in this order: (1) a direct verbal "
                "continuation from the primer into its central tension, (2) an introduction for "
                "the first part of the guest roster, (3) an introduction for the remaining guests, "
                "and (4) one open opening question to the panel. Name every guest in the locked "
                "roster exactly once. Give every guest a distinct on-air bio of three to seven "
                "words, faithfully condensed from that guest's permitted bio source. Do not add "
                "titles, real-world identity claims, or any person not in the locked roster."
            )
        return {
            "schema_version": "discussion_turn_contract.v1",
            "phase": phase,
            "turn_type": turn_type.value,
            "participant_id": participant.id,
            "source_language": episode.source_language,
            "discussion_intensity": self._discussion_intensity(episode),
            "introduce_participants": bool(
                turn_type == TurnType.post_primer_bridge and introduce_participants
            ),
            "allowed_seconds": round(allowed_seconds, 2),
            "max_words": max_words,
            "contribution_instruction": instructions[turn_type],
            "addressed_questions": [
                {
                    "question_id": str(question.get("question_id")),
                    "source_turn_id": str(question.get("source_turn_id")),
                    "source_participant_id": question.get("source_participant_id"),
                    "question": str(question.get("question")),
                }
                for question in addressed_questions
            ],
        }

    def _instruction_with_turn_contract(self, instruction: str, contract: dict) -> str:
        intensity = str(contract.get("discussion_intensity") or "medium")
        delivery = (
            "Use exactly four short, natural spoken sentences, following the required bridge "
            "structure."
            if contract.get("turn_type") == TurnType.post_primer_bridge.value
            and contract.get("introduce_participants")
            else (
            "Use one to four short, natural spoken sentences. At high intensity, be direct "
            "and expressive while staying civil: challenge a claim, not a person."
            if intensity == "high"
            else "Use one to three short, natural spoken sentences."
            )
        )
        lines = [
            "Turn contract:",
            str(contract["contribution_instruction"]),
            (
                "Write only the spoken contribution in no more than "
                f"{contract['max_words']} words. {delivery} Finish the thought naturally "
                "within that budget; do not use headings or bullet points."
            ),
            (
                "Write the spoken contribution entirely in the episode source language "
                f"({contract['source_language']}); retain a foreign technical term only when "
                "there is no natural equivalent."
            ),
        ]
        addressed_questions = contract.get("addressed_questions", [])
        if addressed_questions:
            question = addressed_questions[0]
            lines.append(
                "You were selected to answer this direct question before adding any extension: "
                f"{question['question']}"
            )
            lines.append(
                "Set responding_to to this source turn ID: "
                f"{question['source_turn_id']}."
            )
        return f"{instruction}\n\n" + "\n".join(lines)

    def _unresolved_disagreement_score(
        self,
        session: DiscussionSession,
        participant_id: str,
    ) -> float:
        memory = session.memories.get(participant_id)
        score = 0.0
        if memory and memory.open_questions:
            score += 1.5
        if session.controller_state.get("unresolved_disagreement_count"):
            score += 1.0
        return score

    def _repetition_penalty(self, session: DiscussionSession, participant_id: str) -> float:
        balance = session.speaker_balance_state.get(participant_id)
        if balance is None or balance.total_turns <= 1:
            return 0.0
        return -0.5 * float(balance.total_turns - 1)

    def _duration_overrun_penalty(
        self,
        episode: Episode,
        remaining_seconds: float,
        minimum_remaining_turns_after_selection: int,
    ) -> float:
        required_seconds = (
            minimum_remaining_turns_after_selection
            * min(episode.definition.format.maximum_monologue_seconds, 20)
        )
        if remaining_seconds >= required_seconds:
            return 0.0
        return -2.0

    def _update_controller_state(
        self,
        session: DiscussionSession,
        participant_id: str,
        turn: DiscussionTurn,
        selection: dict,
        tool_log: dict | None = None,
    ) -> None:
        state = dict(session.controller_state or {})
        pending_questions = list(state.get("pending_questions", []))
        addressed_question_ids = set(selection.get("addressed_question_ids", []))
        for question in pending_questions:
            if str(question.get("question_id")) in addressed_question_ids:
                question["status"] = "answered"
                question["answered_by_participant_id"] = participant_id
                question["answered_turn_id"] = str(turn.id)
        for item in turn.structured_output.questions_for_others:
            target_id = item.get("participant_id")
            question_text = item.get("question")
            if not target_id or not question_text:
                continue
            pending_questions.append(
                {
                    "question_id": f"q-{turn.sequence_number}-{len(pending_questions) + 1}",
                    "source_turn_id": str(turn.id),
                    "source_participant_id": participant_id,
                    "participant_id": target_id,
                    "question": question_text,
                    "status": "open",
                }
            )
        state["pending_questions"] = pending_questions
        state["open_question_count"] = sum(
            1 for question in pending_questions if question.get("status") == "open"
        )
        state["answered_question_count"] = sum(
            1 for question in pending_questions if question.get("status") == "answered"
        )
        state["last_selected_participant_id"] = participant_id
        state["last_selection_policy"] = selection.get("policy")
        state["last_candidate_scores"] = selection.get("candidate_scores", [])
        state["unresolved_disagreement_count"] = self._unresolved_disagreement_count(session)
        session.controller_state = state
        if tool_log is not None:
            self._append_tool_log_to_controller_state(session, tool_log)

    def _append_tool_log_to_controller_state(
        self,
        session: DiscussionSession,
        tool_log: dict,
    ) -> None:
        state = dict(session.controller_state or {})
        logs = list(state.get("tool_usage_log", []))
        logs.append(tool_log)
        state["tool_usage_log"] = logs[-50:]
        state["tool_call_count"] = sum(int(item.get("tool_call_count") or 0) for item in logs)
        state["tool_result_count"] = sum(int(item.get("result_count") or 0) for item in logs)
        session.controller_state = state

    def _unresolved_disagreement_count(self, session: DiscussionSession) -> int:
        return sum(
            1
            for turn in session.turns
            if turn.status != "excluded"
            and turn.intent in {"rebuttal", "challenge", "disagreement"}
        )

    def _responding_to_turn_id(
        self,
        session: DiscussionSession,
        responding_to: str | None,
    ) -> UUID | None:
        if responding_to is None:
            return None
        for turn in session.turns:
            if str(turn.id) == responding_to:
                return turn.id
            if str(turn.sequence_number) == responding_to:
                return turn.id
        return None

    def _responding_to_turn_id_or_contract_question(
        self,
        session: DiscussionSession,
        responding_to: str | None,
        contract: dict,
        fallback_target_turn_id: str | None = None,
    ) -> UUID | None:
        resolved = self._responding_to_turn_id(session, responding_to)
        if resolved is not None:
            return resolved
        addressed_questions = contract.get("addressed_questions", [])
        if not addressed_questions:
            return self._responding_to_turn_id(session, fallback_target_turn_id)
        source_turn_id = addressed_questions[0].get("source_turn_id")
        return self._responding_to_turn_id(
            session, str(source_turn_id)
        ) or self._responding_to_turn_id(
            session,
            fallback_target_turn_id,
        )

    def _discussion_intensity(self, episode: Episode) -> str:
        intensity = str(episode.definition.format.discussion_intensity or "medium").lower()
        return intensity if intensity in {"low", "medium", "high"} else "medium"

    def _cross_examination_turn_count(self, session: DiscussionSession) -> int:
        return sum(
            1
            for turn in session.turns
            if turn.status != "excluded"
            and turn.generation_metadata.get("interaction", {}).get("mode")
            == "cross_examination"
        )

    def _interaction_metadata(
        self,
        session: DiscussionSession,
        participant: ParticipantProfile,
        turn_type: TurnType,
        phase: str,
        selection: dict,
        contract: dict,
    ) -> dict:
        addressed_questions = contract.get("addressed_questions", [])
        target_turn_ids = [
            str(question.get("source_turn_id"))
            for question in addressed_questions
            if question.get("source_turn_id")
        ]
        target_participant_ids = [
            str(question.get("source_participant_id"))
            for question in addressed_questions
            if question.get("source_participant_id")
        ]
        if turn_type in {TurnType.rebuttal, TurnType.clarification} and not target_turn_ids:
            prior_turn = next(
                (
                    turn
                    for turn in reversed(session.turns)
                    if turn.status != "excluded" and turn.speaker_participant_id != participant.id
                ),
                None,
            )
            if prior_turn is not None:
                target_turn_ids = [str(prior_turn.id)]
                target_participant_ids = [prior_turn.speaker_participant_id]
        if phase == "cross_examination":
            mode = "cross_examination"
        elif turn_type == TurnType.question:
            mode = "moderator_challenge"
        elif turn_type in {TurnType.rebuttal, TurnType.clarification}:
            mode = "responsive_argument"
        elif turn_type == TurnType.opening_position:
            mode = "opening_position"
        else:
            mode = "transition"
        return {
            "schema_version": "discussion_interaction.v1",
            "mode": mode,
            "phase": phase,
            "discussion_intensity": contract.get("discussion_intensity", "medium"),
            "target_turn_ids": target_turn_ids,
            "target_participant_ids": target_participant_ids,
            "selection_addressed_question_ids": list(selection.get("addressed_question_ids", [])),
        }

    def _estimate_duration(self, text: str) -> float:
        effective_words_per_second = (
            self.settings.words_per_second
            * self.settings.discussion_duration_audio_safety_factor
        )
        return round(len(text.split()) / effective_words_per_second, 2)

    def _turn_duration_allowance(
        self,
        episode: Episode,
        session: DiscussionSession,
        remaining_turns: int,
    ) -> float:
        remaining = max(episode.maximum_duration_seconds - session.estimated_duration_seconds, 0)
        if remaining_turns <= 1:
            return min(episode.definition.format.maximum_monologue_seconds, remaining)
        balanced_allowance = remaining / remaining_turns
        return min(episode.definition.format.maximum_monologue_seconds, balanced_allowance)

    def _initial_host_turn_type(self, episode: Episode) -> TurnType:
        return (
            TurnType.post_primer_bridge
            if episode.definition.media.opening.enabled
            else TurnType.host_opening
        )

    def _post_primer_bridge_duration_allowance(
        self,
        episode: Episode,
        remaining_seconds: float,
    ) -> float:
        target = self._post_primer_bridge_target_seconds(episode)
        return min(
            episode.definition.format.maximum_monologue_seconds,
            remaining_seconds,
            float(target),
        )

    def _post_primer_bridge_target_seconds(self, episode: Episode) -> int:
        bridge = episode.definition.media.opening.post_primer_bridge
        target = bridge.target_duration_seconds
        introduce_participants = bridge.introduce_participants
        if introduce_participants is None:
            introduce_participants = episode.definition.media.opening.introduce_participants
        if not introduce_participants:
            return target
        guest_count = sum(
            participant.participant_type != ParticipantType.host
            for participant in self._active_participants(episode)
        )
        biography_target = 15 + (4 * guest_count)
        return min(45, max(target, biography_target))

    def _minimum_turn_duration_seconds(self) -> float:
        effective_words_per_second = (
            self.settings.words_per_second
            * self.settings.discussion_duration_audio_safety_factor
        )
        return 1 / effective_words_per_second

    def _apply_duration_controls(
        self,
        structured,
        allowed_seconds: float,
        maximum_monologue_seconds: int,
    ):
        original_text = structured.spoken_text
        original_estimated_seconds = self._estimate_duration(original_text)
        effective_limit = max(1.0, min(allowed_seconds, maximum_monologue_seconds))
        if original_estimated_seconds <= effective_limit:
            return structured, {}, "accepted"

        effective_words_per_second = (
            self.settings.words_per_second
            * self.settings.discussion_duration_audio_safety_factor
        )
        max_words = max(1, int(effective_limit * effective_words_per_second))
        words = original_text.split()
        bounded_words = words[:max_words]
        sentence_end_index = max(
            (
                index
                for index, word in enumerate(bounded_words)
                if word.rstrip("\\\"')]}»").endswith((".", "!", "?"))
            ),
            default=-1,
        )
        if sentence_end_index >= max(3, int(max_words * 0.45)):
            adjusted_text = " ".join(bounded_words[: sentence_end_index + 1])
            strategy = "complete_sentence_boundary"
        else:
            adjusted_text = " ".join(bounded_words).rstrip(",;:")
            if adjusted_text[-1:] not in ".!?":
                adjusted_text = f"{adjusted_text}."
            strategy = "word_boundary_fallback"
        adjusted = structured.model_copy(update={"spoken_text": adjusted_text})
        return (
            adjusted,
            {
                "duration_control": {
                    "applied": True,
                    "reason": "turn_duration_limit",
                    "allowed_seconds": round(effective_limit, 2),
                    "original_estimated_seconds": original_estimated_seconds,
                    "adjusted_estimated_seconds": self._estimate_duration(adjusted_text),
                    "maximum_monologue_seconds": maximum_monologue_seconds,
                    "truncation_strategy": strategy,
                }
            },
            "duration_adjusted",
        )

    def _endpoint_for(self, episode: Episode, endpoint_id: str):
        for endpoint in episode.model_endpoints:
            if endpoint.id == endpoint_id:
                return endpoint
        raise ValueError(f"unknown model endpoint {endpoint_id}")

    def _validate_participants(self, episode: Episode) -> None:
        enabled = self._active_participants(episode)
        hosts = [p for p in enabled if p.participant_type == ParticipantType.host]
        if len(hosts) != 1:
            raise ValueError("episode requires exactly one enabled host")
        if len(enabled) < 4:
            raise ValueError("episode requires one host and at least three enabled participants")
        endpoint_by_id = {endpoint.id: endpoint for endpoint in episode.model_endpoints}
        missing_model_ids = [
            participant.id for participant in enabled if not participant.model_id.strip()
        ]
        missing_endpoint_ids = [
            participant.id
            for participant in enabled
            if participant.model_endpoint_id not in endpoint_by_id
        ]
        disabled_endpoint_ids = [
            participant.id
            for participant in enabled
            if participant.model_endpoint_id in endpoint_by_id
            and endpoint_by_id[participant.model_endpoint_id].enabled is False
        ]
        if missing_model_ids or missing_endpoint_ids or disabled_endpoint_ids:
            raise ValueError(
                "episode participant model configuration is incomplete: "
                f"missing_model_ids={missing_model_ids}; "
                f"unknown_model_endpoint_participant_ids={missing_endpoint_ids}; "
                f"disabled_model_endpoint_participant_ids={disabled_endpoint_ids}"
            )

    def _active_participants(self, episode: Episode) -> list[ParticipantProfile]:
        return [participant for participant in episode.participants if participant.enabled]

    def _update_memory(self, session: DiscussionSession, participant_id: str, update) -> None:
        memory = session.memories[participant_id]
        if memory.discussion_session_id is None:
            memory.discussion_session_id = session.id
        memory.version += 1
        memory.unspoken_points = update.unspoken_points
        memory.open_questions = update.open_questions
        memory.position_summary = update.position_summary
        memory.updated_at = datetime.now(UTC)

    def _update_balance(
        self,
        session: DiscussionSession,
        participant_id: str,
        turn: DiscussionTurn,
        sequence_number: int,
    ) -> None:
        balance = session.speaker_balance_state[participant_id]
        balance.total_turns += 1
        balance.total_words += len(turn.spoken_text.split())
        balance.estimated_speaking_seconds += turn.estimated_duration_seconds
        balance.recency_of_last_turn = sequence_number

    def _refresh_session_state(self, episode: Episode) -> None:
        session = self._require_session(episode)
        session.estimated_duration_seconds = 0
        session.coverage_state = {
            dimension: False for dimension in episode.definition.topic.required_dimensions
        }
        session.speaker_balance_state = {
            profile.id: SpeakerBalance() for profile in self._active_participants(episode)
        }
        for turn in session.turns:
            if turn.status == "excluded":
                continue
            covered_dimensions = self._update_coverage_state(episode, session, turn)
            turn.generation_metadata = {
                **turn.generation_metadata,
                "coverage": {
                    "schema_version": "discussion_turn_coverage.v1",
                    "covered_dimensions": covered_dimensions,
                    "coverage_state_after_turn": dict(session.coverage_state),
                },
            }
            session.estimated_duration_seconds += turn.estimated_duration_seconds
            self._update_balance(
                session,
                turn.speaker_participant_id,
                turn,
                turn.sequence_number,
            )

    def _update_coverage_state(
        self,
        episode: Episode,
        session: DiscussionSession,
        turn: DiscussionTurn,
    ) -> list[str]:
        if turn.status == "excluded":
            return []
        if not session.coverage_state:
            session.coverage_state = {
                dimension: False for dimension in episode.definition.topic.required_dimensions
            }
        text_parts = [turn.spoken_text]
        text_parts.extend(claim.text for claim in turn.structured_output.claims)
        turn_keywords = self._keywords(" ".join(text_parts))
        covered: list[str] = []
        for dimension in episode.definition.topic.required_dimensions:
            dimension_keywords = self._keywords(dimension)
            if not dimension_keywords:
                continue
            if dimension_keywords <= turn_keywords or dimension_keywords & turn_keywords:
                session.coverage_state[dimension] = True
                covered.append(dimension)
        return covered

    def _create_transcript(
        self,
        episode: Episode,
        transcript_type: TranscriptType,
        parent_version_id=None,
    ) -> TranscriptVersion:
        assert episode.discussion_session is not None
        transcript = TranscriptVersion(
            episode_id=episode.id,
            type=transcript_type,
            language=episode.source_language,
            parent_version_id=parent_version_id,
            semantic_fidelity_score=1,
        )
        transcript.turns = [
            TranscriptTurn(
                transcript_version_id=transcript.id,
                source_discussion_turn_ids=[turn.id],
                speaker_participant_id=turn.speaker_participant_id,
                turn_type=turn.turn_type,
                text="" if turn.status == "excluded" else turn.spoken_text,
                edit_type=self._transcript_edit_type(turn),
                semantic_difference_score=0,
                claims=turn.structured_output.claims,
                status=("excluded" if turn.status == "excluded" else "pending_review"),
            )
            for turn in episode.discussion_session.turns
        ]
        return transcript

    def _transcript_edit_type(self, turn: DiscussionTurn) -> str:
        if turn.status == "excluded":
            return "excluded"
        if turn.status.startswith("regenerated"):
            return "regenerated"
        if turn.status == "manually_edited":
            return "manual_edit"
        if turn.status == "duration_adjusted":
            return "duration_adjusted"
        return "verbatim"

    def _append_broadcast_revision(
        self,
        episode: Episode,
        edit_reason: str,
        actor: str | None,
        details: dict,
    ) -> None:
        parent_version_id = episode.canonical_transcript_version_id
        transcript = self._create_transcript(
            episode,
            TranscriptType.broadcast,
            parent_version_id=parent_version_id,
        )
        episode.transcripts.append(transcript)
        episode.canonical_transcript_version_id = transcript.id
        episode.quality_results.append(self._transcript_semantic_qc(episode, transcript))
        self._ensure_pending_transcript_approval(episode)
        episode.status = EpisodeStatus.transcript_review
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="transcript.version.created",
                actor=actor or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "parent_version_id": str(parent_version_id) if parent_version_id else None,
                    "edit_reason": edit_reason,
                    **details,
                },
            )
        )

    def _ensure_pending_transcript_approval(self, episode: Episode) -> None:
        canonical_id = str(episode.canonical_transcript_version_id or "")
        if canonical_id:
            for approval in episode.approvals:
                if (
                    approval.stage == "transcript_review"
                    and approval.decision == "pending"
                ):
                    # Legacy revisions did not bind approvals to a transcript. Bind the
                    # active review to the current revision so its editorial decision is
                    # never reused for a later edited transcript.
                    approval.target_type = "transcript_version"
                    approval.target_id = canonical_id
                    return
        episode.approvals.append(
            Approval(
                episode_id=episode.id,
                stage="transcript_review",
                target_type="transcript_version",
                target_id=canonical_id or None,
                decision="pending",
                comment="Transcript changed after review decision.",
            )
        )

    def _ensure_transcript_review_editable(self, episode: Episode) -> None:
        has_pending_transcript_review = any(
            approval.stage == "transcript_review" and approval.decision == "pending"
            for approval in episode.approvals
        )
        if (
            episode.status != EpisodeStatus.transcript_review
            and not has_pending_transcript_review
        ):
            raise ValueError("turn review actions require a pending transcript review")
        canonical = next(
            (
                transcript
                for transcript in episode.transcripts
                if transcript.id == episode.canonical_transcript_version_id
            ),
            None,
        )
        if canonical is None:
            raise ValueError("episode has no canonical transcript")
        if canonical.status == "approved":
            raise ValueError("approved transcripts cannot be edited")

    def _require_session(self, episode: Episode) -> DiscussionSession:
        if episode.discussion_session is None:
            raise ValueError("episode has no discussion session")
        session = episode.discussion_session
        for turn in session.turns:
            if turn.discussion_session_id is None:
                turn.discussion_session_id = session.id
        for memory in session.memories.values():
            if memory.discussion_session_id is None:
                memory.discussion_session_id = session.id
        return session

    def _turn_by_id(self, session: DiscussionSession, turn_id: UUID) -> tuple[int, DiscussionTurn]:
        for index, turn in enumerate(session.turns):
            if turn.id == turn_id:
                return index, turn
        raise KeyError(turn_id)

    def _latest_host_instruction_before(self, episode: Episode, turn_index: int) -> str:
        session = self._require_session(episode)
        for turn in reversed(session.turns[:turn_index]):
            if (
                turn.status != "excluded"
                and self._participant_by_id(episode, turn.speaker_participant_id).participant_type
                == ParticipantType.host
            ):
                return turn.spoken_text
        return "Regenerate this turn in response to the discussion so far."

    def _discussion_qc(self, episode: Episode) -> QualityResult:
        assert episode.discussion_session is not None
        represented_speaker_ids = sorted(
            {
                turn.speaker_participant_id
                for turn in episode.discussion_session.turns
                if turn.status != "excluded"
            }
        )
        configured_speaker_ids = sorted(episode.discussion_session.speaker_balance_state)
        host_turns = [
            turn
            for turn in episode.discussion_session.turns
            if turn.status != "excluded"
            and self._participant_by_id(episode, turn.speaker_participant_id).participant_type
            == ParticipantType.host
        ]
        missing_speaker_ids = [
            participant_id
            for participant_id in configured_speaker_ids
            if participant_id not in represented_speaker_ids
        ]
        missing_non_host_speaker_ids = [
            participant_id
            for participant_id in missing_speaker_ids
            if self._participant_by_id(episode, participant_id).participant_type
            != ParticipantType.host
        ]
        missing_dimensions = [
            dimension
            for dimension, covered in episode.discussion_session.coverage_state.items()
            if covered is not True
        ]
        severity = (
            "pass"
            if host_turns and len(episode.discussion_session.turns) >= 6 and not missing_dimensions
            else "fail"
        )
        return QualityResult(
            episode_id=episode.id,
            target_type="discussion_session",
            target_id=str(episode.discussion_session.id),
            check_type="discussion_minimum_structure",
            severity=severity,
            status=severity,
            score=1.0 if severity == "pass" else 0.0,
            details={
                "turn_count": len(episode.discussion_session.turns),
                "host_turn_count": len(host_turns),
                "speaker_count": len(represented_speaker_ids),
                "configured_speaker_count": len(configured_speaker_ids),
                "configured_speaker_ids": configured_speaker_ids,
                "represented_speaker_count": len(represented_speaker_ids),
                "represented_speaker_ids": represented_speaker_ids,
                "missing_speaker_ids": missing_speaker_ids,
                "missing_non_host_speaker_ids": missing_non_host_speaker_ids,
                "required_dimensions": episode.definition.topic.required_dimensions,
                "coverage_state": dict(episode.discussion_session.coverage_state),
                "covered_dimension_count": sum(
                    1 for covered in episode.discussion_session.coverage_state.values() if covered
                ),
                "missing_dimensions": missing_dimensions,
            },
        )

    def _conversation_quality_qc(self, episode: Episode) -> QualityResult:
        """Assess whether generated turns form a usable discussion, not only a valid transcript."""
        session = self._require_session(episode)
        active_turns = [turn for turn in session.turns if turn.status != "excluded"]
        discussion_intensity = self._discussion_intensity(episode)
        duration_adjusted_turns = [
            turn
            for turn in active_turns
            if turn.generation_metadata.get("duration_control", {}).get("applied")
        ]
        direct_question_turns = [
            turn
            for turn in active_turns
            if turn.generation_metadata.get("turn_contract", {}).get("addressed_questions")
        ]
        linked_direct_answers = [
            turn for turn in direct_question_turns if turn.responding_to_turn_id is not None
        ]
        host_synthesis_turns = [
            turn for turn in active_turns if turn.turn_type == TurnType.host_synthesis
        ]
        responsive_turns = [
            turn
            for turn in active_turns
            if turn.generation_metadata.get("interaction", {}).get("mode")
            in {"responsive_argument", "cross_examination"}
        ]
        linked_responsive_turns = [
            turn for turn in responsive_turns if turn.responding_to_turn_id is not None
        ]
        cross_examination_turns = [
            turn
            for turn in active_turns
            if turn.generation_metadata.get("interaction", {}).get("mode")
            == "cross_examination"
        ]
        disagreement_or_revision_turns = [
            turn
            for turn in active_turns
            if turn.intent.lower() in {"rebuttal", "challenge", "disagreement", "revision"}
        ]
        repeated_opening_turn_ids = self._repeated_opening_turn_ids(active_turns)
        consecutive_same_speaker_turn_ids = [
            str(current.id)
            for previous, current in zip(active_turns, active_turns[1:], strict=False)
            if previous.speaker_participant_id == current.speaker_participant_id
        ]
        warnings: list[dict] = []
        failures: list[dict] = []
        if not host_synthesis_turns:
            failures.append({"issue": "host_synthesis_missing"})
        if duration_adjusted_turns:
            warnings.append(
                {
                    "issue": "post_generation_duration_adjustments",
                    "turn_count": len(duration_adjusted_turns),
                    "turn_ids": [str(turn.id) for turn in duration_adjusted_turns],
                }
            )
        if direct_question_turns and len(linked_direct_answers) != len(direct_question_turns):
            warnings.append(
                {
                    "issue": "direct_question_answer_link_missing",
                    "question_turn_count": len(direct_question_turns),
                    "linked_answer_count": len(linked_direct_answers),
                }
            )
        open_questions = self._open_controller_questions(session)
        if open_questions:
            warnings.append(
                {
                    "issue": "unresolved_direct_questions",
                    "question_count": len(open_questions),
                    "question_ids": [
                        str(question.get("question_id")) for question in open_questions
                    ],
                }
            )
        if discussion_intensity == "high":
            if len(cross_examination_turns) < 2:
                warnings.append(
                    {
                        "issue": "high_intensity_cross_examination_missing",
                        "expected_turn_count": 2,
                        "actual_turn_count": len(cross_examination_turns),
                    }
                )
            if len(linked_responsive_turns) < 2:
                warnings.append(
                    {
                        "issue": "high_intensity_response_links_missing",
                        "expected_turn_count": 2,
                        "actual_turn_count": len(linked_responsive_turns),
                    }
                )
            if not disagreement_or_revision_turns:
                warnings.append(
                    {
                        "issue": "high_intensity_disagreement_or_revision_missing",
                    }
                )
            if repeated_opening_turn_ids:
                warnings.append(
                    {
                        "issue": "repeated_turn_openings",
                        "turn_ids": repeated_opening_turn_ids,
                    }
                )
            if consecutive_same_speaker_turn_ids:
                warnings.append(
                    {
                        "issue": "unbalanced_turn_alternation",
                        "turn_ids": consecutive_same_speaker_turn_ids,
                    }
                )
        if failures:
            severity = "fail"
        elif warnings:
            severity = "warning"
        else:
            severity = "pass"
        score = max(0.0, 1.0 - 0.4 * len(failures) - 0.08 * len(warnings))
        return QualityResult(
            episode_id=episode.id,
            target_type="discussion_session",
            target_id=str(session.id),
            check_type="discussion_conversation_quality",
            severity=severity,
            status=severity,
            score=round(score, 2),
            details={
                "turn_count": len(active_turns),
                "discussion_intensity": discussion_intensity,
                "host_synthesis_turn_count": len(host_synthesis_turns),
                "duration_adjusted_turn_count": len(duration_adjusted_turns),
                "direct_question_turn_count": len(direct_question_turns),
                "linked_direct_answer_count": len(linked_direct_answers),
                "open_direct_question_count": len(open_questions),
                "responsive_turn_count": len(responsive_turns),
                "linked_responsive_turn_count": len(linked_responsive_turns),
                "cross_examination_turn_count": len(cross_examination_turns),
                "disagreement_or_revision_turn_count": len(disagreement_or_revision_turns),
                "repeated_opening_turn_ids": repeated_opening_turn_ids,
                "consecutive_same_speaker_turn_ids": consecutive_same_speaker_turn_ids,
                "failure_count": len(failures),
                "warning_count": len(warnings),
                "failures": failures,
                "warnings": warnings,
            },
        )

    def _repeated_opening_turn_ids(self, turns: list[DiscussionTurn]) -> list[str]:
        """Detect repeated generic openings without imposing a brittle semantic classifier."""
        opening_by_key: dict[str, list[str]] = {}
        for turn in turns:
            tokens = [
                token
                for token in self._normalize_keyword_text(turn.spoken_text).split()
                if token
            ]
            if len(tokens) < 4:
                continue
            key = " ".join(tokens[:4])
            opening_by_key.setdefault(key, []).append(str(turn.id))
        return [
            turn_id
            for turn_ids in opening_by_key.values()
            if len(turn_ids) > 1
            for turn_id in turn_ids
        ]

    def _duration_qc(self, episode: Episode) -> QualityResult:
        session = self._require_session(episode)
        failures: list[dict] = []
        warnings: list[dict] = []
        if session.estimated_duration_seconds > episode.maximum_duration_seconds:
            failures.append(
                {
                    "issue": "episode_exceeds_maximum_duration",
                    "estimated_duration_seconds": session.estimated_duration_seconds,
                    "maximum_duration_seconds": episode.maximum_duration_seconds,
                }
            )
        if session.estimated_duration_seconds < episode.minimum_duration_seconds:
            warnings.append(
                {
                    "issue": "episode_below_minimum_duration",
                    "estimated_duration_seconds": session.estimated_duration_seconds,
                    "minimum_duration_seconds": episode.minimum_duration_seconds,
                }
            )
        for turn in session.turns:
            if turn.status == "excluded":
                continue
            max_monologue_seconds = episode.definition.format.maximum_monologue_seconds
            if turn.estimated_duration_seconds > max_monologue_seconds:
                failures.append(
                    {
                        "issue": "turn_exceeds_maximum_monologue_duration",
                        "turn_id": str(turn.id),
                        "estimated_duration_seconds": turn.estimated_duration_seconds,
                        "maximum_monologue_seconds": max_monologue_seconds,
                    }
                )
            if turn.generation_metadata.get("duration_control", {}).get("applied"):
                warnings.append(
                    {
                        "issue": "turn_duration_control_applied",
                        "turn_id": str(turn.id),
                        **turn.generation_metadata["duration_control"],
                    }
                )
        if failures:
            severity = "fail"
        elif warnings:
            severity = "warning"
        else:
            severity = "pass"
        return QualityResult(
            episode_id=episode.id,
            target_type="discussion_session",
            target_id=str(session.id),
            check_type="discussion_duration_control",
            severity=severity,
            status=severity,
            score=max(0.0, 1.0 - (0.2 * len(failures)) - (0.03 * len(warnings))),
            details={
                "estimated_duration_seconds": session.estimated_duration_seconds,
                "minimum_duration_seconds": episode.minimum_duration_seconds,
                "target_duration_seconds": episode.target_duration_seconds,
                "maximum_duration_seconds": episode.maximum_duration_seconds,
                "maximum_monologue_seconds": episode.definition.format.maximum_monologue_seconds,
                "failure_count": len(failures),
                "warning_count": len(warnings),
                "failures": failures,
                "warnings": warnings,
            },
        )

    def _transcript_semantic_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> QualityResult:
        session = self._require_session(episode)
        source_turns = {turn.id: turn for turn in session.turns}
        failures: list[dict] = []
        warnings: list[dict] = []

        for transcript_turn in transcript.turns:
            if not transcript_turn.source_discussion_turn_ids:
                failures.append(
                    {
                        "transcript_turn_id": str(transcript_turn.id),
                        "issue": "missing_source_turn_link",
                    }
                )
                continue

            source_turn = source_turns.get(transcript_turn.source_discussion_turn_ids[0])
            if source_turn is None:
                failures.append(
                    {
                        "transcript_turn_id": str(transcript_turn.id),
                        "issue": "source_turn_not_found",
                    }
                )
                continue

            if transcript_turn.speaker_participant_id != source_turn.speaker_participant_id:
                failures.append(
                    {
                        "transcript_turn_id": str(transcript_turn.id),
                        "issue": "speaker_attribution_mismatch",
                    }
                )

            if transcript_turn.status == "excluded":
                warnings.append(
                    {
                        "transcript_turn_id": str(transcript_turn.id),
                        "source_turn_id": str(source_turn.id),
                        "issue": "turn_excluded_from_broadcast",
                    }
                )
                continue

            if not transcript_turn.text.strip():
                failures.append(
                    {
                        "transcript_turn_id": str(transcript_turn.id),
                        "issue": "empty_non_excluded_turn",
                    }
                )

            source_claims = {claim.text for claim in source_turn.structured_output.claims}
            added_claims = [
                claim.text for claim in transcript_turn.claims if claim.text not in source_claims
            ]
            if added_claims:
                failures.append(
                    {
                        "transcript_turn_id": str(transcript_turn.id),
                        "issue": "added_claims_detected",
                        "claims": added_claims,
                    }
                )

        if failures:
            severity = "fail"
        elif warnings:
            severity = "warning"
        else:
            severity = "pass"
        score = max(0.0, 1.0 - (0.2 * len(failures)) - (0.03 * len(warnings)))
        transcript.semantic_fidelity_score = score
        return QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id=str(transcript.id),
            check_type="transcript_semantic_fidelity",
            severity=severity,
            status=severity,
            score=score,
            details={
                "turn_count": len(transcript.turns),
                "failure_count": len(failures),
                "warning_count": len(warnings),
                "failures": failures,
                "warnings": warnings,
            },
        )

    def _participant_by_id(self, episode: Episode, participant_id: str) -> ParticipantProfile:
        for participant in episode.participants:
            if participant.id == participant_id:
                return participant
        raise ValueError(f"unknown participant {participant_id}")
