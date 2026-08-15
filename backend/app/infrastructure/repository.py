from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import Settings
from app.core.credentials import credential_reference_scheme, normalize_credential_reference
from app.domain.defaults import (
    B1_CHARACTER_VOICE_ASSIGNMENTS,
    OPENROUTER_CHARACTER_MODEL_ASSIGNMENTS,
    OPENROUTER_ENDPOINT_ID,
    b1_german_voice_profiles,
    default_comfyui_endpoints,
    default_comfyui_workflows,
    default_discussion_prompt_templates,
    default_language_profiles,
    default_model_endpoints,
    default_participants,
    default_publisher_targets,
    default_visual_profiles,
    default_voice_profiles,
    default_voicebox_endpoints,
    openrouter_model_endpoint,
)
from app.domain.enums import (
    AssetType,
    EpisodeStatus,
    ParticipantType,
    QualitySeverity,
    TranscriptType,
)
from app.domain.schemas import (
    Approval,
    ApprovalDecisionRequest,
    Asset,
    AuditEvent,
    ComfyUiEndpoint,
    ComfyUiWorkflow,
    DiscussionPromptTemplate,
    Episode,
    EpisodeCreateRequest,
    EpisodeDefinitionUpdateRequest,
    EpisodeSummary,
    EvidenceClaim,
    LanguageProfile,
    ModelEndpoint,
    ParticipantProfile,
    PrimerNarratorProfile,
    Project,
    PublisherTarget,
    ResearchSource,
    TranscriptVersion,
    VisualProfile,
    VoiceboxEndpoint,
    VoiceProfile,
)
from app.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from app.infrastructure.models import (
    AssetRecord,
    AuditEventRecord,
    ComfyUiEndpointRecord,
    ComfyUiWorkflowRecord,
    DiscussionPromptTemplateRecord,
    EpisodeRecord,
    EvidenceClaimRecord,
    LanguageProfileRecord,
    ModelEndpointRecord,
    ParticipantProfileRecord,
    PrimerNarratorProfileRecord,
    ProjectRecord,
    PublisherTargetRecord,
    ResearchSourceRecord,
    VisualProfileRecord,
    VoiceboxEndpointRecord,
    VoiceProfileRecord,
)
from app.services.production_control_service import ProductionControlService
from sqlalchemy import DateTime as SqlDateTime
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

BACKUP_RECORD_CLASSES = (
    ProjectRecord,
    LanguageProfileRecord,
    EpisodeRecord,
    ResearchSourceRecord,
    EvidenceClaimRecord,
    AssetRecord,
    ModelEndpointRecord,
    ParticipantProfileRecord,
    PrimerNarratorProfileRecord,
    VoiceboxEndpointRecord,
    VoiceProfileRecord,
    ComfyUiEndpointRecord,
    ComfyUiWorkflowRecord,
    DiscussionPromptTemplateRecord,
    VisualProfileRecord,
    PublisherTargetRecord,
    AuditEventRecord,
)


_EPISODE_RECORD_TIMING_TRACK_KEYS = frozenset(
    {
        "character_timestamps",
        "normalized_phoneme_timestamps",
        "phoneme_timestamps",
        "viseme_timestamps",
        "word_timestamps",
    }
)

_RENDER_REVIEW_STAGES = frozenset({"preview_render_review", "final_render_review"})
_ARCHIVED_EPISODE_STATUSES = frozenset(
    {EpisodeStatus.completed.value, EpisodeStatus.cancelled.value}
)


def _approval_is_actionable_in_summary(
    approval: dict,
    assets: list,
    canonical_transcript_version_id: str | None,
) -> bool:
    """Filter historical primer and superseded render approvals from summaries."""
    if approval.get("stage") not in _RENDER_REVIEW_STAGES:
        return True
    if approval.get("target_type") != "render_asset":
        return True
    target_id = approval.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        return True
    assets_by_id = {
        str(asset.get("id")): asset
        for asset in assets
        if isinstance(asset, dict) and isinstance(asset.get("id"), str)
    }
    render_asset = assets_by_id.get(target_id)
    if (
        not isinstance(render_asset, dict)
        or render_asset.get("asset_type") != AssetType.render.value
        or render_asset.get("source_entity_type") != "timeline_asset"
    ):
        return True
    timeline_asset = assets_by_id.get(str(render_asset.get("source_entity_id") or ""))
    if not isinstance(timeline_asset, dict):
        return True
    source_type = timeline_asset.get("source_entity_type")
    if source_type == "primer_production":
        return False
    if timeline_asset.get("status") == "replaced":
        return False
    if source_type == "transcript_version" and canonical_transcript_version_id:
        if timeline_asset.get("source_entity_id") != canonical_transcript_version_id:
            return False
        if render_asset.get("status") != "completed":
            return False
        expected_render_type = (
            "preview" if approval.get("stage") == "preview_render_review" else "final"
        )
        render_assets = [
            asset
            for asset in assets
            if isinstance(asset, dict)
            and asset.get("asset_type") == AssetType.render.value
            and asset.get("status") == "completed"
            and asset.get("source_entity_type") == "timeline_asset"
            and asset.get("source_entity_id") == str(timeline_asset.get("id"))
            and isinstance(asset.get("generation_metadata"), dict)
            and asset["generation_metadata"].get("render_type") == expected_render_type
        ]
        return bool(render_assets) and str(render_assets[-1].get("id")) == target_id
    return True


class EpisodeRepository:
    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        if session_factory is None:
            engine = create_database_engine(Settings(database_url="sqlite:///:memory:"))
            initialize_database(engine)
            session_factory = create_session_factory(engine)
        self._session_factory = session_factory
        self._seed_default_language_profiles()
        self._seed_default_model_endpoints()
        self._seed_default_voicebox_endpoints()
        self._seed_default_voice_profiles()
        self._seed_default_comfyui_endpoints()
        self._seed_default_comfyui_workflows()
        self._seed_default_discussion_prompt_templates()
        self._seed_default_visual_profiles()
        self._seed_default_publisher_targets()
        self._seed_default_participant_profiles()
        self._backfill_frontier_participant_prompt_templates()
        self._repair_frontier_participant_role_template_mismatches()
        self._backfill_default_participant_voice_profiles()
        self._backfill_default_participant_visual_profiles()

    def create(self, request: EpisodeCreateRequest) -> Episode:
        definition = request.definition
        participants = request.participants or self.list_participant_profiles()
        endpoints = request.model_endpoints or self.list_model_endpoints()
        if request.project_id is not None:
            self.get_project(request.project_id)
        participant_by_id = {participant.id: participant for participant in participants}
        selected = [
            self._participant_for_episode_assignment(
                participant_by_id[assignment.participant_profile_id],
                assignment.role,
            )
            for assignment in definition.participants
            if assignment.participant_profile_id in participant_by_id
        ]
        assignment_ids = {
            assignment.participant_profile_id for assignment in definition.participants
        }
        if len(selected) != len(definition.participants):
            missing = sorted(assignment_ids - set(participant_by_id))
            raise ValueError(f"unknown participant profile ids: {', '.join(missing)}")
        configuration_readiness = self._episode_configuration_readiness(
            participants=selected,
            endpoints=endpoints,
        )
        if configuration_readiness["missing_model_endpoint_ids"]:
            missing = ", ".join(configuration_readiness["missing_model_endpoint_ids"])
            raise ValueError(f"unknown model endpoint ids for selected participants: {missing}")
        if configuration_readiness["disabled_model_endpoint_ids"]:
            disabled = ", ".join(configuration_readiness["disabled_model_endpoint_ids"])
            raise ValueError(f"disabled model endpoint ids for selected participants: {disabled}")
        target_seconds = definition.format.target_duration_minutes * 60
        deviation = definition.format.permitted_deviation_percent / 100
        with self._session_factory() as session:
            slug = self._unique_slug(session, self._slugify(definition.title))

        episode = Episode(
            title=definition.title,
            slug=slug,
            subject=definition.topic.central_question,
            central_question=definition.topic.central_question,
            status=EpisodeStatus.draft,
            project_id=request.project_id,
            source_language=definition.languages.source_language,
            target_duration_seconds=target_seconds,
            minimum_duration_seconds=int(target_seconds * (1 - deviation)),
            maximum_duration_seconds=int(target_seconds * (1 + deviation)),
            definition=definition,
            participants=selected,
            model_endpoints=endpoints,
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="episode.created",
                details={
                    "title": episode.title,
                    "project_id": str(request.project_id) if request.project_id else None,
                    "participant_count": len(selected),
                },
            )
        )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="episode.configuration.readiness_checked",
                details=configuration_readiness,
            )
        )
        with self._session_factory() as session:
            session.add(self._to_record(episode))
            self._sync_episode_audit_events(session, episode)
            session.commit()
        return episode

    def update_definition(
        self,
        episode_id: UUID,
        request: EpisodeDefinitionUpdateRequest,
    ) -> Episode:
        episode = self.get(episode_id)
        if not self._episode_definition_is_editable(episode):
            raise ValueError(
                "episode definition is locked after production work exists; "
                "create a new episode instead"
            )
        definition = request.definition
        participants = request.participants or self.list_participant_profiles()
        endpoints = request.model_endpoints or self.list_model_endpoints()
        if request.project_id is not None:
            self.get_project(request.project_id)
        participant_by_id = {participant.id: participant for participant in participants}
        selected = [
            self._participant_for_episode_assignment(
                participant_by_id[assignment.participant_profile_id],
                assignment.role,
            )
            for assignment in definition.participants
            if assignment.participant_profile_id in participant_by_id
        ]
        assignment_ids = {
            assignment.participant_profile_id for assignment in definition.participants
        }
        if len(selected) != len(definition.participants):
            missing = sorted(assignment_ids - set(participant_by_id))
            raise ValueError(f"unknown participant profile ids: {', '.join(missing)}")
        configuration_readiness = self._episode_configuration_readiness(
            participants=selected,
            endpoints=endpoints,
        )
        if configuration_readiness["missing_model_endpoint_ids"]:
            missing = ", ".join(configuration_readiness["missing_model_endpoint_ids"])
            raise ValueError(f"unknown model endpoint ids for selected participants: {missing}")
        if configuration_readiness["disabled_model_endpoint_ids"]:
            disabled = ", ".join(configuration_readiness["disabled_model_endpoint_ids"])
            raise ValueError(f"disabled model endpoint ids for selected participants: {disabled}")
        target_seconds = definition.format.target_duration_minutes * 60
        deviation = definition.format.permitted_deviation_percent / 100
        previous = {
            "title": episode.title,
            "project_id": str(episode.project_id) if episode.project_id else None,
            "target_duration_seconds": episode.target_duration_seconds,
            "participant_ids": [participant.id for participant in episode.participants],
        }
        if definition.title != episode.title:
            with self._session_factory() as session:
                episode.slug = self._unique_slug(session, self._slugify(definition.title))
        episode.title = definition.title
        episode.subject = definition.topic.central_question
        episode.central_question = definition.topic.central_question
        episode.project_id = request.project_id
        episode.source_language = definition.languages.source_language
        episode.target_duration_seconds = target_seconds
        episode.minimum_duration_seconds = int(target_seconds * (1 - deviation))
        episode.maximum_duration_seconds = int(target_seconds * (1 + deviation))
        episode.definition = definition
        episode.participants = selected
        episode.model_endpoints = endpoints
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="episode.definition.updated",
                actor=request.user_id or "system",
                details={
                    "previous": previous,
                    "current": {
                        "title": episode.title,
                        "project_id": str(episode.project_id) if episode.project_id else None,
                        "target_duration_seconds": episode.target_duration_seconds,
                        "participant_ids": [participant.id for participant in episode.participants],
                    },
                    "configuration_readiness": configuration_readiness,
                },
            )
        )
        return self.save(episode)

    @staticmethod
    def _episode_definition_is_editable(episode: Episode) -> bool:
        return (
            episode.status == EpisodeStatus.draft
            and episode.current_workflow_id is None
            and episode.discussion_session is None
            and not episode.transcripts
            and not episode.assets
            and not episode.quality_results
            and not episode.approvals
            and not episode.publish_jobs
        )

    def _participant_for_episode_assignment(
        self,
        participant: ParticipantProfile,
        role: str,
    ) -> ParticipantProfile:
        normalized_role = role.strip().lower()
        role_type_map = {
            "moderator": ParticipantType.host,
            "host": ParticipantType.host,
            "panelist": ParticipantType.panelist,
            "participant": ParticipantType.panelist,
            "fact_checker": ParticipantType.fact_checker,
            "fact-checker": ParticipantType.fact_checker,
            "fact checker": ParticipantType.fact_checker,
            "guest": ParticipantType.guest,
            "audience_proxy": ParticipantType.audience_proxy,
            "audience-proxy": ParticipantType.audience_proxy,
            "audience proxy": ParticipantType.audience_proxy,
        }
        participant_type = role_type_map.get(normalized_role)
        if participant_type is None:
            return participant
        prompt_template_by_type = {
            ParticipantType.host: "moderator_v2",
            ParticipantType.panelist: "panelist_v2",
            ParticipantType.fact_checker: "fact_checker_v1",
            ParticipantType.guest: "guest_v1",
            ParticipantType.audience_proxy: "audience_proxy_v1",
        }
        updates: dict[str, object] = {"participant_type": participant_type}
        prompt_template_id = prompt_template_by_type.get(participant_type)
        if prompt_template_id is not None:
            updates["system_prompt_template"] = prompt_template_id
        return participant.model_copy(update=updates)

    def _episode_configuration_readiness(
        self,
        *,
        participants: list[ParticipantProfile],
        endpoints: list[ModelEndpoint],
    ) -> dict:
        endpoint_by_id = {endpoint.id: endpoint for endpoint in endpoints}
        participant_checks = []
        missing_model_endpoint_ids = sorted(
            {
                participant.model_endpoint_id
                for participant in participants
                if participant.model_endpoint_id not in endpoint_by_id
            }
        )
        disabled_model_endpoint_ids = sorted(
            {
                participant.model_endpoint_id
                for participant in participants
                if (
                    participant.model_endpoint_id in endpoint_by_id
                    and not endpoint_by_id[participant.model_endpoint_id].enabled
                )
            }
        )
        for participant in participants:
            endpoint = endpoint_by_id.get(participant.model_endpoint_id)
            participant_checks.append(
                {
                    "participant_id": participant.id,
                    "participant_type": participant.participant_type.value,
                    "model_endpoint_id": participant.model_endpoint_id,
                    "model_endpoint_configured": endpoint is not None,
                    "model_endpoint_enabled": bool(endpoint.enabled) if endpoint else False,
                    "model_id": participant.model_id,
                    "voice_profile_id": participant.voice_profile_id,
                    "voice_profile_configured": participant.voice_profile_id is not None,
                    "visual_profile_id": participant.visual_profile_id,
                    "visual_profile_configured": participant.visual_profile_id is not None,
                    "tool_policy_id": participant.tool_policy_id,
                }
            )
        return {
            "schema_version": "episode_configuration_readiness.v1",
            "participant_count": len(participants),
            "configured_model_endpoint_count": sum(
                1 for item in participant_checks if item["model_endpoint_configured"]
            ),
            "enabled_model_endpoint_count": sum(
                1 for item in participant_checks if item["model_endpoint_enabled"]
            ),
            "voice_profile_configured_count": sum(
                1 for item in participant_checks if item["voice_profile_configured"]
            ),
            "visual_profile_configured_count": sum(
                1 for item in participant_checks if item["visual_profile_configured"]
            ),
            "missing_model_endpoint_ids": missing_model_endpoint_ids,
            "disabled_model_endpoint_ids": disabled_model_endpoint_ids,
            "participants": participant_checks,
        }

    def list_audit_events(
        self,
        limit: int = 50,
        event_type: str | None = None,
    ) -> list[AuditEvent]:
        with self._session_factory() as session:
            query = select(AuditEventRecord).order_by(AuditEventRecord.created_at.desc())
            if event_type:
                query = query.where(AuditEventRecord.event_type == event_type)
            records = session.scalars(query.limit(limit)).all()
            return [self._audit_from_record(record) for record in records]

    def record_global_audit_event(self, event: AuditEvent) -> AuditEvent:
        with self._session_factory() as session:
            self._record_global_audit(session, event)
            session.commit()
        return event

    def list(self) -> Iterable[Episode]:
        with self._session_factory() as session:
            records = session.scalars(
                select(EpisodeRecord).order_by(EpisodeRecord.updated_at.desc())
            ).all()
            return [self._episode_from_record_with_assets(session, record) for record in records]

    def list_compact(self) -> Iterable[Episode]:
        """Return episode state without hydrating large per-asset timing tracks."""
        with self._session_factory() as session:
            records = session.scalars(
                select(EpisodeRecord).order_by(EpisodeRecord.updated_at.desc())
            ).all()
            return [self._from_record(record) for record in records]

    def list_summaries(self, *, include_archived: bool = True) -> list[EpisodeSummary]:
        with self._session_factory() as session:
            statement = select(EpisodeRecord)
            if not include_archived:
                statement = statement.where(
                    EpisodeRecord.status.not_in(_ARCHIVED_EPISODE_STATUSES)
                )
            records = session.scalars(statement.order_by(EpisodeRecord.updated_at.desc())).all()
            return [self._summary_from_record(record) for record in records]

    def _list_sqlite_summaries(self, session: Session) -> list[EpisodeSummary]:
        rows = session.execute(
            select(
                EpisodeRecord.id,
                EpisodeRecord.title,
                EpisodeRecord.slug,
                EpisodeRecord.status,
                EpisodeRecord.source_language,
                EpisodeRecord.target_duration_seconds,
                EpisodeRecord.minimum_duration_seconds,
                EpisodeRecord.maximum_duration_seconds,
                EpisodeRecord.current_workflow_id,
                EpisodeRecord.canonical_transcript_version_id,
                EpisodeRecord.created_at,
                EpisodeRecord.updated_at,
                func.json_extract(EpisodeRecord.payload, "$.project_id").label("project_id"),
                func.json_extract(
                    EpisodeRecord.payload,
                    "$.definition.languages.outputs",
                ).label("outputs"),
                func.json_extract(EpisodeRecord.payload, "$.discussion_session.phase").label(
                    "discussion_phase"
                ),
                func.json_extract(EpisodeRecord.payload, "$.discussion_session.status").label(
                    "discussion_status"
                ),
                func.coalesce(
                    func.json_array_length(
                        EpisodeRecord.payload,
                        "$.discussion_session.turns",
                    ),
                    0,
                ).label("discussion_turn_count"),
                func.coalesce(
                    func.json_extract(
                        EpisodeRecord.payload,
                        "$.discussion_session.estimated_duration_seconds",
                    ),
                    0,
                ).label("estimated_duration_seconds"),
                func.coalesce(
                    func.json_array_length(EpisodeRecord.payload, "$.transcripts"),
                    0,
                ).label("transcript_count"),
                func.coalesce(
                    func.json_array_length(EpisodeRecord.payload, "$.assets"),
                    0,
                ).label("asset_count"),
                func.coalesce(
                    func.json_array_length(EpisodeRecord.payload, "$.quality_results"),
                    0,
                ).label("quality_result_count"),
                func.coalesce(
                    func.json_array_length(EpisodeRecord.payload, "$.publish_jobs"),
                    0,
                ).label("publish_job_count"),
                func.json_extract(EpisodeRecord.payload, "$.approvals").label("approvals"),
                func.json_extract(EpisodeRecord.payload, "$.assets").label("assets"),
            ).order_by(EpisodeRecord.updated_at.desc())
        ).all()
        return [self._summary_from_sqlite_row(row) for row in rows]

    def get(self, episode_id: UUID) -> Episode:
        with self._session_factory() as session:
            record = session.get(EpisodeRecord, str(episode_id))
            if record is None:
                raise KeyError(episode_id)
            return self._episode_from_record_with_assets(session, record)

    def save(self, episode: Episode) -> Episode:
        episode.updated_at = datetime.now(UTC)
        with self._session_factory() as session:
            session.merge(self._to_record(episode))
            self._sync_asset_projections(session, episode)
            self._sync_research_projections(session, episode)
            self._sync_episode_audit_events(session, episode)
            session.commit()
        return episode

    def list_assets(self, episode_id: UUID) -> list[Asset]:
        episode = self.get(episode_id)
        with self._session_factory() as session:
            records = session.scalars(
                select(AssetRecord)
                .where(AssetRecord.episode_id == str(episode_id))
                .order_by(AssetRecord.created_at.asc(), AssetRecord.id.asc())
            ).all()
            if not records:
                return episode.assets
            return [self._asset_from_record(record) for record in records]

    def list_research_sources(self, episode_id: UUID) -> list[ResearchSource]:
        self.get(episode_id)
        with self._session_factory() as session:
            records = session.scalars(
                select(ResearchSourceRecord)
                .where(ResearchSourceRecord.episode_id == str(episode_id))
                .order_by(
                    ResearchSourceRecord.credibility_score.desc(),
                    ResearchSourceRecord.id.asc(),
                )
            ).all()
            return [self._research_source_from_record(record) for record in records]

    def list_evidence_claims(self, episode_id: UUID) -> list[EvidenceClaim]:
        self.get(episode_id)
        with self._session_factory() as session:
            records = session.scalars(
                select(EvidenceClaimRecord)
                .where(EvidenceClaimRecord.episode_id == str(episode_id))
                .order_by(EvidenceClaimRecord.claim_type.asc(), EvidenceClaimRecord.id.asc())
            ).all()
            return [self._evidence_claim_from_record(record) for record in records]

    def list_model_endpoints(self) -> list[ModelEndpoint]:
        with self._session_factory() as session:
            records = session.scalars(
                select(ModelEndpointRecord).order_by(ModelEndpointRecord.id.asc())
            ).all()
            return [self._endpoint_from_record(record) for record in records]

    def get_model_endpoint(self, endpoint_id: str) -> ModelEndpoint:
        with self._session_factory() as session:
            record = session.get(ModelEndpointRecord, endpoint_id)
            if record is None:
                raise KeyError(endpoint_id)
            return self._endpoint_from_record(record)

    def upsert_model_endpoint(self, endpoint: ModelEndpoint) -> ModelEndpoint:
        with self._session_factory() as session:
            session.merge(self._endpoint_to_record(endpoint))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="model_endpoint.upserted",
                    details={
                        "endpoint_id": endpoint.id,
                        "name": endpoint.name,
                        "provider_type": endpoint.provider_type.value,
                        "enabled": endpoint.enabled,
                        **self._credential_reference_audit_details(endpoint.credential_reference),
                    },
                ),
            )
            session.commit()
        return endpoint

    def provision_openrouter_presets(
        self,
        *,
        assign_participants: bool = True,
    ) -> dict:
        endpoint = openrouter_model_endpoint()
        assigned_participants: dict[str, str] = {}
        missing_participant_ids: list[str] = []
        now = datetime.now(UTC)

        with self._session_factory() as session:
            existing_endpoint = session.get(ModelEndpointRecord, endpoint.id)
            created_endpoint = existing_endpoint is None
            updated_endpoint = existing_endpoint is not None
            session.merge(self._endpoint_to_record(endpoint))

            if assign_participants:
                for participant_id, model_id in OPENROUTER_CHARACTER_MODEL_ASSIGNMENTS.items():
                    record = session.get(ParticipantProfileRecord, participant_id)
                    if record is None:
                        missing_participant_ids.append(participant_id)
                        continue
                    profile = self._profile_from_record(record)
                    profile.model_endpoint_id = OPENROUTER_ENDPOINT_ID
                    profile.model_id = model_id
                    record.payload = profile.model_dump(mode="json")
                    record.model_endpoint_id = OPENROUTER_ENDPOINT_ID
                    record.updated_at = now
                    assigned_participants[participant_id] = model_id

            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="model_endpoint.openrouter_presets_provisioned",
                    details={
                        "model_endpoint_id": endpoint.id,
                        "created_endpoint": created_endpoint,
                        "updated_endpoint": updated_endpoint,
                        "preset_model_count": len(endpoint.capabilities["model_presets"]),
                        "assigned_participant_count": len(assigned_participants),
                        "assigned_participant_ids": list(assigned_participants),
                        "missing_participant_ids": missing_participant_ids,
                        "assign_participants": assign_participants,
                        **self._credential_reference_audit_details(endpoint.credential_reference),
                    },
                ),
            )
            session.commit()

        return {
            "model_endpoint_id": endpoint.id,
            "created_endpoint": created_endpoint,
            "updated_endpoint": updated_endpoint,
            "assigned_participants": assigned_participants,
            "missing_participant_ids": missing_participant_ids,
        }

    def list_projects(self) -> list[Project]:
        with self._session_factory() as session:
            records = session.scalars(
                select(ProjectRecord).order_by(ProjectRecord.updated_at.desc())
            ).all()
            return [self._project_from_record(record) for record in records]

    def get_project(self, project_id: UUID) -> Project:
        with self._session_factory() as session:
            record = session.get(ProjectRecord, str(project_id))
            if record is None:
                raise KeyError(project_id)
            return self._project_from_record(record)

    def upsert_project(self, project: Project) -> Project:
        project.updated_at = datetime.now(UTC)
        with self._session_factory() as session:
            session.merge(self._project_to_record(project))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="project.upserted",
                    details={
                        "project_id": str(project.id),
                        "name": project.name,
                        "default_language": project.default_language,
                        "default_show_format_id": project.default_show_format_id,
                    },
                ),
            )
            session.commit()
        return project

    def delete_project(self, project_id: UUID) -> None:
        with self._session_factory() as session:
            record = session.get(ProjectRecord, str(project_id))
            if record is None:
                raise KeyError(project_id)
            linked_episode = session.scalar(
                select(EpisodeRecord.id)
                .where(EpisodeRecord.payload["project_id"].as_string() == str(project_id))
                .limit(1)
            )
            if linked_episode is not None:
                raise ValueError("project is still used by episodes")
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="project.deleted",
                    details={
                        "project_id": record.id,
                        "name": record.name,
                        "default_language": record.default_language,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def list_language_profiles(self) -> list[LanguageProfile]:
        with self._session_factory() as session:
            records = session.scalars(
                select(LanguageProfileRecord).order_by(LanguageProfileRecord.id.asc())
            ).all()
            return [self._language_profile_from_record(record) for record in records]

    def get_language_profile(self, profile_id: str) -> LanguageProfile:
        with self._session_factory() as session:
            record = session.get(LanguageProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            return self._language_profile_from_record(record)

    def upsert_language_profile(self, profile: LanguageProfile) -> LanguageProfile:
        with self._session_factory() as session:
            session.merge(self._language_profile_to_record(profile))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="language_profile.upserted",
                    details={
                        "profile_id": profile.id,
                        "name": profile.name,
                        "bcp47_tag": profile.bcp47_tag,
                        "default_mode": profile.default_mode,
                        "enabled": profile.enabled,
                    },
                ),
            )
            session.commit()
        return profile

    def delete_language_profile(self, profile_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(LanguageProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            linked_project = session.scalar(
                select(ProjectRecord.id)
                .where(ProjectRecord.default_language == record.bcp47_tag)
                .limit(1)
            )
            if linked_project is not None:
                raise ValueError("language profile is still used by projects")
            linked_episode = next(
                (
                    episode.id
                    for episode in session.scalars(select(EpisodeRecord)).all()
                    if self._episode_uses_language(episode, record.bcp47_tag)
                ),
                None,
            )
            if linked_episode is not None:
                raise ValueError("language profile is still used by episodes")
            linked_voice = session.scalar(
                select(VoiceProfileRecord.id)
                .where(VoiceProfileRecord.language == record.bcp47_tag)
                .limit(1)
            )
            if linked_voice is not None:
                raise ValueError("language profile is still used by voice profiles")
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="language_profile.deleted",
                    details={
                        "profile_id": record.id,
                        "name": record.name,
                        "bcp47_tag": record.bcp47_tag,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def delete_model_endpoint(self, endpoint_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(ModelEndpointRecord, endpoint_id)
            if record is None:
                raise KeyError(endpoint_id)
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="model_endpoint.deleted",
                    details={
                        "endpoint_id": record.id,
                        "name": record.name,
                        "provider_type": record.provider_type,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def list_voicebox_endpoints(self) -> list[VoiceboxEndpoint]:
        with self._session_factory() as session:
            records = session.scalars(
                select(VoiceboxEndpointRecord).order_by(VoiceboxEndpointRecord.id.asc())
            ).all()
            return [self._voicebox_endpoint_from_record(record) for record in records]

    def get_voicebox_endpoint(self, endpoint_id: str) -> VoiceboxEndpoint:
        with self._session_factory() as session:
            record = session.get(VoiceboxEndpointRecord, endpoint_id)
            if record is None:
                raise KeyError(endpoint_id)
            return self._voicebox_endpoint_from_record(record)

    def upsert_voicebox_endpoint(self, endpoint: VoiceboxEndpoint) -> VoiceboxEndpoint:
        with self._session_factory() as session:
            session.merge(self._voicebox_endpoint_to_record(endpoint))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="voicebox_endpoint.upserted",
                    details={
                        "endpoint_id": endpoint.id,
                        "name": endpoint.name,
                        "adapter_type": endpoint.adapter_type,
                        "enabled": endpoint.enabled,
                        **self._credential_reference_audit_details(endpoint.credential_reference),
                    },
                ),
            )
            session.commit()
        return endpoint

    def delete_voicebox_endpoint(self, endpoint_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(VoiceboxEndpointRecord, endpoint_id)
            if record is None:
                raise KeyError(endpoint_id)
            profile_count = session.scalar(
                select(VoiceProfileRecord.id)
                .where(VoiceProfileRecord.voicebox_endpoint_id == endpoint_id)
                .limit(1)
            )
            if profile_count is not None:
                raise ValueError("voicebox endpoint is still used by voice profiles")
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="voicebox_endpoint.deleted",
                    details={
                        "endpoint_id": record.id,
                        "name": record.name,
                        "adapter_type": record.adapter_type,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def list_voice_profiles(self) -> list[VoiceProfile]:
        with self._session_factory() as session:
            records = session.scalars(
                select(VoiceProfileRecord).order_by(VoiceProfileRecord.id.asc())
            ).all()
            return [self._voice_profile_from_record(record) for record in records]

    def list_primer_narrator_profiles(self) -> list[PrimerNarratorProfile]:
        with self._session_factory() as session:
            records = session.scalars(
                select(PrimerNarratorProfileRecord).order_by(PrimerNarratorProfileRecord.id.asc())
            ).all()
            return [self._primer_narrator_profile_from_record(record) for record in records]

    def get_primer_narrator_profile(self, profile_id: str) -> PrimerNarratorProfile:
        with self._session_factory() as session:
            record = session.get(PrimerNarratorProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            return self._primer_narrator_profile_from_record(record)

    def upsert_primer_narrator_profile(
        self, profile: PrimerNarratorProfile
    ) -> PrimerNarratorProfile:
        model_endpoint_ids = {item.id for item in self.list_model_endpoints()}
        if profile.model_endpoint_id not in model_endpoint_ids:
            raise ValueError(f"unknown model endpoint {profile.model_endpoint_id}")
        pronunciation_endpoint_id = profile.pronunciation.model_endpoint_id
        if pronunciation_endpoint_id and pronunciation_endpoint_id not in model_endpoint_ids:
            raise ValueError(
                f"unknown pronunciation model endpoint {pronunciation_endpoint_id}"
            )
        if profile.voice_profile_id not in {item.id for item in self.list_voice_profiles()}:
            raise ValueError(f"unknown voice profile {profile.voice_profile_id}")
        with self._session_factory() as session:
            session.merge(self._primer_narrator_profile_to_record(profile))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="primer_narrator_profile.upserted",
                    details={
                        "profile_id": profile.id,
                        "name": profile.name,
                        "language": profile.language,
                        "model_endpoint_id": profile.model_endpoint_id,
                        "voice_profile_id": profile.voice_profile_id,
                        "pronunciation_enabled": profile.pronunciation.enabled,
                        "pronunciation_dictionary_size": len(
                            profile.pronunciation.custom_dictionary
                        ),
                        "enabled": profile.enabled,
                    },
                ),
            )
            session.commit()
        return profile

    def delete_primer_narrator_profile(self, profile_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(PrimerNarratorProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="primer_narrator_profile.deleted",
                    details={"profile_id": record.id, "name": record.name},
                ),
            )
            session.delete(record)
            session.commit()

    def get_voice_profile(self, profile_id: str) -> VoiceProfile:
        with self._session_factory() as session:
            record = session.get(VoiceProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            return self._voice_profile_from_record(record)

    def upsert_voice_profile(self, profile: VoiceProfile) -> VoiceProfile:
        endpoint_ids = {endpoint.id for endpoint in self.list_voicebox_endpoints()}
        if profile.voicebox_endpoint_id not in endpoint_ids:
            raise ValueError(f"unknown voicebox endpoint {profile.voicebox_endpoint_id}")
        with self._session_factory() as session:
            session.merge(self._voice_profile_to_record(profile))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="voice_profile.upserted",
                    details={
                        "profile_id": profile.id,
                        "name": profile.name,
                        "voicebox_endpoint_id": profile.voicebox_endpoint_id,
                        "language": profile.language,
                        "enabled": profile.enabled,
                    },
                ),
            )
            session.commit()
        return profile

    def delete_voice_profile(self, profile_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(VoiceProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            participant_count = session.scalar(
                select(ParticipantProfileRecord.id)
                .where(
                    ParticipantProfileRecord.payload["voice_profile_id"].as_string() == profile_id
                )
                .limit(1)
            )
            if participant_count is not None:
                raise ValueError("voice profile is still used by participant profiles")
            narrator_count = session.scalar(
                select(PrimerNarratorProfileRecord.id)
                .where(PrimerNarratorProfileRecord.voice_profile_id == profile_id)
                .limit(1)
            )
            if narrator_count is not None:
                raise ValueError("voice profile is still used by primer narrator profiles")
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="voice_profile.deleted",
                    details={
                        "profile_id": record.id,
                        "name": record.name,
                        "voicebox_endpoint_id": record.voicebox_endpoint_id,
                        "language": record.language,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def list_comfyui_endpoints(self) -> list[ComfyUiEndpoint]:
        with self._session_factory() as session:
            records = session.scalars(
                select(ComfyUiEndpointRecord).order_by(ComfyUiEndpointRecord.id.asc())
            ).all()
            return [self._comfyui_endpoint_from_record(record) for record in records]

    def get_comfyui_endpoint(self, endpoint_id: str) -> ComfyUiEndpoint:
        with self._session_factory() as session:
            record = session.get(ComfyUiEndpointRecord, endpoint_id)
            if record is None:
                raise KeyError(endpoint_id)
            return self._comfyui_endpoint_from_record(record)

    def upsert_comfyui_endpoint(self, endpoint: ComfyUiEndpoint) -> ComfyUiEndpoint:
        with self._session_factory() as session:
            session.merge(self._comfyui_endpoint_to_record(endpoint))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="comfyui_endpoint.upserted",
                    details={
                        "endpoint_id": endpoint.id,
                        "name": endpoint.name,
                        "adapter_type": endpoint.adapter_type,
                        "enabled": endpoint.enabled,
                        **self._credential_reference_audit_details(endpoint.credential_reference),
                    },
                ),
            )
            session.commit()
        return endpoint

    def delete_comfyui_endpoint(self, endpoint_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(ComfyUiEndpointRecord, endpoint_id)
            if record is None:
                raise KeyError(endpoint_id)
            workflow_count = session.scalar(
                select(ComfyUiWorkflowRecord.id)
                .where(ComfyUiWorkflowRecord.comfyui_endpoint_id == endpoint_id)
                .limit(1)
            )
            if workflow_count is not None:
                raise ValueError("comfyui endpoint is still used by workflows")
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="comfyui_endpoint.deleted",
                    details={
                        "endpoint_id": record.id,
                        "name": record.name,
                        "adapter_type": record.adapter_type,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def list_comfyui_workflows(self) -> list[ComfyUiWorkflow]:
        with self._session_factory() as session:
            records = session.scalars(
                select(ComfyUiWorkflowRecord).order_by(ComfyUiWorkflowRecord.id.asc())
            ).all()
            return [self._comfyui_workflow_from_record(record) for record in records]

    def get_comfyui_workflow(self, workflow_id: str) -> ComfyUiWorkflow:
        with self._session_factory() as session:
            record = session.get(ComfyUiWorkflowRecord, workflow_id)
            if record is None:
                raise KeyError(workflow_id)
            return self._comfyui_workflow_from_record(record)

    def upsert_comfyui_workflow(self, workflow: ComfyUiWorkflow) -> ComfyUiWorkflow:
        endpoint_ids = {endpoint.id for endpoint in self.list_comfyui_endpoints()}
        if workflow.comfyui_endpoint_id not in endpoint_ids:
            raise ValueError(f"unknown comfyui endpoint {workflow.comfyui_endpoint_id}")
        with self._session_factory() as session:
            session.merge(self._comfyui_workflow_to_record(workflow))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="comfyui_workflow.upserted",
                    details={
                        "workflow_id": workflow.id,
                        "name": workflow.name,
                        "workflow_type": workflow.workflow_type,
                        "version": workflow.version,
                        "comfyui_endpoint_id": workflow.comfyui_endpoint_id,
                        "enabled": workflow.enabled,
                    },
                ),
            )
            session.commit()
        return workflow

    def delete_comfyui_workflow(self, workflow_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(ComfyUiWorkflowRecord, workflow_id)
            if record is None:
                raise KeyError(workflow_id)
            profile_count = session.scalar(
                select(VisualProfileRecord.id)
                .where(
                    (VisualProfileRecord.primary_workflow_id == workflow_id)
                    | (
                        VisualProfileRecord.payload["reaction_workflow_id"].as_string()
                        == workflow_id
                    )
                    | (VisualProfileRecord.payload["broll_workflow_id"].as_string() == workflow_id)
                )
                .limit(1)
            )
            if profile_count is not None:
                raise ValueError("comfyui workflow is still used by visual profiles")
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="comfyui_workflow.deleted",
                    details={
                        "workflow_id": record.id,
                        "name": record.name,
                        "workflow_type": record.workflow_type,
                        "version": record.version,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def list_discussion_prompt_templates(self) -> list[DiscussionPromptTemplate]:
        with self._session_factory() as session:
            records = session.scalars(
                select(DiscussionPromptTemplateRecord).order_by(
                    DiscussionPromptTemplateRecord.id.asc()
                )
            ).all()
            return [self._discussion_prompt_template_from_record(record) for record in records]

    def get_discussion_prompt_template(self, template_id: str) -> DiscussionPromptTemplate:
        with self._session_factory() as session:
            record = session.get(DiscussionPromptTemplateRecord, template_id)
            if record is None:
                raise KeyError(template_id)
            return self._discussion_prompt_template_from_record(record)

    def upsert_discussion_prompt_template(
        self,
        template: DiscussionPromptTemplate,
    ) -> DiscussionPromptTemplate:
        with self._session_factory() as session:
            existing = session.get(DiscussionPromptTemplateRecord, template.id)
            profile_id = self._participant_profile_using_prompt_template(
                session,
                template.id,
            )
            if profile_id is not None and not template.enabled:
                raise ValueError(
                    "discussion prompt template is still used by participant profiles "
                    "and cannot be disabled"
                )
            if (
                profile_id is not None
                and existing is not None
                and existing.participant_type != str(template.participant_type)
            ):
                raise ValueError(
                    "discussion prompt template participant type cannot be changed "
                    "while used by participant profiles"
                )
            session.merge(self._discussion_prompt_template_to_record(template))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="discussion_prompt_template.upserted",
                    details={
                        "template_id": template.id,
                        "version": template.version,
                        "participant_type": template.participant_type.value,
                        "enabled": template.enabled,
                        "created_by": template.created_by,
                    },
                ),
            )
            session.commit()
        return template

    def delete_discussion_prompt_template(self, template_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(DiscussionPromptTemplateRecord, template_id)
            if record is None:
                raise KeyError(template_id)
            if self._participant_profile_using_prompt_template(session, template_id) is not None:
                raise ValueError("discussion prompt template is still used by participant profiles")
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="discussion_prompt_template.deleted",
                    details={
                        "template_id": record.id,
                        "version": record.version,
                        "participant_type": record.participant_type,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def list_visual_profiles(self) -> list[VisualProfile]:
        with self._session_factory() as session:
            records = session.scalars(
                select(VisualProfileRecord).order_by(VisualProfileRecord.id.asc())
            ).all()
            return [self._visual_profile_from_record(record) for record in records]

    def get_visual_profile(self, profile_id: str) -> VisualProfile:
        with self._session_factory() as session:
            record = session.get(VisualProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            return self._visual_profile_from_record(record)

    def upsert_visual_profile(self, profile: VisualProfile) -> VisualProfile:
        workflow_ids = {workflow.id for workflow in self.list_comfyui_workflows()}
        referenced = {profile.primary_workflow_id}
        referenced.update(
            item
            for item in [profile.reaction_workflow_id, profile.broll_workflow_id]
            if item is not None
        )
        missing = sorted(referenced - workflow_ids)
        if missing:
            raise ValueError(f"unknown comfyui workflow ids: {', '.join(missing)}")
        with self._session_factory() as session:
            session.merge(self._visual_profile_to_record(profile))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="visual_profile.upserted",
                    details={
                        "profile_id": profile.id,
                        "name": profile.name,
                        "primary_workflow_id": profile.primary_workflow_id,
                        "enabled": profile.enabled,
                    },
                ),
            )
            session.commit()
        return profile

    def delete_visual_profile(self, profile_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(VisualProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            participant_count = session.scalar(
                select(ParticipantProfileRecord.id)
                .where(
                    ParticipantProfileRecord.payload["visual_profile_id"].as_string() == profile_id
                )
                .limit(1)
            )
            if participant_count is not None:
                raise ValueError("visual profile is still used by participant profiles")
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="visual_profile.deleted",
                    details={
                        "profile_id": record.id,
                        "name": record.name,
                        "primary_workflow_id": record.primary_workflow_id,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def list_publisher_targets(self) -> list[PublisherTarget]:
        with self._session_factory() as session:
            records = session.scalars(
                select(PublisherTargetRecord).order_by(PublisherTargetRecord.id.asc())
            ).all()
            return [self._publisher_target_from_record(record) for record in records]

    def get_publisher_target(self, target_id: str) -> PublisherTarget:
        with self._session_factory() as session:
            record = session.get(PublisherTargetRecord, target_id)
            if record is None:
                raise KeyError(target_id)
            return self._publisher_target_from_record(record)

    def upsert_publisher_target(self, target: PublisherTarget) -> PublisherTarget:
        with self._session_factory() as session:
            session.merge(self._publisher_target_to_record(target))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="publisher_target.upserted",
                    details={
                        "target_id": target.id,
                        "name": target.name,
                        "platform": target.platform,
                        "adapter_type": target.adapter_type,
                        "enabled": target.enabled,
                        **self._credential_reference_audit_details(target.credential_reference),
                    },
                ),
            )
            session.commit()
        return target

    def delete_publisher_target(self, target_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(PublisherTargetRecord, target_id)
            if record is None:
                raise KeyError(target_id)
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="publisher_target.deleted",
                    details={
                        "target_id": record.id,
                        "name": record.name,
                        "platform": record.platform,
                        "adapter_type": record.adapter_type,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def provision_b1_german_voice_presets(
        self,
        *,
        endpoint_id: str = "b1-voicebox",
        assign_participants: bool = True,
        reassign_participants: bool = False,
    ) -> dict:
        created_profile_ids: list[str] = []
        existing_profile_ids: list[str] = []
        assigned_participants: dict[str, str] = {}
        preserved_assigned_participant_ids: list[str] = []
        reassigned_participant_ids: list[str] = []
        now = datetime.now(UTC)

        with self._session_factory() as session:
            endpoint_record = session.get(VoiceboxEndpointRecord, endpoint_id)
            if endpoint_record is None:
                raise KeyError(endpoint_id)
            if endpoint_record.adapter_type != "b1_voice_stream":
                raise ValueError("voicebox endpoint is not a B1 stream adapter")
            endpoint = self._voicebox_endpoint_from_record(endpoint_record)
            presets = self._b1_voice_profiles_for_endpoint(endpoint)
            provisioned_profile_by_source_id = {
                str(profile.prosody.get("b1_source_voice_profile_id") or profile.id): profile.id
                for profile in presets
            }
            preset_ids = [profile.id for profile in presets]

            for profile in presets:
                existing = session.get(VoiceProfileRecord, profile.id)
                if existing is None:
                    session.add(self._voice_profile_to_record(profile))
                    created_profile_ids.append(profile.id)
                else:
                    existing_profile_ids.append(profile.id)

            if assign_participants:
                participant_records = session.scalars(
                    select(ParticipantProfileRecord).order_by(ParticipantProfileRecord.id.asc())
                ).all()
                assigned_voice_ids = {
                    str(voice_profile_id)
                    for record in participant_records
                    if (voice_profile_id := record.payload.get("voice_profile_id"))
                }
                available_voice_ids = [
                    profile_id for profile_id in preset_ids if profile_id not in assigned_voice_ids
                ]
                for record in participant_records:
                    existing_voice_profile_id = record.payload.get("voice_profile_id")
                    matching_source_voice_id = B1_CHARACTER_VOICE_ASSIGNMENTS.get(record.id)
                    matching_voice_id = (
                        provisioned_profile_by_source_id.get(matching_source_voice_id)
                        if matching_source_voice_id
                        else None
                    )
                    if existing_voice_profile_id and (
                        not reassign_participants
                        or not matching_voice_id
                        or existing_voice_profile_id == matching_voice_id
                    ):
                        preserved_assigned_participant_ids.append(record.id)
                        continue
                    if matching_voice_id:
                        profile = self._profile_from_record(record)
                        if (
                            profile.voice_profile_id
                            and profile.voice_profile_id != matching_voice_id
                        ):
                            reassigned_participant_ids.append(record.id)
                        profile.voice_profile_id = matching_voice_id
                        record.payload = profile.model_dump(mode="json")
                        record.updated_at = now
                        assigned_participants[record.id] = profile.voice_profile_id
                        if matching_voice_id in available_voice_ids:
                            available_voice_ids.remove(matching_voice_id)
                        continue
                    if matching_source_voice_id:
                        continue
                    if not available_voice_ids:
                        break
                    profile = self._profile_from_record(record)
                    profile.voice_profile_id = available_voice_ids.pop(0)
                    record.payload = profile.model_dump(mode="json")
                    record.updated_at = now
                    assigned_participants[record.id] = profile.voice_profile_id

            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="voice_profile.b1_presets_provisioned",
                    details={
                        "voicebox_endpoint_id": endpoint_id,
                        "created_voice_profile_count": len(created_profile_ids),
                        "created_voice_profile_ids": created_profile_ids,
                        "existing_voice_profile_count": len(existing_profile_ids),
                        "existing_voice_profile_ids": existing_profile_ids,
                        "assigned_participant_count": len(assigned_participants),
                        "assigned_participant_ids": list(assigned_participants),
                        "preserved_assigned_participant_count": len(
                            preserved_assigned_participant_ids
                        ),
                        "reassigned_participant_count": len(reassigned_participant_ids),
                        "reassigned_participant_ids": reassigned_participant_ids,
                        "assign_participants": assign_participants,
                        "reassign_participants": reassign_participants,
                    },
                ),
            )
            session.commit()

        return {
            "voicebox_endpoint_id": endpoint_id,
            "created_voice_profile_ids": created_profile_ids,
            "existing_voice_profile_ids": existing_profile_ids,
            "assigned_participants": assigned_participants,
            "preserved_assigned_participant_ids": preserved_assigned_participant_ids,
            "reassigned_participant_ids": reassigned_participant_ids,
            "reassign_participants": reassign_participants,
        }

    def sync_b1_voicebox_profile_inventory(
        self,
        *,
        endpoint_id: str,
        discovered_profiles: list[dict],
    ) -> dict:
        """Persist newly discovered German native B1 profiles without replacing cast mappings."""
        created_voice_profile_ids: list[str] = []
        existing_voice_profile_ids: list[str] = []
        skipped_voice_ids: list[str] = []
        now = datetime.now(UTC)
        with self._session_factory() as session:
            endpoint_record = session.get(VoiceboxEndpointRecord, endpoint_id)
            if endpoint_record is None:
                raise KeyError(endpoint_id)
            if endpoint_record.adapter_type != "b1_voice_stream":
                raise ValueError("voicebox endpoint is not a B1 stream adapter")
            existing_records = session.scalars(
                select(VoiceProfileRecord).where(
                    VoiceProfileRecord.voicebox_endpoint_id == endpoint_id
                )
            ).all()
            existing_profiles_by_remote_id = {
                profile.voice_id: profile
                for record in existing_records
                if (profile := self._voice_profile_from_record(record))
            }
            existing_records_by_remote_id = {
                profile.voice_id: record
                for record in existing_records
                if (profile := self._voice_profile_from_record(record))
            }
            for item in discovered_profiles:
                remote_id = item.get("id")
                language = item.get("language")
                if not isinstance(remote_id, str) or not remote_id.strip():
                    continue
                if not isinstance(language, str) or language.lower() != "de":
                    skipped_voice_ids.append(remote_id)
                    continue
                display_name = self._unique_discovered_voice_display_name(
                    self._discovered_voice_display_name(item),
                    remote_id,
                    list(existing_profiles_by_remote_id.values()),
                )
                existing_profile = existing_profiles_by_remote_id.get(remote_id)
                if existing_profile is not None:
                    if existing_profile.prosody.get("b1_inventory_discovered") is True and (
                        existing_profile.name != display_name
                        or existing_profile.speaker_label != display_name
                    ):
                        existing_profile.name = display_name
                        existing_profile.speaker_label = display_name
                        record = existing_records_by_remote_id[remote_id]
                        record.payload = existing_profile.model_dump(mode="json")
                        record.updated_at = now
                    existing_voice_profile_ids.append(existing_profile.id)
                    continue
                profile = VoiceProfile(
                    id=f"b1-native-{remote_id}",
                    name=display_name,
                    voicebox_endpoint_id=endpoint_id,
                    voice_id=remote_id,
                    language="de",
                    speaker_label=display_name,
                    model_id=str(item.get("engine") or "chatterbox"),
                    prosody={
                        "engine": str(item.get("engine") or "chatterbox"),
                        "normalize": True,
                        "effects_chain": [],
                        "b1_inventory_discovered": True,
                    },
                    enabled=True,
                )
                session.merge(self._voice_profile_to_record(profile))
                created_voice_profile_ids.append(profile.id)
                existing_profiles_by_remote_id[remote_id] = profile
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="voice_profile.b1_inventory_synchronized",
                    details={
                        "voicebox_endpoint_id": endpoint_id,
                        "discovered_voice_count": len(discovered_profiles),
                        "created_voice_profile_ids": created_voice_profile_ids,
                        "existing_voice_profile_ids": existing_voice_profile_ids,
                        "skipped_voice_ids": skipped_voice_ids,
                    },
                ),
            )
            session.commit()
        return {
            "voicebox_endpoint_id": endpoint_id,
            "discovered_voice_count": len(discovered_profiles),
            "created_voice_profile_ids": created_voice_profile_ids,
            "existing_voice_profile_ids": existing_voice_profile_ids,
            "skipped_voice_ids": skipped_voice_ids,
        }

    @staticmethod
    def _discovered_voice_display_name(profile: dict) -> str:
        description = str(profile.get("description") or "").strip()
        if ":" in description:
            candidate = description.rsplit(":", 1)[-1].strip()
            if candidate:
                return candidate[:256]
        return str(profile.get("name") or "B1 Voice").strip()[:256]

    @staticmethod
    def _unique_discovered_voice_display_name(
        display_name: str,
        remote_id: str,
        existing_profiles: list[VoiceProfile],
    ) -> str:
        duplicate_name = any(
            profile.voice_id != remote_id and profile.name.casefold() == display_name.casefold()
            for profile in existing_profiles
        )
        if not duplicate_name:
            return display_name
        suffix = f" (B1 {remote_id[:8]})"
        return f"{display_name[: max(0, 256 - len(suffix))]}{suffix}"

    def _b1_voice_profiles_for_endpoint(
        self,
        endpoint: VoiceboxEndpoint,
    ) -> list[VoiceProfile]:
        profiles = b1_german_voice_profiles(endpoint.id)
        raw_local_profile_ids = endpoint.capabilities.get("local_voicebox_profile_ids")
        profile_id_prefix = endpoint.capabilities.get("voice_profile_id_prefix")
        if not isinstance(raw_local_profile_ids, dict) and not isinstance(
            profile_id_prefix,
            str,
        ):
            return profiles
        local_profile_ids = raw_local_profile_ids if isinstance(raw_local_profile_ids, dict) else {}
        engine = endpoint.capabilities.get("voice_profile_engine") or "remote_http"
        mapped_profiles: list[VoiceProfile] = []
        for profile in profiles:
            profile_id = profile.id
            if isinstance(profile_id_prefix, str) and profile_id_prefix.strip():
                profile_id = f"{profile_id_prefix.strip()}{profile.id}"
            local_voice_id = local_profile_ids.get(profile.id)
            prosody = dict(profile.prosody)
            prosody["b1_source_voice_profile_id"] = profile.id
            if isinstance(local_voice_id, str) and local_voice_id.strip():
                prosody["engine"] = str(engine)
            mapped_profiles.append(
                profile.model_copy(
                    update={
                        "id": profile_id,
                        "voice_id": local_voice_id.strip()
                        if isinstance(local_voice_id, str) and local_voice_id.strip()
                        else profile.voice_id,
                        "prosody": prosody,
                    }
                )
            )
        return mapped_profiles

    def list_participant_profiles(self) -> list[ParticipantProfile]:
        with self._session_factory() as session:
            records = session.scalars(
                select(ParticipantProfileRecord).order_by(ParticipantProfileRecord.id.asc())
            ).all()
            return [self._profile_from_record(record) for record in records]

    def get_participant_profile(self, profile_id: str) -> ParticipantProfile:
        with self._session_factory() as session:
            record = session.get(ParticipantProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            return self._profile_from_record(record)

    def upsert_participant_profile(self, profile: ParticipantProfile) -> ParticipantProfile:
        endpoint_ids = {endpoint.id for endpoint in self.list_model_endpoints()}
        if profile.model_endpoint_id not in endpoint_ids:
            raise ValueError(f"unknown model endpoint {profile.model_endpoint_id}")
        templates = {template.id: template for template in self.list_discussion_prompt_templates()}
        template = templates.get(profile.system_prompt_template)
        if template is None:
            raise ValueError(f"unknown discussion prompt template {profile.system_prompt_template}")
        if not template.enabled:
            raise ValueError(
                f"disabled discussion prompt template {profile.system_prompt_template}"
            )
        if str(template.participant_type) != str(profile.participant_type):
            raise ValueError(
                "discussion prompt template participant type does not match "
                f"profile participant type {profile.participant_type}"
            )
        if profile.voice_profile_id is not None:
            voice_profile_ids = {voice.id for voice in self.list_voice_profiles()}
            if profile.voice_profile_id not in voice_profile_ids:
                raise ValueError(f"unknown voice profile {profile.voice_profile_id}")
        if profile.visual_profile_id is not None:
            visual_profile_ids = {visual.id for visual in self.list_visual_profiles()}
            if profile.visual_profile_id not in visual_profile_ids:
                raise ValueError(f"unknown visual profile {profile.visual_profile_id}")
        with self._session_factory() as session:
            session.merge(self._profile_to_record(profile))
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="participant_profile.upserted",
                    details={
                        "profile_id": profile.id,
                        "display_name": profile.display_name,
                        "participant_type": profile.participant_type.value,
                        "model_endpoint_id": profile.model_endpoint_id,
                        "enabled": profile.enabled,
                    },
                ),
            )
            session.commit()
        return profile

    def _participant_profile_using_prompt_template(
        self,
        session: Session,
        template_id: str,
    ) -> str | None:
        return session.scalar(
            select(ParticipantProfileRecord.id)
            .where(
                ParticipantProfileRecord.payload["system_prompt_template"].as_string()
                == template_id
            )
            .limit(1)
        )

    def delete_participant_profile(self, profile_id: str) -> None:
        with self._session_factory() as session:
            record = session.get(ParticipantProfileRecord, profile_id)
            if record is None:
                raise KeyError(profile_id)
            self._record_global_audit(
                session,
                AuditEvent(
                    event_type="participant_profile.deleted",
                    details={
                        "profile_id": record.id,
                        "display_name": record.display_name,
                        "participant_type": record.participant_type,
                        "model_endpoint_id": record.model_endpoint_id,
                    },
                ),
            )
            session.delete(record)
            session.commit()

    def record_approval_decision(
        self,
        episode_id: UUID,
        approval_id: UUID,
        decision: ApprovalDecisionRequest,
    ) -> Episode:
        episode = self.get(episode_id)
        approval = next(
            (item for item in episode.approvals if item.id == approval_id),
            None,
        )
        if approval is None:
            raise KeyError(approval_id)
        previous_status = episode.status
        approval.decision = decision.decision
        approval.comment = decision.comment
        approval.user_id = decision.user_id
        approval.created_at = datetime.now(UTC)
        claim_qc_acceptance: dict[str, object] | None = None

        if approval.stage == "transcript_review":
            canonical_id = episode.canonical_transcript_version_id
            if canonical_id is None:
                raise ValueError("episode has no canonical transcript")
            approval.target_type = "transcript_version"
            approval.target_id = str(canonical_id)
            if decision.decision == "approved":
                self._ensure_canonical_transcript_qc_allows_approval(episode)
            for transcript in episode.transcripts:
                if transcript.id == episode.canonical_transcript_version_id:
                    transcript.status = (
                        "approved" if decision.decision == "approved" else "rejected"
                    )
            if decision.decision == "approved":
                episode.status = EpisodeStatus.ready
                claim_qc = next(
                    (
                        result
                        for result in reversed(episode.quality_results)
                        if result.check_type == "claim_citation_integrity"
                        and result.target_type == "transcript_version"
                        and result.target_id == str(canonical_id)
                    ),
                    None,
                )
                if claim_qc is not None and (
                    claim_qc.status == "fail" or claim_qc.severity == QualitySeverity.fail
                ):
                    claim_qc_acceptance = {
                        "quality_result_id": str(claim_qc.id),
                        "status": claim_qc.status,
                        "unsupported_claim_count": int(
                            claim_qc.details.get("unsupported_claim_count") or 0
                        ),
                        "editorial_decision": "accepted_with_warnings",
                    }
            else:
                episode.status = EpisodeStatus.transcript_review

        if approval.stage == "localized_transcript_review":
            localized_transcript = self._target_approval_transcript(episode, approval)
            if localized_transcript.type != TranscriptType.localized:
                raise ValueError("localized transcript approval must target a localized transcript")
            if decision.decision == "approved":
                self._ensure_localized_transcript_qc_allows_approval(
                    episode,
                    localized_transcript,
                )
            localized_transcript.status = (
                "approved" if decision.decision == "approved" else "rejected"
            )
            episode.status = EpisodeStatus.ready

        if approval.stage == "research_review":
            if decision.decision == "approved":
                self._ensure_research_qc_allows_approval(episode)
                episode.status = EpisodeStatus.draft
            else:
                episode.status = EpisodeStatus.research_review

        if approval.stage in {"preview_render_review", "final_render_review"}:
            render_asset = self._target_approval_render_asset(episode, approval)
            if decision.decision == "approved":
                self._ensure_render_qc_allows_approval(episode, approval, render_asset)
            decided_at = datetime.now(UTC)
            render_asset.generation_metadata = {
                **render_asset.generation_metadata,
                "approval_status": decision.decision,
                "approval_id": str(approval.id),
                "approval_decided_at": decided_at.isoformat(),
                "approval_user_id": decision.user_id,
            }
            render_asset.updated_at = decided_at
            if decision.decision == "rejected":
                render_asset.status = "rejected"
            elif render_asset.status == "rejected":
                render_asset.status = "completed"

        if episode.status != previous_status:
            ProductionControlService().record_stage(
                episode,
                episode.status,
                "approval.decision.recorded",
            )
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="approval.decision.recorded",
                actor=decision.user_id or "system",
                details={
                    "approval_id": str(approval_id),
                    "stage": approval.stage,
                    "decision": decision.decision,
                    **(
                        {"claim_qc": claim_qc_acceptance}
                        if claim_qc_acceptance is not None
                        else {}
                    ),
                },
            )
        )
        return self.save(episode)

    def export_backup_data(self) -> dict:
        tables: dict[str, list[dict]] = {}
        with self._session_factory() as session:
            for record_class in BACKUP_RECORD_CLASSES:
                records = session.scalars(
                    select(record_class).order_by(record_class.id.asc())
                ).all()
                tables[record_class.__tablename__] = [
                    self._record_to_backup_row(record) for record in records
                ]
        return {
            "schema_version": "dialecticore.database.v1",
            "exported_at": datetime.now(UTC).isoformat(),
            "tables": tables,
            "record_counts": {table: len(rows) for table, rows in tables.items()},
        }

    def restore_backup_data(self, backup_data: dict, replace_existing: bool = True) -> dict:
        tables = backup_data.get("tables")
        if not isinstance(tables, dict):
            raise ValueError("backup database payload has no tables object")

        restored_counts: dict[str, int] = {}
        with self._session_factory() as session:
            if replace_existing:
                for record_class in reversed(BACKUP_RECORD_CLASSES):
                    session.execute(delete(record_class))
            for record_class in BACKUP_RECORD_CLASSES:
                table_name = record_class.__tablename__
                rows = tables.get(table_name, [])
                if not isinstance(rows, list):
                    raise ValueError(f"backup table {table_name} is not a list")
                restored_counts[table_name] = 0
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError(f"backup table {table_name} contains a non-object row")
                    kwargs = self._backup_row_to_record_kwargs(record_class, row)
                    session.merge(record_class(**kwargs))
                    restored_counts[table_name] += 1
            session.commit()
        return {
            "schema_version": backup_data.get("schema_version"),
            "replace_existing": replace_existing,
            "record_counts": restored_counts,
        }

    def _ensure_research_qc_allows_approval(self, episode: Episode) -> None:
        matching_results = [
            result
            for result in episode.quality_results
            if result.check_type == "evidence_pack_integrity"
        ]
        if not matching_results:
            raise ValueError("episode has no evidence-pack QC result")
        latest = matching_results[-1]
        if latest.status == "fail":
            raise ValueError("failing evidence-pack QC blocks research approval")

    def _ensure_canonical_transcript_qc_allows_approval(self, episode: Episode) -> None:
        canonical_id = episode.canonical_transcript_version_id
        if canonical_id is None:
            raise ValueError("episode has no canonical transcript")
        matching_results = [
            result
            for result in episode.quality_results
            if result.target_type == "transcript_version"
            and result.target_id == str(canonical_id)
            and result.check_type == "transcript_semantic_fidelity"
        ]
        if not matching_results:
            raise ValueError("canonical transcript has no semantic fidelity QC result")
        latest_result = matching_results[-1]
        if latest_result.status == "fail":
            raise ValueError("canonical transcript has failing semantic fidelity QC")

    def _target_approval_transcript(
        self,
        episode: Episode,
        approval: Approval,
    ) -> TranscriptVersion:
        if approval.target_type != "transcript_version" or approval.target_id is None:
            raise ValueError("approval does not target a transcript version")
        transcript = next(
            (item for item in episode.transcripts if str(item.id) == str(approval.target_id)),
            None,
        )
        if transcript is None:
            raise ValueError("approval target transcript was not found")
        return transcript

    def _ensure_localized_transcript_qc_allows_approval(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> None:
        matching_results = [
            result
            for result in episode.quality_results
            if result.target_type == "transcript_version"
            and result.target_id == str(transcript.id)
            and result.check_type == "localized_transcript_semantic_fidelity"
        ]
        if not matching_results:
            raise ValueError("localized transcript has no semantic fidelity QC result")
        latest_result = matching_results[-1]
        if latest_result.status == "fail":
            raise ValueError("localized transcript has failing semantic fidelity QC")

    def _ensure_render_qc_allows_approval(
        self,
        episode: Episode,
        approval: Approval,
        render_asset: Asset,
    ) -> None:
        if approval.stage == "preview_render_review":
            self._ensure_preview_covers_full_timeline(episode, render_asset)
        expected_check_type = (
            "render_preview_integrity"
            if approval.stage == "preview_render_review"
            else "render_final_integrity"
        )
        matching_results = [
            result
            for result in episode.quality_results
            if result.target_type == "render_asset"
            and result.target_id == str(render_asset.id)
            and result.check_type == expected_check_type
        ]
        if not matching_results:
            raise ValueError("render QC is required before render approval")
        latest_result = matching_results[-1]
        if latest_result.status == "fail" or latest_result.severity == QualitySeverity.fail:
            raise ValueError("failing render QC blocks render approval")

    def _ensure_preview_covers_full_timeline(
        self,
        episode: Episode,
        render_asset: Asset,
    ) -> None:
        if render_asset.generation_metadata.get("review_scope") != "full_timeline":
            raise ValueError(
                "preview is not a full-episode review render; rebuild the timeline "
                "and render a new preview"
            )
        if render_asset.generation_metadata.get("composition_policy") != "studio_camera_cuts.v1":
            raise ValueError(
                "preview uses the legacy full-frame composition policy; rebuild the timeline "
                "and render a studio camera-cuts preview before approval"
            )
        timeline_asset_id = render_asset.source_entity_id
        timeline_asset = next(
            (
                asset
                for asset in episode.assets
                if str(asset.id) == str(timeline_asset_id)
                and asset.asset_type == AssetType.timeline
                and asset.status == "completed"
            ),
            None,
        )
        timeline = (
            timeline_asset.generation_metadata.get("timeline_json")
            if timeline_asset is not None
            else None
        )
        expected_duration_ms = (
            int(timeline.get("duration_ms") or 0) if isinstance(timeline, dict) else 0
        )
        actual_duration_ms = int(render_asset.duration_ms or 0)
        fps = render_asset.generation_metadata.get("media_probe", {}).get("fps")
        try:
            tolerance_ms = max(1, int((1000 / float(fps)) + 0.999))
        except (TypeError, ValueError, ZeroDivisionError):
            tolerance_ms = 50
        if (
            expected_duration_ms <= 0
            or abs(actual_duration_ms - expected_duration_ms) > tolerance_ms
        ):
            raise ValueError(
                "preview does not cover the full episode timeline; rebuild the timeline "
                "and render a new preview"
            )
        probe = render_asset.generation_metadata.get("media_probe", {})
        av_offset_ms = probe.get("av_offset_ms") if isinstance(probe, dict) else None
        if not isinstance(av_offset_ms, int | float) or av_offset_ms > tolerance_ms:
            raise ValueError(
                "preview audio and video are not frame-aligned; render a new preview"
            )

    def _target_approval_render_asset(self, episode: Episode, approval: Approval) -> Asset:
        if approval.target_type != "render_asset" or approval.target_id is None:
            raise ValueError("render approval has no render target")
        asset = next(
            (
                item
                for item in episode.assets
                if item.id == UUID(approval.target_id) and item.asset_type == AssetType.render
            ),
            None,
        )
        if asset is None:
            raise ValueError("render approval target not found")
        expected_render_type = "preview" if approval.stage == "preview_render_review" else "final"
        if asset.generation_metadata.get("render_type") != expected_render_type:
            raise ValueError(
                f"{expected_render_type} render approval target is not a "
                f"{expected_render_type} render"
            )
        return asset

    def _slugify(self, title: str) -> str:
        slug = "".join(char.lower() if char.isalnum() else "-" for char in title)
        return "-".join(part for part in slug.split("-") if part)

    def _unique_slug(self, session: Session, base_slug: str) -> str:
        existing = set(
            session.scalars(
                select(EpisodeRecord.slug).where(EpisodeRecord.slug.like(f"{base_slug}%"))
            ).all()
        )
        if base_slug not in existing:
            return base_slug
        suffix = 2
        while f"{base_slug}-{suffix}" in existing:
            suffix += 1
        return f"{base_slug}-{suffix}"

    def _to_record(self, episode: Episode) -> EpisodeRecord:
        return EpisodeRecord(
            id=str(episode.id),
            title=episode.title,
            slug=episode.slug,
            status=episode.status.value,
            source_language=episode.source_language,
            target_duration_seconds=episode.target_duration_seconds,
            minimum_duration_seconds=episode.minimum_duration_seconds,
            maximum_duration_seconds=episode.maximum_duration_seconds,
            current_workflow_id=episode.current_workflow_id,
            canonical_transcript_version_id=(
                str(episode.canonical_transcript_version_id)
                if episode.canonical_transcript_version_id
                else None
            ),
            payload=self._episode_payload_for_record(episode),
            created_at=episode.created_at,
            updated_at=episode.updated_at,
        )

    def _from_record(self, record: EpisodeRecord) -> Episode:
        return Episode.model_validate(record.payload)

    def _episode_from_record_with_assets(
        self,
        session: Session,
        record: EpisodeRecord,
    ) -> Episode:
        episode = self._from_record(record)
        asset_records = session.scalars(
            select(AssetRecord)
            .where(AssetRecord.episode_id == record.id)
            .order_by(AssetRecord.created_at.asc(), AssetRecord.id.asc())
        ).all()
        if not asset_records:
            return episode
        return episode.model_copy(
            update={"assets": [self._asset_from_record(asset) for asset in asset_records]}
        )

    def _episode_payload_for_record(self, episode: Episode) -> dict:
        """Keep the hot episode record compact; AssetRecord retains full alignment data."""
        assets = [
            asset.model_copy(
                update={
                    "generation_metadata": {
                        key: value
                        for key, value in asset.generation_metadata.items()
                        if key not in _EPISODE_RECORD_TIMING_TRACK_KEYS
                    }
                }
            )
            for asset in episode.assets
        ]
        return episode.model_copy(update={"assets": assets}).model_dump(mode="json")

    def _summary_from_record(self, record: EpisodeRecord) -> EpisodeSummary:
        payload = record.payload if isinstance(record.payload, dict) else {}
        definition = self._payload_dict(payload.get("definition"))
        languages = self._payload_dict(definition.get("languages"))
        discussion = self._payload_dict(payload.get("discussion_session"))
        assets = self._payload_list(payload.get("assets"))
        pending_approvals = [
            Approval.model_validate(approval)
            for approval in self._payload_list(payload.get("approvals"))
            if isinstance(approval, dict)
            and approval.get("decision") == "pending"
            and _approval_is_actionable_in_summary(
                approval,
                assets,
                record.canonical_transcript_version_id,
            )
        ]
        output_languages = [
            output.get("language")
            for output in self._payload_list(languages.get("outputs"))
            if isinstance(output, dict) and output.get("language")
        ]
        return EpisodeSummary(
            id=UUID(record.id),
            project_id=UUID(project_id) if (project_id := payload.get("project_id")) else None,
            title=record.title,
            slug=record.slug,
            status=EpisodeStatus(record.status),
            source_language=record.source_language,
            target_duration_seconds=record.target_duration_seconds,
            minimum_duration_seconds=record.minimum_duration_seconds,
            maximum_duration_seconds=record.maximum_duration_seconds,
            current_workflow_id=record.current_workflow_id,
            canonical_transcript_version_id=(
                UUID(record.canonical_transcript_version_id)
                if record.canonical_transcript_version_id
                else None
            ),
            output_languages=output_languages,
            discussion_phase=self._payload_optional_str(discussion.get("phase")),
            discussion_status=self._payload_optional_str(discussion.get("status")),
            discussion_turn_count=len(self._payload_list(discussion.get("turns"))),
            estimated_duration_seconds=float(discussion.get("estimated_duration_seconds") or 0),
            transcript_count=len(self._payload_list(payload.get("transcripts"))),
            asset_count=len(self._payload_list(payload.get("assets"))),
            quality_result_count=len(self._payload_list(payload.get("quality_results"))),
            publish_job_count=len(self._payload_list(payload.get("publish_jobs"))),
            pending_approval_count=len(pending_approvals),
            pending_approvals=pending_approvals,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _summary_from_sqlite_row(self, row: object) -> EpisodeSummary:
        assets = self._payload_json_list(row.assets)
        pending_approvals = [
            Approval.model_validate(approval)
            for approval in self._payload_json_list(row.approvals)
            if isinstance(approval, dict)
            and approval.get("decision") == "pending"
            and _approval_is_actionable_in_summary(
                approval,
                assets,
                row.canonical_transcript_version_id,
            )
        ]
        output_languages = [
            output.get("language")
            for output in self._payload_json_list(row.outputs)
            if isinstance(output, dict) and output.get("language")
        ]
        return EpisodeSummary(
            id=UUID(row.id),
            project_id=UUID(row.project_id) if row.project_id else None,
            title=row.title,
            slug=row.slug,
            status=EpisodeStatus(row.status),
            source_language=row.source_language,
            target_duration_seconds=row.target_duration_seconds,
            minimum_duration_seconds=row.minimum_duration_seconds,
            maximum_duration_seconds=row.maximum_duration_seconds,
            current_workflow_id=row.current_workflow_id,
            canonical_transcript_version_id=(
                UUID(row.canonical_transcript_version_id)
                if row.canonical_transcript_version_id
                else None
            ),
            output_languages=output_languages,
            discussion_phase=self._payload_optional_str(row.discussion_phase),
            discussion_status=self._payload_optional_str(row.discussion_status),
            discussion_turn_count=int(row.discussion_turn_count or 0),
            estimated_duration_seconds=float(row.estimated_duration_seconds or 0),
            transcript_count=int(row.transcript_count or 0),
            asset_count=int(row.asset_count or 0),
            quality_result_count=int(row.quality_result_count or 0),
            publish_job_count=int(row.publish_job_count or 0),
            pending_approval_count=len(pending_approvals),
            pending_approvals=pending_approvals,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _payload_dict(self, value: object) -> dict:
        return value if isinstance(value, dict) else {}

    def _payload_list(self, value: object) -> list:
        return value if isinstance(value, list) else []

    def _payload_json_list(self, value: object) -> list:
        if isinstance(value, list):
            return value
        if not isinstance(value, str) or not value:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    def _payload_optional_str(self, value: object) -> str | None:
        return value if isinstance(value, str) else None

    def _sync_asset_projections(self, session: Session, episode: Episode) -> None:
        episode_id = str(episode.id)
        session.execute(delete(AssetRecord).where(AssetRecord.episode_id == episode_id))
        for asset in episode.assets:
            session.merge(self._asset_to_record(asset))

    def _asset_to_record(self, asset: Asset) -> AssetRecord:
        return AssetRecord(
            id=str(asset.id),
            episode_id=str(asset.episode_id),
            asset_type=asset.asset_type.value,
            language=asset.language,
            source_entity_type=asset.source_entity_type,
            source_entity_id=asset.source_entity_id,
            storage_uri=asset.storage_uri,
            mime_type=asset.mime_type,
            duration_ms=asset.duration_ms,
            width=asset.width,
            height=asset.height,
            fps=asset.fps,
            checksum=asset.checksum,
            status=asset.status,
            payload=asset.model_dump(mode="json"),
            created_at=asset.created_at,
            updated_at=asset.updated_at,
        )

    def _asset_from_record(self, record: AssetRecord) -> Asset:
        return Asset.model_validate(record.payload)

    def _sync_research_projections(self, session: Session, episode: Episode) -> None:
        episode_id = str(episode.id)
        session.execute(
            delete(ResearchSourceRecord).where(ResearchSourceRecord.episode_id == episode_id)
        )
        session.execute(
            delete(EvidenceClaimRecord).where(EvidenceClaimRecord.episode_id == episode_id)
        )
        evidence_asset = self._latest_evidence_pack_asset(episode)
        if evidence_asset is None:
            return
        pack = evidence_asset.generation_metadata.get("evidence_pack")
        if not isinstance(pack, dict):
            return
        synced_at = datetime.now(UTC)
        source_count = 0
        for source in pack.get("source_index", []):
            if not isinstance(source, dict):
                continue
            research_source = self._research_source_from_pack_source(
                episode.id,
                source,
                str(evidence_asset.id),
                synced_at,
            )
            session.merge(self._research_source_to_record(research_source, synced_at))
            source_count += 1
        claim_count = 0
        seen_claim_ids: set[str] = set()
        for category, status in self._evidence_claim_categories().items():
            for claim in pack.get(category, []):
                if not isinstance(claim, dict):
                    continue
                evidence_claim = self._evidence_claim_from_pack_claim(
                    episode.id,
                    claim,
                    category,
                    status,
                    str(evidence_asset.id),
                )
                if evidence_claim.id in seen_claim_ids:
                    continue
                seen_claim_ids.add(evidence_claim.id)
                session.merge(self._evidence_claim_to_record(evidence_claim, synced_at))
                claim_count += 1
        if source_count or claim_count:
            self._record_global_audit(
                session,
                AuditEvent(
                    episode_id=episode.id,
                    event_type="research.projections.synced",
                    details={
                        "evidence_pack_asset_id": str(evidence_asset.id),
                        "source_count": source_count,
                        "claim_count": claim_count,
                    },
                ),
            )

    def _latest_evidence_pack_asset(self, episode: Episode) -> Asset | None:
        completed_assets = [
            asset
            for asset in episode.assets
            if asset.asset_type == AssetType.evidence_pack and asset.status == "completed"
        ]
        if not completed_assets:
            return None
        return max(completed_assets, key=lambda asset: asset.created_at)

    def _research_source_from_pack_source(
        self,
        episode_id: UUID,
        source: dict,
        evidence_pack_asset_id: str,
        default_retrieved_at: datetime,
    ) -> ResearchSource:
        retrieved_at = self._coerce_datetime(source.get("retrieved_at"), default_retrieved_at)
        metadata = {
            key: value
            for key, value in source.items()
            if key
            not in {
                "id",
                "uri",
                "title",
                "author",
                "publisher",
                "published_at",
                "retrieved_at",
                "content_checksum",
                "source_type",
                "confidence",
            }
        }
        metadata["evidence_pack_asset_id"] = evidence_pack_asset_id
        return ResearchSource(
            id=str(source.get("id") or f"source-{evidence_pack_asset_id}"),
            episode_id=episode_id,
            url=source.get("uri"),
            title=str(source.get("title") or "Untitled source"),
            publisher=source.get("publisher") or source.get("author"),
            published_at=source.get("published_at"),
            retrieved_at=retrieved_at,
            content_hash=source.get("content_checksum"),
            source_type=str(source.get("source_type") or "unknown"),
            credibility_score=float(source.get("confidence") or 0.5),
            metadata=metadata,
        )

    def _evidence_claim_from_pack_claim(
        self,
        episode_id: UUID,
        claim: dict,
        category: str,
        default_status: str,
        evidence_pack_asset_id: str,
    ) -> EvidenceClaim:
        statement = str(claim.get("statement") or claim.get("text") or "Untitled claim")
        supporting_source_ids = [
            str(item)
            for item in claim.get("supporting_source_ids") or claim.get("evidence_refs") or []
        ]
        contradicting_source_ids = [
            str(item)
            for item in claim.get("contradicting_source_ids")
            or claim.get("contradicting_evidence_refs")
            or []
        ]
        metadata = dict(claim.get("extraction_metadata") or {})
        metadata["evidence_pack_asset_id"] = evidence_pack_asset_id
        metadata["evidence_pack_category"] = category
        notes = str(claim.get("notes") or claim.get("uncertainty") or "")
        return EvidenceClaim(
            id=str(claim.get("id") or self._projection_hash_id(statement)),
            episode_id=episode_id,
            statement=statement,
            text=statement,
            claim_type=str(claim.get("claim_type") or category),
            confidence=float(claim.get("confidence") or 0.5),
            status=self._claim_status(claim.get("status"), default_status),
            supporting_source_ids=supporting_source_ids,
            contradicting_source_ids=contradicting_source_ids,
            notes=notes,
            evidence_refs=supporting_source_ids,
            contradicting_evidence_refs=contradicting_source_ids,
            uncertainty=claim.get("uncertainty"),
            extraction_metadata=metadata,
        )

    def _research_source_to_record(
        self,
        source: ResearchSource,
        synced_at: datetime,
    ) -> ResearchSourceRecord:
        return ResearchSourceRecord(
            id=source.id,
            episode_id=str(source.episode_id),
            url=source.url,
            title=source.title,
            publisher=source.publisher,
            published_at=source.published_at,
            retrieved_at=source.retrieved_at,
            content_hash=source.content_hash,
            source_type=source.source_type,
            credibility_score=source.credibility_score,
            payload=source.model_dump(mode="json"),
            created_at=synced_at,
            updated_at=synced_at,
        )

    def _research_source_from_record(self, record: ResearchSourceRecord) -> ResearchSource:
        return ResearchSource.model_validate(record.payload)

    def _evidence_claim_to_record(
        self,
        claim: EvidenceClaim,
        synced_at: datetime,
    ) -> EvidenceClaimRecord:
        statement = claim.statement or claim.text
        return EvidenceClaimRecord(
            id=claim.id,
            episode_id=str(claim.episode_id),
            statement=statement,
            claim_type=claim.claim_type,
            confidence=claim.confidence,
            status=claim.status,
            payload=claim.model_dump(mode="json"),
            created_at=synced_at,
            updated_at=synced_at,
        )

    def _evidence_claim_from_record(self, record: EvidenceClaimRecord) -> EvidenceClaim:
        return EvidenceClaim.model_validate(record.payload)

    def _coerce_datetime(self, value: object, default: datetime) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return default
        return default

    def _projection_hash_id(self, value: str) -> str:
        return f"claim-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"

    def _claim_status(self, value: object, default_status: str) -> str:
        allowed = {
            "verified",
            "supported",
            "uncertain",
            "opinion",
            "prediction",
            "unsupported",
            "contradicted",
        }
        candidate = str(value or default_status)
        return candidate if candidate in allowed else default_status

    def _evidence_claim_categories(self) -> dict[str, str]:
        return {
            "definitions": "verified",
            "verified_facts": "verified",
            "supported_claims": "supported",
            "uncertain_claims": "uncertain",
            "disputed_claims": "contradicted",
            "competing_interpretations": "opinion",
            "important_statistics": "supported",
        }

    def _sync_episode_audit_events(self, session: Session, episode: Episode) -> None:
        for event in episode.audit_events:
            session.merge(self._audit_to_record(event))

    def _record_global_audit(self, session: Session, event: AuditEvent) -> None:
        session.add(self._audit_to_record(event))

    def _credential_reference_audit_details(self, reference: str | None) -> dict:
        try:
            configured = normalize_credential_reference(reference) is not None
        except ValueError:
            configured = True
        return {
            "credential_reference_configured": configured,
            "credential_reference_scheme": (
                credential_reference_scheme(reference) if configured else None
            ),
        }

    def _audit_to_record(self, event: AuditEvent) -> AuditEventRecord:
        return AuditEventRecord(
            id=str(event.id),
            episode_id=str(event.episode_id) if event.episode_id else None,
            event_type=event.event_type,
            actor=event.actor,
            details=event.details,
            created_at=event.created_at,
        )

    def _audit_from_record(self, record: AuditEventRecord) -> AuditEvent:
        return AuditEvent(
            id=UUID(record.id),
            episode_id=UUID(record.episode_id) if record.episode_id else None,
            event_type=record.event_type,
            actor=record.actor,
            details=record.details,
            created_at=record.created_at,
        )

    def _record_to_backup_row(self, record: object) -> dict:
        row = {}
        for column in record.__table__.columns:
            value = getattr(record, column.name)
            row[column.name] = value.isoformat() if isinstance(value, datetime) else value
        return row

    def _backup_row_to_record_kwargs(self, record_class: type, row: dict) -> dict:
        kwargs = {}
        for column in record_class.__table__.columns:
            value = row.get(column.name)
            if (
                value is not None
                and isinstance(column.type, SqlDateTime)
                and isinstance(value, str)
            ):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            kwargs[column.name] = value
        return kwargs

    def _project_to_record(self, project: Project) -> ProjectRecord:
        return ProjectRecord(
            id=str(project.id),
            name=project.name,
            description=project.description,
            default_language=project.default_language,
            default_show_format_id=project.default_show_format_id,
            payload=project.model_dump(mode="json"),
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    def _project_from_record(self, record: ProjectRecord) -> Project:
        return Project.model_validate(record.payload)

    def _language_profile_to_record(self, profile: LanguageProfile) -> LanguageProfileRecord:
        now = datetime.now(UTC)
        return LanguageProfileRecord(
            id=profile.id,
            name=profile.name,
            bcp47_tag=profile.bcp47_tag,
            default_mode=profile.default_mode,
            subtitle_direction=profile.subtitle_direction,
            enabled=profile.enabled,
            payload=profile.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _language_profile_from_record(self, record: LanguageProfileRecord) -> LanguageProfile:
        return LanguageProfile.model_validate(record.payload)

    def _seed_default_language_profiles(self) -> None:
        with self._session_factory() as session:
            count = session.scalar(select(LanguageProfileRecord.id).limit(1))
            if count is not None:
                return
            for profile in default_language_profiles():
                session.add(self._language_profile_to_record(profile))
            session.commit()

    def _episode_uses_language(self, record: EpisodeRecord, language: str) -> bool:
        if record.source_language == language:
            return True
        definition = record.payload.get("definition") if isinstance(record.payload, dict) else None
        languages = definition.get("languages") if isinstance(definition, dict) else None
        outputs = languages.get("outputs") if isinstance(languages, dict) else []
        if not isinstance(outputs, list):
            return False
        return any(
            isinstance(output, dict) and output.get("language") == language for output in outputs
        )

    def _seed_default_model_endpoints(self) -> None:
        with self._session_factory() as session:
            count = session.scalar(select(ModelEndpointRecord.id).limit(1))
            if count is not None:
                return
            for endpoint in default_model_endpoints():
                session.add(self._endpoint_to_record(endpoint))
            session.commit()

    def _seed_default_participant_profiles(self) -> None:
        with self._session_factory() as session:
            existing_ids = set(session.scalars(select(ParticipantProfileRecord.id)).all())
            for profile in default_participants():
                if profile.id not in existing_ids:
                    session.add(self._profile_to_record(profile))
            session.commit()

    def _backfill_frontier_participant_prompt_templates(self) -> None:
        """Upgrade only untouched frontier defaults, leaving custom templates alone."""
        default_templates = {
            profile.id: profile.system_prompt_template
            for profile in default_participants()
            if profile.id in {"chatgpt", "claude", "deepseek", "gemini", "grok", "mistral"}
        }
        legacy_templates = {"moderator_v1", "panelist_v1"}
        with self._session_factory() as session:
            changed = False
            for participant_id, template_id in default_templates.items():
                record = session.get(ParticipantProfileRecord, participant_id)
                if record is None:
                    continue
                profile = self._profile_from_record(record)
                if profile.system_prompt_template not in legacy_templates:
                    continue
                profile.system_prompt_template = template_id
                record.payload = profile.model_dump(mode="json")
                record.updated_at = datetime.now(UTC)
                self._record_global_audit(
                    session,
                    AuditEvent(
                        event_type="participant_profile.prompt_template_backfilled",
                        details={
                            "profile_id": profile.id,
                            "prompt_template_id": template_id,
                        },
                    ),
                )
                changed = True
            if changed:
                session.commit()

    def _repair_frontier_participant_role_template_mismatches(self) -> None:
        """Repair legacy character records where an episode role leaked into global setup."""
        defaults = {
            profile.id: profile
            for profile in default_participants()
            if profile.id in {"chatgpt", "claude", "deepseek", "gemini", "grok", "mistral"}
        }
        default_template_ids = {profile.system_prompt_template for profile in defaults.values()}
        with self._session_factory() as session:
            template_types = dict(
                session.execute(
                    select(
                        DiscussionPromptTemplateRecord.id,
                        DiscussionPromptTemplateRecord.participant_type,
                    )
                ).all()
            )
            changed = False
            for participant_id, default in defaults.items():
                record = session.get(ParticipantProfileRecord, participant_id)
                if record is None:
                    continue
                profile = self._profile_from_record(record)
                template_type = template_types.get(profile.system_prompt_template)
                if template_type == profile.participant_type.value:
                    continue
                if profile.system_prompt_template not in default_template_ids:
                    continue
                previous_type = profile.participant_type.value
                previous_template = profile.system_prompt_template
                profile.participant_type = default.participant_type
                profile.system_prompt_template = default.system_prompt_template
                record.participant_type = profile.participant_type.value
                record.payload = profile.model_dump(mode="json")
                record.updated_at = datetime.now(UTC)
                self._record_global_audit(
                    session,
                    AuditEvent(
                        event_type="participant_profile.role_template_repaired",
                        details={
                            "profile_id": profile.id,
                            "previous_participant_type": previous_type,
                            "previous_prompt_template": previous_template,
                            "participant_type": profile.participant_type.value,
                            "prompt_template_id": profile.system_prompt_template,
                            "reason": "episode_role_must_not_mutate_global_character",
                        },
                    ),
                )
                changed = True
            if changed:
                session.commit()

    def _backfill_default_participant_voice_profiles(self) -> None:
        defaults = {
            profile.id: profile.voice_profile_id
            for profile in default_participants()
            if profile.voice_profile_id is not None
        }
        with self._session_factory() as session:
            changed = False
            for participant_id, voice_profile_id in defaults.items():
                record = session.get(ParticipantProfileRecord, participant_id)
                if record is None:
                    continue
                profile = self._profile_from_record(record)
                if profile.voice_profile_id is not None:
                    continue
                profile.voice_profile_id = voice_profile_id
                record.payload = profile.model_dump(mode="json")
                record.updated_at = datetime.now(UTC)
                self._record_global_audit(
                    session,
                    AuditEvent(
                        event_type="participant_profile.voice_profile_backfilled",
                        details={
                            "profile_id": profile.id,
                            "voice_profile_id": voice_profile_id,
                        },
                    ),
                )
                changed = True
            if changed:
                session.commit()

    def _backfill_default_participant_visual_profiles(self) -> None:
        defaults = {
            profile.id: profile.visual_profile_id
            for profile in default_participants()
            if profile.visual_profile_id is not None
        }
        with self._session_factory() as session:
            changed = False
            for participant_id, visual_profile_id in defaults.items():
                record = session.get(ParticipantProfileRecord, participant_id)
                if record is None:
                    continue
                profile = self._profile_from_record(record)
                if profile.visual_profile_id is not None:
                    continue
                profile.visual_profile_id = visual_profile_id
                record.payload = profile.model_dump(mode="json")
                record.updated_at = datetime.now(UTC)
                self._record_global_audit(
                    session,
                    AuditEvent(
                        event_type="participant_profile.visual_profile_backfilled",
                        details={
                            "profile_id": profile.id,
                            "visual_profile_id": visual_profile_id,
                        },
                    ),
                )
                changed = True
            if changed:
                session.commit()

    def _seed_default_voicebox_endpoints(self) -> None:
        with self._session_factory() as session:
            count = session.scalar(select(VoiceboxEndpointRecord.id).limit(1))
            if count is not None:
                return
            for endpoint in default_voicebox_endpoints():
                session.add(self._voicebox_endpoint_to_record(endpoint))
            session.commit()

    def _seed_default_voice_profiles(self) -> None:
        with self._session_factory() as session:
            existing_ids = set(session.scalars(select(VoiceProfileRecord.id)).all())
            for profile in default_voice_profiles():
                if profile.id not in existing_ids:
                    session.add(self._voice_profile_to_record(profile))
            session.commit()

    def _seed_default_comfyui_endpoints(self) -> None:
        with self._session_factory() as session:
            count = session.scalar(select(ComfyUiEndpointRecord.id).limit(1))
            if count is not None:
                return
            for endpoint in default_comfyui_endpoints():
                session.add(self._comfyui_endpoint_to_record(endpoint))
            session.commit()

    def _seed_default_comfyui_workflows(self) -> None:
        with self._session_factory() as session:
            existing_ids = set(session.scalars(select(ComfyUiWorkflowRecord.id)).all())
            for workflow in default_comfyui_workflows():
                if workflow.id not in existing_ids:
                    session.add(self._comfyui_workflow_to_record(workflow))
            session.commit()

    def _seed_default_discussion_prompt_templates(self) -> None:
        with self._session_factory() as session:
            existing_ids = set(session.scalars(select(DiscussionPromptTemplateRecord.id)).all())
            for template in default_discussion_prompt_templates():
                if template.id not in existing_ids:
                    session.add(self._discussion_prompt_template_to_record(template))
            session.commit()

    def _seed_default_visual_profiles(self) -> None:
        with self._session_factory() as session:
            existing_ids = set(session.scalars(select(VisualProfileRecord.id)).all())
            for profile in default_visual_profiles():
                if profile.id not in existing_ids:
                    session.add(self._visual_profile_to_record(profile))
            session.commit()

    def _seed_default_publisher_targets(self) -> None:
        with self._session_factory() as session:
            count = session.scalar(select(PublisherTargetRecord.id).limit(1))
            if count is not None:
                return
            for target in default_publisher_targets():
                session.add(self._publisher_target_to_record(target))
            session.commit()

    def _endpoint_to_record(self, endpoint: ModelEndpoint) -> ModelEndpointRecord:
        now = datetime.now(UTC)
        return ModelEndpointRecord(
            id=endpoint.id,
            name=endpoint.name,
            provider_type=endpoint.provider_type.value,
            base_url=endpoint.base_url,
            credential_reference=endpoint.credential_reference,
            enabled=endpoint.enabled,
            health_status=endpoint.health_status,
            payload=endpoint.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _endpoint_from_record(self, record: ModelEndpointRecord) -> ModelEndpoint:
        return ModelEndpoint.model_validate(record.payload)

    def _profile_to_record(self, profile: ParticipantProfile) -> ParticipantProfileRecord:
        now = datetime.now(UTC)
        return ParticipantProfileRecord(
            id=profile.id,
            name=profile.name,
            display_name=profile.display_name,
            participant_type=profile.participant_type.value,
            model_endpoint_id=profile.model_endpoint_id,
            model_id=profile.model_id,
            enabled=profile.enabled,
            payload=profile.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _profile_from_record(self, record: ParticipantProfileRecord) -> ParticipantProfile:
        return ParticipantProfile.model_validate(record.payload)

    def _voicebox_endpoint_to_record(
        self,
        endpoint: VoiceboxEndpoint,
    ) -> VoiceboxEndpointRecord:
        now = datetime.now(UTC)
        return VoiceboxEndpointRecord(
            id=endpoint.id,
            name=endpoint.name,
            adapter_type=endpoint.adapter_type,
            base_url=endpoint.base_url,
            credential_reference=endpoint.credential_reference,
            enabled=endpoint.enabled,
            health_status=endpoint.health_status,
            payload=endpoint.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _voicebox_endpoint_from_record(
        self,
        record: VoiceboxEndpointRecord,
    ) -> VoiceboxEndpoint:
        return VoiceboxEndpoint.model_validate(record.payload)

    def _voice_profile_to_record(self, profile: VoiceProfile) -> VoiceProfileRecord:
        now = datetime.now(UTC)
        return VoiceProfileRecord(
            id=profile.id,
            name=profile.name,
            voicebox_endpoint_id=profile.voicebox_endpoint_id,
            voice_id=profile.voice_id,
            language=profile.language,
            enabled=profile.enabled,
            payload=profile.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _voice_profile_from_record(self, record: VoiceProfileRecord) -> VoiceProfile:
        return VoiceProfile.model_validate(record.payload)

    def _primer_narrator_profile_to_record(
        self, profile: PrimerNarratorProfile
    ) -> PrimerNarratorProfileRecord:
        return PrimerNarratorProfileRecord(
            id=profile.id,
            name=profile.name,
            language=profile.language,
            model_endpoint_id=profile.model_endpoint_id,
            model_id=profile.model_id,
            voice_profile_id=profile.voice_profile_id,
            enabled=profile.enabled,
            payload=profile.model_dump(mode="json"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def _primer_narrator_profile_from_record(
        self, record: PrimerNarratorProfileRecord
    ) -> PrimerNarratorProfile:
        return PrimerNarratorProfile.model_validate(record.payload)

    def _comfyui_endpoint_to_record(
        self,
        endpoint: ComfyUiEndpoint,
    ) -> ComfyUiEndpointRecord:
        now = datetime.now(UTC)
        return ComfyUiEndpointRecord(
            id=endpoint.id,
            name=endpoint.name,
            adapter_type=endpoint.adapter_type,
            base_url=endpoint.base_url,
            credential_reference=endpoint.credential_reference,
            enabled=endpoint.enabled,
            health_status=endpoint.health_status,
            payload=endpoint.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _comfyui_endpoint_from_record(
        self,
        record: ComfyUiEndpointRecord,
    ) -> ComfyUiEndpoint:
        return ComfyUiEndpoint.model_validate(record.payload)

    def _comfyui_workflow_to_record(
        self,
        workflow: ComfyUiWorkflow,
    ) -> ComfyUiWorkflowRecord:
        now = datetime.now(UTC)
        return ComfyUiWorkflowRecord(
            id=workflow.id,
            name=workflow.name,
            workflow_type=workflow.workflow_type,
            version=workflow.version,
            comfyui_endpoint_id=workflow.comfyui_endpoint_id,
            output_asset_type=workflow.output_asset_type.value,
            enabled=workflow.enabled,
            payload=workflow.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _comfyui_workflow_from_record(
        self,
        record: ComfyUiWorkflowRecord,
    ) -> ComfyUiWorkflow:
        return ComfyUiWorkflow.model_validate(record.payload)

    def _discussion_prompt_template_to_record(
        self,
        template: DiscussionPromptTemplate,
    ) -> DiscussionPromptTemplateRecord:
        now = datetime.now(UTC)
        return DiscussionPromptTemplateRecord(
            id=template.id,
            version=template.version,
            participant_type=template.participant_type.value,
            enabled=template.enabled,
            payload=template.model_dump(mode="json"),
            created_at=template.created_at,
            updated_at=now,
        )

    def _discussion_prompt_template_from_record(
        self,
        record: DiscussionPromptTemplateRecord,
    ) -> DiscussionPromptTemplate:
        return DiscussionPromptTemplate.model_validate(record.payload)

    def _visual_profile_to_record(self, profile: VisualProfile) -> VisualProfileRecord:
        now = datetime.now(UTC)
        return VisualProfileRecord(
            id=profile.id,
            name=profile.name,
            character_name=profile.character_name,
            primary_workflow_id=profile.primary_workflow_id,
            enabled=profile.enabled,
            payload=profile.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _visual_profile_from_record(self, record: VisualProfileRecord) -> VisualProfile:
        return VisualProfile.model_validate(record.payload)

    def _publisher_target_to_record(
        self,
        target: PublisherTarget,
    ) -> PublisherTargetRecord:
        now = datetime.now(UTC)
        return PublisherTargetRecord(
            id=target.id,
            name=target.name,
            platform=target.platform,
            adapter_type=target.adapter_type,
            base_url=target.base_url,
            credential_reference=target.credential_reference,
            channel_id=target.channel_id,
            enabled=target.enabled,
            health_status=target.health_status,
            payload=target.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _publisher_target_from_record(
        self,
        record: PublisherTargetRecord,
    ) -> PublisherTarget:
        return PublisherTarget.model_validate(record.payload)
