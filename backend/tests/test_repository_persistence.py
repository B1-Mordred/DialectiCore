from pathlib import Path
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.domain.defaults import (
    B1_CHARACTER_VOICE_ASSIGNMENTS,
    default_comfyui_workflows,
    default_discussion_prompt_templates,
    default_model_endpoints,
    default_participants,
    default_voice_profiles,
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
    AudioAssetPlanRequest,
    AudioGenerationRequest,
    AuditEvent,
    ComfyUiEndpoint,
    ComfyUiWorkflow,
    DiscussionPromptTemplate,
    Episode,
    EpisodeCreateRequest,
    EpisodeDefinition,
    LanguageProfile,
    LocalizationRequest,
    ModelEndpoint,
    ParticipantProfile,
    Project,
    PublisherTarget,
    QualityResult,
    ResearchBuildRequest,
    SubtitleGenerationRequest,
    TranscriptTurn,
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
from app.infrastructure.models import ParticipantProfileRecord
from app.infrastructure.repository import EpisodeRepository
from app.services.discussion_engine import DiscussionEngine
from app.services.localization_service import LocalizationService
from app.services.model_gateway import ModelGateway
from app.services.production_control_service import ProductionControlService
from app.services.research_service import ResearchService
from app.services.subtitle_service import SubtitleService
from app.services.voicebox_service import VoiceboxService
from tests.test_discussion_engine import definition
from tests.test_research_service import research_definition


def persistent_repository(db_path: Path) -> EpisodeRepository:
    engine = create_database_engine(Settings(database_url=f"sqlite:///{db_path}"))
    initialize_database(engine)
    return EpisodeRepository(create_session_factory(engine))


def test_discussion_prompt_templates_are_persisted_versioned_and_guarded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)

    defaults = repo.list_discussion_prompt_templates()
    default_ids = {template.id for template in default_discussion_prompt_templates()}
    assert default_ids <= {template.id for template in defaults}
    assert all(template.version for template in defaults)
    assert all(template.created_by for template in defaults)
    assert all(template.change_summary for template in defaults)

    created = repo.upsert_discussion_prompt_template(
        DiscussionPromptTemplate(
            id="operator_panelist_v2",
            version="2.0.0",
            participant_type="panelist",
            system="You are {display_name}. Return only JSON.",
            user="Question: {central_question}\nPublic transcript:\n{public_transcript}",
            variables={"turn_context": ["central_question", "public_transcript"]},
            created_by="producer",
            change_summary="Operator-tuned panelist prompt.",
        )
    )
    assert created.version == "2.0.0"

    reloaded = persistent_repository(db_path).get_discussion_prompt_template(
        "operator_panelist_v2"
    )
    assert reloaded.created_by == "producer"
    assert reloaded.variables["turn_context"] == ["central_question", "public_transcript"]

    profile = default_participants()[1]
    profile.id = "custom-template-panelist"
    profile.system_prompt_template = "operator_panelist_v2"
    repo.upsert_participant_profile(profile)
    with pytest.raises(ValueError, match="still used by participant profiles"):
        repo.delete_discussion_prompt_template("operator_panelist_v2")

    repo.delete_participant_profile("custom-template-panelist")
    repo.delete_discussion_prompt_template("operator_panelist_v2")
    with pytest.raises(KeyError):
        repo.get_discussion_prompt_template("operator_panelist_v2")


def test_participant_profiles_require_enabled_matching_prompt_templates(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    repo.upsert_discussion_prompt_template(
        DiscussionPromptTemplate(
            id="disabled_panelist_v1",
            version="1.0.0",
            participant_type="panelist",
            system="You are {display_name}.",
            user="Question: {central_question}",
            enabled=False,
        )
    )
    profile = default_participants()[1]
    profile.id = "disabled-template-panelist"
    profile.system_prompt_template = "disabled_panelist_v1"

    with pytest.raises(ValueError, match="disabled discussion prompt template"):
        repo.upsert_participant_profile(profile)

    profile.system_prompt_template = "moderator_v1"
    with pytest.raises(ValueError, match="participant type does not match"):
        repo.upsert_participant_profile(profile)


def test_repository_repairs_frontier_role_template_mismatch_without_losing_persona(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    session_factory = repo._session_factory
    with repo._session_factory() as session:
        record = session.get(ParticipantProfileRecord, "chatgpt")
        assert record is not None
        profile = repo._profile_from_record(record)
        profile.participant_type = ParticipantType.panelist
        profile.system_prompt_template = "moderator_v2"
        profile.perspective = "customized character perspective"
        record.participant_type = "panelist"
        record.payload = profile.model_dump(mode="json")
        session.commit()

    repaired = EpisodeRepository(session_factory)
    chatgpt = repaired.get_participant_profile("chatgpt")

    assert chatgpt.participant_type == "host"
    assert chatgpt.system_prompt_template == "moderator_v2"
    assert chatgpt.perspective == "customized character perspective"


def test_episode_creation_applies_assignment_roles_without_mutating_profiles(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    definition_with_reassigned_roles = EpisodeDefinition.model_validate(
        {
            "title": "Assigned frontier roles",
            "topic": {
                "central_question": "How should frontier models debate product tradeoffs?",
                "required_dimensions": ["reasoning", "cost", "tone"],
            },
            "format": {"target_duration_minutes": 2, "participant_count": 4},
            "participants": [
                {"participant_profile_id": "claude", "role": "moderator"},
                {"participant_profile_id": "chatgpt", "role": "panelist"},
                {"participant_profile_id": "deepseek", "role": "panelist"},
                {"participant_profile_id": "mistral", "role": "panelist"},
            ],
        }
    )

    episode = repo.create(EpisodeCreateRequest(definition=definition_with_reassigned_roles))
    reloaded = persistent_repository(db_path).get(episode.id)

    episode_type_by_id = {
        participant.id: participant.participant_type for participant in reloaded.participants
    }
    default_type_by_id = {
        participant.id: participant.participant_type
        for participant in persistent_repository(db_path).list_participant_profiles()
    }
    assert episode_type_by_id["claude"] == "host"
    assert episode_type_by_id["chatgpt"] == "panelist"
    assert default_type_by_id["claude"] == "panelist"
    assert default_type_by_id["chatgpt"] == "host"


def test_episode_assignments_apply_guest_fact_checker_and_audience_roles(tmp_path: Path) -> None:
    repo = persistent_repository(tmp_path / "dialecticore.db")
    definition_with_episode_roles = EpisodeDefinition.model_validate(
        {
            "title": "Episode-scoped cast roles",
            "topic": {
                "central_question": "How should the cast divide discussion responsibilities?",
                "required_dimensions": ["roles", "evidence", "audience"],
            },
            "format": {"target_duration_minutes": 2, "participant_count": 4},
            "participants": [
                {"participant_profile_id": "chatgpt", "role": "moderator"},
                {"participant_profile_id": "claude", "role": "guest"},
                {"participant_profile_id": "deepseek", "role": "fact_checker"},
                {"participant_profile_id": "mistral", "role": "audience_proxy"},
            ],
        }
    )

    episode = repo.create(EpisodeCreateRequest(definition=definition_with_episode_roles))
    assigned = {participant.id: participant for participant in episode.participants}

    assert assigned["chatgpt"].participant_type == "host"
    assert assigned["chatgpt"].system_prompt_template == "moderator_v2"
    assert assigned["claude"].participant_type == "guest"
    assert assigned["claude"].system_prompt_template == "guest_v1"
    assert assigned["deepseek"].participant_type == "fact_checker"
    assert assigned["deepseek"].system_prompt_template == "fact_checker_v1"
    assert assigned["mistral"].participant_type == "audience_proxy"
    assert assigned["mistral"].system_prompt_template == "audience_proxy_v1"


def test_render_approval_requires_non_failing_render_qc(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    episode = repo.create(EpisodeCreateRequest(definition=definition()))
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={"render_type": "final", "approval_status": "pending"},
    )
    approval = Approval(
        episode_id=episode.id,
        stage="final_render_review",
        target_type="render_asset",
        target_id=str(render_asset.id),
    )
    episode.assets.append(render_asset)
    episode.approvals.append(approval)
    repo.save(episode)

    request = ApprovalDecisionRequest(decision="approved", user_id="producer-1")
    with pytest.raises(ValueError, match="render QC is required before render approval"):
        repo.record_approval_decision(episode.id, approval.id, request)

    episode = repo.get(episode.id)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(render_asset.id),
            check_type="render_final_integrity",
            severity=QualitySeverity.fail,
            status="fail",
            score=0.0,
            details={"issues": [{"severity": "fail", "issue": "runtime_out_of_bounds"}]},
        )
    )
    repo.save(episode)

    with pytest.raises(ValueError, match="failing render QC blocks render approval"):
        repo.record_approval_decision(episode.id, approval.id, request)

    episode = repo.get(episode.id)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(render_asset.id),
            check_type="render_final_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
            details={"failure_count": 0, "warning_count": 0},
        )
    )
    repo.save(episode)
    decided = repo.record_approval_decision(episode.id, approval.id, request)
    approved_asset = next(asset for asset in decided.assets if asset.id == render_asset.id)
    assert approved_asset.generation_metadata["approval_status"] == "approved"


def test_production_v2_qualification_approval_updates_custom_workflow_gate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    episode = repo.create(EpisodeCreateRequest(definition=definition()))
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        source_entity_type="production_v2_qualification",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/qualification/v2.mp4",
        mime_type="video/mp4",
        checksum="sha256:qualification-v2",
        status="completed",
        generation_metadata={"approval_status": "pending"},
    )
    approval = Approval(
        episode_id=episode.id,
        stage="production_v2_integrated_qualification_review",
        target_type="render_asset",
        target_id=str(render_asset.id),
    )
    episode.assets.append(render_asset)
    episode.approvals.append(approval)
    episode.workflow_control["production_v2_qualification"] = {
        "schema_version": "dialecticore.production_v2.qualification.v2",
        "render_asset_id": str(render_asset.id),
        "approval_id": str(approval.id),
        "status": "pending_review",
    }
    repo.save(episode)

    decided = repo.record_approval_decision(
        episode.id,
        approval.id,
        ApprovalDecisionRequest(decision="approved", user_id="producer"),
    )

    decided_render = next(asset for asset in decided.assets if asset.id == render_asset.id)
    control = decided.workflow_control["production_v2_qualification"]
    assert decided_render.generation_metadata["approval_status"] == "approved"
    assert control["status"] == "approved_for_full_production"
    assert control["decided_by"] == "producer"


def test_preview_approval_requires_a_full_timeline_render(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    episode = repo.create(EpisodeCreateRequest(definition=definition()))
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/timelines/timeline.json",
        mime_type="application/json",
        checksum="sha256:timeline",
        status="completed",
        generation_metadata={"timeline_json": {"duration_ms": 60_000}},
    )
    preview_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/preview.mp4",
        mime_type="video/mp4",
        duration_ms=30_000,
        checksum="sha256:preview",
        status="completed",
        generation_metadata={"render_type": "preview", "approval_status": "pending"},
    )
    approval = Approval(
        episode_id=episode.id,
        stage="preview_render_review",
        target_type="render_asset",
        target_id=str(preview_asset.id),
    )
    episode.assets.extend([timeline_asset, preview_asset])
    episode.approvals.append(approval)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(preview_asset.id),
            check_type="render_preview_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
            details={"failure_count": 0, "warning_count": 0},
        )
    )
    repo.save(episode)

    with pytest.raises(ValueError, match="full-episode review render"):
        repo.record_approval_decision(
            episode.id,
            approval.id,
            ApprovalDecisionRequest(decision="approved", user_id="producer-1"),
        )


def test_preview_approval_requires_frame_scheduled_timing(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    episode = repo.create(EpisodeCreateRequest(definition=definition()))
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/timelines/timeline.json",
        mime_type="application/json",
        checksum="sha256:timeline",
        status="completed",
        generation_metadata={"timeline_json": {"duration_ms": 60_000}},
    )
    preview_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/preview.mp4",
        mime_type="video/mp4",
        duration_ms=60_000,
        checksum="sha256:preview",
        status="completed",
        generation_metadata={
            "render_type": "preview",
            "review_scope": "full_timeline",
            "composition_policy": "studio_camera_cuts.v1",
            "media_probe": {"fps": 24, "av_offset_ms": 0},
            "approval_status": "pending",
        },
    )
    approval = Approval(
        episode_id=episode.id,
        stage="preview_render_review",
        target_type="render_asset",
        target_id=str(preview_asset.id),
    )
    episode.assets.extend([timeline_asset, preview_asset])
    episode.approvals.append(approval)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(preview_asset.id),
            check_type="render_preview_integrity",
            severity=QualitySeverity.warning,
            status="warning",
            score=1.0,
            details={"failure_count": 0, "warning_count": 1},
        )
    )
    repo.save(episode)

    approved = repo.record_approval_decision(
        episode.id,
        approval.id,
        ApprovalDecisionRequest(decision="approved", user_id="producer-1"),
    )

    approved_preview = next(
        asset for asset in approved.assets if asset.id == preview_asset.id
    )
    assert approved_preview.generation_metadata["approval_status"] == "approved"


def test_used_discussion_prompt_templates_cannot_be_disabled_or_retyped(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    profile = default_participants()[1]
    profile.id = "guarded-template-panelist"
    profile.system_prompt_template = "panelist_v1"
    repo.upsert_participant_profile(profile)

    existing = repo.get_discussion_prompt_template("panelist_v1")
    disabled = existing.model_copy(update={"enabled": False})
    with pytest.raises(ValueError, match="cannot be disabled"):
        repo.upsert_discussion_prompt_template(disabled)

    retyped = existing.model_copy(update={"participant_type": "host"})
    with pytest.raises(ValueError, match="participant type cannot be changed"):
        repo.upsert_discussion_prompt_template(retyped)


@pytest.mark.asyncio
async def test_episode_aggregate_survives_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )

    reloaded = persistent_repository(db_path).get(episode.id)
    assert reloaded.id == episode.id
    assert reloaded.slug == episode.slug
    assert reloaded.status == EpisodeStatus.draft

    engine = DiscussionEngine(ModelGateway(), Settings())
    produced = await engine.run(reloaded)
    persistent_repository(db_path).save(produced)

    final = persistent_repository(db_path).get(episode.id)
    assert final.status == EpisodeStatus.transcript_review
    assert final.discussion_session is not None
    assert len(final.discussion_session.turns) >= 10
    assert all(
        turn.discussion_session_id == final.discussion_session.id
        for turn in final.discussion_session.turns
    )
    assert all(
        memory.discussion_session_id == final.discussion_session.id
        for memory in final.discussion_session.memories.values()
    )
    assert {transcript.type for transcript in final.transcripts} == {
        TranscriptType.raw,
        TranscriptType.broadcast,
    }
    for transcript in final.transcripts:
        assert all(turn.transcript_version_id == transcript.id for turn in transcript.turns)
    audit_events = persistent_repository(db_path).list_audit_events(limit=100)
    event_types = {event.event_type for event in audit_events}
    assert {"episode.created", "discussion.turn.created", "approval.required"} <= event_types


def test_model_endpoint_records_survive_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    repo.upsert_model_endpoint(
        ModelEndpoint(
            id="openai-compatible",
            name="OpenAI Compatible",
            provider_type="openai_compatible",
            base_url="https://api.example.test/v1",
            credential_reference="env:OPENAI_API_KEY",
        )
    )

    reloaded = persistent_repository(db_path)
    endpoint = reloaded.get_model_endpoint("openai-compatible")
    assert endpoint.provider_type == "openai_compatible"
    assert endpoint.credential_reference == "env:OPENAI_API_KEY"
    assert reloaded.list_audit_events()[0].event_type == "model_endpoint.upserted"
    reloaded.record_global_audit_event(
        AuditEvent(event_type="backup.restore_validated", actor="tester")
    )
    filtered_events = reloaded.list_audit_events(
        limit=5,
        event_type="backup.restore_validated",
    )
    assert [event.event_type for event in filtered_events] == ["backup.restore_validated"]


def test_endpoint_audit_records_safe_credential_reference_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    repo.upsert_model_endpoint(
        ModelEndpoint(
            id="openai-compatible",
            name="OpenAI Compatible",
            provider_type="openai_compatible",
            base_url="https://api.example.test/v1",
            credential_reference="env:OPENAI_API_KEY",
        )
    )
    repo.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="voicebox-local",
            name="Voicebox Local",
            adapter_type="voicebox_http",
            base_url="https://voicebox.example.test",
            credential_reference=None,
        )
    )
    repo.upsert_comfyui_endpoint(
        ComfyUiEndpoint(
            id="comfyui-unsupported",
            name="ComfyUI Unsupported Secret Store",
            adapter_type="comfyui_http",
            base_url="https://comfyui.example.test",
            credential_reference="vault:comfyui-token",
        )
    )
    repo.upsert_publisher_target(
        PublisherTarget(
            id="publisher-file",
            name="Publisher File Secret",
            platform="generic",
            adapter_type="generic_http",
            base_url="https://publisher.example.test",
            credential_reference="file:/run/secrets/publisher-token",
        )
    )

    audit_by_type = {
        event.event_type: event.details
        for event in persistent_repository(db_path).list_audit_events(limit=20)
    }

    model_audit = audit_by_type["model_endpoint.upserted"]
    assert model_audit["credential_reference_configured"] is True
    assert model_audit["credential_reference_scheme"] == "env"
    assert "OPENAI_API_KEY" not in str(model_audit)

    voicebox_audit = audit_by_type["voicebox_endpoint.upserted"]
    assert voicebox_audit["credential_reference_configured"] is False
    assert voicebox_audit["credential_reference_scheme"] is None

    comfyui_audit = audit_by_type["comfyui_endpoint.upserted"]
    assert comfyui_audit["credential_reference_configured"] is True
    assert comfyui_audit["credential_reference_scheme"] == "unsupported"
    assert "comfyui-token" not in str(comfyui_audit)

    publisher_audit = audit_by_type["publisher_target.upserted"]
    assert publisher_audit["credential_reference_configured"] is True
    assert publisher_audit["credential_reference_scheme"] == "file"
    assert "publisher-token" not in str(publisher_audit)


def test_project_records_survive_repository_recreation_and_link_episodes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    project = repo.upsert_project(
        Project(
            name="AI Futures",
            description="Editorial slate for AI policy episodes.",
            default_language="en",
            default_show_format_id="analytical_panel_v1",
        )
    )
    episode = repo.create(EpisodeCreateRequest(project_id=project.id, definition=definition()))

    reloaded = persistent_repository(db_path)
    persisted_project = reloaded.get_project(project.id)
    persisted_episode = reloaded.get(episode.id)
    assert persisted_project.name == "AI Futures"
    assert persisted_project.default_language == "en"
    assert persisted_episode.project_id == project.id
    with pytest.raises(ValueError, match="still used by episodes"):
        reloaded.delete_project(project.id)
    event_types = {event.event_type for event in reloaded.list_audit_events(limit=20)}
    assert {"project.upserted", "episode.created"} <= event_types


def test_language_profile_records_survive_recreation_backup_and_delete_guards(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    assert repo.get_language_profile("en").default_mode == "canonical"
    repo.upsert_language_profile(
        LanguageProfile(
            id="fr",
            name="French",
            bcp47_tag="fr",
            native_name="Francais",
            default_mode="localized_reperformance",
            line_breaking={"max_chars_per_line": 38},
            voice_defaults={"speaking_rate": 0.96},
        )
    )
    project = repo.upsert_project(Project(name="French Slate", default_language="fr"))

    reloaded = persistent_repository(db_path)
    profile = reloaded.get_language_profile("fr")
    assert profile.name == "French"
    assert profile.line_breaking["max_chars_per_line"] == 38
    with pytest.raises(ValueError, match="still used by projects"):
        reloaded.delete_language_profile("fr")

    backup_data = reloaded.export_backup_data()
    assert backup_data["record_counts"]["language_profile_records"] >= 3
    restored = EpisodeRepository()
    result = restored.restore_backup_data(backup_data)
    assert result["record_counts"]["language_profile_records"] >= 3
    assert restored.get_language_profile("fr").native_name == "Francais"
    assert restored.get_project(project.id).default_language == "fr"
    event_types = {event.event_type for event in reloaded.list_audit_events(limit=20)}
    assert "language_profile.upserted" in event_types


def test_research_source_and_evidence_claim_records_survive_repository_recreation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    repo = persistent_repository(db_path)
    episode = repo.create(EpisodeCreateRequest(definition=research_definition()))
    researched = ResearchService(settings).build_evidence_pack(
        episode,
        ResearchBuildRequest(
            user_id="researcher",
            sources=[
                {
                    "title": "Governance Review",
                    "uri": "https://example.edu/governance-review",
                    "source_type": "academic_paper",
                    "published_at": "2026",
                    "confidence": 0.86,
                    "content": (
                        "AI assistants improved productivity by 27 percent in software "
                        "maintenance teams when review ownership was explicit."
                    ),
                }
            ],
        ),
    )
    repo.save(researched)

    reloaded = persistent_repository(db_path)
    sources = reloaded.list_research_sources(episode.id)
    claims = reloaded.list_evidence_claims(episode.id)
    assert any(source.source_type == "academic_paper" for source in sources)
    academic_source = next(source for source in sources if source.source_type == "academic_paper")
    assert academic_source.episode_id == episode.id
    assert academic_source.url == "https://example.edu/governance-review"
    assert academic_source.content_hash and academic_source.content_hash.startswith("sha256:")
    assert academic_source.credibility_score >= 0.86
    assert academic_source.metadata["evidence_pack_asset_id"]
    assert any(claim.status in {"supported", "verified"} for claim in claims)
    supported_claim = next(claim for claim in claims if claim.supporting_source_ids)
    assert supported_claim.episode_id == episode.id
    assert supported_claim.statement == supported_claim.text
    assert supported_claim.extraction_metadata["evidence_pack_asset_id"]
    event_types = {event.event_type for event in reloaded.list_audit_events(limit=20)}
    assert "research.projections.synced" in event_types


def test_research_approval_records_ready_stage_in_active_workflow_run(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    repo = persistent_repository(db_path)
    episode = repo.create(EpisodeCreateRequest(definition=research_definition()))
    episode = ProductionControlService(settings).begin_run(episode, user_id="producer-1")
    researched = ResearchService(settings).build_evidence_pack(
        episode,
        ResearchBuildRequest(user_id="researcher"),
    )
    repo.save(researched)
    approval = next(item for item in researched.approvals if item.stage == "research_review")

    decided = repo.record_approval_decision(
        researched.id,
        approval.id,
        ApprovalDecisionRequest(
            decision="approved",
            comment="Evidence pack accepted.",
            user_id="producer-1",
        ),
    )

    assert decided.status == EpisodeStatus.draft
    run = decided.workflow_control["run"]
    assert run["current_stage"] == EpisodeStatus.draft.value
    assert [entry["stage"] for entry in run["stage_history"]][-2:] == [
        EpisodeStatus.research_review.value,
        EpisodeStatus.draft.value,
    ]
    assert run["stage_history"][-1]["source"] == "approval.decision.recorded"


def test_participant_profile_records_survive_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    repo.upsert_participant_profile(
        ParticipantProfile(
            id="guest",
            name="guest",
            display_name="Guest",
            participant_type="guest",
            model_endpoint_id="mock",
            model_id="mock-guest-v1",
            system_prompt_template="guest_v1",
            perspective="bring an external viewpoint",
            expertise="field experience",
            speaking_style="plain spoken",
        )
    )

    reloaded = persistent_repository(db_path)
    profile = reloaded.get_participant_profile("guest")
    assert profile.display_name == "Guest"
    assert profile.model_endpoint_id == "mock"
    assert reloaded.list_audit_events()[0].event_type == "participant_profile.upserted"


def test_voicebox_records_survive_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)

    assert repo.get_voicebox_endpoint("mock-voicebox").health_status == "healthy"
    assert repo.get_voice_profile("voice-host").voicebox_endpoint_id == "mock-voicebox"
    assert repo.get_participant_profile("host").voice_profile_id == "voice-host"

    repo.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="voicebox-remote",
            name="Voicebox Remote",
            adapter_type="voicebox_http",
            base_url="https://voicebox.example.test",
            credential_reference="env:VOICEBOX_TOKEN",
        )
    )
    repo.upsert_voice_profile(
        VoiceProfile(
            id="remote-voice",
            name="Remote Voice",
            voicebox_endpoint_id="voicebox-remote",
            voice_id="remote-voice-1",
            language="de",
        )
    )

    reloaded = persistent_repository(db_path)
    endpoint = reloaded.get_voicebox_endpoint("voicebox-remote")
    profile = reloaded.get_voice_profile("remote-voice")
    assert endpoint.base_url == "https://voicebox.example.test"
    assert profile.voicebox_endpoint_id == "voicebox-remote"
    event_types = {event.event_type for event in reloaded.list_audit_events(limit=20)}
    assert {"voicebox_endpoint.upserted", "voice_profile.upserted"} <= event_types


def test_b1_voice_preset_provisioning_assigns_matching_frontier_characters(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    repo.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="b1-voicebox",
            name="B1 Voicebox",
            adapter_type="b1_voice_stream",
            base_url="https://voice.ai.b1.germering",
            credential_reference="env:B1_API_KEY",
        )
    )

    result = repo.provision_b1_german_voice_presets()

    assert result["assigned_participants"] == B1_CHARACTER_VOICE_ASSIGNMENTS
    reloaded = persistent_repository(db_path)
    for participant_id, voice_profile_id in B1_CHARACTER_VOICE_ASSIGNMENTS.items():
        profile = reloaded.get_participant_profile(participant_id)
        voice = reloaded.get_voice_profile(voice_profile_id)
        assert profile.display_name == voice.name.removeprefix("A_DE_").removeprefix("A_")
        assert profile.voice_profile_id == voice_profile_id
        assert voice.model_id == "chatterbox"
        assert voice.voicebox_endpoint_id == "b1-voicebox"
    assert reloaded.get_participant_profile("host").voice_profile_id == "voice-host"


def test_b1_voice_inventory_sync_adds_new_native_german_profiles_without_cast_reassignment(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    repo.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="b1-voicebox",
            name="B1 Voicebox",
            adapter_type="b1_voice_stream",
            base_url="https://voice.ai.b1.germering",
            credential_reference="env:B1_API_KEY",
        )
    )
    repo.provision_b1_german_voice_presets(assign_participants=False)
    claude_assignment = repo.get_participant_profile("claude").voice_profile_id

    result = repo.sync_b1_voicebox_profile_inventory(
        endpoint_id="b1-voicebox",
        discovered_profiles=[
            {
                "id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
                "name": "A_DE_Claude",
                "description": "Remote native profile: A_DE_Claude",
                "language": "de",
                "engine": "chatterbox",
            },
            {
                "id": "6948c168-c67e-41c7-b3dc-e4470de24c6d",
                "name": "Narrator",
                "description": "Remote native profile: Erzahler",
                "language": "de",
                "engine": "chatterbox",
            },
            {
                "id": "english-voice",
                "name": "English voice",
                "description": "Not part of the German cast",
                "language": "en",
                "engine": "chatterbox",
            },
            {
                "id": "846bb94f-0e47-4f07-843a-03600088998c",
                "name": "Another ChatGPT voice",
                "description": "Remote native profile: A_ChatGPT",
                "language": "de",
                "engine": "chatterbox",
            },
        ],
    )

    narrator_id = "b1-native-6948c168-c67e-41c7-b3dc-e4470de24c6d"
    chatgpt_id = "b1-native-846bb94f-0e47-4f07-843a-03600088998c"
    assert result["created_voice_profile_ids"] == [narrator_id, chatgpt_id]
    assert "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5" in result[
        "existing_voice_profile_ids"
    ]
    assert result["skipped_voice_ids"] == ["english-voice"]
    assert repo.get_voice_profile(narrator_id).name == "Erzahler"
    assert repo.get_voice_profile(narrator_id).voice_id == "6948c168-c67e-41c7-b3dc-e4470de24c6d"
    assert repo.get_voice_profile(chatgpt_id).name == "A_ChatGPT (B1 846bb94f)"
    assert repo.get_participant_profile("claude").voice_profile_id == claude_assignment


def test_b1_voice_preset_provisioning_supports_local_voicebox_bridge(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    repo.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="b1-voicebox-local-bridge",
            name="B1 Voicebox Local Bridge",
            adapter_type="b1_voice_stream",
            base_url="http://127.0.0.1:17493",
            capabilities={
                "default_engine": "remote_http",
                "voice_profile_engine": "remote_http",
                "voice_profile_id_prefix": "bridge-",
                "local_voicebox_profile_ids": {
                    "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5": (
                        "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5"
                    ),
                    "1865b646-41ca-4140-ba9d-1a40d9fe623a": (
                        "8c54e9a6-f5a6-4d02-9706-e7403c80ea72"
                    ),
                },
            },
        )
    )

    result = repo.provision_b1_german_voice_presets(
        endpoint_id="b1-voicebox-local-bridge",
    )

    assert result["assigned_participants"] == {
        participant_id: f"bridge-{voice_profile_id}"
        for participant_id, voice_profile_id in B1_CHARACTER_VOICE_ASSIGNMENTS.items()
    }
    reloaded = persistent_repository(db_path)
    claude_voice = reloaded.get_voice_profile("bridge-0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5")
    chatgpt_voice = reloaded.get_voice_profile(
        "bridge-1865b646-41ca-4140-ba9d-1a40d9fe623a"
    )
    assert claude_voice.voicebox_endpoint_id == "b1-voicebox-local-bridge"
    assert claude_voice.voice_id == "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5"
    assert claude_voice.prosody["engine"] == "remote_http"
    assert claude_voice.prosody["b1_source_voice_profile_id"] == (
        "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5"
    )
    assert chatgpt_voice.voice_id == "8c54e9a6-f5a6-4d02-9706-e7403c80ea72"
    assert chatgpt_voice.prosody["engine"] == "remote_http"


def test_b1_voice_preset_provisioning_can_reassign_bridge_profiles_to_native_b1(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    repo.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="b1-voicebox-local-bridge",
            name="B1 Voicebox Local Bridge",
            adapter_type="b1_voice_stream",
            base_url="http://127.0.0.1:17493",
            capabilities={
                "default_engine": "remote_http",
                "voice_profile_engine": "remote_http",
                "voice_profile_id_prefix": "bridge-",
                "local_voicebox_profile_ids": {
                    "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5": (
                        "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5"
                    ),
                    "1865b646-41ca-4140-ba9d-1a40d9fe623a": (
                        "8c54e9a6-f5a6-4d02-9706-e7403c80ea72"
                    ),
                },
            },
        )
    )
    repo.provision_b1_german_voice_presets(endpoint_id="b1-voicebox-local-bridge")
    assert repo.get_participant_profile("claude").voice_profile_id == (
        "bridge-0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5"
    )

    repo.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="b1-voicebox",
            name="B1 Voicebox",
            adapter_type="b1_voice_stream",
            base_url="https://voice.ai.b1.germering",
            credential_reference="env:B1_API_KEY",
        )
    )
    preserved = repo.provision_b1_german_voice_presets(endpoint_id="b1-voicebox")
    assert preserved["assigned_participants"] == {}
    assert set(preserved["preserved_assigned_participant_ids"]) >= set(
        B1_CHARACTER_VOICE_ASSIGNMENTS
    )
    assert repo.get_participant_profile("claude").voice_profile_id == (
        "bridge-0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5"
    )

    reassigned = repo.provision_b1_german_voice_presets(
        endpoint_id="b1-voicebox",
        reassign_participants=True,
    )

    assert reassigned["assigned_participants"] == B1_CHARACTER_VOICE_ASSIGNMENTS
    assert set(reassigned["reassigned_participant_ids"]) == set(
        B1_CHARACTER_VOICE_ASSIGNMENTS
    )
    reloaded = persistent_repository(db_path)
    for participant_id, voice_profile_id in B1_CHARACTER_VOICE_ASSIGNMENTS.items():
        assert reloaded.get_participant_profile(participant_id).voice_profile_id == (
            voice_profile_id
        )
    assert reloaded.get_participant_profile("host").voice_profile_id == "voice-host"


@pytest.mark.asyncio
async def test_frontier_characters_generate_mock_audio_before_b1_voice_assignment(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    frontier_ids = ["chatgpt", "claude", "deepseek", "grok", "gemini", "mistral"]

    assert {f"voice-{participant_id}" for participant_id in frontier_ids} <= {
        profile.id for profile in repo.list_voice_profiles()
    }
    assert {f"voice-{participant_id}" for participant_id in frontier_ids} <= {
        profile.id for profile in default_voice_profiles()
    }
    assert all(
        repo.get_participant_profile(participant_id).voice_profile_id is None
        for participant_id in frontier_ids
    )

    episode_id = uuid4()
    transcript = TranscriptVersion(
        episode_id=episode_id,
        type=TranscriptType.broadcast,
        language="en",
        status="approved",
    )
    for participant_id in frontier_ids:
        transcript.turns.append(
            TranscriptTurn(
                source_discussion_turn_ids=[],
                speaker_participant_id=participant_id,
                text=f"{participant_id} contributes a short production-ready line.",
                status="accepted",
            )
        )
    episode = Episode(
        id=episode_id,
        title="Frontier Audio",
        slug="frontier-audio",
        subject="Frontier Audio",
        central_question="Can every frontier character speak locally?",
        target_duration_seconds=120,
        minimum_duration_seconds=108,
        maximum_duration_seconds=132,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            repo.get_participant_profile(participant_id) for participant_id in frontier_ids
        ],
        model_endpoints=repo.list_model_endpoints(),
        transcripts=[transcript],
    )
    service = VoiceboxService(Settings(object_storage_local_path=str(tmp_path / "object-store")))
    planned = service.plan_audio_assets(
        episode,
        AudioAssetPlanRequest(transcript_version_id=transcript.id, user_id="tester"),
    )
    generated = await service.generate_audio_assets(
        planned,
        AudioGenerationRequest(transcript_version_id=transcript.id, user_id="tester"),
        voicebox_endpoints=repo.list_voicebox_endpoints(),
        voice_profiles=repo.list_voice_profiles(),
    )

    completed_audio_by_participant = {
        asset.generation_metadata["speaker_participant_id"]: asset
        for asset in generated.assets
        if asset.asset_type == AssetType.audio and asset.status == "completed"
    }
    assert set(completed_audio_by_participant) == set(frontier_ids)
    for participant_id, asset in completed_audio_by_participant.items():
        assert asset.generation_metadata["voice_profile_id"] == f"voice-{participant_id}"
        assert asset.generation_metadata["voicebox_endpoint_id"] == "mock-voicebox"
        assert asset.storage_uri is not None
        assert asset.checksum is not None


def test_comfyui_visual_records_survive_repository_recreation(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)

    assert repo.get_comfyui_endpoint("mock-comfyui").health_status == "healthy"
    assert repo.get_comfyui_workflow("workflow-talking-head-v1").comfyui_endpoint_id == (
        "mock-comfyui"
    )
    assert repo.get_comfyui_workflow("workflow-talking-head-v1").api_workflow["10"][
        "class_type"
    ] == "CreateVideo"
    assert repo.get_comfyui_workflow("workflow-talking-head-v1").api_workflow["12"][
        "class_type"
    ] == "SaveVideo"
    assert repo.get_visual_profile("visual-host").primary_workflow_id == (
        "workflow-talking-head-v1"
    )
    assert repo.get_participant_profile("host").visual_profile_id == "visual-host"
    assert repo.get_visual_profile("visual-chatgpt").character_name == "ChatGPT"
    assert repo.get_participant_profile("chatgpt").visual_profile_id == "visual-chatgpt"

    repo.upsert_comfyui_endpoint(
        ComfyUiEndpoint(
            id="comfyui-remote",
            name="ComfyUI Remote",
            adapter_type="comfyui_http",
            base_url="https://comfyui.example.test",
            credential_reference="env:COMFYUI_TOKEN",
        )
    )
    repo.upsert_comfyui_workflow(
        ComfyUiWorkflow(
            id="remote-talking-head",
            name="Remote Talking Head",
            workflow_type="talking_head",
            comfyui_endpoint_id="comfyui-remote",
            output_asset_type="video",
            default_parameters={"width": 1280, "height": 720, "fps": 24},
        )
    )
    repo.upsert_visual_profile(
        VisualProfile(
            id="remote-visual",
            name="Remote Visual",
            character_name="Remote Character",
            primary_workflow_id="remote-talking-head",
            style_prompt="studio lit participant",
        )
    )

    reloaded = persistent_repository(db_path)
    endpoint = reloaded.get_comfyui_endpoint("comfyui-remote")
    workflow = reloaded.get_comfyui_workflow("remote-talking-head")
    profile = reloaded.get_visual_profile("remote-visual")
    assert endpoint.base_url == "https://comfyui.example.test"
    assert workflow.default_parameters["fps"] == 24
    assert reloaded.get_comfyui_workflow("workflow-topic-broll-v1").api_workflow["12"][
        "class_type"
    ] == "SaveImage"
    assert profile.primary_workflow_id == "remote-talking-head"

    with pytest.raises(ValueError, match="unknown visual profile"):
        reloaded.upsert_participant_profile(
            ParticipantProfile(
                id="guest-visual",
                name="guest-visual",
                display_name="Guest Visual",
                participant_type="guest",
                model_endpoint_id="mock",
                model_id="mock-guest-v1",
                system_prompt_template="guest_v1",
                perspective="external viewpoint",
                expertise="field experience",
                speaking_style="plain spoken",
                visual_profile_id="missing-visual",
            )
        )
    with pytest.raises(ValueError, match="still used by visual profiles"):
        reloaded.delete_comfyui_workflow("workflow-talking-head-v1")
    with pytest.raises(ValueError, match="still used by workflows"):
        reloaded.delete_comfyui_endpoint("mock-comfyui")

    event_types = {event.event_type for event in reloaded.list_audit_events(limit=20)}
    assert {
        "comfyui_endpoint.upserted",
        "comfyui_workflow.upserted",
        "visual_profile.upserted",
    } <= event_types
    assert {workflow.id for workflow in default_comfyui_workflows()} <= {
        workflow.id for workflow in reloaded.list_comfyui_workflows()
    }


def test_episode_creation_uses_persisted_participant_profiles(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)

    episode = repo.create(EpisodeCreateRequest(definition=definition()))

    assert [participant.id for participant in episode.participants] == [
        "host",
        "optimist",
        "skeptic",
        "practitioner",
    ]
    readiness = next(
        event
        for event in episode.audit_events
        if event.event_type == "episode.configuration.readiness_checked"
    )
    assert readiness.details["schema_version"] == "episode_configuration_readiness.v1"
    assert readiness.details["participant_count"] == 4
    assert readiness.details["configured_model_endpoint_count"] == 4
    assert readiness.details["enabled_model_endpoint_count"] == 4
    assert readiness.details["voice_profile_configured_count"] == 4
    assert readiness.details["visual_profile_configured_count"] == 4
    assert readiness.details["missing_model_endpoint_ids"] == []
    assert readiness.details["disabled_model_endpoint_ids"] == []
    assert [item["participant_id"] for item in readiness.details["participants"]] == [
        "host",
        "optimist",
        "skeptic",
        "practitioner",
    ]


def test_episode_creation_rejects_cast_with_unusable_model_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    participants = [
        participant.model_copy(update={"model_endpoint_id": "missing-provider"})
        if participant.id == "optimist"
        else participant
        for participant in default_participants()
    ]
    with pytest.raises(ValueError, match="unknown model endpoint ids.*missing-provider"):
        repo.create(EpisodeCreateRequest(definition=definition(), participants=participants))

    disabled_endpoints = [
        endpoint.model_copy(update={"enabled": False}) if endpoint.id == "mock" else endpoint
        for endpoint in default_model_endpoints()
    ]
    with pytest.raises(ValueError, match="disabled model endpoint ids.*mock"):
        repo.create(
            EpisodeCreateRequest(
                definition=definition(),
                model_endpoints=disabled_endpoints,
            )
        )


@pytest.mark.asyncio
async def test_localized_transcripts_and_audio_assets_survive_recreation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "dialecticore.db"
    repo = persistent_repository(db_path)
    definition_payload = definition().model_dump(mode="json")
    definition_payload["languages"] = {
        "source_language": "en",
        "outputs": [
            {"language": "en", "mode": "canonical"},
            {"language": "de", "mode": "localized_reperformance"},
        ],
        "semantic_fidelity_threshold": 0.92,
        "allow_new_claims": False,
    }
    episode_definition = EpisodeDefinition.model_validate(definition_payload)
    episode = repo.create(EpisodeCreateRequest(definition=episode_definition))
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    produced = await DiscussionEngine(ModelGateway(), settings).run(episode)
    produced.approvals[0].decision = "approved"
    canonical = next(
        transcript
        for transcript in produced.transcripts
        if transcript.id == produced.canonical_transcript_version_id
    )
    canonical.status = "approved"
    repo.save(produced)

    localized = LocalizationService().create_language_variants(
        persistent_repository(db_path).get(episode.id),
        request=LocalizationRequest(languages=["de"], user_id="tester"),
    )
    persistent_repository(db_path).save(localized)
    localized_transcript = next(
        transcript
        for transcript in localized.transcripts
        if transcript.type == TranscriptType.localized and transcript.language == "de"
    )
    localized_approval = next(
        approval
        for approval in localized.approvals
        if approval.stage == "localized_transcript_review"
        and approval.target_id == str(localized_transcript.id)
    )
    localized = persistent_repository(db_path).record_approval_decision(
        localized.id,
        localized_approval.id,
        ApprovalDecisionRequest(
            decision="approved",
            comment="German script is approved.",
            user_id="tester",
        ),
    )
    voicebox = VoiceboxService(settings)
    planned = voicebox.plan_audio_assets(
        localized,
        request=AudioAssetPlanRequest(language="de", user_id="tester"),
    )
    generated = await voicebox.generate_audio_assets(
        planned,
        request=AudioGenerationRequest(language="de", user_id="tester"),
        voicebox_endpoints=persistent_repository(db_path).list_voicebox_endpoints(),
        voice_profiles=persistent_repository(db_path).list_voice_profiles(),
    )
    subtitled = SubtitleService(settings).generate_subtitles(
        generated,
        request=SubtitleGenerationRequest(language="de", user_id="tester"),
    )
    persistent_repository(db_path).save(subtitled)

    reloaded = persistent_repository(db_path).get(episode.id)
    assert any(
        transcript.type == TranscriptType.localized and transcript.language == "de"
        for transcript in reloaded.transcripts
    )
    assert all(
        turn.transcript_version_id == transcript.id
        for transcript in reloaded.transcripts
        for turn in transcript.turns
    )
    assert any(
        asset.asset_type == AssetType.audio and asset.language == "de"
        for asset in reloaded.assets
    )
    projected_assets = persistent_repository(db_path).list_assets(episode.id)
    assert len(projected_assets) == len(reloaded.assets)
    assert any(
        asset.asset_type == AssetType.audio
        and asset.language == "de"
        and asset.status == "completed"
        for asset in projected_assets
    )
    audio = next(
        asset
        for asset in reloaded.assets
        if asset.asset_type == AssetType.audio and asset.language == "de"
    )
    assert audio.storage_uri and audio.storage_uri.startswith("object://dialecticore/audio/")
    assert audio.generation_metadata["storage_backend"] == "local_object_store"
    assert Path(audio.generation_metadata["object_storage_path"]).exists()
    assert audio.generation_metadata["media_probe"]["duration_ms"] == audio.duration_ms
    assert audio.generation_metadata["media_probe"]["clipping_detected"] is False
    assert audio.generation_metadata["media_probe"]["silence_ratio"] < 0.35
    assert audio.generation_metadata["phoneme_timing"]["ready_for_lipsync"] is True
    assert audio.generation_metadata["normalized_phoneme_timestamps"]
    assert audio.generation_metadata["viseme_timestamps"]
    subtitle = next(
        asset
        for asset in reloaded.assets
        if asset.asset_type == AssetType.subtitle and asset.language == "de"
    )
    assert subtitle.status == "completed"
    assert subtitle.generation_metadata["subtitle_text"].startswith("WEBVTT")
    assert subtitle.generation_metadata["missing_audio_count"] == 0
    assert subtitle.generation_metadata["word_timed_cue_count"] > 0
