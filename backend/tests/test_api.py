import base64
import hashlib
import io
import json
import shutil
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import app.main as main_module
import httpx
import pytest
from app.api.routes import (
    _compact_publish_binding_summary,
    _episode_production_test_report,
    _latest_live_provider_preflight_summary,
    _managed_media_smoke_evidence,
    _pending_episode_approvals,
    _pending_transcript_review_handoff,
    _production_media_readiness,
    _production_operator_next_action,
    _production_operator_next_actions,
    _production_real_life_test_readiness,
    _provider_repair_handoff,
    _publish_evidence_binding,
    get_asset_replacement_service,
    get_backup_service,
    get_comfyui_service,
    get_discussion_engine,
    get_localization_service,
    get_model_endpoint_service,
    get_primer_media_service,
    get_primer_production_service,
    get_production_control_service,
    get_publisher_service,
    get_redis_bus_service,
    get_render_service,
    get_repository,
    get_research_service,
    get_settings,
    get_subtitle_service,
    get_system_health_service,
    get_system_metrics_service,
    get_timeline_service,
    get_voicebox_service,
    get_worker_lease_service,
    get_worker_status_service,
)
from app.core.config import Settings
from app.domain.defaults import (
    B1_CHARACTER_VOICE_ASSIGNMENTS,
    OPENROUTER_CHARACTER_MODEL_ASSIGNMENTS,
    OPENROUTER_MODEL_PRESETS,
)
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity, TranscriptType, TurnType
from app.domain.schemas import (
    Approval,
    Asset,
    AssetReplacementRequest,
    AuditEvent,
    Claim,
    ComfyUiEndpoint,
    DiscussionSession,
    DiscussionTurn,
    EpisodeCreateRequest,
    EpisodeDefinition,
    ModelEndpoint,
    PrimerMediaCandidate,
    PrimerNarratorProfile,
    PrimerPronunciationSettings,
    PublisherTarget,
    PublishJob,
    PublishRequest,
    QualityResult,
    ResearchBuildRequest,
    StructuredTurnOutput,
    TranscriptTurn,
    TranscriptVersion,
    VisualReferenceImage,
    VoiceboxEndpoint,
    VoiceProfile,
    WorkerHeartbeatRequest,
    WorkflowActionRequest,
)
from app.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from app.infrastructure.repository import EpisodeRepository
from app.main import app
from app.services.asset_replacement_service import AssetReplacementService
from app.services.auth_service import AuthService
from app.services.backup_service import BackupService
from app.services.comfyui_service import ComfyUiService
from app.services.discussion_engine import DiscussionEngine
from app.services.localization_service import LocalizationService
from app.services.model_endpoint_service import ModelEndpointService
from app.services.model_gateway import ModelGateway, SecretResolver
from app.services.object_storage import create_object_store
from app.services.primer_media_service import PrimerMediaService
from app.services.primer_production_service import PrimerProductionService
from app.services.production_control_service import ProductionControlService
from app.services.publisher_service import PublisherService
from app.services.redis_bus_service import RedisBusService
from app.services.render_service import RenderService
from app.services.research_service import ResearchService
from app.services.subtitle_service import SubtitleService
from app.services.system_health_service import SystemHealthService
from app.services.system_metrics_service import SystemMetricsService
from app.services.timeline_service import TimelineService
from app.services.voicebox_service import VoiceboxService
from app.services.worker_lease_service import WorkerLeaseService
from app.services.worker_status_service import WorkerStatusService, configured_worker_roles
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from tests.test_discussion_engine import definition
from tests.test_research_service import research_definition


class FakeRedisClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.streams: list[tuple[str, dict]] = []
        self.xadd_options: list[dict] = []

    def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1

    def xadd(
        self,
        stream: str,
        fields: dict,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        self.streams.append((stream, fields))
        self.xadd_options.append({"maxlen": maxlen, "approximate": approximate})
        return "1700000000000-0"


class FakeRequestLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def info(self, event: str, **kwargs) -> None:
        self.records.append((event, kwargs))


class FailingDiscussionEngine:
    async def run(self, episode):
        episode.status = EpisodeStatus.discussing
        raise ValueError("provider request timed out; provider_context=participant_id=host")

    def status(self, episode):
        raise AssertionError("status should not be requested after failed produce")


class FakeWorkflowRenderService:
    def __init__(self, settings: Settings) -> None:
        self.object_store = create_object_store(settings)

    def enqueue_render(self, episode, request, presets):
        # Workflow tests model a completed renderer; production uses the dedicated
        # render worker to claim submitted requests asynchronously.
        return self.render_episode(episode, request, presets)

    def render_episode(self, episode, request, presets):
        timeline_asset = next(
            asset
            for asset in episode.assets
            if asset.id == request.timeline_asset_id and asset.asset_type == AssetType.timeline
        )
        timeline = timeline_asset.generation_metadata.get("timeline_json", {})
        duration_ms = int(timeline.get("duration_ms") or 1_000)
        stored = self.object_store.put_bytes(
            key=f"renders/{episode.id}/{request.render_type}.mp4",
            payload=f"{request.render_type}-render".encode(),
            content_type="video/mp4",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.render,
            language=request.language or episode.source_language,
            source_entity_type="timeline_asset",
            source_entity_id=str(request.timeline_asset_id),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            duration_ms=duration_ms,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "render_type": request.render_type,
                "preset_id": request.preset_id,
                "timeline_asset_id": str(request.timeline_asset_id),
                "review_scope": "full_timeline",
                "composition_policy": "studio_camera_cuts.v1",
                "media_probe": {"fps": 30, "av_offset_ms": 0},
            },
        )
        episode.assets.append(asset)
        if request.render_type == "preview":
            approval = Approval(
                episode_id=episode.id,
                stage="preview_render_review",
                target_type="render_asset",
                target_id=str(asset.id),
                comment="Preview render approval gates final rendering.",
            )
            episode.approvals.append(approval)
            asset.generation_metadata["approval_status"] = "pending"
            asset.generation_metadata["approval_id"] = str(approval.id)
        if request.render_type == "final":
            approval = Approval(
                episode_id=episode.id,
                stage="final_render_review",
                target_type="render_asset",
                target_id=str(asset.id),
                comment="Final render approval blocks delivery packaging.",
            )
            episode.approvals.append(approval)
            asset.generation_metadata["approval_status"] = "pending"
            asset.generation_metadata["approval_id"] = str(approval.id)
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="render_asset",
                target_id=str(asset.id),
                check_type=(
                    "render_preview_integrity"
                    if request.render_type == "preview"
                    else "render_final_integrity"
                ),
                severity=QualitySeverity.pass_,
                status="pass",
                score=1.0,
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        return episode

    def generate_thumbnail(self, episode, request):
        stored = self.object_store.put_bytes(
            key=f"thumbnails/{episode.id}/thumb.jpg",
            payload=b"thumbnail",
            content_type="image/jpeg",
        )
        thumbnail = Asset(
            episode_id=episode.id,
            asset_type=AssetType.thumbnail,
            language=episode.source_language,
            source_entity_type="render_asset",
            source_entity_id=str(request.render_asset_id),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            width=1280,
            height=720,
            checksum=stored.checksum,
            status="completed",
        )
        episode.assets.append(thumbnail)
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="thumbnail_asset",
                target_id=str(thumbnail.id),
                check_type="thumbnail_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                score=1.0,
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        return episode

    def export_youtube_package(self, episode, request):
        included_files = ["youtube-package.json", "video/render.mp4"]
        subtitle_asset = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.subtitle and asset.status == "completed"
            ),
            None,
        )
        subtitle_path = f"subtitles/{episode.source_language}.vtt"
        subtitles = []
        if subtitle_asset is not None:
            included_files.append(subtitle_path)
            subtitles.append(
                {
                    "asset_id": str(subtitle_asset.id),
                    "language": subtitle_asset.language,
                    "path": subtitle_path,
                }
            )
        if request.thumbnail_asset_id:
            included_files.append("thumbnail/thumbnail.jpg")
        manifest = {
            "id": "fake-package",
            "schema_version": "youtube_package.v1",
            "episode_id": str(episode.id),
            "title": episode.title,
            "language": episode.source_language,
            "render_asset_id": str(request.render_asset_id),
            "thumbnail_asset_id": (
                str(request.thumbnail_asset_id) if request.thumbnail_asset_id else None
            ),
            "chapters": [],
            "subtitles": subtitles,
            "evidence_lineage": {"referenced_sources": []},
        }
        package_buffer = io.BytesIO()
        with zipfile.ZipFile(package_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("youtube-package.json", json.dumps(manifest))
            archive.writestr("video/render.mp4", b"render")
            if request.thumbnail_asset_id:
                archive.writestr("thumbnail/thumbnail.jpg", b"thumbnail")
            for subtitle in subtitles:
                archive.writestr(str(subtitle["path"]), "WEBVTT\n")
        stored = self.object_store.put_bytes(
            key=f"exports/{episode.id}/package.zip",
            payload=package_buffer.getvalue(),
            content_type="application/zip",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            language=episode.source_language,
            source_entity_type="render_asset",
            source_entity_id=str(request.render_asset_id),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "render_asset_id": str(request.render_asset_id),
                "thumbnail_asset_id": (
                    str(request.thumbnail_asset_id) if request.thumbnail_asset_id else None
                ),
                "included_files": included_files,
                "youtube_package_manifest": manifest,
            },
        )
        episode.assets.append(asset)
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="export_package_asset",
                target_id=str(asset.id),
                check_type="youtube_package_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                score=1.0,
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        return episode

    def generate_production_manifest(self, episode, request):
        package = next(
            asset for asset in episode.assets if str(asset.id) == str(request.package_asset_id)
        )
        if request.regenerate:
            for existing in episode.assets:
                if (
                    existing.asset_type == AssetType.production_manifest
                    and existing.status == "completed"
                    and existing.source_entity_type == "export_package"
                    and existing.source_entity_id == str(package.id)
                ):
                    existing.status = "replaced"
        publish_jobs = [
            {
                "id": str(job.id),
                "package_asset_id": str(job.package_asset_id),
                "status": job.status,
            }
            for job in episode.publish_jobs
            if str(job.package_asset_id) == str(package.id)
        ]
        publish_quality_results = [
            {
                "id": str(result.id),
                "target_type": result.target_type,
                "target_id": result.target_id,
                "check_type": result.check_type,
                "status": result.status,
            }
            for result in episode.quality_results
            if result.check_type == "publish_delivery_integrity"
        ]
        stored = self.object_store.put_bytes(
            key=f"manifests/{episode.id}/production.json",
            payload=b"production-manifest",
            content_type="application/vnd.dialecticore.production-manifest+json",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            language=episode.source_language,
            source_entity_type="export_package",
            source_entity_id=str(request.package_asset_id),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "package_asset_id": str(package.id),
                "production_manifest": {
                    "schema_version": "production_manifest.v1",
                    "delivery_package": _manifest_delivery_package_entry(package),
                    "publish_jobs": publish_jobs,
                    "quality_results": publish_quality_results,
                },
            },
        )
        episode.assets.append(asset)
        return episode


def _stamp_alembic_revision(database_url: str, revision: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
            connection.execute(text("DELETE FROM alembic_version"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": revision},
            )
    finally:
        engine.dispose()


def _persistent_repository(database_url: str) -> EpisodeRepository:
    engine = create_database_engine(Settings(database_url=database_url))
    initialize_database(engine)
    return EpisodeRepository(create_session_factory(engine))


def _repository_with_remote_providers() -> EpisodeRepository:
    repository = EpisodeRepository()
    repository.upsert_model_endpoint(
        ModelEndpoint(
            id="remote-model",
            name="Remote Model",
            provider_type="openai_compatible",
            base_url="https://models.example.test",
            enabled=True,
            health_status="healthy",
        )
    )
    repository.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="remote-voicebox",
            name="Remote Voicebox",
            adapter_type="voicebox_http",
            base_url="https://voicebox.example.test",
            enabled=True,
            health_status="healthy",
        )
    )
    repository.upsert_comfyui_endpoint(
        ComfyUiEndpoint(
            id="remote-comfyui",
            name="Remote ComfyUI",
            adapter_type="comfyui_http",
            base_url="https://comfyui.example.test",
            enabled=True,
            health_status="healthy",
        )
    )
    repository.upsert_publisher_target(
        PublisherTarget(
            id="remote-publisher",
            name="Remote Publisher",
            platform="generic",
            adapter_type="http",
            base_url="https://publisher.example.test",
            enabled=True,
            health_status="healthy",
            capabilities={"delivery_path": "/deliveries"},
        )
    )
    return repository


def test_episode_api_creates_and_produces_mock_discussion() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        Settings(),
    )
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        created = client.post("/api/v1/episodes", json=payload)
        assert created.status_code == 200
        episode_id = created.json()["id"]

        produced = client.post(f"/api/v1/episodes/{episode_id}/produce")
        assert produced.status_code == 200
        status = produced.json()
        assert status["status"] == "TRANSCRIPT_REVIEW"
        assert status["turn_count"] >= 10
        assert status["awaiting_approval"] is True
        run = status["workflow_control"]["run"]
        assert run["schema_version"] == "production_workflow_run.v1"
        assert run["state"] == "running"
        assert run["current_stage"] == "TRANSCRIPT_REVIEW"
        assert run["run_sequence"] == 1
        assert [item["stage"] for item in run["stage_history"]] == [
            "DRAFT",
            "PREPARING_DISCUSSION",
            "DISCUSSING",
            "TRANSCRIPT_QC",
            "TRANSCRIPT_REVIEW",
        ]
        stage_statuses = {item["stage"]: item["status"] for item in run["stage_plan"]}
        assert stage_statuses["PREPARING_DISCUSSION"] == "completed"
        assert stage_statuses["DISCUSSING"] == "completed"
        assert stage_statuses["TRANSCRIPT_QC"] == "completed"
        assert stage_statuses["TRANSCRIPT_REVIEW"] == "running"

        episode = client.get(f"/api/v1/episodes/{episode_id}").json()
        assert episode["workflow_control"]["run"]["run_id"] == run["run_id"]
        summaries = client.get("/api/v1/episodes/summaries")
        assert summaries.status_code == 200
        summary = summaries.json()[0]
        assert summary["id"] == episode_id
        assert summary["title"] == episode["title"]
        assert summary["status"] == "TRANSCRIPT_REVIEW"
        assert summary["discussion_turn_count"] == len(episode["discussion_session"]["turns"])
        assert summary["pending_approval_count"] == 1
        assert summary["pending_approvals"][0]["stage"] == "transcript_review"
        assert summary["output_languages"] == ["en"]
        assert "definition" not in summary
        assert "discussion_session" not in summary
        assert "transcripts" not in summary
        assert "assets" not in summary
        assert "audit_events" not in summary
        initial_transcript_qc = [
            result
            for result in episode["quality_results"]
            if result["check_type"] == "transcript_semantic_fidelity"
        ]
        assert initial_transcript_qc[-1]["status"] == "pass"
        first_turn_id = episode["discussion_session"]["turns"][1]["id"]
        second_turn_id = episode["discussion_session"]["turns"][2]["id"]

        # A later workflow action can leave the episode marked READY while the
        # current broadcast transcript still has a pending review approval.
        # Turn review actions must follow the pending transcript review.
        stored_episode = repository.get(UUID(episode_id))
        stored_episode.status = EpisodeStatus.ready
        repository.save(stored_episode)

        regenerated = client.post(
            f"/api/v1/episodes/{episode_id}/discussion/turns/{first_turn_id}/regenerate",
            json={"comment": "Make this turn sharper.", "user_id": "tester"},
        )
        assert regenerated.status_code == 200
        regenerated_body = regenerated.json()
        assert regenerated_body["discussion_session"]["turns"][1]["status"] == "regenerated"
        assert len(regenerated_body["transcripts"]) == 3

        manually_edited = client.put(
            f"/api/v1/episodes/{episode_id}/discussion/turns/{first_turn_id}/text",
            json={
                "text": "A human editor tightened this point before recording.",
                "comment": "Use this exact wording.",
                "user_id": "tester",
            },
        )
        assert manually_edited.status_code == 200
        manually_edited_body = manually_edited.json()
        manually_edited_turn = manually_edited_body["discussion_session"]["turns"][1]
        assert manually_edited_turn["spoken_text"] == (
            "A human editor tightened this point before recording."
        )
        assert manually_edited_turn["status"] == "manually_edited"
        assert manually_edited_turn["structured_output"]["claims"] == []
        assert manually_edited_body["status"] == "TRANSCRIPT_REVIEW"
        assert len(manually_edited_body["transcripts"]) == 4
        canonical = next(
            transcript
            for transcript in manually_edited_body["transcripts"]
            if transcript["id"] == manually_edited_body["canonical_transcript_version_id"]
        )
        assert canonical["turns"][1]["text"] == (
            "A human editor tightened this point before recording."
        )
        assert canonical["turns"][1]["edit_type"] == "manual_edit"

        excluded = client.post(
            f"/api/v1/episodes/{episode_id}/discussion/turns/{second_turn_id}/exclude",
            json={"comment": "Remove duplicated point.", "user_id": "tester"},
        )
        assert excluded.status_code == 200
        excluded_body = excluded.json()
        assert excluded_body["discussion_session"]["turns"][2]["status"] == "excluded"
        assert len(excluded_body["transcripts"]) == 5
        transcript_qc = [
            result
            for result in excluded_body["quality_results"]
            if result["check_type"] == "transcript_semantic_fidelity"
        ]
        assert transcript_qc[-1]["status"] == "warning"

        transcripts = client.get(f"/api/v1/episodes/{episode_id}/transcripts")
        assert transcripts.status_code == 200
        transcript_types = {item["type"] for item in transcripts.json()["transcripts"]}
        assert transcript_types == {"raw", "broadcast"}

        approvals = client.get(f"/api/v1/episodes/{episode_id}/approvals")
        approval_id = approvals.json()["approvals"][0]["id"]
        decided = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/{approval_id}/decision",
            json={"decision": "approved", "comment": "Transcript is ready.", "user_id": "tester"},
        )
        assert decided.status_code == 200
        decided_body = decided.json()
        assert decided_body["status"] == "READY"
        run = decided_body["workflow_control"]["run"]
        assert run["current_stage"] == "READY"
        assert run["stage_history"][-1]["stage"] == "READY"
        assert run["stage_history"][-1]["source"] == "approval.decision.recorded"

        blocked = client.post(
            f"/api/v1/episodes/{episode_id}/discussion/turns/{first_turn_id}/exclude",
            json={"comment": "Too late.", "user_id": "tester"},
        )
        assert blocked.status_code == 422

        approved_transcript_id = decided_body["canonical_transcript_version_id"]
        reopened = client.post(
            f"/api/v1/episodes/{episode_id}/discussion/transcript/reopen",
            json={
                "comment": "Make one final editorial correction.",
                "user_id": "tester",
            },
        )
        assert reopened.status_code == 200
        reopened_body = reopened.json()
        assert reopened_body["status"] == "TRANSCRIPT_REVIEW"
        assert reopened_body["canonical_transcript_version_id"] != approved_transcript_id
        approved_transcript = next(
            transcript
            for transcript in reopened_body["transcripts"]
            if transcript["id"] == approved_transcript_id
        )
        editable_transcript = next(
            transcript
            for transcript in reopened_body["transcripts"]
            if transcript["id"] == reopened_body["canonical_transcript_version_id"]
        )
        assert approved_transcript["status"] == "approved"
        assert editable_transcript["status"] == "pending_review"
        assert editable_transcript["parent_version_id"] == approved_transcript_id
        assert any(
            approval["stage"] == "transcript_review"
            and approval["decision"] == "pending"
            and approval["target_id"] == editable_transcript["id"]
            for approval in reopened_body["approvals"]
        )

        editable = client.put(
            f"/api/v1/episodes/{episode_id}/discussion/turns/{first_turn_id}/text",
            json={
                "text": "The restored transcript can now be changed before performance production.",
                "comment": "Editorial revision after approval.",
                "user_id": "tester",
            },
        )
        assert editable.status_code == 200
        assert editable.json()["status"] == "TRANSCRIPT_REVIEW"

        audit = client.get("/api/v1/audit-events?limit=100")
        assert audit.status_code == 200
        events = audit.json()
        event_types = {event["event_type"] for event in events}
        assert {
            "episode.created",
            "episode.configuration.readiness_checked",
            "workflow.run.started",
            "discussion.turn.created",
            "transcript.version.created",
            "transcript.turn.regenerated",
            "transcript.turn.manually_edited",
            "transcript.turn.excluded",
            "transcript.approved_revision.reopened",
            "approval.decision.recorded",
        } <= event_types
        readiness = next(
            event
            for event in events
            if event["event_type"] == "episode.configuration.readiness_checked"
        )
        assert readiness["details"]["schema_version"] == "episode_configuration_readiness.v1"
        assert readiness["details"]["participant_count"] == 4
        assert readiness["details"]["enabled_model_endpoint_count"] == 4
        assert readiness["details"]["voice_profile_configured_count"] == 4
        assert readiness["details"]["visual_profile_configured_count"] == 4
    finally:
        app.dependency_overrides.clear()


def test_primer_spoken_script_api_prepares_edits_and_approves_review() -> None:
    repository = EpisodeRepository()
    narrator = PrimerNarratorProfile(
        id="api-pronunciation-narrator",
        name="API Pronunciation Narrator",
        language="en",
        model_endpoint_id="mock",
        model_id="mock-model",
        voice_profile_id=repository.list_voice_profiles()[0].id,
        pronunciation=PrimerPronunciationSettings(
            enabled=True,
            use_ai=False,
            acronym_policy="spell_out",
        ),
    )
    repository.upsert_primer_narrator_profile(narrator)
    primer = PrimerProductionService.__new__(PrimerProductionService)
    primer.settings = Settings()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_primer_production_service] = lambda: primer
    client = TestClient(app)

    try:
        created = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        )
        assert created.status_code == 200
        episode_id = created.json()["id"]
        episode = repository.get(UUID(episode_id))
        episode.definition.media.opening.narrator_profile_id = narrator.id
        editorial = (
            "The EU reviews a documented infrastructure proposal. "
            "Public institutions compare costs and benefits. "
            "The evidence identifies practical constraints. "
            "The final decision remains subject to public safeguards. "
            "Which conditions should guide implementation?"
        )
        episode.workflow_control["primer_production"] = {
            "script": editorial,
            "editorial_polish": {"status": "applied"},
        }
        repository.save(episode)
        primer._approved_editorial_script = lambda _episode: editorial

        initial = client.get(f"/api/v1/episodes/{episode_id}/primer")
        assert initial.status_code == 200
        assert initial.json()["spoken_script"]["status"] == "not_prepared"

        prepared = client.post(
            f"/api/v1/episodes/{episode_id}/primer/spoken-script/prepare",
            json={"user_id": "api-editor"},
        )
        assert prepared.status_code == 200
        assert prepared.json()["status"] == "review_required"
        assert "E U" in prepared.json()["spoken_script"]

        edited = client.put(
            f"/api/v1/episodes/{episode_id}/primer/spoken-script",
            json={
                "user_id": "api-editor",
                "replacements": prepared.json()["replacements"],
                "punctuation_script": prepared.json()["spoken_script"],
            },
        )
        assert edited.status_code == 200
        assert edited.json()["status"] == "review_required"

        approved = client.post(
            f"/api/v1/episodes/{episode_id}/primer/spoken-script/approve",
            json={"user_id": "api-editor"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["approved_by"] == "api-editor"
    finally:
        app.dependency_overrides.clear()


def test_episode_api_persists_failed_produce_run_for_provider_errors() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: FailingDiscussionEngine()
    app.dependency_overrides[get_production_control_service] = lambda: ProductionControlService()
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        created = client.post("/api/v1/episodes", json=payload)
        assert created.status_code == 200
        episode_id = created.json()["id"]

        produced = client.post(f"/api/v1/episodes/{episode_id}/produce")

        assert produced.status_code == 422
        assert "provider request timed out" in produced.json()["detail"]
        episode = client.get(f"/api/v1/episodes/{episode_id}").json()
        assert episode["status"] == "FAILED"
        control = episode["workflow_control"]
        assert control["failed_stage"] == "DISCUSSING"
        assert "provider request timed out" in control["failure_reason"]
        run = control["run"]
        assert run["state"] == "failed"
        assert run["completion_reason"] == "stage_failed"
        assert run["failed_stage"] == "DISCUSSING"
        assert run["stage_history"][-1]["stage"] == "FAILED"
        assert any(
            event["event_type"] == "workflow.stage.failed" for event in episode["audit_events"]
        )
    finally:
        app.dependency_overrides.clear()


def test_primer_media_acquisition_failure_persists_candidate_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = EpisodeRepository()
    primer_media = PrimerMediaService(Settings())
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_primer_media_service] = lambda: primer_media
    client = TestClient(app)

    def reject_download(_media_url: str) -> tuple[bytes, str]:
        raise ValueError("primer media download is 64.0 MiB; the configured limit is 50 MiB")

    monkeypatch.setattr(primer_media, "_download_media", reject_download)

    try:
        created = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        )
        assert created.status_code == 200
        episode_id = created.json()["id"]
        episode = repository.get(episode_id)
        candidate = PrimerMediaCandidate(
            id="candidate-download-too-large",
            media_url="https://media.example.test/source.mp4",
            source_url="https://example.test/source",
            source_title="Official source",
            source_type="official_media",
            media_type="video",
            title="Source clip",
            rationale="Relevant official footage.",
            rights_status="official_source",
        )
        episode.workflow_control["primer_media"] = {
            "candidates": [candidate.model_dump(mode="json")]
        }
        repository.save(episode)

        acquisition = client.post(
            f"/api/v1/episodes/{episode_id}/primer-media/acquire",
            json={"candidate_id": candidate.id, "user_id": "tester"},
        )

        assert acquisition.status_code == 422
        assert "64.0 MiB" in acquisition.json()["detail"]
        candidates = client.get(f"/api/v1/episodes/{episode_id}/primer-media/candidates")
        assert candidates.status_code == 200
        assert candidates.json()[0]["status"] == "failed"
        assert "configured limit is 50 MiB" in candidates.json()[0]["failure_reason"]
    finally:
        app.dependency_overrides.clear()


def test_episode_pilot_readiness_reports_real_run_blockers_and_ready_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    monkeypatch.setattr(
        "app.services.system_health_service.shutil.which",
        lambda tool: f"/usr/bin/{tool}" if tool in {"ffmpeg", "ffprobe"} else None,
    )
    client = TestClient(app)

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]

        initial = client.get(f"/api/v1/episodes/{episode_id}/pilot-readiness")
        assert initial.status_code == 200
        initial_body = initial.json()
        assert initial_body["schema_version"] == "episode_pilot_readiness.v1"
        assert initial_body["status"] == "fail"
        assert initial_body["production_target"] == "native_visual"
        assert initial_body["target_status"] == "fail"
        assert initial_body["selected_pilot_mode"]["mode"] == "native_visual"
        initial_modes = {mode["mode"]: mode for mode in initial_body["pilot_modes"]}
        assert initial_modes["audio_first"]["status"] == "fail"
        assert initial_modes["native_visual"]["status"] == "fail"
        initial_stages = {stage["category"]: stage for stage in initial_body["stages"]}
        assert initial_stages["discussion"]["status"] == "fail"
        assert (
            "all selected participants need non-mock model endpoints for a real pilot"
            in initial_stages["discussion"]["blockers"]
        )
        assert initial_stages["speech"]["status"] == "fail"
        assert initial_stages["visuals"]["status"] == "fail"
        assert initial_stages["rendering"]["status"] == "pass"

        repository.upsert_model_endpoint(
            ModelEndpoint(
                id="openrouter",
                name="OpenRouter",
                provider_type="openai_compatible",
                base_url="https://openrouter.ai/api/v1",
                enabled=True,
                health_status="healthy",
            )
        )
        repository.upsert_voicebox_endpoint(
            VoiceboxEndpoint(
                id="b1-voicebox",
                name="B1 Voicebox",
                adapter_type="b1_voice_stream",
                base_url="https://voice.ai.b1.germering",
                enabled=True,
                health_status="healthy",
            )
        )
        repository.upsert_voice_profile(
            VoiceProfile(
                id="voice-b1-pilot",
                name="B1 Pilot Voice",
                voicebox_endpoint_id="b1-voicebox",
                voice_id="bd4e9bf1-482b-4900-97c1-48275d1ba28c",
                language="de",
                model_id="chatterbox",
            )
        )
        repository.upsert_comfyui_endpoint(
            ComfyUiEndpoint(
                id="mock-comfyui",
                name="Remote ComfyUI",
                adapter_type="comfyui_http",
                base_url="https://comfy.ai.b1.germering",
                enabled=True,
                health_status="healthy",
                capabilities={
                    "managed_media_catalog_ready": True,
                    "managed_media_api": True,
                    "managed_media_available_presets": [
                        "image-default",
                        "image-edit",
                        "image-upscale",
                        "video-text",
                        "video-image",
                        "talking-head-lipsync",
                    ],
                    "managed_media_missing_presets": [],
                },
            )
        )
        episode = repository.get(episode_id)
        selected_visual_ids = {
            participant.visual_profile_id
            for participant in episode.participants
            if participant.visual_profile_id
        }
        for visual_id in selected_visual_ids:
            visual = repository.get_visual_profile(visual_id)
            visual.reference_images = [
                VisualReferenceImage(
                    reference_type="portrait",
                    uri=f"object://references/{visual.id}/portrait.png",
                ),
                VisualReferenceImage(
                    reference_type="full_body",
                    uri=f"object://references/{visual.id}/full-body.png",
                ),
            ]
            repository.upsert_visual_profile(visual)
        for participant in episode.participants:
            participant.model_endpoint_id = "openrouter"
            participant.model_id = "openai/gpt-5.4-mini"
            participant.voice_profile_id = "voice-b1-pilot"
        repository.save(episode)

        repository.upsert_comfyui_endpoint(
            ComfyUiEndpoint(
                id="mock-comfyui",
                name="Remote ComfyUI",
                adapter_type="comfyui_http",
                base_url="https://comfy.ai.b1.germering",
                enabled=True,
                health_status="unhealthy",
                capabilities={
                    "native_comfyui": True,
                    "prompt_admission_ready": False,
                    "prompt_admission_probe": {
                        "ready": False,
                        "status_code": 503,
                        "response": {
                            "detail": {
                                "code": "hardware_resource_policy",
                                "message": ("GPU admission blocked by hardware resource policy"),
                                "hardware_resource_policy": {
                                    "detail": (
                                        "largest GPU free VRAM is 825 MiB; policy reserve "
                                        "requires 1024 MiB"
                                    )
                                },
                            }
                        },
                    },
                },
            )
        )

        visual_blocked = client.get(f"/api/v1/episodes/{episode_id}/pilot-readiness")
        assert visual_blocked.status_code == 200
        visual_blocked_body = visual_blocked.json()
        visual_blocked_modes = {mode["mode"]: mode for mode in visual_blocked_body["pilot_modes"]}
        assert visual_blocked_body["status"] == "fail"
        assert visual_blocked_body["production_target"] == "native_visual"
        assert visual_blocked_body["target_status"] == "fail"
        assert visual_blocked_modes["audio_first"]["status"] == "warning"
        assert visual_blocked_modes["audio_first"]["blockers"] == []
        assert (
            "Character animation is not ready for this mode"
            in visual_blocked_modes["audio_first"]["warnings"]
        )
        assert visual_blocked_modes["native_visual"]["status"] == "fail"
        assert (
            "one or more selected visual workflows use unhealthy ComfyUI endpoints"
            in visual_blocked_modes["native_visual"]["blockers"]
        )
        assert (
            "one or more selected native ComfyUI endpoints block prompt admission"
            in visual_blocked_modes["native_visual"]["blockers"]
        )
        visual_stage = {stage["category"]: stage for stage in visual_blocked_body["stages"]}[
            "visuals"
        ]
        assert (
            visual_stage["details"]["readiness_checks"][
                "selected_native_comfyui_prompt_admission_ready"
            ]
            is False
        )
        admission_blockers = visual_stage["details"]["prompt_admission_blocked_endpoints"]
        assert len(admission_blockers) == 1
        blocked_endpoint = admission_blockers[0]["endpoint"]
        assert blocked_endpoint == {
            "id": "mock-comfyui",
            "name": "Remote ComfyUI",
            "adapter_type": "comfyui_http",
            "health_status": "unhealthy",
            "base_url_configured": True,
            "prompt_admission": {
                "ready": False,
                "status_code": 503,
                "code": "hardware_resource_policy",
                "message": "GPU admission blocked by hardware resource policy",
                "detail": ("largest GPU free VRAM is 825 MiB; policy reserve requires 1024 MiB"),
            },
        }
        assert admission_blockers[0]["participant_ids"] == [
            "host",
            "optimist",
            "skeptic",
            "practitioner",
        ]
        assert admission_blockers[0]["workflow_ids"] == ["workflow-talking-head-v1"]
        assert admission_blockers[0]["visual_profile_ids"] == [
            "visual-host",
            "visual-optimist",
            "visual-skeptic",
            "visual-practitioner",
        ]

        episode.definition.workflow.production_target = "audio_first"
        repository.save(episode)

        audio_first = client.get(f"/api/v1/episodes/{episode_id}/pilot-readiness")
        assert audio_first.status_code == 200
        audio_first_body = audio_first.json()
        assert audio_first_body["status"] == "warning"
        assert audio_first_body["production_target"] == "audio_first"
        assert audio_first_body["target_status"] == "warning"
        assert audio_first_body["selected_pilot_mode"]["mode"] == "audio_first"
        assert audio_first_body["selected_pilot_mode"]["blockers"] == []
        assert audio_first_body["blockers"] == []
        assert (
            "one or more selected visual workflows use unhealthy ComfyUI endpoints"
            in audio_first_body["all_stage_blockers"]
        )

        repository.upsert_comfyui_endpoint(
            ComfyUiEndpoint(
                id="mock-comfyui",
                name="Remote ComfyUI",
                adapter_type="comfyui_http",
                base_url="https://comfy.ai.b1.germering",
                enabled=True,
                health_status="healthy",
                capabilities={
                    "managed_media_catalog_ready": True,
                    "managed_media_api": True,
                    "managed_media_available_presets": [
                        "image-default",
                        "image-edit",
                        "image-upscale",
                        "video-text",
                        "video-image",
                        "talking-head-lipsync",
                    ],
                    "managed_media_missing_presets": [],
                },
            )
        )
        episode.definition.workflow.production_target = "native_visual"
        repository.save(episode)

        ready = client.get(f"/api/v1/episodes/{episode_id}/pilot-readiness")
        assert ready.status_code == 200
        ready_body = ready.json()
        assert ready_body["status"] == "pass"
        assert ready_body["production_target"] == "native_visual"
        assert ready_body["target_status"] == "pass"
        ready_modes = {mode["mode"]: mode for mode in ready_body["pilot_modes"]}
        assert ready_modes["audio_first"]["status"] == "pass"
        assert ready_modes["native_visual"]["status"] == "pass"
        ready_stages = {stage["category"]: stage for stage in ready_body["stages"]}
        assert {stage["status"] for stage in ready_stages.values()} == {"pass"}
        assert ready_stages["discussion"]["details"]["remote_model_participant_count"] == 4
        assert ready_stages["speech"]["details"]["remote_voice_participant_count"] == 4
        assert ready_stages["visuals"]["details"]["remote_visual_participant_count"] == 4
        assert ready_stages["visuals"]["details"]["managed_media_required_endpoints"] == [
            {
                "endpoint": {
                    "id": "mock-comfyui",
                    "name": "Remote ComfyUI",
                    "adapter_type": "comfyui_http",
                    "health_status": "healthy",
                    "base_url_configured": True,
                    "managed_media": {
                        "ready": True,
                        "api": True,
                        "status_code": None,
                        "model_count": None,
                        "required_presets": [],
                        "available_presets": [
                            "image-default",
                            "image-edit",
                            "image-upscale",
                            "video-text",
                            "video-image",
                            "talking-head-lipsync",
                        ],
                        "missing_presets": [],
                    },
                },
                "participant_ids": [
                    "host",
                    "optimist",
                    "skeptic",
                    "practitioner",
                ],
                "workflow_ids": ["workflow-talking-head-v1"],
                "visual_profile_ids": [
                    "visual-host",
                    "visual-optimist",
                    "visual-skeptic",
                    "visual-practitioner",
                ],
                "required_presets": ["talking-head-lipsync"],
                "available_presets": [
                    "image-default",
                    "image-edit",
                    "image-upscale",
                    "talking-head-lipsync",
                    "video-image",
                    "video-text",
                ],
                "missing_presets": [],
                "catalog_ready": True,
                "catalog_status_code": None,
                "model_count": None,
            }
        ]
        assert ready_stages["rendering"]["details"]["ffmpeg_path"] == "/usr/bin/ffmpeg"
        assert ready_body["blockers"] == []

        episode = repository.get(episode_id)
        episode.definition.media.directing.studio_layout = "seated_panel"
        repository.save(episode)

        seated = client.get(f"/api/v1/episodes/{episode_id}/pilot-readiness")
        assert seated.status_code == 200
        seated_body = seated.json()
        seated_stages = {stage["category"]: stage for stage in seated_body["stages"]}
        seated_visuals = seated_stages["visuals"]
        assert seated_body["status"] == "fail"
        assert (
            "B1 studio-panel-shot is unavailable for seated panel production"
            in seated_visuals["blockers"]
        )
        assert (
            seated_visuals["details"]["readiness_checks"]["selected_seated_panel_media_available"]
            is False
        )
        assert seated_visuals["details"]["seated_panel_required_presets"] == [
            "studio-seated-character-p40",
            "studio-panel-shot",
        ]
        assert seated_visuals["details"]["seated_panel_missing_preset_endpoints"][0][
            "missing_presets"
        ] == ["studio-seated-character-p40", "studio-panel-shot"]

        repository.upsert_comfyui_endpoint(
            ComfyUiEndpoint(
                id="mock-comfyui",
                name="Remote ComfyUI",
                adapter_type="comfyui_http",
                base_url="https://comfy.ai.b1.germering",
                enabled=True,
                health_status="healthy",
                capabilities={
                    "managed_media_catalog_ready": True,
                    "managed_media_api": True,
                    "managed_media_available_presets": [
                        "talking-head-lipsync",
                        "studio-seated-character-p40",
                        "studio-panel-shot",
                    ],
                    "managed_media_available_model_ids": [
                        "talking-head-lipsync",
                        "studio-seated-character-p40",
                        "studio-panel-shot",
                    ],
                    "managed_media_missing_presets": [],
                },
            )
        )
        repository.upsert_comfyui_endpoint(
            ComfyUiEndpoint(
                id="b1-comfyui",
                name="B1 Managed Media",
                adapter_type="comfyui_http",
                base_url="https://api.ai.b1.germering",
                enabled=True,
                health_status="healthy",
                capabilities={
                    "managed_media_catalog_ready": True,
                    "managed_media_api": True,
                    "managed_media_available_presets": [
                        "talking-head-lipsync",
                        "studio-seated-character-p40",
                        "studio-panel-shot",
                    ],
                    "managed_media_available_model_ids": [
                        "talking-head-lipsync",
                        "studio-seated-character-p40",
                        "studio-panel-shot",
                    ],
                    "managed_media_missing_presets": [],
                },
            )
        )
        seated_ready = client.get(f"/api/v1/episodes/{episode_id}/pilot-readiness")
        assert seated_ready.status_code == 200
        seated_ready_body = seated_ready.json()
        assert seated_ready_body["status"] == "pass"
        seated_ready_visuals = {stage["category"]: stage for stage in seated_ready_body["stages"]}[
            "visuals"
        ]
        assert (
            seated_ready_visuals["details"]["readiness_checks"][
                "selected_seated_panel_media_available"
            ]
            is True
        )
        assert (
            seated_ready_visuals["details"]["readiness_checks"][
                "selected_seated_panel_workflows_configured"
            ]
            is True
        )
        assert seated_ready_visuals["details"]["seated_panel_missing_preset_endpoints"] == []
        assert seated_ready_visuals["details"]["seated_panel_workflow_issues"] == []
    finally:
        app.dependency_overrides.clear()


def test_episode_pilot_readiness_can_refresh_comfyui_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubComfyUiService:
        async def check_endpoint_health(self, endpoint: ComfyUiEndpoint) -> ComfyUiEndpoint:
            return endpoint.model_copy(
                update={
                    "health_status": "healthy",
                    "capabilities": {
                        **endpoint.capabilities,
                        "native_comfyui": True,
                        "prompt_admission_ready": True,
                        "managed_media_catalog_ready": True,
                        "managed_media_api": True,
                        "managed_media_available_presets": [
                            "image-default",
                            "image-edit",
                            "image-upscale",
                            "video-text",
                            "video-image",
                            "talking-head-lipsync",
                        ],
                        "managed_media_missing_presets": [],
                        "prompt_admission_probe": {
                            "ready": True,
                            "status_code": 200,
                            "response": {"prompt_id": "probe-ok"},
                        },
                    },
                }
            )

    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_comfyui_service] = lambda: StubComfyUiService()
    monkeypatch.setattr(
        "app.services.system_health_service.shutil.which",
        lambda tool: f"/usr/bin/{tool}" if tool in {"ffmpeg", "ffprobe"} else None,
    )
    client = TestClient(app)

    try:
        endpoint = repository.get_comfyui_endpoint("mock-comfyui")
        endpoint.adapter_type = "comfyui_http"
        endpoint.base_url = "https://comfy.ai.b1.germering"
        endpoint.health_status = "unhealthy"
        endpoint.capabilities = {
            **endpoint.capabilities,
            "native_comfyui": True,
            "prompt_admission_ready": False,
        }
        repository.upsert_comfyui_endpoint(endpoint)
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]

        readiness = client.get(
            f"/api/v1/episodes/{episode_id}/pilot-readiness?refresh_comfyui_health=true"
        )

        assert readiness.status_code == 200
        body = readiness.json()
        assert body["comfyui_health_refresh"] == {
            "schema_version": "pilot_comfyui_health_refresh.v1",
            "status": "pass",
            "candidate_endpoint_count": 1,
            "refreshed": [
                {
                    "endpoint_id": "mock-comfyui",
                    "status": "pass",
                    "health_status": "healthy",
                    "native_comfyui": True,
                    "prompt_admission_ready": True,
                    "prompt_admission": {
                        "ready": True,
                        "status_code": 200,
                        "code": None,
                        "message": None,
                        "detail": None,
                    },
                }
            ],
            "issues": [],
        }
        assert repository.get_comfyui_endpoint("mock-comfyui").health_status == "healthy"
        visual_stage = {stage["category"]: stage for stage in body["stages"]}["visuals"]
        assert (
            visual_stage["details"]["readiness_checks"][
                "selected_native_comfyui_prompt_admission_ready"
            ]
            is True
        )
    finally:
        app.dependency_overrides.clear()


def test_episode_create_api_rejects_unknown_or_disabled_participant_model_endpoint() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    payload = definition().model_dump(mode="json")

    try:
        missing_participants = [
            participant.model_copy(update={"model_endpoint_id": "missing-provider"}).model_dump(
                mode="json"
            )
            if participant.id == "optimist"
            else participant.model_dump(mode="json")
            for participant in repository.list_participant_profiles()
        ]
        missing_response = client.post(
            "/api/v1/episodes",
            json={
                "definition": payload,
                "participants": missing_participants,
                "model_endpoints": [
                    endpoint.model_dump(mode="json")
                    for endpoint in repository.list_model_endpoints()
                ],
            },
        )
        assert missing_response.status_code == 422
        assert "unknown model endpoint ids" in missing_response.json()["detail"]
        assert "missing-provider" in missing_response.json()["detail"]

        disabled_endpoints = [
            endpoint.model_copy(update={"enabled": False}).model_dump(mode="json")
            if endpoint.id == "mock"
            else endpoint.model_dump(mode="json")
            for endpoint in repository.list_model_endpoints()
        ]
        disabled_response = client.post(
            "/api/v1/episodes",
            json={
                "definition": payload,
                "participants": [
                    participant.model_dump(mode="json")
                    for participant in repository.list_participant_profiles()
                ],
                "model_endpoints": disabled_endpoints,
            },
        )
        assert disabled_response.status_code == 422
        assert "disabled model endpoint ids" in disabled_response.json()["detail"]
        assert "mock" in disabled_response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_episode_production_settings_api_updates_duration_bounds() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    episode = repository.create(EpisodeCreateRequest(definition=definition()))

    try:
        invalid = client.patch(
            f"/api/v1/episodes/{episode.id}/production-settings",
            json={
                "target_duration_seconds": 60,
                "minimum_duration_seconds": 80,
                "maximum_duration_seconds": 90,
                "user_id": "tester",
            },
        )
        assert invalid.status_code == 422
        assert "minimum duration" in invalid.json()["detail"]

        response = client.patch(
            f"/api/v1/episodes/{episode.id}/production-settings",
            json={
                "target_duration_seconds": 76,
                "minimum_duration_seconds": 60,
                "maximum_duration_seconds": 90,
                "user_id": "tester",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["target_duration_seconds"] == 76
        assert body["minimum_duration_seconds"] == 60
        assert body["maximum_duration_seconds"] == 90
        audit = body["audit_events"][-1]
        assert audit["event_type"] == "episode.production_settings.updated"
        assert audit["actor"] == "tester"
        assert audit["details"]["previous"]["maximum_duration_seconds"] == (
            episode.maximum_duration_seconds
        )
        assert audit["details"]["current"]["maximum_duration_seconds"] == 90
        assert "media_previous" not in audit["details"]

        panel_master = Asset(
            episode_id=episode.id,
            asset_type=AssetType.studio_scene,
            source_entity_type="episode",
            source_entity_id=f"{episode.id}:panel:establishing_wide",
            status="completed",
            generation_metadata={
                "visual_role": "studio_panel_keyframe",
                "approval_status": "approved",
                "prompt_inputs": {"studio_layout": "seated_panel"},
            },
        )
        seated_turn = Asset(
            episode_id=episode.id,
            asset_type=AssetType.video,
            source_entity_type="transcript_turn",
            source_entity_id="turn-1",
            status="completed",
            generation_metadata={
                "visual_role": "video_primary",
                "render_ready": True,
                "prompt_inputs": {"studio_layout": "seated_panel"},
            },
        )
        unrelated_broll = Asset(
            episode_id=episode.id,
            asset_type=AssetType.broll,
            source_entity_type="transcript_turn",
            source_entity_id="turn-1",
            status="completed",
            generation_metadata={"visual_role": "broll"},
        )
        episode.assets.extend([panel_master, seated_turn, unrelated_broll])
        repository.save(episode)

        scene_response = client.patch(
            f"/api/v1/episodes/{episode.id}/production-settings",
            json={
                "target_duration_seconds": 76,
                "minimum_duration_seconds": 60,
                "maximum_duration_seconds": 90,
                "scene_reference_image_uri": (
                    "object://dialecticore/show-media/scene-reference-images/studio.png"
                ),
                "user_id": "tester",
            },
        )
        assert scene_response.status_code == 200
        scene_body = scene_response.json()
        assert scene_body["definition"]["media"]["scene_reference_image_uri"] == (
            "object://dialecticore/show-media/scene-reference-images/studio.png"
        )
        scene_audit = scene_body["audit_events"][-1]
        assert scene_audit["details"]["media_previous"]["scene_reference_image_uri"] is None
        assert scene_audit["details"]["media_current"]["scene_reference_image_uri"] == (
            "object://dialecticore/show-media/scene-reference-images/studio.png"
        )
        assert scene_audit["details"]["scene_reference_change"] == {
            "invalidated_scene_asset_ids": [str(panel_master.id), str(seated_turn.id)],
            "invalidated_scene_asset_count": 2,
            "requires_panel_coverage_rebuild": True,
        }
        assets_by_id = {asset["id"]: asset for asset in scene_body["assets"]}
        assert assets_by_id[str(panel_master.id)]["status"] == "replaced"
        assert (
            assets_by_id[str(panel_master.id)]["generation_metadata"]["approval_status"]
            == "superseded"
        )
        assert assets_by_id[str(seated_turn.id)]["status"] == "replaced"
        assert assets_by_id[str(seated_turn.id)]["generation_metadata"]["render_ready"] is False
        assert assets_by_id[str(unrelated_broll.id)]["status"] == "completed"

        clear_response = client.patch(
            f"/api/v1/episodes/{episode.id}/production-settings",
            json={
                "target_duration_seconds": 76,
                "minimum_duration_seconds": 60,
                "maximum_duration_seconds": 90,
                "scene_reference_image_uri": None,
                "user_id": "tester",
            },
        )
        assert clear_response.status_code == 200
        clear_body = clear_response.json()
        assert clear_body["definition"]["media"]["scene_reference_image_uri"] is None
        clear_audit = clear_body["audit_events"][-1]
        assert clear_audit["details"]["media_previous"]["scene_reference_image_uri"] == (
            "object://dialecticore/show-media/scene-reference-images/studio.png"
        )
        assert clear_audit["details"]["media_current"]["scene_reference_image_uri"] is None

        directing_response = client.patch(
            f"/api/v1/episodes/{episode.id}/production-settings",
            json={
                "target_duration_seconds": 76,
                "minimum_duration_seconds": 60,
                "maximum_duration_seconds": 90,
                "directing": {
                    "mode": "speaker_only",
                    "planning_mode": "manual",
                    "require_generated_studio": True,
                    "require_group_cutaways": True,
                    "require_reaction_cutaways": True,
                    "broll_policy": "contextual_only",
                    "default_camera_views": ["speaker_medium"],
                    "allowed_camera_actions": ["cut"],
                },
                "user_id": "tester",
            },
        )
        assert directing_response.status_code == 200
        directing_body = directing_response.json()
        directing = directing_body["definition"]["media"]["directing"]
        assert directing["mode"] == "speaker_only"
        assert directing["require_generated_studio"] is False
        assert directing["require_group_cutaways"] is False
        assert directing["require_reaction_cutaways"] is False
        directing_audit = directing_body["audit_events"][-1]
        assert directing_audit["details"]["media_previous"]["directing"]["mode"] == (
            "studio_directed"
        )
        assert directing_audit["details"]["media_current"]["directing"] == directing
    finally:
        app.dependency_overrides.clear()


def test_episode_definition_update_api_edits_pristine_draft_only() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    episode = repository.create(EpisodeCreateRequest(definition=definition()))

    try:
        updated_definition = definition()
        updated_definition.title = "Updated frontier model pilot"
        updated_definition.topic.central_question = (
            "Which frontier model is the best practical choice?"
        )
        updated_definition.format.target_duration_minutes = 3
        response = client.put(
            f"/api/v1/episodes/{episode.id}",
            json={
                "project_id": None,
                "definition": updated_definition.model_dump(mode="json"),
                "user_id": "tester",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Updated frontier model pilot"
        assert body["central_question"] == "Which frontier model is the best practical choice?"
        assert body["target_duration_seconds"] == 180
        audit = body["audit_events"][-1]
        assert audit["event_type"] == "episode.definition.updated"
        assert audit["actor"] == "tester"
        assert audit["details"]["previous"]["title"] == episode.title

        stored = repository.get(episode.id)
        stored.assets.append(
            Asset(
                episode_id=stored.id,
                asset_type=AssetType.image,
                language="de",
                source_entity_type="episode",
                source_entity_id=str(stored.id),
                status="completed",
            )
        )
        repository.save(stored)
        blocked = client.put(
            f"/api/v1/episodes/{episode.id}",
            json={
                "project_id": None,
                "definition": updated_definition.model_dump(mode="json"),
            },
        )
        assert blocked.status_code == 422
        assert "locked after production work exists" in blocked.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_production_control_run_tracks_operator_signals() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))

    service.begin_run(episode, user_id="producer-1")
    episode.status = EpisodeStatus.discussing
    service.record_stage(episode, EpisodeStatus.discussing, "test")
    service.pause(
        episode,
        WorkflowActionRequest(
            action="pause",
            user_id="producer-1",
            comment="Hold for review.",
        ),
    )
    service.resume(
        episode,
        WorkflowActionRequest(action="resume", user_id="producer-2"),
    )
    service.cancel(
        episode,
        WorkflowActionRequest(
            action="cancel",
            user_id="producer-3",
            comment="Stop this run.",
        ),
    )

    run = episode.workflow_control["run"]
    assert run["state"] == "cancelled"
    assert run["completion_reason"] == "cancelled"
    assert run["current_stage"] == "CANCELLED"
    assert [signal["signal_type"] for signal in run["signals"]] == [
        "pause",
        "resume",
        "cancel",
    ]
    assert run["signals"][0]["stage"] == "DISCUSSING"
    assert run["signals"][0]["actor"] == "producer-1"
    assert run["signals"][1]["actor"] == "producer-2"
    assert run["signals"][2]["comment"] == "Stop this run."
    assert [item["status"] for item in episode.workflow_control["temporal_signal_log"]] == [
        "disabled",
        "disabled",
        "disabled",
        "disabled",
    ]
    event_log = episode.workflow_control["workflow_event_log"]
    assert [event["event_type"] for event in event_log] == [
        "workflow.run.started",
        "workflow.stage.entered",
        "workflow.signal.received",
        "workflow.signal.received",
        "workflow.signal.received",
        "workflow.run.completed",
    ]
    replay = service.replay_workflow(episode)
    assert replay["schema_version"] == "workflow_replay_report.v1"
    assert replay["status"] == "pass"
    assert replay["event_count"] == 6
    assert replay["replayed"]["state"] == "cancelled"
    assert replay["replayed"]["current_stage"] == "CANCELLED"
    assert replay["replayed"]["signal_count"] == 3
    assert replay["issues"] == []


def test_stop_run_preserves_episode_and_clears_active_production_attention() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))

    service.begin_run(episode, user_id="producer-1")
    episode.status = EpisodeStatus.discussing
    stopped = service.stop_run(
        episode,
        WorkflowActionRequest(
            action="stop_run",
            user_id="producer-2",
            comment="Stop automation but keep this episode editable.",
        ),
    )
    repository.save(stopped)

    run = stopped.workflow_control["run"]
    assert stopped.status == EpisodeStatus.discussing
    assert stopped.workflow_control["cancelled"] is False
    assert run["state"] == "stopped"
    assert run["completion_reason"] == "stopped_by_operator"
    assert run["current_stage"] == "DISCUSSING"
    assert run["signals"][-1]["signal_type"] == "stop_run"
    assert stopped.audit_events[-1].event_type == "workflow.run.stopped"

    health = SystemHealthService(Settings()).summary(repository)
    production_runs = {component["name"]: component for component in health["components"]}[
        "production_runs"
    ]
    assert production_runs["details"]["production_run_count"] == 1
    assert production_runs["details"]["active_production_runs"] == 0
    assert production_runs["details"]["running_active_production_runs"] == 0
    assert production_runs["details"]["attention_count"] == 0
    assert production_runs["details"]["by_state"] == {"stopped": 1}
    assert production_runs["details"]["failed_readiness_checks"] == []


def test_workflow_orchestration_readiness_ignores_stopped_run_attempts(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        service = ProductionControlService(settings)
        episode = service.begin_run(episode, user_id="producer-1")
        recorded_at = datetime.now(UTC)
        episode.workflow_control["worker_orchestration_log"] = [
            {
                "schema_version": "workflow_worker_orchestration_attempt.v1",
                "summary_id": "summary-stopped-blocked",
                "attempt_sequence": 1,
                "recorded_at": recorded_at.isoformat(),
                "worker_id": "workflow-worker",
                "policy": "local_stage_worker_orchestrator_v1",
                "progressed_stage_count": 0,
                "error_count": 0,
                "stage_attempts": [],
                "temporal_dispatch_count": 0,
                "production_handoff": {
                    "schema_version": "talkshow_production_handoff.v1",
                    "episode_id": str(episode.id),
                    "status": "blocked",
                    "blocking_reasons": ["completed_audio_missing"],
                },
            }
        ]
        episode = service.stop_run(
            episode,
            WorkflowActionRequest(action="stop_run", user_id="producer-1"),
        )
        repository.save(episode)

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        orchestration = components["workflow_orchestration"]
        assert orchestration["details"]["blocked_production_handoff_count"] == 1
        assert orchestration["details"]["current_attempt_count"] == 0
        assert orchestration["details"]["current_blocked_production_handoff_count"] == 0
        assert orchestration["details"]["failed_readiness_checks"] == []

        live = client.get("/api/v1/system/live-provider-readiness")
        assert live.status_code == 200
        live_checks = {check["category"]: check for check in live.json()["checks"]}
        assert live_checks["workflow_orchestration"]["status"] == "pass"
        assert live_checks["workflow_orchestration"]["blockers"] == []
    finally:
        app.dependency_overrides.clear()


def test_production_control_blocks_completion_without_required_assets_and_qc() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))

    service.begin_run(episode, user_id="producer-1")
    before_run = json.loads(json.dumps(episode.workflow_control["run"]))
    before_events = list(episode.workflow_control["workflow_event_log"])
    try:
        service.record_stage(episode, EpisodeStatus.completed, "test")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected completion readiness gate to block completion")

    assert "production cannot be marked completed until gates pass" in message
    assert "completed_final_render_missing" in message
    assert "completed_export_package_missing" in message
    assert "completed_production_manifest_missing" in message
    assert "canonical_transcript_missing" in message
    assert episode.workflow_control["run"]["state"] == "running"
    assert episode.workflow_control["run"]["current_stage"] == before_run["current_stage"]
    assert episode.workflow_control["run"]["stage_history"] == before_run["stage_history"]
    assert "completion_gate" not in episode.workflow_control["run"]
    assert episode.workflow_control["workflow_event_log"] == before_events


def test_completion_readiness_requires_research_evidence_when_enabled() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=research_definition()))
    _append_approved_canonical_transcript(episode)

    readiness = service.completion_readiness(episode)

    assert readiness["research_required"] is True
    assert "research_evidence_pack_missing" in readiness["failed_checks"]
    assert readiness["evidence_pack_asset_id"] is None
    assert readiness["claim_qc_id"] is None


def test_completion_readiness_requires_required_research_approval(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    service = ProductionControlService(settings)
    episode = repository.create(EpisodeCreateRequest(definition=research_definition()))
    _append_approved_canonical_transcript(episode)
    episode = ResearchService(settings).build_evidence_pack(
        episode,
        ResearchBuildRequest(user_id="researcher"),
    )

    readiness = service.completion_readiness(episode)

    assert readiness["research_approval_required"] is True
    assert readiness["research_approval_status"] == "pending"
    assert "research_approval_missing" in readiness["failed_checks"]

    approval = next(item for item in episode.approvals if item.stage == "research_review")
    approval.decision = "rejected"
    readiness = service.completion_readiness(episode)

    assert readiness["research_approval_status"] == "rejected"
    assert "research_approval_rejected" in readiness["failed_checks"]


def test_completion_readiness_blocks_missing_discussion_dimension_coverage() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    session = DiscussionSession(
        episode_id=episode.id,
        status="completed",
        coverage_state={
            "productivity": True,
            "employment": True,
            "quality": False,
        },
    )
    episode.discussion_session = session
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="discussion_session",
            target_id=str(session.id),
            check_type="discussion_minimum_structure",
            severity=QualitySeverity.fail,
            status="fail",
            details={
                "coverage_state": dict(session.coverage_state),
                "missing_dimensions": ["quality"],
            },
        )
    )

    readiness = service.completion_readiness(episode)

    assert readiness["status"] == "fail"
    assert "discussion_structure_qc_failing" in readiness["failed_checks"]
    assert "failing_quality_results_present" in readiness["failed_checks"]
    assert readiness["discussion_qc_status"] == "fail"
    assert readiness["discussion_qc_missing_dimensions"] == ["quality"]
    assert readiness["failing_quality_results"][0]["check_type"] == ("discussion_minimum_structure")


def test_completion_readiness_requires_configured_localized_outputs() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=localized_definition()))
    canonical = _append_approved_canonical_transcript(episode)

    missing = service.completion_readiness(episode)

    assert missing["localized_outputs_required"] is True
    assert missing["missing_localized_output_languages"] == ["de"]
    assert "localized_output_missing" in missing["failed_checks"]
    assert missing["localized_output_readiness"]["outputs"] == [
        {
            "language": "de",
            "mode": "localized_reperformance",
            "transcript_version_id": None,
            "transcript_status": None,
            "qc_id": None,
            "qc_status": None,
        }
    ]

    localized = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.localized,
        language="de",
        parent_version_id=canonical.id,
        status="pending_review",
        turns=[
            TranscriptTurn(
                source_discussion_turn_ids=canonical.turns[0].source_discussion_turn_ids,
                speaker_participant_id=canonical.turns[0].speaker_participant_id,
                text="[de] " + canonical.turns[0].text,
                status="pending_review",
            )
        ],
    )
    episode.transcripts.append(localized)

    pending = service.completion_readiness(episode)
    assert pending["not_approved_localized_output_languages"] == ["de"]
    assert pending["localized_output_qc_missing_languages"] == ["de"]
    assert "localized_output_not_approved" in pending["failed_checks"]
    assert "localized_output_qc_missing" in pending["failed_checks"]

    failing_qc = QualityResult(
        episode_id=episode.id,
        target_type="transcript_version",
        target_id=str(localized.id),
        check_type="localized_transcript_semantic_fidelity",
        severity=QualitySeverity.fail,
        status="fail",
        score=0.1,
        details={"failure_count": 1},
    )
    episode.quality_results.append(failing_qc)
    localized.status = "approved"

    failing = service.completion_readiness(episode)
    assert failing["not_approved_localized_output_languages"] == []
    assert failing["localized_output_qc_missing_languages"] == []
    assert failing["localized_output_qc_failing_languages"] == ["de"]
    assert "localized_output_qc_failing" in failing["failed_checks"]


def test_completion_readiness_requires_transcript_media_coverage() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)

    readiness = service.completion_readiness(episode)

    assert readiness["character_configuration"]["ready"] is True
    assert readiness["character_configuration"]["active_speaker_count"] == 1
    assert readiness["character_configuration"]["configured_model_speaker_count"] == 1
    assert readiness["character_configuration"]["configured_voice_speaker_count"] == 1
    assert readiness["character_configuration"]["configured_visual_speaker_count"] == 1
    assert readiness["playable_turn_count"] == 1
    assert readiness["completed_audio_turn_count"] == 0
    assert readiness["completed_primary_visual_turn_count"] == 0
    assert readiness["missing_audio_turn_ids"] == [str(transcript.turns[0].id)]
    assert readiness["missing_primary_visual_turn_ids"] == [str(transcript.turns[0].id)]
    assert {
        "completed_audio_missing",
        "completed_character_visual_missing",
        "subtitle_asset_missing",
        "timeline_asset_missing",
    } <= set(readiness["failed_checks"])


def test_completion_readiness_blocks_incomplete_character_configuration() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    speaker = next(
        participant
        for participant in episode.participants
        if participant.id == transcript.turns[0].speaker_participant_id
    )
    speaker.model_id = ""
    speaker.voice_profile_id = None
    speaker.visual_profile_id = None

    readiness = service.completion_readiness(episode)

    assert readiness["status"] == "fail"
    assert {
        "character_model_missing",
        "character_voice_missing",
        "character_visual_missing",
    } <= set(readiness["failed_checks"])
    assert readiness["character_configuration"] == {
        "schema_version": "character_configuration_handoff.v1",
        "ready": False,
        "policy": "each_playable_speaker_requires_model_voice_and_visual_profile",
        "active_speaker_count": 1,
        "configured_model_speaker_count": 0,
        "configured_voice_speaker_count": 0,
        "configured_visual_speaker_count": 0,
        "unknown_speaker_participant_ids": [],
        "missing_model_participant_ids": ["host"],
        "missing_voice_participant_ids": ["host"],
        "missing_visual_participant_ids": ["host"],
        "participants": [
            {
                "participant_id": "host",
                "display_name": "Moderator",
                "participant_type": "host",
                "model_endpoint_id": "mock",
                "model_id": "",
                "voice_profile_id": None,
                "visual_profile_id": None,
                "model_ready": False,
                "voice_ready": False,
                "visual_ready": False,
            }
        ],
    }


def test_completion_readiness_blocks_stale_character_media_after_profile_change() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    speaker = next(
        participant
        for participant in episode.participants
        if participant.id == transcript.turns[0].speaker_participant_id
    )
    speaker.voice_profile_id = "voice-host-updated"
    speaker.visual_profile_id = "visual-host-updated"

    readiness = service.completion_readiness(episode)

    assert readiness["completed_audio_turn_count"] == 1
    assert readiness["completed_primary_visual_turn_count"] == 1
    assert readiness["stale_voice_asset_turn_ids"] == [str(transcript.turns[0].id)]
    assert readiness["stale_visual_asset_turn_ids"] == [str(transcript.turns[0].id)]
    assert "character_voice_asset_stale" in readiness["failed_checks"]
    assert "character_visual_asset_stale" in readiness["failed_checks"]
    assert media["audio"].generation_metadata["voice_profile_id"] == "voice-host"
    assert media["visual"].generation_metadata["visual_profile_id"] == "visual-host"


def test_completion_readiness_blocks_stale_model_turn_after_model_change() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    discussion_session = DiscussionSession(
        episode_id=episode.id,
        status="completed",
    )
    discussion_turn = DiscussionTurn(
        discussion_session_id=discussion_session.id,
        sequence_number=1,
        speaker_participant_id=transcript.turns[0].speaker_participant_id,
        turn_type=TurnType.host_opening,
        spoken_text=transcript.turns[0].text,
        intent="open",
        estimated_duration_seconds=1,
        structured_output=StructuredTurnOutput(
            spoken_text=transcript.turns[0].text,
            intent="open",
        ),
        raw_provider_response={},
        generation_metadata={
            "model_endpoint_id": "mock",
            "model_id": "mock-host-v1",
        },
    )
    discussion_session.turns.append(discussion_turn)
    episode.discussion_session = discussion_session
    transcript.turns[0].source_discussion_turn_ids = [discussion_turn.id]
    speaker = next(
        participant
        for participant in episode.participants
        if participant.id == transcript.turns[0].speaker_participant_id
    )
    speaker.model_id = "mock-host-v2"

    readiness = service.completion_readiness(episode)

    assert readiness["stale_model_turn_ids"] == [str(transcript.turns[0].id)]
    assert "character_model_turn_stale" in readiness["failed_checks"]


def test_completion_readiness_requires_transcript_media_qc() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    _append_completed_transcript_media(episode, transcript, include_quality_results=False)

    readiness = service.completion_readiness(episode)

    assert readiness["completed_audio_turn_count"] == 1
    assert readiness["completed_primary_visual_turn_count"] == 1
    assert readiness["audio_qc_id"] is None
    assert readiness["audio_qc_status"] is None
    assert readiness["visual_qc_id"] is None
    assert readiness["visual_qc_status"] is None
    assert readiness["subtitle_qc_id"] is None
    assert readiness["subtitle_qc_status"] is None
    assert readiness["timeline_qc_id"] is None
    assert readiness["timeline_qc_status"] is None
    assert "audio_qc_missing" in readiness["failed_checks"]
    assert "visual_qc_missing" in readiness["failed_checks"]
    assert "subtitle_qc_missing" in readiness["failed_checks"]
    assert "timeline_qc_missing" in readiness["failed_checks"]


def test_completion_readiness_requires_shot_planned_reusable_visuals() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    turn = transcript.turns[0]
    reaction_loop = Asset(
        episode_id=episode.id,
        asset_type=AssetType.reaction_loop,
        language=transcript.language,
        source_entity_type="participant_profile",
        source_entity_id=turn.speaker_participant_id,
        storage_uri="object://dialecticore/visuals/host-reaction.mp4",
        mime_type="video/mp4",
        checksum="sha256:reaction",
        status="planned",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "reaction_loop",
            "render_ready": False,
        },
    )
    studio_scene = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        language=transcript.language,
        source_entity_type="episode",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/visuals/studio.mp4",
        mime_type="video/mp4",
        checksum="sha256:studio",
        status="planned",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "studio_scene",
            "render_ready": False,
        },
    )
    episode.assets.extend([reaction_loop, studio_scene])
    media["visual"].generation_metadata["shot_plan"] = {
        "reusable_reaction_asset_id": str(reaction_loop.id),
        "studio_scene_asset_id": str(studio_scene.id),
    }

    readiness = service.completion_readiness(episode)

    assert readiness["expected_reaction_loop_segment_count"] == 1
    assert readiness["linked_reaction_loop_segment_count"] == 0
    assert readiness["missing_reaction_loop_turn_ids"] == [str(turn.id)]
    assert readiness["expected_studio_scene_segment_count"] == 1
    assert readiness["linked_studio_scene_segment_count"] == 0
    assert readiness["missing_studio_scene_turn_ids"] == [str(turn.id)]
    assert "shot_planned_reaction_loop_missing" in readiness["failed_checks"]
    assert "shot_planned_studio_scene_missing" in readiness["failed_checks"]


def test_completion_readiness_requires_final_render_from_transcript_timeline() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    _append_completed_transcript_media(episode, transcript)
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={"render_type": "final"},
    )
    episode.assets.append(final_render)

    readiness = service.completion_readiness(episode)

    assert readiness["final_render_asset_id"] == str(final_render.id)
    assert readiness["final_render_timeline_linked"] is False
    assert "final_render_timeline_mismatch" in readiness["failed_checks"]


def test_completion_readiness_requires_final_render_qc() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    episode.assets.append(final_render)

    readiness = service.completion_readiness(episode)

    assert readiness["final_render_asset_id"] == str(final_render.id)
    assert readiness["final_render_qc_id"] is None
    assert readiness["final_render_qc_status"] is None
    assert "final_render_qc_missing" in readiness["failed_checks"]


def test_completion_readiness_ignores_failed_qc_for_superseded_timeline_asset() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    old_timeline = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language=transcript.language,
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        storage_uri="object://dialecticore/timelines/old.json",
        mime_type="application/vnd.dialecticore.timeline+json",
        checksum="sha256:old-timeline",
        status="completed",
    )
    episode.assets.append(old_timeline)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="timeline_asset",
            target_id=str(old_timeline.id),
            check_type="timeline_integrity",
            severity=QualitySeverity.fail,
            status="fail",
            score=0.0,
        )
    )
    _append_completed_transcript_media(episode, transcript)

    readiness = service.completion_readiness(episode)

    assert "completed_final_render_missing" in readiness["failed_checks"]
    assert "failing_quality_results_present" not in readiness["failed_checks"]
    assert readiness["failing_quality_results"] == []


def test_completion_readiness_ignores_failed_render_for_superseded_timeline() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    old_timeline = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language=transcript.language,
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        status="replaced",
    )
    failed_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=transcript.language,
        source_entity_type="timeline_asset",
        source_entity_id=str(old_timeline.id),
        status="failed",
        generation_metadata={"render_type": "preview"},
    )
    episode.assets.extend([old_timeline, failed_render])
    _append_completed_transcript_media(episode, transcript)

    readiness = service.completion_readiness(episode)

    assert "unresolved_failed_assets_present" not in readiness["failed_checks"]
    assert readiness["unresolved_failed_assets"] == []
    assert readiness["nonblocking_unresolved_failed_assets"][0]["asset_id"] == str(
        failed_render.id
    )


def test_completion_readiness_ignores_failed_qc_for_superseded_subtitle() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    old_subtitle = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language=transcript.language,
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        status="replaced",
    )
    episode.assets.append(old_subtitle)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="asset",
            target_id=str(old_subtitle.id),
            check_type="subtitle_generation_completeness",
            severity=QualitySeverity.fail,
            status="fail",
        )
    )
    _append_completed_transcript_media(episode, transcript)

    readiness = service.completion_readiness(episode)

    assert "failing_quality_results_present" not in readiness["failed_checks"]
    assert readiness["failing_quality_results"] == []


def test_duration_failure_is_superseded_by_shorter_canonical_timeline_turn() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    transcript.turns[0].source_discussion_turn_ids = ["legacy-long-turn"]
    media = _append_completed_transcript_media(episode, transcript)
    media["timeline"].generation_metadata["timeline_json"]["segments"][0][
        "duration_ms"
    ] = 21_000
    result = QualityResult(
        episode_id=episode.id,
        target_type="discussion_session",
        target_id="discussion-session",
        check_type="discussion_duration_control",
        severity=QualitySeverity.fail,
        status="fail",
        details={
            "maximum_monologue_seconds": 25,
            "failures": [
                {
                    "issue": "turn_exceeds_maximum_monologue_duration",
                    "turn_id": "legacy-long-turn",
                }
            ],
        },
    )

    assert service._discussion_duration_failure_superseded(episode, result) is True


def test_completion_readiness_blocks_stale_render_source_assets() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    episode.assets.append(final_render)
    _append_approved_preview_render(episode, media["timeline"])
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(final_render.id),
            check_type="render_final_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
            details={
                "stale_source_asset_count": 1,
                "missing_source_asset_count": 0,
                "issues": [
                    {
                        "severity": "fail",
                        "issue": "render_source_asset_stale",
                        "asset_id": str(media["timeline"].id),
                    }
                ],
            },
        )
    )

    readiness = service.completion_readiness(episode)

    assert readiness["final_render_source_assets_fresh"] is False
    assert readiness["final_render_stale_source_asset_count"] == 1
    assert readiness["final_render_missing_source_asset_count"] == 0
    assert "final_render_source_assets_stale" in readiness["failed_checks"]


def test_completion_readiness_reports_visual_source_summary() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)

    native_readiness = service.completion_readiness(episode)

    assert native_readiness["visual_source_summary"] == {
        "schema_version": "visual_source_summary.v1",
        "playable_turn_count": 1,
        "completed_primary_visual_turn_count": 1,
        "native_primary_visual_turn_count": 1,
        "fallback_primary_visual_turn_count": 0,
        "missing_primary_visual_turn_count": 0,
        "native_visual_complete": True,
        "fallback_visual_used": False,
        "fallback_primary_visual_turn_ids": [],
        "missing_primary_visual_turn_ids": [],
        "sample_fallback_reasons": [],
    }

    media["visual"].generation_metadata["fallback_visual"] = True
    media["visual"].generation_metadata["fallback_reason"] = "ComfyUI prompt admission denied"
    fallback_readiness = service.completion_readiness(episode)

    assert fallback_readiness["status"] == "fail"
    assert fallback_readiness["production_target"] == "native_visual"
    assert fallback_readiness["production_target_satisfied"] is False
    assert "native_primary_visuals_missing" in fallback_readiness["failed_checks"]
    assert fallback_readiness["visual_source_summary"] == {
        "schema_version": "visual_source_summary.v1",
        "playable_turn_count": 1,
        "completed_primary_visual_turn_count": 1,
        "native_primary_visual_turn_count": 0,
        "fallback_primary_visual_turn_count": 1,
        "missing_primary_visual_turn_count": 0,
        "native_visual_complete": False,
        "fallback_visual_used": True,
        "fallback_primary_visual_turn_ids": [str(transcript.turns[0].id)],
        "missing_primary_visual_turn_ids": [],
        "sample_fallback_reasons": ["ComfyUI prompt admission denied"],
    }

    episode.definition.workflow.production_target = "audio_first"
    audio_first_readiness = service.completion_readiness(episode)

    assert audio_first_readiness["production_target"] == "audio_first"
    assert audio_first_readiness["production_target_satisfied"] is True
    assert "native_primary_visuals_missing" not in audio_first_readiness["failed_checks"]


def test_production_media_readiness_reports_managed_media_execution_evidence() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    _append_completed_transcript_media(episode, transcript)
    completion = service.completion_readiness(episode)
    native_visual_mode = {"mode": "native_visual", "status": "pass"}
    pilot = {
        "stages": [
            {
                "category": "visuals",
                "status": "pass",
                "details": {
                    "readiness_checks": {
                        "selected_b1_managed_media_presets_available": True,
                        "selected_native_comfyui_prompt_admission_ready": True,
                    },
                    "managed_media_required_endpoints": [],
                    "managed_media_missing_preset_endpoints": [],
                },
            }
        ]
    }

    no_managed = _production_media_readiness(
        episode,
        completion,
        pilot,
        native_visual_mode,
    )

    assert no_managed["managed_media_execution_ready"] is None
    assert no_managed["managed_media_execution"]["status"] == "not_required"
    assert no_managed["managed_media_operator_action"] == "no_managed_media_action_required"

    pilot["stages"][0]["details"]["managed_media_required_endpoints"] = [
        {"endpoint": {"id": "b1-comfyui"}, "required_presets": ["video-image"]}
    ]
    configured_not_attempted = _production_media_readiness(
        episode,
        completion,
        pilot,
        native_visual_mode,
    )

    assert configured_not_attempted["managed_media_execution_ready"] is False
    assert configured_not_attempted["managed_media_execution"]["required"] is True
    assert configured_not_attempted["managed_media_execution"]["status"] == "not_attempted"
    assert configured_not_attempted["managed_media_operator_action"] == (
        "run_b1_managed_media_smoke_or_start_native_visual_production"
    )

    failed_managed_visual = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(transcript.turns[0].id),
        status="failed",
        generation_metadata={
            "adapter": "b1_managed_media",
            "remote_job_id": "job_failed",
            "managed_media_payload": {
                "model": "image-default",
                "operation": "image-generation",
            },
            "provider_response": {
                "state": "failed",
                "stage": "failed",
                "model_alias": "image-default",
                "operation": "image-generation",
                "failure_category": "gpu_runner_error",
                "failure_message": "ValueError",
                "traceback": "must not leak into compact report",
            },
        },
    )
    episode.assets.append(failed_managed_visual)

    failed = _production_media_readiness(
        episode,
        completion,
        pilot,
        native_visual_mode,
    )

    assert failed["managed_media_execution_ready"] is False
    assert failed["managed_media_execution"]["status"] == "fail"
    assert failed["managed_media_execution"]["failed_count"] == 1
    assert failed["managed_media_operator_action"] == (
        "fix_b1_managed_media_runner_then_retry_visual_assets"
    )
    assert failed["managed_media_execution"]["models"] == ["image-default"]
    assert failed["managed_media_execution"]["operations"] == ["image-generation"]
    assert failed["managed_media_execution"]["failure_samples"] == [
        {
            "asset_id": str(failed_managed_visual.id),
            "asset_type": "video",
            "status": "failed",
            "remote_job_id": "job_failed",
            "model": "image-default",
            "operation": "image-generation",
            "provider_state": "failed",
            "stage": "failed",
            "failure_category": "gpu_runner_error",
            "failure_message": "ValueError",
        }
    ]
    assert "traceback" not in json.dumps(failed["managed_media_execution"])

    failed_managed_visual.status = "completed"
    failed_managed_visual.storage_uri = "object://dialecticore/video/managed.mp4"
    failed_managed_visual.checksum = "sha256:managed"
    failed_managed_visual.generation_metadata["provider_response"] = {
        "state": "completed",
        "stage": "completed",
        "model_alias": "image-default",
        "operation": "image-generation",
    }

    completed = _production_media_readiness(
        episode,
        completion,
        pilot,
        native_visual_mode,
    )

    assert completed["managed_media_execution_ready"] is True
    assert completed["managed_media_execution"]["status"] == "pass"
    assert completed["managed_media_execution"]["completed_artifact_count"] == 1
    assert completed["managed_media_operator_action"] == "managed_media_execution_ready"


def test_managed_media_smoke_evidence_summarizes_runner_failure(tmp_path: Path) -> None:
    missing = _managed_media_smoke_evidence(str(tmp_path / "missing.json"))
    assert missing == {
        "schema_version": "managed_media_smoke_evidence_summary.v1",
        "configured": True,
        "path": str(tmp_path / "missing.json"),
        "status": "missing",
        "ready": False,
    }

    evidence_path = tmp_path / "b1-managed-media-smoke.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "b1_managed_media_smoke_evidence.v1",
                "created_at": "2026-07-30T08:42:07+00:00",
                "api_base": "https://api.ai.b1.germering",
                "model": "image-default",
                "modality": "image",
                "operation": "image-generation",
                "status": "runner_failed",
                "job_id": "job-a",
                "terminal": {
                    "job_id": "job-a",
                    "state": "failed",
                    "stage": "failed",
                    "failure_category": "gpu_runner_error",
                    "failure_message": "ValueError",
                    "artifact_count": 0,
                    "traceback": "must not leak",
                },
            }
        ),
        encoding="utf-8",
    )

    summary = _managed_media_smoke_evidence(
        str(evidence_path),
        now=datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
    )

    assert summary == {
        "schema_version": "managed_media_smoke_evidence_summary.v1",
        "configured": True,
        "path": str(evidence_path),
        "status": "runner_failed",
        "ready": False,
        "created_at": "2026-07-30T08:42:07+00:00",
        "age_seconds": 1073,
        "fresh": True,
        "freshness_window_seconds": 86400,
        "api_base": "https://api.ai.b1.germering",
        "model": "image-default",
        "modality": "image",
        "operation": "image-generation",
        "job_id": "job-a",
        "terminal_state": "failed",
        "terminal_stage": "failed",
        "failure_category": "gpu_runner_error",
        "failure_message": "ValueError",
        "artifact_count": 0,
        "busy": False,
        "busy_details": None,
        "action": "fix_b1_managed_media_runner_then_rerun_smoke",
    }
    assert "traceback" not in json.dumps(summary)


def test_provider_repair_handoff_summarizes_requirements_file(tmp_path: Path) -> None:
    requirements_path = tmp_path / "media-requirements.md"
    requirements_path.write_text(
        "\n".join(
            [
                "### Voicebox Smoke Recheck Added 2026-07-30T11:12:07+00:00",
                "Acceptance for the B1-side fix: return HTTP 200.",
                "### B1 Managed Media Smoke Recheck Added 2026-07-30T11:09:17+00:00",
                "- job_id: `job_123`",
            ]
        ),
        encoding="utf-8",
    )

    handoff = _provider_repair_handoff(str(requirements_path))

    assert handoff["schema_version"] == "provider_repair_handoff.v1"
    assert handoff["status"] == "present"
    assert handoff["exists"] is True
    assert handoff["file_size_bytes"] > 0
    assert handoff["section_count"] == 2
    assert handoff["latest_sections"] == [
        "Voicebox Smoke Recheck Added 2026-07-30T11:12:07+00:00",
        "B1 Managed Media Smoke Recheck Added 2026-07-30T11:09:17+00:00",
    ]
    assert handoff["has_voicebox_requirements"] is True
    assert handoff["has_managed_media_requirements"] is True


def test_provider_repair_handoff_reports_missing_file(tmp_path: Path) -> None:
    handoff = _provider_repair_handoff(str(tmp_path / "missing.md"))

    assert handoff == {
        "schema_version": "provider_repair_handoff.v1",
        "configured": True,
        "path": str(tmp_path / "missing.md"),
        "exists": False,
        "status": "missing",
        "section_count": 0,
        "latest_sections": [],
        "has_voicebox_requirements": False,
        "has_managed_media_requirements": False,
    }


def test_publish_evidence_binding_warns_when_payload_manifest_is_not_current() -> None:
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    package = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        status="completed",
        source_entity_type="render_asset",
        source_entity_id="render-1",
        storage_uri="object://dialecticore/package.zip",
        checksum="sha256:package",
    )
    old_manifest = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        status="completed",
        source_entity_type="export_package",
        source_entity_id=str(package.id),
        storage_uri="object://dialecticore/old-manifest.json",
        checksum="sha256:old",
        generation_metadata={"production_manifest": {"schema_version": "production_manifest.v1"}},
    )
    current_manifest = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        status="completed",
        source_entity_type="export_package",
        source_entity_id=str(package.id),
        storage_uri="object://dialecticore/current-manifest.json",
        checksum="sha256:current",
        generation_metadata={"production_manifest": {"publish_jobs": []}},
    )
    job = PublishJob(
        episode_id=episode.id,
        publisher_target_id="mock-youtube",
        platform="youtube",
        package_asset_id=package.id,
        status="completed",
        dry_run=True,
        publish_url="mock://youtube/job",
        delivery_payload={
            "schema_version": "publish_delivery_payload.v1",
            "package_asset_id": str(package.id),
            "package_checksum": package.checksum,
            "production_manifest_asset_id": str(old_manifest.id),
            "production_manifest_checksum": old_manifest.checksum,
            "production_manifest_schema_version": "production_manifest.v1",
        },
    )
    current_manifest.generation_metadata["production_manifest"]["publish_jobs"].append(
        {
            "id": str(job.id),
            "status": "completed",
            "package_asset_id": str(package.id),
        }
    )
    episode.assets.extend([package, old_manifest, current_manifest])

    binding = _publish_evidence_binding(
        episode,
        job,
        {"asset_id": str(package.id), "checksum": package.checksum},
        {"asset_id": str(current_manifest.id), "checksum": current_manifest.checksum},
    )

    assert binding["schema_version"] == "publish_evidence_binding.v1"
    assert binding["status"] == "warning"
    assert binding["payload_package_matches"] is True
    assert binding["current_manifest_embeds_publish_job"] is True
    assert binding["current_manifest_publish_job_status_matches"] is True
    assert binding["payload_manifest_exists"] is True
    assert binding["payload_manifest_is_current"] is False
    assert binding["payload_production_manifest_asset_id"] == str(old_manifest.id)
    assert binding["current_production_manifest_asset_id"] == str(current_manifest.id)
    assert _compact_publish_binding_summary(binding) == {
        "status": "warning",
        "publish_job_id": str(job.id),
        "job_status": "completed",
        "dry_run": True,
        "package_asset_matches": True,
        "payload_package_matches": True,
        "current_manifest_embeds_publish_job": True,
        "current_manifest_publish_job_status_matches": True,
        "payload_manifest_is_current": False,
        "payload_production_manifest_schema_version": "production_manifest.v1",
    }


def test_publish_evidence_binding_fails_on_package_payload_mismatch() -> None:
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    package = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        status="completed",
        source_entity_type="render_asset",
        source_entity_id="render-1",
        checksum="sha256:package",
    )
    manifest = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        status="completed",
        source_entity_type="export_package",
        source_entity_id=str(package.id),
        generation_metadata={"production_manifest": {"publish_jobs": []}},
    )
    job = PublishJob(
        episode_id=episode.id,
        publisher_target_id="mock-youtube",
        platform="youtube",
        package_asset_id=package.id,
        status="completed",
        delivery_payload={
            "package_asset_id": str(package.id),
            "package_checksum": "sha256:wrong",
            "production_manifest_asset_id": str(manifest.id),
            "production_manifest_schema_version": "production_manifest.v1",
        },
    )
    manifest.generation_metadata["production_manifest"]["publish_jobs"].append(
        {
            "id": str(job.id),
            "status": "completed",
            "package_asset_id": str(package.id),
        }
    )
    episode.assets.extend([package, manifest])

    binding = _publish_evidence_binding(
        episode,
        job,
        {"asset_id": str(package.id), "checksum": package.checksum},
        {"asset_id": str(manifest.id), "checksum": manifest.checksum},
    )

    assert binding["status"] == "fail"
    assert binding["payload_package_matches"] is False


def test_production_media_readiness_uses_failed_managed_media_smoke_action(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    _append_completed_transcript_media(episode, transcript)
    completion = service.completion_readiness(episode)
    pilot = {
        "stages": [
            {
                "category": "visuals",
                "status": "pass",
                "details": {
                    "readiness_checks": {
                        "selected_b1_managed_media_presets_available": True,
                        "selected_native_comfyui_prompt_admission_ready": True,
                    },
                    "managed_media_required_endpoints": [
                        {"endpoint": {"id": "b1-comfyui"}, "required_presets": ["video-image"]}
                    ],
                    "managed_media_missing_preset_endpoints": [],
                },
            }
        ]
    }
    evidence_path = tmp_path / "b1-managed-media-smoke.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "b1_managed_media_smoke_evidence.v1",
                "status": "runner_failed",
                "model": "image-default",
                "operation": "image-generation",
                "terminal": {
                    "state": "failed",
                    "failure_category": "gpu_runner_error",
                    "failure_message": "ValueError",
                    "artifact_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    readiness = _production_media_readiness(
        episode,
        completion,
        pilot,
        {"mode": "native_visual", "status": "pass"},
        managed_media_smoke_evidence_path=str(evidence_path),
    )

    assert readiness["managed_media_execution"]["status"] == "not_attempted"
    assert readiness["managed_media_smoke"]["status"] == "runner_failed"
    assert readiness["managed_media_smoke"]["failure_category"] == "gpu_runner_error"
    assert readiness["managed_media_operator_action"] == (
        "fix_b1_managed_media_runner_then_rerun_smoke"
    )


def test_production_operator_next_actions_include_parallel_media_blockers() -> None:
    actions = _production_operator_next_actions(
        primary_action="fix_voicebox_generation_then_retry_audio_assets",
        report_status="warning",
        blockers=["canonical_transcript_missing"],
        media_readiness={
            "audio_operator_action": "fix_voicebox_generation_then_retry_audio_assets",
            "audio_generation": {"status": "fail"},
            "managed_media_operator_action": ("fix_b1_managed_media_runner_then_rerun_smoke"),
            "managed_media_smoke": {"status": "runner_failed"},
            "managed_media_execution": {"status": "not_attempted"},
        },
        package_inspection=None,
        latest_publish_job=None,
        asset_download_status="fail",
    )

    assert actions[:2] == [
        {
            "scope": "speech",
            "action": "fix_voicebox_generation_then_retry_audio_assets",
            "status": "fail",
        },
        {
            "scope": "managed_media",
            "action": "fix_b1_managed_media_runner_then_rerun_smoke",
            "status": "runner_failed",
        },
    ]
    assert {(action["scope"], action["action"]) for action in actions} >= {
        ("delivery_artifacts", "restore_or_regenerate_missing_delivery_artifacts"),
        ("export_package", "inspect_or_regenerate_youtube_export_package"),
        ("publishing", "run_dry_run_publish_for_real_life_test"),
        ("completion", "resolve_completion_readiness_blockers"),
    }


def test_latest_live_provider_preflight_summary_uses_audited_cast_evidence() -> None:
    repository = EpisodeRepository()
    repository.record_global_audit_event(
        AuditEvent(
            event_type="live_provider.cast_preflight_checked",
            actor="operator",
            details={
                "schema_version": "live_provider_cast_preflight_audit.v1",
                "status": "fail",
                "participant_ids": ["chatgpt", "claude"],
                "blocking_sections": ["voicebox"],
                "model_summary": {
                    "participant_count": 2,
                    "failed_count": 0,
                    "failed_model_ids": [],
                },
                "voicebox_summary": {
                    "participant_count": 2,
                    "failed_count": 2,
                    "failed_voice_profile_ids": ["voice-chatgpt", "voice-claude"],
                },
            },
        )
    )

    summary = _latest_live_provider_preflight_summary(repository)

    assert summary["schema_version"] == "production_live_provider_preflight_summary.v1"
    assert summary["status"] == "fail"
    assert summary["ready"] is False
    assert summary["actor"] == "operator"
    assert summary["participant_ids"] == ["chatgpt", "claude"]
    assert summary["blocking_sections"] == ["voicebox"]
    assert summary["model_participant_count"] == 2
    assert summary["model_failed_count"] == 0
    assert summary["voicebox_participant_count"] == 2
    assert summary["voicebox_failed_count"] == 2
    assert summary["failed_voice_profile_ids"] == ["voice-chatgpt", "voice-claude"]
    assert summary["action"] == "fix_voicebox_generation_then_rerun_live_preflight"

    repository.record_global_audit_event(
        AuditEvent(
            event_type="live_provider.cast_preflight_checked",
            actor="operator",
            details={
                "schema_version": "live_provider_cast_preflight_audit.v1",
                "status": "fail",
                "participant_ids": ["chatgpt", "claude"],
                "blocking_sections": ["openrouter", "voicebox"],
                "model_summary": {
                    "participant_count": 2,
                    "failed_count": 1,
                    "failed_model_ids": ["anthropic/claude-sonnet-5"],
                },
                "voicebox_summary": {
                    "participant_count": 2,
                    "failed_count": 2,
                    "failed_voice_profile_ids": ["voice-chatgpt", "voice-claude"],
                },
            },
        )
    )

    mixed_summary = _latest_live_provider_preflight_summary(repository)

    assert mixed_summary["blocking_sections"] == ["openrouter", "voicebox"]
    assert mixed_summary["action"] == "fix_live_provider_failures_then_rerun_preflight"


def test_production_operator_next_actions_include_live_provider_preflight_failure() -> None:
    actions = _production_operator_next_actions(
        primary_action="inspect_export_package_and_publish_evidence",
        report_status="pass",
        blockers=[],
        media_readiness={"native_visual_ready": True},
        package_inspection={"status": "pass"},
        latest_publish_job=type("PublishJob", (), {"status": "completed"})(),
        asset_download_status="pass",
        live_provider_preflight={
            "status": "fail",
            "action": "fix_voicebox_generation_then_rerun_live_preflight",
            "blocking_sections": ["voicebox"],
            "model_failed_count": 0,
            "voicebox_failed_count": 6,
        },
    )

    assert actions == [
        {
            "scope": "acceptance",
            "action": "inspect_export_package_and_publish_evidence",
            "status": "pass",
        },
        {
            "scope": "live_provider_preflight",
            "action": "fix_voicebox_generation_then_rerun_live_preflight",
            "status": "fail",
            "blocking_sections": ["voicebox"],
            "model_failed_count": 0,
            "voicebox_failed_count": 6,
        },
    ]


def test_real_life_test_readiness_separates_local_acceptance_from_provider_gates() -> None:
    readiness = _production_real_life_test_readiness(
        report_status="pass",
        audio_first_test_ready=True,
        native_visual_test_ready=False,
        live_provider_preflight={
            "ready": False,
            "action": "fix_voicebox_generation_then_rerun_live_preflight",
        },
        media_readiness={
            "managed_media_smoke": {
                "ready": False,
                "action": "fix_b1_managed_media_runner_then_rerun_smoke",
            }
        },
    )

    assert readiness == {
        "schema_version": "production_real_life_test_readiness.v1",
        "audio_first_ready": False,
        "native_visual_ready": False,
        "ready": False,
        "recommended_mode": None,
        "audio_first_blockers": ["fix_voicebox_generation_then_rerun_live_preflight"],
        "native_visual_blockers": [
            "local_acceptance_not_ready",
            "fix_voicebox_generation_then_rerun_live_preflight",
            "fix_b1_managed_media_runner_then_rerun_smoke",
        ],
        "live_provider_preflight_ready": False,
        "managed_media_smoke_ready": False,
        "next_action": "rerun_live_provider_preflight_after_provider_fix",
    }


def test_production_operator_next_action_uses_workflow_handoff_before_delivery_noise() -> None:
    workflow_run = {
        "status": "awaiting_approval",
        "stop_reason": "pending_approval",
        "pending_approval_stages": ["preview_render_review"],
        "handoff": {
            "status": "review_ready",
            "next_handoff_action": "review_preview_render",
            "blocking_reasons": [],
        },
    }

    action = _production_operator_next_action(
        report_status="warning",
        blockers=["final_render_missing", "publish_job_missing"],
        media_readiness={
            "audio_operator_action": "audio_generation_ready",
            "managed_media_operator_action": "no_managed_media_action_required",
        },
        package_inspection=None,
        latest_publish_job=None,
        asset_download_status="fail",
        workflow_run_until_blocked=workflow_run,
    )
    actions = _production_operator_next_actions(
        primary_action=action,
        report_status="warning",
        blockers=["final_render_missing", "publish_job_missing"],
        media_readiness={
            "audio_operator_action": "audio_generation_ready",
            "managed_media_operator_action": "no_managed_media_action_required",
        },
        package_inspection=None,
        latest_publish_job=None,
        asset_download_status="fail",
        workflow_run_until_blocked=workflow_run,
    )

    assert action == "review_preview_render"
    assert actions[0] == {
        "scope": "workflow",
        "action": "review_preview_render",
        "status": "review_ready",
        "stop_reason": "pending_approval",
        "pending_approval_stages": ["preview_render_review"],
        "blocking_reasons": [],
    }
    assert {(item["scope"], item["action"]) for item in actions} >= {
        ("delivery_artifacts", "restore_or_regenerate_missing_delivery_artifacts"),
        ("export_package", "inspect_or_regenerate_youtube_export_package"),
        ("publishing", "run_dry_run_publish_for_real_life_test"),
    }


def test_production_operator_next_action_keeps_provider_failure_ahead_of_handoff() -> None:
    action = _production_operator_next_action(
        report_status="warning",
        blockers=["completed_audio_missing"],
        media_readiness={
            "audio_operator_action": "fix_voicebox_generation_then_retry_audio_assets",
            "audio_generation": {"status": "fail"},
            "managed_media_operator_action": "no_managed_media_action_required",
        },
        package_inspection=None,
        latest_publish_job=None,
        asset_download_status="fail",
        workflow_run_until_blocked={
            "status": "blocked",
            "stop_reason": "no_progress",
            "handoff": {
                "status": "blocked",
                "next_handoff_action": "produce_remaining_speech_assets",
                "blocking_reasons": ["completed_audio_missing"],
            },
        },
    )

    assert action == "fix_voicebox_generation_then_retry_audio_assets"


def test_production_operator_next_actions_keep_native_visual_followup_on_pass() -> None:
    actions = _production_operator_next_actions(
        primary_action="inspect_export_package_and_publish_evidence",
        report_status="pass",
        blockers=[],
        media_readiness={
            "native_visual_ready": False,
            "visual_source_summary": {
                "fallback_primary_visual_turn_count": 13,
                "missing_primary_visual_turn_count": 0,
            },
            "managed_media_operator_action": ("fix_b1_managed_media_runner_then_rerun_smoke"),
            "managed_media_smoke": {"status": "runner_failed"},
            "managed_media_execution": {"status": "not_attempted"},
        },
        package_inspection={"status": "pass"},
        latest_publish_job=type("PublishJob", (), {"status": "completed"})(),
        asset_download_status="pass",
    )

    assert actions == [
        {
            "scope": "acceptance",
            "action": "inspect_export_package_and_publish_evidence",
            "status": "pass",
        },
        {
            "scope": "managed_media",
            "action": "fix_b1_managed_media_runner_then_rerun_smoke",
            "status": "runner_failed",
        },
        {
            "scope": "native_visual",
            "action": "retry_fallback_visuals_as_native_after_b1_fix",
            "status": "fallback_visuals_present",
            "asset_count": 13,
        },
    ]


def test_production_operator_next_actions_keep_speech_followup_on_pass() -> None:
    actions = _production_operator_next_actions(
        primary_action="inspect_export_package_and_publish_evidence",
        report_status="pass",
        blockers=[],
        media_readiness={
            "audio_operator_action": "fix_voicebox_generation_then_retry_audio_assets",
            "audio_generation": {"status": "pass"},
            "native_visual_ready": True,
        },
        package_inspection={"status": "pass"},
        latest_publish_job=type("PublishJob", (), {"status": "completed"})(),
        asset_download_status="pass",
    )

    assert actions == [
        {
            "scope": "acceptance",
            "action": "inspect_export_package_and_publish_evidence",
            "status": "pass",
        },
        {
            "scope": "speech",
            "action": "fix_voicebox_generation_then_retry_audio_assets",
            "status": "pass",
        },
    ]


def test_production_report_prioritizes_failed_voicebox_audio_action(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    _append_completed_transcript_media(episode, transcript)
    failed_audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language=transcript.language,
        source_entity_type="transcript_turn",
        source_entity_id=str(transcript.turns[0].id),
        status="failed",
        generation_metadata={
            "adapter": "voicebox",
            "adapter_type": "b1_voice_stream",
            "voicebox_endpoint_id": "b1-voicebox",
            "voice_profile_id": "voice-claude",
            "remote_profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
            "failure_type": "HTTPStatusError",
            "failure": "Server error '500 Internal Server Error'",
        },
    )
    episode.assets.append(failed_audio)
    completion = service.completion_readiness(episode)
    pilot = {
        "stages": [
            {
                "category": "visuals",
                "status": "pass",
                "details": {
                    "readiness_checks": {
                        "selected_b1_managed_media_presets_available": True,
                        "selected_native_comfyui_prompt_admission_ready": True,
                    },
                    "managed_media_required_endpoints": [],
                    "managed_media_missing_preset_endpoints": [],
                },
            }
        ]
    }

    report = _episode_production_test_report(
        episode,
        completion,
        pilot,
        create_object_store(settings),
    )

    assert report["operator_next_action"] == ("fix_voicebox_generation_then_retry_audio_assets")
    assert report["media_readiness"]["audio_generation_ready"] is False
    assert report["media_readiness"]["audio_operator_action"] == (
        "fix_voicebox_generation_then_retry_audio_assets"
    )
    assert report["media_readiness"]["audio_generation"]["status"] == "fail"
    assert report["media_readiness"]["audio_generation"]["failure_samples"] == [
        {
            "asset_id": str(failed_audio.id),
            "status": "failed",
            "voicebox_endpoint_id": "b1-voicebox",
            "voice_profile_id": "voice-claude",
            "remote_profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
            "adapter_type": "b1_voice_stream",
            "failure_type": "HTTPStatusError",
            "failure": "Server error '500 Internal Server Error'",
        }
    ]


def test_production_media_readiness_marks_partial_voicebox_audio_not_ready() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    media["audio"].generation_metadata["adapter"] = "voicebox"
    planned_audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language=transcript.language,
        source_entity_type="transcript_turn",
        source_entity_id="turn-b",
        status="planned",
        generation_metadata={"adapter": "voicebox"},
    )
    episode.assets.append(planned_audio)
    completion = service.completion_readiness(episode)
    pilot = {
        "stages": [
            {
                "category": "visuals",
                "status": "pass",
                "details": {
                    "readiness_checks": {
                        "selected_b1_managed_media_presets_available": True,
                        "selected_native_comfyui_prompt_admission_ready": True,
                    },
                    "managed_media_required_endpoints": [],
                    "managed_media_missing_preset_endpoints": [],
                },
            }
        ]
    }

    readiness = _production_media_readiness(
        episode,
        completion,
        pilot,
        {"mode": "native_visual", "status": "pass"},
    )

    assert readiness["audio_generation_ready"] is False
    assert readiness["audio_generation"]["status"] == "partial"
    assert readiness["audio_generation"]["voicebox_asset_count"] == 2
    assert readiness["audio_generation"]["completed_artifact_count"] == 1
    assert readiness["audio_operator_action"] == "produce_remaining_speech_assets"


def test_production_media_readiness_prioritizes_unhealthy_voicebox_provider() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    media["audio"].generation_metadata.update(
        {
            "adapter": "voicebox",
            "voice_profile_id": "voice-claude",
            "voicebox_endpoint_id": "b1-voicebox",
        }
    )
    planned_audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language=transcript.language,
        source_entity_type="transcript_turn",
        source_entity_id="turn-b",
        status="planned",
        generation_metadata={
            "adapter": "voicebox",
            "voice_profile_id": "voice-claude",
        },
    )
    episode.assets.append(planned_audio)
    completion = service.completion_readiness(episode)
    pilot = {
        "stages": [
            {
                "category": "visuals",
                "status": "pass",
                "details": {
                    "readiness_checks": {
                        "selected_b1_managed_media_presets_available": True,
                        "selected_native_comfyui_prompt_admission_ready": True,
                    },
                    "managed_media_required_endpoints": [],
                    "managed_media_missing_preset_endpoints": [],
                },
            }
        ]
    }

    readiness = _production_media_readiness(
        episode,
        completion,
        pilot,
        {"mode": "native_visual", "status": "pass"},
        voicebox_endpoints=[
            VoiceboxEndpoint(
                id="b1-voicebox",
                name="B1 Voicebox",
                adapter_type="b1_voice_stream",
                base_url="https://voice.ai.b1.germering",
                health_status="unhealthy",
                capabilities={
                    "generation_canary": {
                        "status": "fail",
                        "status_code": 500,
                        "riff_wave": False,
                    }
                },
            )
        ],
        voice_profiles=[
            VoiceProfile(
                id="voice-claude",
                name="Claude",
                voicebox_endpoint_id="b1-voicebox",
                voice_id="bd4e9bf1-482b-4900-97c1-48275d1ba28c",
                language="de",
            )
        ],
    )

    assert readiness["audio_generation_ready"] is False
    assert readiness["audio_generation"]["status"] == "partial"
    assert readiness["audio_generation"]["provider_ready"] is False
    assert readiness["audio_generation"]["required_endpoint_ids"] == ["b1-voicebox"]
    assert readiness["audio_generation"]["unhealthy_endpoint_count"] == 1
    assert readiness["audio_generation"]["provider_issue_samples"] == [
        {
            "endpoint_id": "b1-voicebox",
            "health_status": "unhealthy",
            "adapter_type": "b1_voice_stream",
            "canary_status": "fail",
            "canary_status_code": 500,
            "canary_riff_wave": False,
            "action": "fix_voicebox_generation_then_rerun_health_check",
        }
    ]
    assert readiness["audio_operator_action"] == ("fix_voicebox_generation_then_retry_audio_assets")


def test_production_media_readiness_reports_cancelled_audio_reset_action() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    media["audio"].generation_metadata.update(
        {
            "adapter": "voicebox",
            "voice_profile_id": "voice-claude",
            "voicebox_endpoint_id": "b1-voicebox",
        }
    )
    cancelled_audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language=transcript.language,
        source_entity_type="transcript_turn",
        source_entity_id="turn-b",
        status="cancelled",
        generation_metadata={
            "adapter": "voicebox",
            "voice_profile_id": "voice-claude",
            "ready_for_retry": False,
        },
    )
    episode.assets.append(cancelled_audio)
    completion = service.completion_readiness(episode)
    pilot = {
        "stages": [
            {
                "category": "visuals",
                "status": "pass",
                "details": {
                    "readiness_checks": {
                        "selected_b1_managed_media_presets_available": True,
                        "selected_native_comfyui_prompt_admission_ready": True,
                    },
                    "managed_media_required_endpoints": [],
                    "managed_media_missing_preset_endpoints": [],
                },
            }
        ]
    }

    readiness = _production_media_readiness(
        episode,
        completion,
        pilot,
        {"mode": "native_visual", "status": "pass"},
        voicebox_endpoints=[
            VoiceboxEndpoint(
                id="b1-voicebox",
                name="B1 Voicebox",
                adapter_type="b1_voice_stream",
                base_url="https://voice.ai.b1.germering",
                health_status="healthy",
            )
        ],
        voice_profiles=[
            VoiceProfile(
                id="voice-claude",
                name="Claude",
                voicebox_endpoint_id="b1-voicebox",
                voice_id="bd4e9bf1-482b-4900-97c1-48275d1ba28c",
                language="de",
            )
        ],
    )

    assert readiness["audio_generation_ready"] is False
    assert readiness["audio_generation"]["provider_ready"] is True
    assert readiness["audio_generation"]["cancelled_count"] == 1
    assert readiness["audio_generation"]["blocked_cancelled_count"] == 1
    assert readiness["audio_generation"]["retryable_cancelled_count"] == 0
    assert readiness["audio_operator_action"] == "reset_cancelled_audio_assets_for_retry"


def test_completion_readiness_requires_approved_preview_render() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    episode.assets.append(final_render)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(final_render.id),
            check_type="render_final_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )

    readiness = service.completion_readiness(episode)

    assert readiness["preview_render_asset_id"] is None
    assert readiness["preview_render_qc_id"] is None
    assert readiness["preview_render_qc_status"] is None
    assert readiness["preview_render_approved"] is False
    assert "preview_render_missing" in readiness["failed_checks"]


def test_completion_readiness_requires_thumbnail_for_final_render() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    episode.assets.append(final_render)
    _append_approved_preview_render(episode, media["timeline"])
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(final_render.id),
            check_type="render_final_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )

    readiness = service.completion_readiness(episode)

    assert readiness["thumbnail_asset_id"] is None
    assert readiness["thumbnail_qc_id"] is None
    assert readiness["thumbnail_qc_status"] is None
    assert "thumbnail_missing" in readiness["failed_checks"]


def test_completion_readiness_requires_manifest_thumbnail_evidence() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language=episode.source_language,
        source_entity_type="render_asset",
        source_entity_id=str(final_render.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language=episode.source_language,
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {"asset_id": str(package_asset.id)},
            }
        },
    )
    episode.assets.extend([final_render, package_asset, manifest_asset])
    _append_final_render_qc(episode, final_render)
    _embed_package_thumbnail_evidence(episode, package_asset)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )

    readiness = service.completion_readiness(episode)

    assert "production_manifest_invalid" in readiness["failed_checks"]
    assert readiness["production_manifest_valid"] is False
    assert readiness["production_manifest_invalid_reason"] == (
        "embedded delivery package manifest is missing"
    )


def test_completion_readiness_requires_package_subtitle_evidence() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language=episode.source_language,
        source_entity_type="render_asset",
        source_entity_id=str(final_render.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
        generation_metadata={
            "included_files": ["youtube-package.json", "video/render.mp4"],
            "youtube_package_manifest": {},
        },
    )
    episode.assets.extend([final_render, package_asset])
    _append_final_render_qc(episode, final_render)
    thumbnail = _append_thumbnail_for_render(episode, final_render)
    package_asset.generation_metadata["thumbnail_asset_id"] = str(thumbnail.id)
    package_asset.generation_metadata["included_files"].append("thumbnail/thumbnail.jpg")
    package_asset.generation_metadata["youtube_package_manifest"]["thumbnail_asset_id"] = str(
        thumbnail.id
    )
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )

    readiness = service.completion_readiness(episode)

    assert readiness["export_package_subtitles_included"] is False
    assert "export_package_subtitles_missing" in readiness["failed_checks"]


def test_completion_readiness_requires_manifest_subtitle_evidence() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language=episode.source_language,
        source_entity_type="render_asset",
        source_entity_id=str(final_render.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language=episode.source_language,
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {
                    "asset_id": str(package_asset.id),
                    "included_files": [
                        "youtube-package.json",
                        "video/render.mp4",
                        "thumbnail/thumbnail.jpg",
                        "subtitles/en.vtt",
                    ],
                    "manifest": {"thumbnail_asset_id": "placeholder"},
                },
            }
        },
    )
    episode.assets.extend([final_render, package_asset, manifest_asset])
    _append_final_render_qc(episode, final_render)
    thumbnail = _append_thumbnail_for_render(episode, final_render)
    _embed_package_delivery_evidence(episode, package_asset)
    manifest_asset.generation_metadata["production_manifest"]["delivery_package"]["manifest"][
        "thumbnail_asset_id"
    ] = str(thumbnail.id)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )

    readiness = service.completion_readiness(episode)

    assert readiness["production_manifest_valid"] is False
    assert readiness["production_manifest_invalid_reason"] == (
        "embedded delivery package subtitle manifest is missing"
    )
    assert "production_manifest_invalid" in readiness["failed_checks"]


def _append_completed_publish_job_with_qc(episode, package_asset) -> PublishJob:
    _embed_package_delivery_evidence(episode, package_asset)
    publish_job = PublishJob(
        episode_id=episode.id,
        publisher_target_id="youtube-dry-run",
        platform="youtube",
        package_asset_id=package_asset.id,
        status="completed",
        dry_run=True,
        remote_job_id="dry-run-job",
        publish_url="mock://youtube/dry-run-job",
    )
    episode.publish_jobs.append(publish_job)
    publish_qc = _append_publish_delivery_qc(episode, publish_job)
    _embed_publish_evidence_in_manifest(episode, package_asset, publish_job, publish_qc)
    return publish_job


def _embed_package_delivery_evidence(episode, package_asset: Asset) -> None:
    thumbnail = next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.thumbnail
            and asset.status == "completed"
            and asset.source_entity_type == "render_asset"
            and asset.source_entity_id == str(package_asset.source_entity_id)
        ),
        None,
    )
    subtitle = next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.subtitle and asset.status == "completed"
        ),
        None,
    )
    included_files = list(package_asset.generation_metadata.get("included_files", []))
    package_manifest = dict(package_asset.generation_metadata.get("youtube_package_manifest", {}))
    if thumbnail is not None:
        included_files.append("thumbnail/thumbnail.jpg")
        package_manifest["thumbnail_asset_id"] = str(thumbnail.id)
    if subtitle is not None:
        subtitle_path = f"subtitles/{subtitle.language}.vtt"
        included_files.append(subtitle_path)
        package_manifest["subtitles"] = [
            {
                "asset_id": str(subtitle.id),
                "language": subtitle.language,
                "path": subtitle_path,
            }
        ]
    package_asset.generation_metadata = {
        **package_asset.generation_metadata,
        "included_files": list(dict.fromkeys(included_files)),
        "youtube_package_manifest": package_manifest,
    }
    if thumbnail is not None:
        package_asset.generation_metadata["thumbnail_asset_id"] = str(thumbnail.id)


def _embed_package_thumbnail_evidence(episode, package_asset: Asset) -> None:
    _embed_package_delivery_evidence(episode, package_asset)


def _append_publish_delivery_qc(episode, publish_job: PublishJob) -> QualityResult:
    qc = QualityResult(
        episode_id=episode.id,
        target_type="publish_job",
        target_id=str(publish_job.id),
        check_type="publish_delivery_integrity",
        severity=QualitySeverity.warning,
        status="warning",
        score=1.0,
        details={"dry_run": True, "warning_count": 1, "failure_count": 0},
    )
    episode.quality_results.append(qc)
    return qc


def localized_definition() -> EpisodeDefinition:
    payload = definition().model_dump(mode="json")
    payload["languages"] = {
        "source_language": "en",
        "outputs": [
            {"language": "en", "mode": "canonical"},
            {"language": "de", "mode": "localized_reperformance"},
        ],
        "semantic_fidelity_threshold": 0.92,
        "allow_new_claims": False,
    }
    return EpisodeDefinition.model_validate(payload)


def _append_approved_canonical_transcript(episode) -> TranscriptVersion:
    turn = TranscriptTurn(
        source_discussion_turn_ids=[],
        speaker_participant_id="host",
        text="Welcome to the completed production handoff.",
        status="approved",
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language=episode.source_language,
        status="approved",
        turns=[turn],
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    return transcript


def _append_completed_transcript_media(
    episode,
    transcript: TranscriptVersion,
    *,
    include_quality_results: bool = True,
) -> dict[str, Asset]:
    turn = transcript.turns[0]
    audio_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language=transcript.language,
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/audio/turn.wav",
        mime_type="audio/wav",
        duration_ms=1000,
        checksum="sha256:audio",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "voice_profile_id": "voice-host",
        },
    )
    visual_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language=transcript.language,
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/video/turn.mp4",
        mime_type="video/mp4",
        duration_ms=1000,
        width=1920,
        height=1080,
        fps=30,
        checksum="sha256:video",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "video_primary",
            "visual_profile_id": "visual-host",
        },
    )
    subtitle_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language=transcript.language,
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        storage_uri="object://dialecticore/subtitles/en.vtt",
        mime_type="text/vtt",
        checksum="sha256:subtitle",
        status="completed",
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language=transcript.language,
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        storage_uri="object://dialecticore/timelines/timeline.json",
        mime_type="application/vnd.dialecticore.timeline+json",
        checksum="sha256:timeline",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "timeline_json": {
                "schema_version": "episode_timeline.v1",
                "transcript_version_id": str(transcript.id),
                "segments": [
                    {
                        "source_turn_id": str(turn.id),
                        "audio_asset_id": str(audio_asset.id),
                        "video_asset_id": str(visual_asset.id),
                        "subtitle_asset_id": str(subtitle_asset.id),
                    }
                ],
            },
        },
    )
    episode.assets.extend([audio_asset, visual_asset, subtitle_asset, timeline_asset])
    if include_quality_results:
        _append_transcript_media_qc(
            episode,
            transcript,
            subtitle_asset=subtitle_asset,
            timeline_asset=timeline_asset,
        )
    return {
        "audio": audio_asset,
        "visual": visual_asset,
        "subtitle": subtitle_asset,
        "timeline": timeline_asset,
    }


def _append_transcript_media_qc(
    episode,
    transcript: TranscriptVersion,
    *,
    subtitle_asset: Asset,
    timeline_asset: Asset,
) -> dict[str, QualityResult]:
    audio_qc = QualityResult(
        episode_id=episode.id,
        target_type="transcript_version",
        target_id=str(transcript.id),
        check_type="audio_media_integrity",
        severity=QualitySeverity.pass_,
        status="pass",
        score=1.0,
        details={
            "language": transcript.language,
            "checked_audio_asset_count": 1,
            "completed_audio_asset_count": 1,
            "failure_count": 0,
            "warning_count": 0,
        },
    )
    visual_qc = QualityResult(
        episode_id=episode.id,
        target_type="transcript_version",
        target_id=str(transcript.id),
        check_type="visual_media_integrity",
        severity=QualitySeverity.pass_,
        status="pass",
        score=1.0,
        details={
            "language": transcript.language,
            "checked_visual_asset_count": 1,
            "completed_visual_asset_count": 1,
            "failure_count": 0,
            "warning_count": 0,
        },
    )
    subtitle_qc = QualityResult(
        episode_id=episode.id,
        target_type="asset",
        target_id=str(subtitle_asset.id),
        check_type="subtitle_generation_completeness",
        severity=QualitySeverity.pass_,
        status="pass",
        score=1.0,
        details={
            "language": transcript.language,
            "transcript_version_id": str(transcript.id),
            "required_turn_count": 1,
            "covered_turn_count": 1,
            "cue_count": 1,
            "missing_audio_count": 0,
            "timing_overlap_count": 0,
            "max_sync_error_ms": 0,
        },
    )
    timeline_qc = QualityResult(
        episode_id=episode.id,
        target_type="timeline_asset",
        target_id=str(timeline_asset.id),
        check_type="timeline_integrity",
        severity=QualitySeverity.pass_,
        status="pass",
        score=1.0,
        details={
            "language": transcript.language,
            "transcript_version_id": str(transcript.id),
            "timeline_asset_id": str(timeline_asset.id),
            "segment_count": 1,
            "playable_turn_count": 1,
            "missing_audio_segment_count": 0,
            "missing_primary_video_segment_count": 0,
            "subtitle_linked_segment_count": 1,
            "failure_count": 0,
            "warning_count": 0,
        },
    )
    episode.quality_results.extend([audio_qc, visual_qc, subtitle_qc, timeline_qc])
    return {
        "audio_qc": audio_qc,
        "visual_qc": visual_qc,
        "subtitle_qc": subtitle_qc,
        "timeline_qc": timeline_qc,
    }


def _embed_publish_evidence_in_manifest(
    episode,
    package_asset,
    publish_job: PublishJob,
    publish_qc: QualityResult,
) -> None:
    _embed_package_thumbnail_evidence(episode, package_asset)
    for asset in episode.assets:
        if (
            asset.asset_type == AssetType.production_manifest
            and asset.status == "completed"
            and asset.source_entity_type == "export_package"
            and asset.source_entity_id == str(package_asset.id)
        ):
            manifest = asset.generation_metadata.setdefault("production_manifest", {})
            manifest["delivery_package"] = _manifest_delivery_package_entry(package_asset)
            manifest["publish_jobs"] = [
                {
                    "id": str(publish_job.id),
                    "package_asset_id": str(package_asset.id),
                    "status": publish_job.status,
                }
            ]
            manifest["quality_results"] = [
                {
                    "id": str(publish_qc.id),
                    "target_type": "publish_job",
                    "target_id": str(publish_job.id),
                    "check_type": "publish_delivery_integrity",
                    "status": publish_qc.status,
                }
            ]


def _manifest_delivery_package_entry(package_asset: Asset) -> dict:
    entry = {"asset_id": str(package_asset.id)}
    package_manifest = package_asset.generation_metadata.get("youtube_package_manifest")
    if isinstance(package_manifest, dict):
        entry["manifest"] = dict(package_manifest)
    included_files = package_asset.generation_metadata.get("included_files")
    if isinstance(included_files, list):
        entry["included_files"] = list(included_files)
    return entry


def _append_approved_preview_render(episode, timeline_asset: Asset) -> Asset:
    existing = next(
        (
            asset
            for asset in episode.assets
            if asset.asset_type == AssetType.render
            and asset.status == "completed"
            and asset.source_entity_type == "timeline_asset"
            and asset.source_entity_id == str(timeline_asset.id)
            and asset.generation_metadata.get("render_type") == "preview"
        ),
        None,
    )
    if existing is not None:
        return existing
    preview_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=timeline_asset.language,
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/preview.mp4",
        mime_type="video/mp4",
        checksum="sha256:preview",
        status="completed",
        generation_metadata={
            "render_type": "preview",
            "timeline_asset_id": str(timeline_asset.id),
            "approval_status": "approved",
        },
    )
    episode.assets.append(preview_asset)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="render_asset",
            target_id=str(preview_asset.id),
            check_type="render_preview_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="preview_render_review",
            target_type="render_asset",
            target_id=str(preview_asset.id),
            decision="approved",
            user_id="producer-1",
        )
    )
    return preview_asset


def _append_final_render_qc(episode, render_asset: Asset) -> QualityResult:
    if render_asset.source_entity_type == "timeline_asset":
        timeline_asset = next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.timeline
                and str(asset.id) == str(render_asset.source_entity_id)
            ),
            None,
        )
        if timeline_asset is not None:
            _append_approved_preview_render(episode, timeline_asset)
    _append_thumbnail_for_render(episode, render_asset)
    qc = QualityResult(
        episode_id=episode.id,
        target_type="render_asset",
        target_id=str(render_asset.id),
        check_type="render_final_integrity",
        severity=QualitySeverity.pass_,
        status="pass",
        score=1.0,
    )
    episode.quality_results.append(qc)
    return qc


def _append_thumbnail_for_render(episode, render_asset: Asset) -> Asset:
    existing = next(
        (
            asset
            for asset in episode.assets
            if asset.asset_type == AssetType.thumbnail
            and asset.status == "completed"
            and asset.source_entity_type == "render_asset"
            and asset.source_entity_id == str(render_asset.id)
        ),
        None,
    )
    if existing is not None:
        return existing
    thumbnail = Asset(
        episode_id=episode.id,
        asset_type=AssetType.thumbnail,
        language=render_asset.language,
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/thumbnails/final.jpg",
        mime_type="image/jpeg",
        width=1280,
        height=720,
        checksum="sha256:thumbnail",
        status="completed",
        generation_metadata={"render_asset_id": str(render_asset.id)},
    )
    episode.assets.append(thumbnail)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="thumbnail_asset",
            target_id=str(thumbnail.id),
            check_type="thumbnail_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )
    return thumbnail


def test_production_control_records_completion_only_when_readiness_passes() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {"asset_id": str(package_asset.id)},
            }
        },
    )
    episode.assets.extend([render_asset, package_asset, manifest_asset])
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="producer-1",
        )
    )
    _append_final_render_qc(episode, render_asset)
    package_qc = QualityResult(
        episode_id=episode.id,
        target_type="export_package_asset",
        target_id=str(package_asset.id),
        check_type="youtube_package_integrity",
        severity=QualitySeverity.pass_,
        status="pass",
        score=1.0,
    )
    episode.quality_results.append(package_qc)
    publish_job = PublishJob(
        episode_id=episode.id,
        publisher_target_id="youtube-dry-run",
        platform="youtube",
        package_asset_id=package_asset.id,
        status="completed",
        dry_run=True,
        remote_job_id="dry-run-job",
        publish_url="mock://youtube/dry-run-job",
    )
    episode.publish_jobs.append(publish_job)

    service.begin_run(episode, user_id="producer-1")
    with pytest.raises(ValueError, match="publish_delivery_qc_missing"):
        service.record_stage(episode, EpisodeStatus.completed, "test")

    publish_qc = _append_publish_delivery_qc(episode, publish_job)
    with pytest.raises(ValueError, match="production_manifest_publish_evidence_missing"):
        service.record_stage(episode, EpisodeStatus.completed, "test")

    _embed_publish_evidence_in_manifest(episode, package_asset, publish_job, publish_qc)
    service.record_stage(episode, EpisodeStatus.completed, "test")

    run = episode.workflow_control["run"]
    assert run["state"] == "completed"
    assert run["completion_reason"] == "completed"
    assert run["completion_gate"]["status"] == "pass"
    assert run["completion_gate"]["final_render_asset_id"] == str(render_asset.id)
    assert run["completion_gate"]["export_package_asset_id"] == str(package_asset.id)
    assert run["completion_gate"]["export_package_qc_id"] == str(package_qc.id)
    assert run["completion_gate"]["export_package_qc_status"] == "pass"
    assert run["completion_gate"]["audio_qc_status"] == "pass"
    assert run["completion_gate"]["visual_qc_status"] == "pass"
    assert run["completion_gate"]["production_manifest_asset_id"] == str(manifest_asset.id)
    assert run["completion_gate"]["production_manifest_valid"] is True
    assert run["completion_gate"]["publish_job_id"] == str(publish_job.id)
    assert run["completion_gate"]["publish_job_status"] == "completed"
    assert run["completion_gate"]["publish_job_dry_run"] is True
    publish_qc = next(
        result
        for result in episode.quality_results
        if result.check_type == "publish_delivery_integrity"
        and result.target_id == str(publish_job.id)
    )
    assert run["completion_gate"]["publish_delivery_qc_id"] == str(publish_qc.id)
    assert run["completion_gate"]["publish_delivery_qc_status"] == "warning"


def test_production_control_blocks_completion_without_package_qc_or_valid_manifest() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={"production_manifest": {"schema_version": "draft"}},
    )
    episode.assets.extend([render_asset, package_asset, manifest_asset])
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="producer-1",
        )
    )
    _append_final_render_qc(episode, render_asset)

    readiness = service.completion_readiness(episode)

    assert readiness["status"] == "fail"
    assert "export_package_qc_missing" in readiness["failed_checks"]
    assert "production_manifest_invalid" in readiness["failed_checks"]
    assert "publish_job_missing" in readiness["failed_checks"]
    assert readiness["export_package_qc_id"] is None
    assert readiness["export_package_qc_status"] is None
    assert readiness["publish_job_id"] is None
    assert readiness["publish_job_status"] is None
    assert readiness["production_manifest_asset_id"] == str(manifest_asset.id)
    assert readiness["production_manifest_valid"] is False
    assert readiness["production_manifest_invalid_reason"] == (
        "embedded production_manifest schema_version is invalid"
    )

    service.begin_run(episode, user_id="producer-1")
    with pytest.raises(ValueError, match="export_package_qc_missing"):
        service.record_stage(episode, EpisodeStatus.completed, "test")


def test_production_control_blocks_completion_with_unlinked_production_manifest() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {},
            }
        },
    )
    episode.assets.extend([render_asset, package_asset, manifest_asset])
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="producer-1",
        )
    )
    _append_approved_preview_render(episode, media["timeline"])
    _append_thumbnail_for_render(episode, render_asset)
    _embed_package_thumbnail_evidence(episode, package_asset)
    episode.quality_results.extend(
        [
            QualityResult(
                episode_id=episode.id,
                target_type="render_asset",
                target_id=str(render_asset.id),
                check_type="render_final_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                score=1.0,
            ),
            QualityResult(
                episode_id=episode.id,
                target_type="export_package_asset",
                target_id=str(package_asset.id),
                check_type="youtube_package_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                score=1.0,
            ),
        ]
    )
    publish_job = PublishJob(
        episode_id=episode.id,
        publisher_target_id="youtube-dry-run",
        platform="youtube",
        package_asset_id=package_asset.id,
        status="completed",
        dry_run=True,
        remote_job_id="dry-run-job",
        publish_url="mock://youtube/dry-run-job",
    )
    episode.publish_jobs.append(publish_job)
    publish_qc = _append_publish_delivery_qc(episode, publish_job)
    manifest = manifest_asset.generation_metadata["production_manifest"]
    manifest["publish_jobs"] = [
        {
            "id": str(publish_job.id),
            "package_asset_id": str(package_asset.id),
            "status": publish_job.status,
        }
    ]
    manifest["quality_results"] = [
        {
            "id": str(publish_qc.id),
            "target_type": "publish_job",
            "target_id": str(publish_job.id),
            "check_type": "publish_delivery_integrity",
            "status": publish_qc.status,
        }
    ]

    readiness = service.completion_readiness(episode)

    assert readiness["status"] == "fail"
    assert readiness["failed_checks"] == ["production_manifest_invalid"]
    assert readiness["production_manifest_asset_id"] == str(manifest_asset.id)
    assert readiness["production_manifest_valid"] is False
    assert readiness["production_manifest_invalid_reason"] == (
        "embedded delivery package asset_id is missing"
    )


def test_completion_readiness_requires_manifest_talkshow_visual_handoff() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
        generation_metadata={"render_asset_id": str(render_asset.id)},
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {"asset_id": str(package_asset.id)},
                "timeline_segments": [
                    {
                        "id": "segment-1",
                        "source_turn_id": str(transcript.turns[0].id),
                        "reaction_visual_asset_id": "reaction-asset",
                    }
                ],
            }
        },
    )
    episode.assets.extend([render_asset, package_asset, manifest_asset])

    readiness = service.completion_readiness(episode)

    assert "production_manifest_invalid" in readiness["failed_checks"]
    assert readiness["production_manifest_valid"] is False
    assert readiness["production_manifest_invalid_reason"] == (
        "embedded talkshow visual handoff is missing"
    )


def test_live_publish_requires_manifest_talkshow_visual_handoff() -> None:
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id="render-final",
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {"asset_id": str(package_asset.id)},
                "timeline_segments": [
                    {
                        "id": "segment-1",
                        "reaction_visual_asset_id": "reaction-asset",
                    }
                ],
            }
        },
    )
    episode.assets.extend([package_asset, manifest_asset])
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )
    live_target = PublisherTarget(
        id="generic-live",
        name="Generic Live",
        platform="generic",
        adapter_type="generic_http",
        base_url="https://publisher.example.test",
        enabled=True,
        capabilities={"automated_live_publish": True},
    )

    with pytest.raises(
        ValueError,
        match="valid production_manifest.v1 asset is required before live publishing",
    ):
        PublisherService().publish_package(
            episode,
            PublishRequest(
                publisher_target_id="generic-live",
                package_asset_id=package_asset.id,
                dry_run=False,
                user_id="tester",
            ),
            [live_target],
        )


def test_completion_readiness_uses_episode_quality_blocking_policy() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    episode.definition.quality.block_on_missing_audio = False
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {"asset_id": str(package_asset.id)},
            }
        },
    )
    failed_audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id="turn-a",
        status="failed",
    )
    episode.assets.extend([render_asset, package_asset, manifest_asset, failed_audio])
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="producer-1",
        )
    )
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id="transcript-a",
            check_type="audio_generation_completeness",
            severity=QualitySeverity.fail,
            status="fail",
            details={"missing_transcript_turn_ids": ["turn-a"]},
        )
    )
    _append_final_render_qc(episode, render_asset)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )
    _append_completed_publish_job_with_qc(episode, package_asset)

    readiness = service.completion_readiness(episode)

    assert readiness["status"] == "pass"
    assert readiness["quality_blocking_policy"]["block_on_missing_audio"] is False
    assert readiness["unresolved_failed_assets"] == []
    assert readiness["failing_quality_results"] == []
    assert readiness["nonblocking_unresolved_failed_assets"][0]["asset_type"] == "audio"
    assert readiness["nonblocking_failing_quality_results"][0]["check_type"] == (
        "audio_generation_completeness"
    )


def test_completion_readiness_records_manual_replacement_resolution(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = ProductionControlService()
    replacement_service = AssetReplacementService(settings)
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {"asset_id": str(package_asset.id)},
            }
        },
    )
    failed_visual = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id="turn-video-1",
        storage_uri="object://dialecticore/video/bad.mp4",
        mime_type="video/mp4",
        checksum="sha256:bad",
        status="failed",
    )
    episode.assets.extend([render_asset, package_asset, manifest_asset, failed_visual])
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="producer-1",
        )
    )
    _append_final_render_qc(episode, render_asset)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )
    _append_completed_publish_job_with_qc(episode, package_asset)

    blocked = service.completion_readiness(episode)
    assert blocked["status"] == "fail"
    assert "unresolved_failed_assets_present" in blocked["failed_checks"]
    assert blocked["unresolved_failed_assets"][0]["asset_id"] == str(failed_visual.id)
    assert blocked["resolved_failed_assets"] == []

    replaced = replacement_service.replace_asset(
        episode,
        failed_visual.id,
        AssetReplacementRequest(
            storage_uri="object://dialecticore/video/fixed.mp4",
            mime_type="video/mp4",
            checksum="sha256:fixed",
            user_id="editor-1",
            comment="Operator supplied corrected talking-head clip.",
        ),
    )
    readiness = service.completion_readiness(replaced)

    assert readiness["status"] == "pass"
    assert readiness["unresolved_failed_assets"] == []
    resolved = readiness["resolved_failed_assets"][0]
    replacement = next(
        asset
        for asset in replaced.assets
        if asset.generation_metadata.get("replacement_of_asset_id") == str(failed_visual.id)
    )
    assert resolved["asset_id"] == str(failed_visual.id)
    assert resolved["replacement_asset_id"] == str(replacement.id)
    assert resolved["replacement_status"] == "completed"
    assert resolved["replacement_checksum"] == "sha256:fixed"
    assert resolved["replacement_ready"] is True
    assert resolved["replacement_reason"] == "Operator supplied corrected talking-head clip."


def test_completion_readiness_blocks_subtitle_sync_over_threshold() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    episode.definition.quality.block_on_missing_subtitles = False
    episode.definition.quality.block_on_sync_error_ms = 180
    transcript = _append_approved_canonical_transcript(episode)
    media = _append_completed_transcript_media(episode, transcript)
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(media["timeline"].id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(media["timeline"].id),
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.production_manifest,
        language="en",
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        storage_uri="object://dialecticore/manifests/production.json",
        mime_type="application/vnd.dialecticore.production-manifest+json",
        checksum="sha256:manifest",
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {"asset_id": str(package_asset.id)},
            }
        },
    )
    episode.assets.extend([render_asset, package_asset, manifest_asset])
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="producer-1",
        )
    )
    _append_final_render_qc(episode, render_asset)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="asset",
            target_id="subtitle-a",
            check_type="subtitle_generation_completeness",
            severity=QualitySeverity.fail,
            status="fail",
            details={"max_sync_error_ms": 250},
        )
    )
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
        )
    )
    _append_completed_publish_job_with_qc(episode, package_asset)

    readiness = service.completion_readiness(episode)

    assert readiness["status"] == "fail"
    assert readiness["failed_checks"] == ["failing_quality_results_present"]
    assert readiness["failing_quality_results"][0]["check_type"] == (
        "subtitle_generation_completeness"
    )
    assert readiness["failing_quality_results"][0]["blocks_completion"] is True


def test_workflow_completion_readiness_endpoint_reports_failed_gates() -> None:
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    app.dependency_overrides[get_repository] = lambda: repository

    try:
        client = TestClient(app)
        response = client.get(f"/api/v1/episodes/{episode.id}/workflow/completion-readiness")
        assert response.status_code == 200
        body = response.json()
        assert body["schema_version"] == "production_completion_readiness.v1"
        assert body["status"] == "fail"
        assert "completed_final_render_missing" in body["failed_checks"]
        assert body["final_render_asset_id"] is None
        assert body["final_render_approved"] is False
    finally:
        app.dependency_overrides.clear()


def test_system_health_surfaces_completion_blocked_production_runs() -> None:
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    episode.workflow_control = {
        "run": {
            "run_id": "run-completion-blocked",
            "run_sequence": 1,
            "state": "running",
            "current_stage": "COMPLETED",
            "updated_at": datetime.now(UTC).isoformat(),
            "completion_gate": {
                "schema_version": "production_completion_readiness.v1",
                "status": "fail",
                "failed_checks": [
                    "completed_export_package_missing",
                    "failing_quality_results_present",
                ],
            },
        }
    }
    repository.save(episode)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()

    try:
        client = TestClient(app)
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        body = health.json()
        components = {component["name"]: component for component in body["components"]}
        production_runs = components["production_runs"]
        assert production_runs["status"] == "degraded"
        assert production_runs["details"]["completion_blocked_production_runs"] == 1
        assert production_runs["details"]["attention_count"] == 1
        assert production_runs["details"]["by_attention_reason"] == {"completion_blocked": 1}
        assert production_runs["details"]["by_completion_failed_check"] == {
            "completed_export_package_missing": 1,
            "failing_quality_results_present": 1,
        }
        assert production_runs["details"]["attention_runs"][0]["completion_failed_checks"] == [
            "completed_export_package_missing",
            "failing_quality_results_present",
        ]
        assert production_runs["details"]["failed_readiness_checks"] == [
            "no_running_active_production_runs",
            "no_completion_blocked_production_runs",
        ]
        assert body["counts"]["completion_blocked_production_runs"] == 1

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        run_readiness = checks["production_runs"]
        assert run_readiness["status"] == "fail"
        assert run_readiness["details"]["completion_blocked_production_runs"] == 1
        assert run_readiness["details"]["by_completion_failed_check"] == {
            "completed_export_package_missing": 1,
            "failing_quality_results_present": 1,
        }
        assert (
            "one or more production runs are blocked by completion gates"
            in (run_readiness["blockers"])
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert 'dialecticore_production_run_count{kind="completion_blocked"} 1' in metrics.text
        assert (
            'dialecticore_production_run_count{kind="completion_failed_check",'
            'check="completed_export_package_missing"} 1'
        ) in metrics.text
        assert (
            'dialecticore_production_run_count{kind="completion_failed_check",'
            'check="failing_quality_results_present"} 1'
        ) in metrics.text
    finally:
        app.dependency_overrides.clear()


def test_system_health_surfaces_running_completion_handoff_as_pending_work() -> None:
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    now = datetime.now(UTC).isoformat()
    episode.status = EpisodeStatus.exporting
    episode.workflow_control = {
        "run": {
            "run_id": "run-completion-handoff-blocked",
            "run_sequence": 1,
            "state": "running",
            "current_stage": "EXPORTING",
            "updated_at": now,
            "last_worker_orchestration": {
                "summary_id": "summary-completion-blocked",
                "attempt_sequence": 3,
                "recorded_at": now,
                "worker_id": "workflow-worker",
                "completion_handoff": {
                    "schema_version": "workflow_completion_handoff.v1",
                    "episode_id": str(episode.id),
                    "status": "blocked",
                    "failed_checks": ["publish_job_missing"],
                },
            },
        },
        "worker_orchestration_log": [
            {
                "schema_version": "workflow_worker_orchestration_attempt.v1",
                "summary_id": "summary-completion-blocked",
                "attempt_sequence": 3,
                "recorded_at": now,
                "worker_id": "workflow-worker",
                "completion_handoff": {
                    "schema_version": "workflow_completion_handoff.v1",
                    "episode_id": str(episode.id),
                    "status": "blocked",
                    "failed_checks": ["publish_job_missing"],
                },
            }
        ],
    }
    repository.save(episode)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()

    try:
        client = TestClient(app)
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        body = health.json()
        components = {component["name"]: component for component in body["components"]}
        production_runs = components["production_runs"]
        assert production_runs["status"] == "degraded"
        assert production_runs["details"]["completion_blocked_production_runs"] == 0
        assert production_runs["details"]["waiting_for_completion_action_production_runs"] == 1
        assert production_runs["details"]["by_attention_reason"] == {
            "waiting_for_completion_action": 1
        }
        assert production_runs["details"]["by_completion_failed_check"] == {
            "publish_job_missing": 1
        }
        attention = production_runs["details"]["attention_runs"][0]
        assert attention["completion_handoff_status"] == "blocked"
        assert attention["completion_failed_checks"] == ["publish_job_missing"]
        assert body["counts"]["completion_blocked_production_runs"] == 0

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        run_readiness = checks["production_runs"]
        assert run_readiness["status"] == "warning"
        assert run_readiness["details"]["completion_blocked_production_runs"] == 0
        assert run_readiness["details"]["waiting_for_completion_action_production_runs"] == 1
        assert run_readiness["details"]["by_completion_failed_check"] == {"publish_job_missing": 1}
        assert (
            "one or more production runs are waiting for the next stage or review"
            in run_readiness["warnings"]
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert 'dialecticore_production_run_count{kind="completion_blocked"} 0' in metrics.text
        assert (
            'dialecticore_production_run_count{kind="completion_failed_check",'
            'check="publish_job_missing"} 1'
        ) in metrics.text
    finally:
        app.dependency_overrides.clear()


def test_production_control_temporal_signal_transport_records_sent_and_failed() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService(
        Settings(
            temporal_signal_transport_enabled=True,
            temporal_signal_endpoint="https://temporal.example.test/signals",
            temporal_namespace="dialecticore",
            temporal_task_queue="production",
        )
    )
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    seen_payloads: list[dict] = []

    def fake_post(payload: dict) -> httpx.Response:
        seen_payloads.append(payload)
        if payload["signal"]["signal_type"] == "resume":
            raise httpx.ConnectError("temporal unavailable")
        return httpx.Response(202)

    service._post_temporal_signal = fake_post  # type: ignore[method-assign]

    service.begin_run(episode, user_id="producer-1")
    service.pause(episode, WorkflowActionRequest(action="pause", user_id="producer-1"))
    service.resume(episode, WorkflowActionRequest(action="resume", user_id="producer-1"))

    signal_log = episode.workflow_control["temporal_signal_log"]
    assert [entry["signal_type"] for entry in signal_log] == ["start", "pause", "resume"]
    assert [entry["status"] for entry in signal_log] == ["sent", "sent", "failed"]
    assert signal_log[0]["namespace"] == "dialecticore"
    assert signal_log[0]["task_queue"] == "production"
    assert signal_log[2]["error"] == "temporal unavailable"
    assert seen_payloads[0]["schema_version"] == "temporal_signal_request.v1"
    assert seen_payloads[0]["policy"] == service.external_temporal_policy
    assert seen_payloads[1]["signal"]["signal_type"] == "pause"


def test_project_api_manages_projects_and_links_episode() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    try:
        created_project = client.post(
            "/api/v1/projects",
            json={
                "name": "AI Policy Slate",
                "description": "Episodes about practical AI policy.",
                "default_language": "en",
                "default_show_format_id": "analytical_panel_v1",
            },
        )
        assert created_project.status_code == 200
        project_id = created_project.json()["id"]

        listed = client.get("/api/v1/projects")
        assert listed.status_code == 200
        assert listed.json()[0]["name"] == "AI Policy Slate"

        updated = client.put(
            f"/api/v1/projects/{project_id}",
            json={
                "name": "AI Governance Slate",
                "description": "Updated slate.",
                "default_language": "de",
                "default_show_format_id": "debate_roundtable_v1",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["default_language"] == "de"

        created_episode = client.post(
            "/api/v1/episodes",
            json={"project_id": project_id, "definition": definition().model_dump(mode="json")},
        )
        assert created_episode.status_code == 200
        assert created_episode.json()["project_id"] == project_id

        blocked_delete = client.delete(f"/api/v1/projects/{project_id}")
        assert blocked_delete.status_code == 422
        assert "still used by episodes" in blocked_delete.json()["detail"]

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["counts"]["projects"] == 1

        event_types = {
            event["event_type"] for event in client.get("/api/v1/audit-events?limit=20").json()
        }
        assert {"project.upserted", "episode.created"} <= event_types
    finally:
        app.dependency_overrides.clear()


def test_language_profile_api_manages_language_catalog() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    try:
        listed_defaults = client.get("/api/v1/language-profiles")
        assert listed_defaults.status_code == 200
        assert {profile["id"] for profile in listed_defaults.json()} >= {"en", "de"}

        created = client.post(
            "/api/v1/language-profiles",
            json={
                "id": "fr",
                "name": "French",
                "bcp47_tag": "fr",
                "native_name": "Francais",
                "default_mode": "localized_reperformance",
                "subtitle_direction": "ltr",
                "line_breaking": {"max_chars_per_line": 38},
                "voice_defaults": {"speaking_rate": 0.96},
            },
        )
        assert created.status_code == 200
        assert created.json()["bcp47_tag"] == "fr"

        updated = client.put(
            "/api/v1/language-profiles/fr",
            json={
                "id": "ignored-by-route",
                "name": "French Updated",
                "bcp47_tag": "fr",
                "native_name": "Francais",
                "default_mode": "translation",
                "subtitle_direction": "ltr",
                "enabled": False,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["id"] == "fr"
        assert updated.json()["default_mode"] == "translation"
        assert updated.json()["enabled"] is False

        project = client.post(
            "/api/v1/projects",
            json={
                "name": "German Slate",
                "description": "",
                "default_language": "de",
                "default_show_format_id": "analytical_panel_v1",
            },
        )
        assert project.status_code == 200

        blocked_delete = client.delete("/api/v1/language-profiles/de")
        assert blocked_delete.status_code == 422

        deleted = client.delete("/api/v1/language-profiles/fr")
        assert deleted.status_code == 204
        assert client.get("/api/v1/language-profiles/fr").status_code == 404

        event_types = {
            event["event_type"] for event in client.get("/api/v1/audit-events?limit=20").json()
        }
        assert {"language_profile.upserted", "language_profile.deleted"} <= event_types
    finally:
        app.dependency_overrides.clear()


def test_episode_workflow_start_creates_durable_run_without_discussion() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]

        started = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/start",
            json={
                "user_id": "tester",
                "comment": "Start the durable worker-managed production run.",
            },
        )

        assert started.status_code == 200
        body = started.json()
        assert body["discussion_session"] is None
        assert body["workflow_control"]["run"]["state"] == "running"
        assert body["workflow_control"]["run"]["started_by"] == "tester"
        assert body["workflow_control"]["run"]["current_stage"] == "DRAFT"
        assert body["workflow_control"]["temporal_signal_log"][-1]["signal_type"] == "start"
        event_types = [event["event_type"] for event in body["audit_events"]]
        assert "workflow.run.started" in event_types
        assert "workflow.run.start_note" in event_types

        replay = client.get(f"/api/v1/episodes/{episode_id}/workflow/replay")
        assert replay.status_code == 200
        replay_body = replay.json()
        assert replay_body["replayed"]["state"] == "running"
        assert replay_body["replayed"]["current_stage"] == "DRAFT"

        blocked = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/start",
            json={"user_id": "tester"},
        )
        assert blocked.status_code == 422
        assert "already active" in blocked.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_episode_workflow_actions_pause_resume_cancel_and_retry() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        Settings(),
    )
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]

        premature_complete = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={"action": "complete", "user_id": "tester"},
        )
        assert premature_complete.status_code == 422
        assert (
            "production cannot be marked completed until gates pass"
            in (premature_complete.json()["detail"])
        )

        paused = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={
                "action": "pause",
                "user_id": "tester",
                "comment": "Hold for editorial review.",
            },
        )
        assert paused.status_code == 200
        paused_body = paused.json()
        assert paused_body["status"] == "DRAFT"
        assert paused_body["workflow_control"]["paused"] is True
        assert paused_body["workflow_control"]["paused_stage"] == "DRAFT"
        assert paused_body["audit_events"][-1]["event_type"] == "workflow.paused"

        status = client.get(f"/api/v1/episodes/{episode_id}/status")
        assert status.status_code == 200
        assert status.json()["workflow_paused"] is True
        assert status.json()["workflow_cancelled"] is False

        blocked = client.post(f"/api/v1/episodes/{episode_id}/produce")
        assert blocked.status_code == 422
        assert "paused" in blocked.json()["detail"]

        resumed = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={"action": "resume", "user_id": "tester"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["workflow_control"]["paused"] is False
        assert resumed.json()["workflow_control"]["resume_count"] == 1
        assert resumed.json()["audit_events"][-1]["event_type"] == "workflow.resumed"

        cancelled = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={
                "action": "cancel",
                "user_id": "tester",
                "comment": "Producer cancelled this run.",
            },
        )
        assert cancelled.status_code == 200
        cancelled_body = cancelled.json()
        assert cancelled_body["status"] == "CANCELLED"
        assert cancelled_body["workflow_control"]["cancelled"] is True
        assert cancelled_body["workflow_control"]["cancelled_from_stage"] == "DRAFT"
        assert cancelled_body["audit_events"][-2]["event_type"] == "workflow.cancelled"
        assert cancelled_body["audit_events"][-1]["event_type"] == "workflow.stage.changed"

        blocked_cancelled = client.post(f"/api/v1/episodes/{episode_id}/produce")
        assert blocked_cancelled.status_code == 422
        assert "cancelled" in blocked_cancelled.json()["detail"]

        failed_episode = repository.get(episode_id)
        failed_episode.status = EpisodeStatus.failed
        failed_episode.workflow_control = {
            **failed_episode.workflow_control,
            "failed_stage": "GENERATING_AUDIO",
        }
        repository.save(failed_episode)
        retried = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={
                "action": "retry_failed_stage",
                "user_id": "tester",
                "comment": "Retry audio stage after provider recovery.",
            },
        )
        assert retried.status_code == 200
        retried_body = retried.json()
        assert retried_body["status"] == "GENERATING_AUDIO"
        assert retried_body["workflow_control"]["retry_count"] == 1
        assert retried_body["workflow_control"]["retry_target_stage"] == "GENERATING_AUDIO"
        assert retried_body["audit_events"][-2]["event_type"] == "workflow.retry_requested"

        retry_status = client.get(f"/api/v1/episodes/{episode_id}/status")
        assert retry_status.status_code == 200
        assert retry_status.json()["workflow_paused"] is False
        assert retry_status.json()["workflow_cancelled"] is False
        assert retry_status.json()["workflow_control"]["retry_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_episode_workflow_stage_approval_and_rejection_actions() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        Settings(),
    )
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]
        produced = client.post(f"/api/v1/episodes/{episode_id}/produce")
        assert produced.status_code == 200
        assert produced.json()["status"] == "TRANSCRIPT_REVIEW"

        approved = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={
                "action": "approve_stage",
                "user_id": "reviewer-1",
                "comment": "Transcript review can proceed.",
            },
        )
        assert approved.status_code == 200
        approved_body = approved.json()
        assert approved_body["status"] == "TRANSCRIPT_REVIEW"
        assert approved_body["workflow_control"]["stage_approval_count"] == 1
        approval_decision = approved_body["workflow_control"]["stage_decisions"][-1]
        assert approval_decision["schema_version"] == "workflow_stage_decision.v1"
        assert approval_decision["decision"] == "approved"
        assert approval_decision["stage"] == "TRANSCRIPT_REVIEW"
        assert approved_body["workflow_control"]["run"]["signals"][-1]["signal_type"] == (
            "approve_stage"
        )
        assert (
            approved_body["workflow_control"]["temporal_signal_log"][-1]["signal_type"]
            == "approve_stage"
        )
        assert approved_body["audit_events"][-1]["event_type"] == ("workflow.stage.approved")

        rejected = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={
                "action": "reject_stage",
                "user_id": "reviewer-1",
                "comment": "Needs manual transcript correction.",
            },
        )
        assert rejected.status_code == 200
        rejected_body = rejected.json()
        assert rejected_body["status"] == "FAILED"
        assert rejected_body["workflow_control"]["stage_rejection_count"] == 1
        assert rejected_body["workflow_control"]["failed_stage"] == "TRANSCRIPT_REVIEW"
        rejection_decision = rejected_body["workflow_control"]["stage_decisions"][-1]
        assert rejection_decision["decision"] == "rejected"
        assert rejection_decision["stage"] == "TRANSCRIPT_REVIEW"
        run = rejected_body["workflow_control"]["run"]
        assert run["state"] == "failed"
        assert run["current_stage"] == "FAILED"
        assert run["completion_reason"] == "stage_rejected"
        assert run["failed_stage"] == "TRANSCRIPT_REVIEW"
        assert run["signals"][-1]["signal_type"] == "reject_stage"
        assert (
            rejected_body["workflow_control"]["temporal_signal_log"][-1]["signal_type"]
            == "reject_stage"
        )
        assert rejected_body["audit_events"][-2]["event_type"] == ("workflow.stage.rejected")
        assert rejected_body["audit_events"][-1]["event_type"] == "workflow.stage.changed"

        replay = client.get(f"/api/v1/episodes/{episode_id}/workflow/replay")
        assert replay.status_code == 200
        replay_body = replay.json()
        assert replay_body["status"] == "pass"
        assert replay_body["replayed"]["state"] == "failed"
        assert replay_body["replayed"]["completion_reason"] == "stage_rejected"
        assert replay_body["replayed"]["current_stage"] == "FAILED"

        retried = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={"action": "retry_failed_stage", "user_id": "producer-1"},
        )
        assert retried.status_code == 200
        retried_body = retried.json()
        assert retried_body["status"] == "TRANSCRIPT_REVIEW"
        assert retried_body["workflow_control"]["retry_target_stage"] == ("TRANSCRIPT_REVIEW")
        assert retried_body["workflow_control"]["run"]["state"] == "running"
        assert retried_body["workflow_control"]["run"]["completion_reason"] is None
        assert retried_body["workflow_control"]["run"]["signals"][-1]["signal_type"] == (
            "retry_failed_stage"
        )

        rejected_again = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={
                "action": "reject_stage",
                "user_id": "reviewer-1",
                "comment": "Manual timing edit required.",
            },
        )
        assert rejected_again.status_code == 200
        edited_episode = repository.get(episode_id)
        edited_episode.audit_events.append(
            AuditEvent(
                episode_id=edited_episode.id,
                event_type="timeline.asset.edited",
                actor="editor-1",
                details={
                    "transcript_version_id": "transcript-manual-1",
                    "asset_id": "timeline-manual-2",
                    "previous_asset_id": "timeline-manual-1",
                    "segment_count": 3,
                    "duration_ms": 12_000,
                    "checksum": "sha256:manual-timeline",
                    "comment": "Manual timing edit applied.",
                    "secret": "not-in-workflow-evidence",
                },
            )
        )
        repository.save(edited_episode)
        continued = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={
                "action": "continue_after_manual_edit",
                "user_id": "editor-1",
                "comment": "Manual edit applied.",
            },
        )
        assert continued.status_code == 200
        continued_body = continued.json()
        assert continued_body["status"] == "TRANSCRIPT_REVIEW"
        assert continued_body["workflow_control"]["manual_edit_continue_count"] == 1
        assert continued_body["workflow_control"]["manual_edit_previous_stage"] == "FAILED"
        assert continued_body["workflow_control"]["manual_edit_target_stage"] == (
            "TRANSCRIPT_REVIEW"
        )
        manual_edit_evidence = continued_body["workflow_control"]["manual_edit_evidence"]
        assert manual_edit_evidence["schema_version"] == "manual_edit_evidence.v1"
        assert manual_edit_evidence["event_count"] == 1
        assert manual_edit_evidence["by_event_type"] == {"timeline.asset.edited": 1}
        assert manual_edit_evidence["events"][0]["event_type"] == "timeline.asset.edited"
        assert manual_edit_evidence["events"][0]["details"] == {
            "transcript_version_id": "transcript-manual-1",
            "asset_id": "timeline-manual-2",
            "previous_asset_id": "timeline-manual-1",
            "segment_count": 3,
            "duration_ms": 12_000,
            "checksum": "sha256:manual-timeline",
            "comment": "Manual timing edit applied.",
        }
        assert "secret" not in json.dumps(manual_edit_evidence)
        assert manual_edit_evidence["evidence_checksum"].startswith("sha256:")
        assert continued_body["workflow_control"]["paused"] is False
        assert continued_body["workflow_control"]["cancelled"] is False
        assert continued_body["workflow_control"]["run"]["state"] == "running"
        assert continued_body["workflow_control"]["run"]["current_stage"] == ("TRANSCRIPT_REVIEW")
        assert continued_body["workflow_control"]["run"]["completion_reason"] is None
        assert continued_body["workflow_control"]["run"]["signals"][-1]["signal_type"] == (
            "continue_after_manual_edit"
        )
        assert (
            continued_body["workflow_control"]["run"]["signals"][-1]["manual_edit_evidence"][
                "evidence_checksum"
            ]
            == manual_edit_evidence["evidence_checksum"]
        )
        assert (
            continued_body["workflow_control"]["temporal_signal_log"][-1]["signal_type"]
            == "continue_after_manual_edit"
        )
        assert (
            continued_body["workflow_control"]["temporal_signal_log"][-1]["manual_edit_evidence"][
                "evidence_checksum"
            ]
            == manual_edit_evidence["evidence_checksum"]
        )
        assert continued_body["audit_events"][-2]["event_type"] == (
            "workflow.manual_edit.continued"
        )
        assert (
            continued_body["audit_events"][-2]["details"]["manual_edit_evidence"][
                "evidence_checksum"
            ]
            == manual_edit_evidence["evidence_checksum"]
        )
        assert continued_body["audit_events"][-1]["event_type"] == "workflow.stage.changed"

        continued_replay = client.get(f"/api/v1/episodes/{episode_id}/workflow/replay")
        assert continued_replay.status_code == 200
        continued_replay_body = continued_replay.json()
        assert continued_replay_body["status"] == "pass"
        assert continued_replay_body["replayed"]["state"] == "running"
        assert continued_replay_body["replayed"]["completion_reason"] is None
        assert continued_replay_body["replayed"]["current_stage"] == "TRANSCRIPT_REVIEW"
        replayed_manual_evidence = continued_replay_body["replayed"]["signals"][-1][
            "manual_edit_evidence"
        ]
        current_manual_evidence = continued_replay_body["current"]["signals"][-1][
            "manual_edit_evidence"
        ]
        assert replayed_manual_evidence == current_manual_evidence
        assert replayed_manual_evidence == {
            "schema_version": "manual_edit_evidence.v1",
            "event_count": 1,
            "by_event_type": {"timeline.asset.edited": 1},
            "evidence_checksum": manual_edit_evidence["evidence_checksum"],
        }
        assert "Manual timing edit applied." not in json.dumps(continued_replay_body)
        assert "timeline-manual-2" not in json.dumps(continued_replay_body)

        terminal_episode = repository.get(episode_id)
        terminal_episode.status = EpisodeStatus.completed
        repository.save(terminal_episode)
        terminal_response = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/actions",
            json={"action": "approve_stage", "user_id": "producer-1"},
        )
        assert terminal_response.status_code == 422
        assert "terminal episodes" in terminal_response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_retry_reopen_stage_history_is_replayable_when_target_stage_changes() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))

    service.begin_run(episode, user_id="producer-1")
    episode.status = EpisodeStatus.failed
    retry_created_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    episode.workflow_control = {
        **episode.workflow_control,
        "failed_stage": EpisodeStatus.generating_audio.value,
        "failure_reason": "voicebox worker failed",
        "retry_exhausted": True,
        "retry_exhausted_stage": EpisodeStatus.generating_audio.value,
        "stage_retry_queue": [
            {
                "schema_version": "workflow_stage_retry.v1",
                "retry_id": "retry-audio-exhausted",
                "stage": "voicebox",
                "target_stage": EpisodeStatus.generating_audio.value,
                "source_summary_id": "summary-audio",
                "attempt_number": 3,
                "max_attempts": 3,
                "status": "exhausted",
                "created_at": retry_created_at,
                "next_retry_not_before": None,
                "backoff_seconds": 180,
                "error": "voicebox worker failed",
            }
        ],
    }

    service.retry_failed_stage(
        episode,
        WorkflowActionRequest(action="retry_failed_stage", user_id="producer-1"),
    )

    run = episode.workflow_control["run"]
    assert run["current_stage"] == EpisodeStatus.generating_audio.value
    assert run["stage_history"][-1]["stage"] == EpisodeStatus.generating_audio.value
    assert episode.workflow_control["failure_reason"] is None
    assert "retry_exhausted" not in episode.workflow_control
    retry_resolution = episode.workflow_control["last_stage_retry_resolution"]
    assert retry_resolution["resolution"] == "operator_retried"
    assert retry_resolution["resolved_count"] == 1
    retry_entry = episode.workflow_control["stage_retry_queue"][0]
    assert retry_entry["status"] == "operator_retried"
    assert retry_entry["previous_status"] == "exhausted"
    assert retry_entry["resolved_by"] == "producer-1"
    assert retry_entry["next_retry_not_before"] is None
    event_log = episode.workflow_control["workflow_event_log"]
    assert event_log[-2]["event_type"] == "workflow.stage_retry.resolved"
    assert event_log[-2]["target_stage"] == EpisodeStatus.generating_audio.value
    assert event_log[-2]["resolution"] == "operator_retried"
    assert event_log[-2]["resolved_count"] == 1
    assert event_log[-1]["event_type"] == "workflow.stage.entered"
    assert event_log[-1]["stage"] == EpisodeStatus.generating_audio.value
    replay = service.replay_workflow(episode)
    assert replay["status"] == "pass"
    assert replay["replayed"]["stage_history"][-1]["stage"] == (
        EpisodeStatus.generating_audio.value
    )


def test_workflow_retry_acknowledgement_resolves_obsolete_backlog_entry() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        ProductionControlService().begin_run(episode, user_id="producer-1")
        created_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        episode.workflow_control = {
            **episode.workflow_control,
            "retry_exhausted": True,
            "retry_exhausted_stage": EpisodeStatus.discussing.value,
            "stage_retry_queue": [
                {
                    "schema_version": "workflow_stage_retry.v1",
                    "retry_id": "retry-discussion-exhausted",
                    "stage": "discussion",
                    "target_stage": EpisodeStatus.discussing.value,
                    "source_summary_id": "summary-discussion",
                    "attempt_number": 3,
                    "max_attempts": 3,
                    "status": "exhausted",
                    "created_at": created_at,
                    "next_retry_not_before": None,
                    "backoff_seconds": 180,
                    "error": "obsolete smoke run failure",
                },
                {
                    "schema_version": "workflow_stage_retry.v1",
                    "retry_id": "retry-audio-due",
                    "stage": "audio",
                    "target_stage": EpisodeStatus.generating_audio.value,
                    "source_summary_id": "summary-audio",
                    "attempt_number": 1,
                    "max_attempts": 3,
                    "status": "scheduled",
                    "created_at": created_at,
                    "next_retry_not_before": created_at,
                    "backoff_seconds": 60,
                    "error": "still actionable",
                },
            ],
        }
        repository.save(episode)

        response = client.post(
            f"/api/v1/episodes/{episode.id}/workflow/retries/retry-discussion-exhausted/resolve",
            json={
                "user_id": "producer-1",
                "comment": "Old smoke run was superseded by a passing audio-first run.",
            },
        )

        assert response.status_code == 200
        body = response.json()
        retries = {
            retry["retry_id"]: retry for retry in body["workflow_control"]["stage_retry_queue"]
        }
        acknowledged = retries["retry-discussion-exhausted"]
        assert acknowledged["status"] == "operator_acknowledged"
        assert acknowledged["previous_status"] == "exhausted"
        assert acknowledged["resolved_by"] == "producer-1"
        assert acknowledged["next_retry_not_before"] is None
        assert acknowledged["resolution_comment"] == (
            "Old smoke run was superseded by a passing audio-first run."
        )
        assert "retry_exhausted" not in body["workflow_control"]
        assert (
            body["workflow_control"]["last_stage_retry_acknowledgement"]["retry_id"]
            == "retry-discussion-exhausted"
        )
        assert body["workflow_control"]["workflow_event_log"][-1]["event_type"] == (
            "workflow.stage_retry.operator_acknowledged"
        )
        assert body["audit_events"][-1]["event_type"] == "workflow.stage_retry.acknowledged"

        backlog = client.get("/api/v1/system/workflow-retries")
        assert backlog.status_code == 200
        backlog_body = backlog.json()
        assert backlog_body["summary"]["total_retry_entries"] == 1
        assert backlog_body["summary"]["resolved_retry_entries"] == 1
        assert backlog_body["summary"]["by_resolution_status"] == {"operator_acknowledged": 1}
        assert [entry["retry_id"] for entry in backlog_body["entries"]] == ["retry-audio-due"]
    finally:
        app.dependency_overrides.clear()
    health = SystemHealthService(Settings()).summary(repository)
    workflow_retries = next(
        component for component in health["components"] if component["name"] == "workflow_retries"
    )
    assert workflow_retries["details"]["total_retry_entries"] == 1
    assert workflow_retries["details"]["exhausted_retry_entries"] == 0
    assert "no_exhausted_retries" not in workflow_retries["details"]["failed_readiness_checks"]


def test_manual_edit_resolution_journals_resolved_stage_retry_without_breaking_replay() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))

    service.begin_run(episode, user_id="producer-1")
    episode.status = EpisodeStatus.failed
    episode.workflow_control = {
        **episode.workflow_control,
        "failed_stage": EpisodeStatus.transcript_review.value,
        "last_rejected_stage": EpisodeStatus.transcript_review.value,
        "failed_at": datetime.now(UTC).isoformat(),
        "retry_exhausted": True,
        "retry_exhausted_stage": EpisodeStatus.transcript_review.value,
        "stage_retry_queue": [
            {
                "schema_version": "workflow_stage_retry.v1",
                "retry_id": "retry-transcript-exhausted",
                "stage": "qc",
                "target_stage": EpisodeStatus.transcript_review.value,
                "source_summary_id": "summary-transcript",
                "attempt_number": 3,
                "max_attempts": 3,
                "status": "exhausted",
                "created_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                "next_retry_not_before": None,
                "backoff_seconds": 180,
                "error": "stage rejected by reviewer",
            }
        ],
    }
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="asset.replaced",
            actor="editor-1",
            details={
                "original_asset_id": "asset-old",
                "replacement_asset_id": "asset-new",
                "asset_type": "audio",
                "source_entity_type": "transcript_turn",
                "source_entity_id": "turn-1",
                "comment": "Manual audio fix.",
                "authorization": "redacted-by-allowlist",
            },
        )
    )

    service.continue_after_manual_edit(
        episode,
        WorkflowActionRequest(
            action="continue_after_manual_edit",
            user_id="editor-1",
            comment="Transcript timing corrected.",
        ),
    )

    retry_resolution = episode.workflow_control["last_stage_retry_resolution"]
    assert retry_resolution["resolution"] == "manual_edit_resolved"
    assert retry_resolution["target_stage"] == EpisodeStatus.transcript_review.value
    assert retry_resolution["resolved_by"] == "editor-1"
    assert "retry_exhausted" not in episode.workflow_control
    manual_edit_evidence = episode.workflow_control["manual_edit_evidence"]
    assert manual_edit_evidence["schema_version"] == "manual_edit_evidence.v1"
    assert manual_edit_evidence["event_count"] == 1
    assert manual_edit_evidence["by_event_type"] == {"asset.replaced": 1}
    assert manual_edit_evidence["events"][0]["details"]["replacement_asset_id"] == "asset-new"
    assert "authorization" not in json.dumps(manual_edit_evidence)
    retry_entry = episode.workflow_control["stage_retry_queue"][0]
    assert retry_entry["status"] == "manual_edit_resolved"
    assert retry_entry["previous_status"] == "exhausted"
    event_log = episode.workflow_control["workflow_event_log"]
    assert event_log[-2]["event_type"] == "workflow.stage_retry.resolved"
    assert event_log[-2]["resolution"] == "manual_edit_resolved"
    assert event_log[-1]["event_type"] == "workflow.stage.entered"
    replay = service.replay_workflow(episode)
    assert replay["status"] == "pass"
    assert replay["replayed"]["current_stage"] == EpisodeStatus.transcript_review.value


def test_manual_edit_continuation_keeps_a_ready_episode_ready() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    service.begin_run(episode, user_id="producer-1")
    episode.status = EpisodeStatus.ready
    episode.workflow_control = {
        **episode.workflow_control,
        "failed_stage": EpisodeStatus.generating_visuals.value,
        "failure_reason": None,
        "paused": False,
    }

    service.continue_after_manual_edit(
        episode,
        WorkflowActionRequest(
            action="continue_after_manual_edit",
            user_id="editor-1",
            comment="Directed media and timeline repaired.",
        ),
    )

    assert episode.status == EpisodeStatus.ready
    assert episode.workflow_control["manual_edit_previous_stage"] == EpisodeStatus.ready.value
    assert episode.workflow_control["manual_edit_target_stage"] == EpisodeStatus.ready.value
    assert episode.workflow_control["run"]["current_stage"] == EpisodeStatus.ready.value


def test_worker_orchestration_counts_publishing_manifest_creation_as_progress() -> None:
    repository = EpisodeRepository()
    service = ProductionControlService(Settings())
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    service.begin_run(episode, user_id="producer-1")

    service.record_worker_orchestration(
        episode,
        {
            "policy": "local_ordered_coordination_v1",
            "batch_limit": 10,
            "stage_order": ["publishing"],
            "progressed_stage_count": 1,
            "error_count": 0,
            "stages": {
                "publishing": {
                    "episodes_scanned": 1,
                    "thumbnails_created": 0,
                    "youtube_packages_created": 0,
                    "production_manifests_created": 1,
                    "dry_run_publish_jobs_created": 0,
                    "live_publish_jobs_created": 0,
                    "error_count": 0,
                    "skipped": 0,
                    "errors": [],
                }
            },
        },
        worker_id="workflow-worker",
    )

    orchestration = episode.workflow_control["worker_orchestration_log"][0]
    attempt = orchestration["stage_attempts"][0]
    assert orchestration["progressed_stage_count"] == 1
    assert attempt["stage"] == "publishing"
    assert attempt["status"] == "progressed"
    assert attempt["progress_count"] == 1
    assert attempt["error_count"] == 0
    assert attempt["episodes_scanned"] == 1
    assert attempt["skipped"] == 0
    assert attempt["summary_checksum"].startswith("sha256:")
    assert attempt["stage_manifest"]["schema_version"] == "workflow_stage_manifest.v1"
    assert attempt["stage_manifest"]["stage"] == "publishing"
    assert attempt["stage_manifest"]["status"] == "progressed"
    assert attempt["stage_manifest"]["progress_count"] == 1
    assert attempt["stage_manifest"]["progress_metrics"]["production_manifests_created"] == 1
    assert attempt["stage_manifest"]["summary_checksum"] == attempt["summary_checksum"]
    assert attempt["stage_manifest"]["manifest_checksum"].startswith("sha256:")
    assert (
        episode.workflow_control["run"]["last_worker_orchestration"]["progressed_stage_count"] == 1
    )


def test_resolved_stage_retry_history_counts_toward_later_retry_budget() -> None:
    repository = EpisodeRepository()
    settings = Settings(
        workflow_stage_retry_max_attempts=2,
        workflow_stage_retry_backoff_seconds=5,
    )
    service = ProductionControlService(settings)
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    service.begin_run(episode, user_id="producer-1")
    episode.workflow_control = {
        **episode.workflow_control,
        "stage_retry_queue": [
            {
                "schema_version": "workflow_stage_retry.v1",
                "retry_id": "retry-audio-resolved",
                "stage": "voicebox",
                "target_stage": EpisodeStatus.generating_audio.value,
                "source_summary_id": "summary-audio-1",
                "attempt_number": 1,
                "max_attempts": 2,
                "status": "operator_retried",
                "previous_status": "scheduled",
                "created_at": (datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
                "resolved_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                "resolved_by": "producer-1",
                "resolution_signal_id": "signal-retry-audio",
                "next_retry_not_before": None,
                "backoff_seconds": 5,
                "error": "voicebox worker failed",
            }
        ],
    }

    service.record_worker_orchestration(
        episode,
        {
            "policy": "test",
            "batch_limit": 10,
            "stage_order": ["voicebox"],
            "progressed_stage_count": 0,
            "error_count": 1,
            "stages": {
                "voicebox": {
                    "error_count": 1,
                    "episodes_scanned": 1,
                    "errors": [
                        {
                            "episode_id": str(episode.id),
                            "error": "voicebox worker failed again",
                        }
                    ],
                }
            },
        },
        worker_id="workflow-worker",
    )

    retry_queue = episode.workflow_control["stage_retry_queue"]
    assert len(retry_queue) == 2
    assert retry_queue[0]["status"] == "operator_retried"
    latest_retry = retry_queue[-1]
    assert latest_retry["attempt_number"] == 2
    assert latest_retry["status"] == "exhausted"
    assert latest_retry["target_stage"] == EpisodeStatus.generating_audio.value
    assert episode.workflow_control["retry_exhausted"] is True
    assert episode.workflow_control["retry_exhausted_stage"] == (
        EpisodeStatus.generating_audio.value
    )


def test_worker_orchestration_replay_does_not_duplicate_retry_attempts() -> None:
    repository = EpisodeRepository()
    settings = Settings(
        workflow_stage_retry_max_attempts=2,
        workflow_stage_retry_backoff_seconds=5,
    )
    service = ProductionControlService(settings)
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    service.begin_run(episode, user_id="producer-1")
    replayed_summary = {
        "schema_version": "workflow_worker_orchestration_summary.v1",
        "orchestration_attempt_id": "orchestration-replay-safe",
        "policy": "test",
        "batch_limit": 10,
        "stage_order": ["voicebox"],
        "progressed_stage_count": 0,
        "error_count": 1,
        "stages": {
            "voicebox": {
                "error_count": 1,
                "episodes_scanned": 1,
                "errors": [
                    {
                        "episode_id": str(episode.id),
                        "error": "voicebox worker failed",
                    }
                ],
            }
        },
    }

    service.record_worker_orchestration(
        episode,
        replayed_summary,
        worker_id="workflow-worker",
    )
    service.record_worker_orchestration(
        episode,
        replayed_summary,
        worker_id="workflow-worker",
    )

    retry_queue = episode.workflow_control["stage_retry_queue"]
    orchestration_log = episode.workflow_control["worker_orchestration_log"]
    assert len(orchestration_log) == 1
    assert orchestration_log[0]["summary_id"] == "orchestration-replay-safe"
    assert len(retry_queue) == 1
    assert retry_queue[0]["source_summary_id"] == "orchestration-replay-safe"
    assert retry_queue[0]["attempt_number"] == 1
    assert retry_queue[0]["status"] == "scheduled"
    assert "retry_exhausted" not in episode.workflow_control

    next_summary = {
        **replayed_summary,
        "orchestration_attempt_id": "orchestration-next-pass",
    }
    service.record_worker_orchestration(
        episode,
        next_summary,
        worker_id="workflow-worker",
    )

    retry_queue = episode.workflow_control["stage_retry_queue"]
    assert len(episode.workflow_control["worker_orchestration_log"]) == 2
    assert len(retry_queue) == 2
    assert retry_queue[-1]["source_summary_id"] == "orchestration-next-pass"
    assert retry_queue[-1]["attempt_number"] == 2
    assert retry_queue[-1]["status"] == "exhausted"
    assert episode.workflow_control["retry_exhausted"] is True


def test_episode_workflow_replay_endpoint_reports_run_consistency() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        Settings(),
    )
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]
        produced = client.post(f"/api/v1/episodes/{episode_id}/produce")
        assert produced.status_code == 200

        replay = client.get(f"/api/v1/episodes/{episode_id}/workflow/replay")
        assert replay.status_code == 200
        body = replay.json()
        assert body["status"] == "pass"
        assert body["event_count"] >= 5
        assert body["event_log_checksum"].startswith("sha256:")
        assert body["replayed"]["current_stage"] == "TRANSCRIPT_REVIEW"
        assert body["current"]["current_stage"] == "TRANSCRIPT_REVIEW"
        assert body["issues"] == []
    finally:
        app.dependency_overrides.clear()


def test_episode_asset_replacement_api_marks_original_replaced(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        Settings(),
    )
    app.dependency_overrides[get_asset_replacement_service] = lambda: AssetReplacementService(
        settings
    )
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]
        episode = repository.get(episode_id)
        failed_asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language="en",
            source_entity_type="transcript_turn",
            source_entity_id="manual-turn-1",
            storage_uri="object://dialecticore/audio/bad.wav",
            mime_type="audio/wav",
            status="failed",
            checksum="sha256:bad",
        )
        episode.assets.append(failed_asset)
        repository.save(episode)

        replaced = client.post(
            f"/api/v1/episodes/{episode_id}/assets/{failed_asset.id}/replace",
            json={
                "storage_uri": "object://dialecticore/audio/fixed.wav",
                "mime_type": "audio/wav",
                "checksum": "sha256:fixed",
                "duration_ms": 1200,
                "user_id": "editor-1",
                "comment": "Manual replacement after QC failure.",
            },
        )
        assert replaced.status_code == 200
        body = replaced.json()
        original = next(asset for asset in body["assets"] if asset["id"] == str(failed_asset.id))
        replacement = next(
            asset
            for asset in body["assets"]
            if asset["generation_metadata"].get("replacement_of_asset_id") == str(failed_asset.id)
        )
        assert original["status"] == "replaced"
        assert original["generation_metadata"]["replaced_by_asset_id"] == replacement["id"]
        assert replacement["asset_type"] == "audio"
        assert replacement["status"] == "completed"
        assert replacement["storage_uri"] == "object://dialecticore/audio/fixed.wav"
        assert replacement["checksum"] == "sha256:fixed"
        assert replacement["duration_ms"] == 1200
        assert replacement["generation_metadata"]["manual_replacement"] is True
        assert body["audit_events"][-1]["event_type"] == "asset.replaced"

        blocked = client.post(
            f"/api/v1/episodes/{episode_id}/assets/{failed_asset.id}/replace",
            json={"storage_uri": "object://dialecticore/audio/second.wav"},
        )
        assert blocked.status_code == 422
        assert "already been replaced" in blocked.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_episode_asset_download_serves_configured_object_store_file(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    object_store = create_object_store(settings)
    stored = object_store.put_bytes(
        key="exports/test-package.zip",
        payload=b"package-bytes",
        content_type="application/zip",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]
        episode = repository.get(episode_id)
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            language=episode.source_language,
            source_entity_type="render_asset",
            source_entity_id="render-download-test",
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            checksum=stored.checksum,
            status="completed",
        )
        episode.assets.append(asset)
        repository.save(episode)

        downloaded = client.get(f"/api/v1/episodes/{episode_id}/assets/{asset.id}/download")

        assert downloaded.status_code == 200
        assert downloaded.content == b"package-bytes"
        assert downloaded.headers["content-type"] == "application/zip"
        assert "export_package-" in downloaded.headers["content-disposition"]
        assert ".zip" in downloaded.headers["content-disposition"]

        video_stored = object_store.put_bytes(
            key="renders/test-primer.mp4",
            payload=b"video-bytes",
            content_type="video/mp4",
        )
        video_asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.render,
            language=episode.source_language,
            source_entity_type="primer_timeline",
            source_entity_id="primer-render-download-test",
            storage_uri=video_stored.uri,
            mime_type=video_stored.content_type,
            checksum=video_stored.checksum,
            status="completed",
        )
        episode.assets.append(video_asset)
        repository.save(episode)

        streamed = client.get(
            f"/api/v1/episodes/{episode_id}/assets/{video_asset.id}/download",
            headers={"range": "bytes=0-4"},
        )

        assert streamed.status_code == 206
        assert streamed.content == b"video"
        assert streamed.headers["content-type"] == "video/mp4"
        assert streamed.headers["content-disposition"].startswith("inline;")
        assert streamed.headers["content-range"] == "bytes 0-4/11"

        stored.path.unlink()
        missing = client.get(f"/api/v1/episodes/{episode_id}/assets/{asset.id}/download")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "stored object not found"
    finally:
        app.dependency_overrides.clear()


def test_episode_detail_omits_large_audio_timing_tracks_from_ui_projection() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]
        episode = repository.get(episode_id)
        audio_asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language=episode.source_language,
            source_entity_type="primer_narration",
            source_entity_id="narration-timing-test",
            generation_metadata={
                "word_timestamps": [{"word": "Hello", "start_ms": 0, "end_ms": 250}],
                "phoneme_timestamps": [{"phoneme": "h", "start_ms": 0, "end_ms": 80}],
                "viseme_timestamps": [{"viseme": "closed", "start_ms": 0, "end_ms": 80}],
                "word_timing": {"word_count": 1, "duration_ms": 250},
                "voice_profile_id": "narrator",
            },
            status="completed",
        )
        episode.assets.append(audio_asset)
        repository.save(episode)

        detail = client.get(f"/api/v1/episodes/{episode_id}")

        assert detail.status_code == 200
        metadata = detail.json()["assets"][0]["generation_metadata"]
        assert "word_timestamps" not in metadata
        assert "phoneme_timestamps" not in metadata
        assert "viseme_timestamps" not in metadata
        assert metadata["word_timing"] == {"word_count": 1, "duration_ms": 250}
        assert metadata["voice_profile_id"] == "narrator"

        stored_metadata = repository.get(episode_id).assets[0].generation_metadata
        assert stored_metadata["word_timestamps"][0]["word"] == "Hello"
        assert stored_metadata["phoneme_timestamps"][0]["phoneme"] == "h"
        assert stored_metadata["viseme_timestamps"][0]["viseme"] == "closed"

        compact_metadata = list(repository.list_compact())[0].assets[0].generation_metadata
        assert "word_timestamps" not in compact_metadata
        assert "phoneme_timestamps" not in compact_metadata
        assert "viseme_timestamps" not in compact_metadata
    finally:
        app.dependency_overrides.clear()


def test_episode_media_index_is_compact_and_exposes_completed_video_metadata() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]
        episode = repository.get(episode_id)
        canonical_transcript_id = uuid4()
        episode.canonical_transcript_version_id = canonical_transcript_id
        episode.assets.extend(
            [
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.video,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-1",
                    storage_uri="object://dialecticore/video/turn-1.mp4",
                    mime_type="video/mp4",
                    duration_ms=4200,
                    width=512,
                    height=512,
                    fps=12,
                    status="completed",
                    generation_metadata={
                        "character_name": "Mistral",
                        "transcript_version_id": str(canonical_transcript_id),
                        "visual_role": "video_primary",
                        "provider_performance_applied": True,
                        "large_provider_payload": {"unused": "not serialized"},
                    },
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.audio,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-1",
                    status="completed",
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.video,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-2",
                    status="failed",
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.video,
                    source_entity_type="episode_opening",
                    source_entity_id="primer-source-video",
                    storage_uri="object://dialecticore/video/primer-source.mp4",
                    mime_type="video/mp4",
                    status="completed",
                    generation_metadata={"opening_media": True},
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.video,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-2",
                    storage_uri="object://dialecticore/video/turn-2-broll.mp4",
                    mime_type="video/mp4",
                    status="completed",
                    generation_metadata={"visual_role": "broll"},
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.video,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-previous-version",
                    storage_uri="object://dialecticore/video/turn-previous-version.mp4",
                    mime_type="video/mp4",
                    status="completed",
                    generation_metadata={
                        "transcript_version_id": str(uuid4()),
                        "visual_role": "video_primary",
                    },
                ),
            ]
        )
        repository.save(episode)

        response = client.get(f"/api/v1/episodes/{episode_id}/media-index")

        assert response.status_code == 200
        assert response.json() == [
            {
                "id": str(episode.assets[0].id),
                "asset_type": "video",
                "source_entity_type": "transcript_turn",
                "source_entity_id": "turn-1",
                "storage_uri": "object://dialecticore/video/turn-1.mp4",
                "mime_type": "video/mp4",
                "duration_ms": 4200,
                "width": 512,
                "height": 512,
                "fps": 12.0,
                "status": "completed",
                "character_name": "Mistral",
                "visual_role": "video_primary",
                "performance_applied": True,
            }
        ]
    finally:
        app.dependency_overrides.clear()


def test_episode_youtube_package_inspection_reads_stored_zip_manifest(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    package_payload = io.BytesIO()
    manifest = {
        "id": "inspect-package",
        "schema_version": "youtube_package.v1",
        "title": "Inspection package",
        "language": "en",
        "render_asset_id": "render-inspect",
        "chapters": [{"title": "Opening", "start_time": "00:00"}],
        "subtitles": [{"language": "en", "path": "subtitles/en.vtt"}],
        "evidence_lineage": {"referenced_sources": [{"source_id": "source-a"}]},
    }
    with zipfile.ZipFile(package_payload, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("youtube-package.json", json.dumps(manifest))
        archive.writestr("video/render.mp4", b"render")
        archive.writestr("subtitles/en.vtt", "WEBVTT\n")
    stored = create_object_store(settings).put_bytes(
        key="exports/inspect-package.zip",
        payload=package_payload.getvalue(),
        content_type="application/zip",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]
        episode = repository.get(episode_id)
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            language="en",
            source_entity_type="render_asset",
            source_entity_id="render-inspect",
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "included_files": [
                    "youtube-package.json",
                    "video/render.mp4",
                    "subtitles/en.vtt",
                ],
                "youtube_package_manifest": manifest,
            },
        )
        episode.assets.append(asset)
        repository.save(episode)

        inspected = client.get(
            f"/api/v1/episodes/{episode_id}/youtube-package/inspect",
            params={"package_asset_id": str(asset.id)},
        )

        assert inspected.status_code == 200
        body = inspected.json()
        assert body["schema_version"] == "youtube_package_inspection.v1"
        assert body["status"] == "pass"
        assert body["file_count"] == 3
        assert body["chapter_count"] == 1
        assert body["subtitle_count"] == 1
        assert body["evidence_source_count"] == 1
        assert body["manifest_matches_asset_metadata"] is True
        assert body["package"]["downloadable"] is True
    finally:
        app.dependency_overrides.clear()


def test_episode_youtube_package_inspection_warns_on_missing_declared_files(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    manifest = {
        "id": "inspect-package",
        "schema_version": "youtube_package.v1",
        "title": "Inspection package",
        "language": "en",
        "render_asset_id": "render-inspect",
        "thumbnail_asset_id": "thumbnail-inspect",
        "chapters": [],
        "subtitles": [{"language": "en", "path": "subtitles/en.vtt"}],
        "evidence_lineage": {"referenced_sources": []},
    }
    package_payload = io.BytesIO()
    with zipfile.ZipFile(package_payload, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("youtube-package.json", json.dumps(manifest))
        archive.writestr("video/render.mp4", b"render")
    stored = create_object_store(settings).put_bytes(
        key="exports/inspect-package-missing-files.zip",
        payload=package_payload.getvalue(),
        content_type="application/zip",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]
        episode = repository.get(episode_id)
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            language="en",
            source_entity_type="render_asset",
            source_entity_id="render-inspect",
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "included_files": ["youtube-package.json", "video/render.mp4"],
                "youtube_package_manifest": manifest,
            },
        )
        episode.assets.append(asset)
        repository.save(episode)

        inspected = client.get(
            f"/api/v1/episodes/{episode_id}/youtube-package/inspect",
            params={"package_asset_id": str(asset.id)},
        )

        assert inspected.status_code == 200
        body = inspected.json()
        assert body["status"] == "warning"
        assert body["issues"] == [
            "youtube_package_declared_thumbnail_missing_file",
            "youtube_package_declared_subtitles_missing_files",
        ]
    finally:
        app.dependency_overrides.clear()


def test_discussion_prompt_template_api_crud_and_delete_guard(tmp_path: Path) -> None:
    repository = _persistent_repository(f"sqlite:///{tmp_path / 'prompt_templates.db'}")
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    try:
        defaults = client.get("/api/v1/discussion-prompt-templates")
        assert defaults.status_code == 200
        assert {"moderator_v1", "panelist_v1"} <= {template["id"] for template in defaults.json()}
        assert all(template["version"] for template in defaults.json())

        created = client.post(
            "/api/v1/discussion-prompt-templates",
            json={
                "id": "operator_panelist_v2",
                "version": "2.0.0",
                "participant_type": "panelist",
                "system": "You are {display_name}. Return only JSON.",
                "user": "Central question: {central_question}\n{public_transcript}",
                "variables": {"turn_context": ["central_question", "public_transcript"]},
                "created_by": "producer",
                "enabled": True,
                "change_summary": "Operator-tuned panelist prompt.",
            },
        )
        assert created.status_code == 200
        assert created.json()["version"] == "2.0.0"

        updated = client.put(
            "/api/v1/discussion-prompt-templates/operator_panelist_v2",
            json={
                "id": "ignored",
                "version": "2.0.1",
                "participant_type": "panelist",
                "system": "You are {display_name}. Return only JSON.",
                "user": "Updated: {central_question}\n{public_transcript}",
                "variables": {"turn_context": ["central_question", "public_transcript"]},
                "created_by": "producer",
                "enabled": True,
                "change_summary": "Tightened operator panelist prompt.",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["id"] == "operator_panelist_v2"
        assert updated.json()["version"] == "2.0.1"

        blocked_disable = client.put(
            "/api/v1/discussion-prompt-templates/panelist_v1",
            json={
                "id": "panelist_v1",
                "version": "1.0.0",
                "participant_type": "panelist",
                "system": "You are {display_name}.",
                "user": "Question: {central_question}",
                "enabled": False,
                "change_summary": "Disable in-use template.",
            },
        )
        assert blocked_disable.status_code == 422
        assert "cannot be disabled" in blocked_disable.json()["detail"]

        blocked_retype = client.put(
            "/api/v1/discussion-prompt-templates/panelist_v1",
            json={
                "id": "panelist_v1",
                "version": "1.0.0",
                "participant_type": "host",
                "system": "You are {display_name}.",
                "user": "Question: {central_question}",
                "enabled": True,
                "change_summary": "Retype in-use template.",
            },
        )
        assert blocked_retype.status_code == 422
        assert "participant type cannot be changed" in blocked_retype.json()["detail"]

        disabled_template = client.post(
            "/api/v1/discussion-prompt-templates",
            json={
                "id": "disabled_panelist_v1",
                "version": "1.0.0",
                "participant_type": "panelist",
                "system": "You are {display_name}.",
                "user": "Question: {central_question}",
                "enabled": False,
                "change_summary": "Disabled template.",
            },
        )
        assert disabled_template.status_code == 200

        disabled_profile = client.post(
            "/api/v1/participant-profiles",
            json={
                "id": "disabled-template-panelist",
                "name": "disabled-template-panelist",
                "display_name": "Disabled Template Panelist",
                "participant_type": "panelist",
                "model_endpoint_id": "mock",
                "model_id": "mock-panelist-v1",
                "system_prompt_template": "disabled_panelist_v1",
                "perspective": "test disabled template behavior",
                "expertise": "prompt governance",
                "speaking_style": "brief",
            },
        )
        assert disabled_profile.status_code == 422
        assert "disabled discussion prompt template" in disabled_profile.json()["detail"]

        mismatched_profile = client.post(
            "/api/v1/participant-profiles",
            json={
                "id": "mismatched-template-panelist",
                "name": "mismatched-template-panelist",
                "display_name": "Mismatched Template Panelist",
                "participant_type": "panelist",
                "model_endpoint_id": "mock",
                "model_id": "mock-panelist-v1",
                "system_prompt_template": "moderator_v1",
                "perspective": "test template type behavior",
                "expertise": "prompt governance",
                "speaking_style": "brief",
            },
        )
        assert mismatched_profile.status_code == 422
        assert "participant type does not match" in mismatched_profile.json()["detail"]

        blocked_delete = client.delete("/api/v1/discussion-prompt-templates/panelist_v1")
        assert blocked_delete.status_code == 422
        assert "participant profiles" in blocked_delete.json()["detail"]

        deleted = client.delete("/api/v1/discussion-prompt-templates/operator_panelist_v2")
        assert deleted.status_code == 204
        missing = client.get("/api/v1/discussion-prompt-templates/operator_panelist_v2")
        assert missing.status_code == 404
        events = {
            event["event_type"] for event in client.get("/api/v1/audit-events?limit=20").json()
        }
        assert "discussion_prompt_template.upserted" in events
        assert "discussion_prompt_template.deleted" in events
    finally:
        app.dependency_overrides.clear()


def test_api_middleware_propagates_correlation_id_and_structured_request_log(
    tmp_path: Path,
) -> None:
    repository = _persistent_repository(f"sqlite:///{tmp_path / 'correlation.db'}")
    fake_logger = FakeRequestLogger()
    app.dependency_overrides[get_repository] = lambda: repository
    original_logger = main_module.request_logger
    main_module.request_logger = fake_logger
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        response = client.get(
            f"/api/v1/episodes/{episode.id}",
            headers={"x-correlation-id": "operator.trace/1"},
        )

        assert response.status_code == 200
        assert response.headers["x-correlation-id"] == "operator.trace-1"
        assert fake_logger.records
        event, kwargs = fake_logger.records[-1]
        structured = kwargs["extra"]["structured"]
        assert event == "api.request"
        assert kwargs["extra"]["correlation_id"] == "operator.trace-1"
        assert structured["schema_version"] == "dialecticore.api_request_log.v1"
        assert structured["method"] == "GET"
        assert structured["path"] == f"/api/v1/episodes/{episode.id}"
        assert structured["status_code"] == 200
        assert structured["episode_id"] == str(episode.id)
        assert structured["duration_ms"] >= 0
    finally:
        main_module.request_logger = original_logger
        app.dependency_overrides.clear()


def test_system_health_reports_components_counts_and_pending_work(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        worker_required_roles=(
            "workflow-worker,discussion-worker,research-worker,localization-worker,"
            "voicebox-adapter,comfyui-adapter,timeline-worker,render-worker,qc-worker,"
            "publishing-worker"
        ),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        now = datetime.now(UTC)
        episode.workflow_control = {
            "paused": True,
            "worker_orchestration_log": [
                {
                    "schema_version": "workflow_worker_orchestration_attempt.v1",
                    "summary_id": "summary-1",
                    "attempt_sequence": 1,
                    "recorded_at": now.isoformat(),
                    "worker_id": "workflow-worker",
                    "policy": "local_ordered_coordination_v1",
                    "progressed_stage_count": 2,
                    "error_count": 1,
                    "stage_attempts": [
                        {
                            "stage": "research",
                            "status": "progressed",
                            "progress_count": 1,
                            "error_count": 0,
                        },
                        {
                            "stage": "localization",
                            "status": "failed",
                            "progress_count": 0,
                            "error_count": 1,
                        },
                    ],
                    "temporal_dispatch_count": 1,
                    "production_handoff": {
                        "schema_version": "talkshow_production_handoff.v1",
                        "episode_id": str(episode.id),
                        "status": "blocked",
                        "blocking_reasons": ["completed_audio_missing"],
                        "playable_turn_count": 2,
                        "speech": {
                            "ready": False,
                            "completed_audio_asset_count": 1,
                        },
                    },
                }
            ],
            "temporal_stage_dispatch_log": [
                {
                    "schema_version": "temporal_stage_dispatch.v1",
                    "dispatch_id": "dispatch-blocked",
                    "dispatch_sequence": 1,
                    "requested_at": now.isoformat(),
                    "requested_by": "workflow-worker",
                    "status": "blocked",
                    "stage": "localization",
                    "activity_name": "dialecticore.production.localization",
                    "namespace": "default",
                    "task_queue": "",
                }
            ],
            "stage_retry_queue": [
                {
                    "schema_version": "workflow_stage_retry.v1",
                    "retry_id": "retry-due",
                    "stage": "voicebox",
                    "target_stage": "GENERATING_AUDIO",
                    "attempt_number": 1,
                    "max_attempts": 3,
                    "status": "scheduled",
                    "created_at": (now - timedelta(seconds=120)).isoformat(),
                    "next_retry_not_before": (now - timedelta(seconds=60)).isoformat(),
                },
                {
                    "schema_version": "workflow_stage_retry.v1",
                    "retry_id": "retry-backoff",
                    "stage": "voicebox",
                    "target_stage": "GENERATING_AUDIO",
                    "attempt_number": 2,
                    "max_attempts": 3,
                    "status": "scheduled",
                    "created_at": now.isoformat(),
                    "next_retry_not_before": (now + timedelta(seconds=60)).isoformat(),
                },
                {
                    "schema_version": "workflow_stage_retry.v1",
                    "retry_id": "retry-exhausted",
                    "stage": "render",
                    "target_stage": "RENDERING_FINAL",
                    "attempt_number": 3,
                    "max_attempts": 3,
                    "status": "exhausted",
                    "created_at": (now + timedelta(seconds=1)).isoformat(),
                    "next_retry_not_before": None,
                },
                {
                    "schema_version": "workflow_stage_retry.v1",
                    "retry_id": "retry-resolved",
                    "stage": "voicebox",
                    "target_stage": "GENERATING_AUDIO",
                    "attempt_number": 1,
                    "max_attempts": 3,
                    "status": "operator_retried",
                    "previous_status": "scheduled",
                    "created_at": (now - timedelta(seconds=240)).isoformat(),
                    "resolved_at": (now - timedelta(seconds=180)).isoformat(),
                    "resolved_by": "producer-1",
                    "resolution_signal_id": "signal-resolved",
                    "next_retry_not_before": None,
                },
            ],
        }
        episode.assets.extend(
            [
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.audio,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-a",
                    status="submitted",
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.video,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-v",
                    status="failed",
                ),
            ]
        )
        repository.save(episode)

        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
        body = response.json()
        components = {component["name"]: component for component in body["components"]}
        assert body["service"] == "production-api"
        assert body["status"] in {"healthy", "degraded"}
        assert components["database"]["status"] == "healthy"
        assert components["database"]["details"]["readiness_checks"] == {"database_reachable": True}
        assert components["database"]["details"]["failed_readiness_checks"] == []
        assert components["deployment_readiness"]["status"] == "healthy"
        assert components["deployment_readiness"]["details"]["production_mode"] is False
        assert components["deployment_readiness"]["details"]["issue_count"] == 0
        assert components["runtime_paths"]["status"] == "healthy"
        assert components["runtime_paths"]["details"]["schema_version"] == "runtime_paths.v1"
        assert components["runtime_paths"]["details"]["required_path_count"] == 3
        assert components["runtime_paths"]["details"]["unavailable_path_count"] == 0
        assert components["runtime_paths"]["details"]["low_free_space_path_count"] == 0
        assert components["runtime_paths"]["details"]["min_free_bytes"] == 0
        assert (
            components["runtime_paths"]["details"]["paths"]["backup"]["writable_target_or_parent"]
            is True
        )
        assert (
            components["runtime_paths"]["details"]["paths"]["backup"]["free_bytes_sufficient"]
            is True
        )
        assert (
            components["runtime_paths"]["details"]["paths"]["runtime_state"][
                "writable_target_or_parent"
            ]
            is True
        )
        assert (
            components["runtime_paths"]["details"]["paths"]["object_storage_local"]["required"]
            is True
        )
        assert components["object_storage"]["details"]["backend"] == "local"
        for tool in ("ffmpeg", "ffprobe"):
            tool_path = shutil.which(tool)
            assert components[tool]["status"] == ("healthy" if tool_path else "degraded")
            assert components[tool]["details"]["path"] == tool_path
            assert components[tool]["details"]["readiness_checks"] == {
                "tool_available": bool(tool_path)
            }
            assert components[tool]["details"]["failed_readiness_checks"] == (
                [] if tool_path else ["tool_available"]
            )
        assert components["model_endpoints"]["details"]["readiness_checks"] == {
            "endpoints_configured": True,
            "enabled_endpoint_available": True,
            "enabled_endpoints_not_unhealthy": True,
            "enabled_endpoints_health_known": True,
        }
        assert components["model_endpoints"]["details"]["failed_readiness_checks"] == []
        assert components["voicebox_endpoints"]["details"]["readiness_checks"] == {
            "endpoints_configured": True,
            "enabled_endpoint_available": True,
            "enabled_endpoints_not_unhealthy": True,
            "enabled_endpoints_health_known": True,
        }
        assert components["voicebox_endpoints"]["details"]["failed_readiness_checks"] == []
        assert components["comfyui_endpoints"]["details"]["readiness_checks"] == {
            "endpoints_configured": True,
            "enabled_endpoint_available": True,
            "enabled_endpoints_not_unhealthy": True,
            "enabled_endpoints_health_known": True,
        }
        assert components["comfyui_endpoints"]["details"]["failed_readiness_checks"] == []
        assert components["temporal_runtime"]["status"] == "healthy"
        assert components["temporal_runtime"]["details"]["mode"] == "local"
        assert components["temporal_runtime"]["details"]["execution_policy"] == (
            "local_durable_workflow_control"
        )
        assert components["publisher_targets"]["status"] == "healthy"
        assert components["publisher_targets"]["details"]["schema_version"] == (
            "publisher_target_health.v1"
        )
        assert components["publisher_targets"]["details"]["enabled"] == 1
        assert components["publisher_targets"]["details"]["live_enabled"] == 0
        assert components["publisher_targets"]["details"]["automated_live_enabled"] is False
        assert components["publisher_targets"]["details"]["automated_live_capable_enabled"] == 0
        assert components["publisher_targets"]["details"]["mock_enabled"] == 1
        assert components["publisher_targets"]["details"]["by_adapter_type"] == {"mock": 1}
        assert components["publisher_targets"]["details"]["by_health_status"] == {"healthy": 1}
        assert components["publisher_targets"]["details"]["by_platform"] == {"youtube": 1}
        assert components["publisher_targets"]["details"]["issue_count"] == 0
        assert components["production_runs"]["status"] == "degraded"
        assert components["production_runs"]["details"]["schema_version"] == (
            "production_run_health.v1"
        )
        assert components["production_runs"]["details"]["production_run_count"] == 1
        assert components["production_runs"]["details"]["active_production_runs"] == 1
        assert components["production_runs"]["details"]["paused_active_production_runs"] == 1
        assert components["production_runs"]["details"]["running_active_production_runs"] == 0
        assert components["production_runs"]["details"]["attention_count"] == 1
        assert components["production_runs"]["details"]["by_state"] == {"untracked": 1}
        assert components["production_runs"]["details"]["by_attention_reason"] == {"paused": 1}
        assert components["production_runs"]["details"]["readiness_checks"] == {
            "no_failed_active_production_runs": True,
            "no_cancelled_active_production_runs": True,
            "no_paused_active_production_runs": False,
            "no_running_active_production_runs": True,
            "no_completion_blocked_production_runs": True,
            "no_production_runs_waiting_for_action": True,
        }
        assert components["production_runs"]["details"]["failed_readiness_checks"] == [
            "no_paused_active_production_runs"
        ]
        assert len(components["production_runs"]["details"]["attention_runs"]) == 1
        assert components["production_runs"]["details"]["attention_runs"][0][
            "attention_reasons"
        ] == ["paused"]
        assert components["worker_registry"]["status"] == "degraded"
        assert components["worker_registry"]["details"]["configured_role_names"] == [
            "workflow-worker",
            "discussion-worker",
            "research-worker",
            "localization-worker",
            "voicebox-adapter",
            "comfyui-adapter",
            "timeline-worker",
            "render-worker",
            "qc-worker",
            "publishing-worker",
        ]
        assert components["worker_registry"]["details"]["active_role_names"] == []
        assert components["worker_registry"]["details"]["readiness_checks"] == {
            "worker_status_supplied": True,
            "active_worker_heartbeats_present": False,
            "configured_worker_roles_covered": False,
            "worker_heartbeats_fresh": True,
            "worker_heartbeats_not_failed": True,
            "worker_heartbeats_not_degraded": True,
            "worker_runtime_state_files_parse": True,
        }
        assert components["worker_registry"]["details"]["failed_readiness_checks"] == [
            "active_worker_heartbeats_present",
            "configured_worker_roles_covered",
        ]
        assert components["worker_registry"]["details"]["missing_role_names"] == [
            "workflow-worker",
            "discussion-worker",
            "research-worker",
            "localization-worker",
            "voicebox-adapter",
            "comfyui-adapter",
            "timeline-worker",
            "render-worker",
            "qc-worker",
            "publishing-worker",
        ]
        assert components["worker_registry"]["details"]["by_worker_status"] == {}
        assert components["worker_registry"]["details"]["by_worker_role"] == {}
        assert components["workflow_orchestration"]["status"] == "degraded"
        assert components["workflow_orchestration"]["details"]["attempt_count"] == 1
        assert components["workflow_orchestration"]["details"]["progressed_stage_count"] == 2
        assert components["workflow_orchestration"]["details"]["error_count"] == 1
        assert components["workflow_orchestration"]["details"]["failed_stage_count"] == 1
        assert components["workflow_orchestration"]["details"]["dispatch_count"] == 1
        assert components["workflow_orchestration"]["details"]["blocked_dispatch_count"] == 1
        assert components["workflow_orchestration"]["details"]["production_handoff_count"] == 1
        assert (
            components["workflow_orchestration"]["details"]["blocked_production_handoff_count"] == 1
        )
        assert components["workflow_orchestration"]["details"]["by_production_handoff_status"] == {
            "blocked": 1
        }
        assert components["workflow_orchestration"]["details"]["by_production_handoff_blocker"] == {
            "completed_audio_missing": 1
        }
        assert components["workflow_orchestration"]["details"]["by_worker"] == {
            "workflow-worker": 1
        }
        assert components["workflow_orchestration"]["details"]["by_dispatch_status"] == {
            "blocked": 1
        }
        assert components["workflow_orchestration"]["details"]["by_failed_stage"] == {
            "localization": 1
        }
        assert components["workflow_orchestration"]["details"]["by_progressed_stage"] == {
            "research": 1
        }
        assert components["workflow_orchestration"]["details"]["by_blocked_dispatch_stage"] == {
            "localization": 1
        }
        assert components["workflow_orchestration"]["details"]["readiness_checks"] == {
            "no_workflow_orchestration_errors": False,
            "no_failed_workflow_stages": False,
            "no_blocked_temporal_dispatches": False,
            "no_blocked_production_handoffs": True,
            "no_production_handoffs_waiting_for_action": True,
        }
        assert components["workflow_orchestration"]["details"]["failed_readiness_checks"] == [
            "no_workflow_orchestration_errors",
            "no_failed_workflow_stages",
            "no_blocked_temporal_dispatches",
        ]
        assert components["workflow_orchestration"]["details"]["by_ready_dispatch_stage"] == {}
        assert components["workflow_orchestration"]["details"]["latest_attempt"][
            "by_failed_stage"
        ] == {"localization": 1}
        assert components["workflow_orchestration"]["details"]["latest_attempt"][
            "by_progressed_stage"
        ] == {"research": 1}
        assert components["workflow_retries"]["status"] == "degraded"
        assert components["workflow_retries"]["details"]["total_retry_entries"] == 3
        assert components["workflow_retries"]["details"]["historical_retry_entries"] == 4
        assert components["workflow_retries"]["details"]["resolved_retry_entries"] == 1
        assert components["workflow_retries"]["details"]["scheduled_retry_entries"] == 2
        assert components["workflow_retries"]["details"]["exhausted_retry_entries"] == 1
        assert components["workflow_retries"]["details"]["due_retry_entries"] == 1
        assert components["workflow_retries"]["details"]["backoff_retry_entries"] == 1
        assert components["workflow_retries"]["details"]["unknown_schedule_retry_entries"] == 0
        assert components["workflow_retries"]["details"]["by_status"] == {
            "exhausted": 1,
            "scheduled": 2,
        }
        assert components["workflow_retries"]["details"]["by_stage"] == {
            "render": 1,
            "voicebox": 2,
        }
        assert components["workflow_retries"]["details"]["by_schedule_status"] == {
            "backoff": 1,
            "due": 1,
            "not_scheduled": 1,
        }
        assert components["workflow_retries"]["details"]["by_due_stage"] == {"voicebox": 1}
        assert components["workflow_retries"]["details"]["by_backoff_stage"] == {"voicebox": 1}
        assert components["workflow_retries"]["details"]["by_unknown_schedule_stage"] == {}
        assert components["workflow_retries"]["details"]["by_exhausted_stage"] == {"render": 1}
        assert components["workflow_retries"]["details"]["by_resolution_status"] == {
            "operator_retried": 1
        }
        assert components["workflow_retries"]["details"]["by_resolution_stage"] == {"voicebox": 1}
        assert components["workflow_retries"]["details"]["readiness_checks"] == {
            "no_exhausted_workflow_retries": False,
            "no_scheduled_workflow_retries": False,
            "no_due_workflow_retries": False,
            "no_backoff_workflow_retries": False,
            "no_unknown_schedule_workflow_retries": True,
        }
        assert components["workflow_retries"]["details"]["failed_readiness_checks"] == [
            "no_exhausted_workflow_retries",
            "no_scheduled_workflow_retries",
            "no_due_workflow_retries",
            "no_backoff_workflow_retries",
        ]
        assert components["workflow_retries"]["details"]["latest_retry"]["retry_id"] == (
            "retry-exhausted"
        )
        assert components["workflow_retries"]["details"]["next_retry"]["retry_id"] == "retry-due"
        assert (
            components["workflow_retries"]["details"]["next_retry_not_before"]
            == (now - timedelta(seconds=60)).isoformat()
        )
        assert body["counts"]["episodes"] == 1
        assert body["counts"]["paused_episodes"] == 1
        assert body["counts"]["production_runs"] == 1
        assert body["counts"]["active_production_runs"] == 1
        assert body["counts"]["paused_active_production_runs"] == 1
        assert body["counts"]["production_runs_needing_attention"] == 1
        assert body["counts"]["workflow_orchestration_attempts"] == 1
        assert body["counts"]["workflow_orchestration_errors"] == 1
        assert body["counts"]["temporal_stage_dispatches"] == 1
        assert body["counts"]["blocked_temporal_stage_dispatches"] == 1
        assert body["counts"]["workflow_stage_retries"] == 3
        assert body["counts"]["scheduled_workflow_stage_retries"] == 2
        assert body["counts"]["exhausted_workflow_stage_retries"] == 1
        assert body["counts"]["due_workflow_stage_retries"] == 1
        assert body["counts"]["model_endpoints"] >= 1
        assert body["counts"]["active_workers"] == 0
        assert body["queues"]["pending_audio_jobs"] == 1
        assert body["queues"]["submitted_audio_jobs"] == 1
        assert body["queues"]["running_audio_jobs"] == 0
        assert body["queues"]["pending_visual_jobs"] == 0
        assert body["queues"]["submitted_visual_jobs"] == 0
        assert body["queues"]["running_visual_jobs"] == 0
        assert body["queues"]["pending_subtitle_jobs"] == 0
        assert body["queues"]["submitted_subtitle_jobs"] == 0
        assert body["queues"]["running_subtitle_jobs"] == 0
        assert body["queues"]["planned_subtitle_assets"] == 0
        assert body["queues"]["failed_assets"] == 1
        assert body["queues"]["failed_visual_assets"] == 1
        assert body["queues"]["failed_subtitle_assets"] == 0
        assert body["settings"]["object_storage_backend"] == "local"
        assert body["settings"]["object_storage_endpoint_configured"] is True
        assert body["settings"]["object_storage_bucket_configured"] is True
        assert body["settings"]["object_storage_region_configured"] is True
        assert body["settings"]["object_storage_access_key_reference_configured"] is False
        assert body["settings"]["object_storage_secret_key_reference_configured"] is False
        assert body["settings"]["object_storage_credential_pair_configured"] is True
        assert body["settings"]["object_storage_force_path_style"] is True
        assert body["settings"]["object_storage_auto_create_bucket"] is True
        assert "object_storage_endpoint" not in body["settings"]
        assert "object_storage_bucket" not in body["settings"]
        assert "object_storage_access_key_reference" not in body["settings"]
        assert "object_storage_secret_key_reference" not in body["settings"]
        assert body["settings"]["runtime_state_path_configured"] is True
        assert body["settings"]["backup_path_configured"] is True
        assert "runtime_state_path" not in body["settings"]
        assert "backup_path" not in body["settings"]
        assert body["settings"]["auth_trusted_identity_enabled"] is False
        assert body["settings"]["auth_trusted_identity_header"] == "x-forwarded-user"
        assert body["settings"]["auth_trusted_email_header"] == "x-forwarded-email"
        assert body["settings"]["auth_trusted_groups_header"] == "x-forwarded-groups"
        assert body["settings"]["auth_trusted_default_role"] == "viewer"
        assert body["settings"]["auth_trusted_group_role_map_configured"] is False
        assert body["settings"]["auth_provider_session_enabled"] is False
        assert body["settings"]["auth_provider_session_introspection_configured"] is False
        assert body["settings"]["auth_provider_session_client_id_reference_configured"] is False
        assert body["settings"]["auth_provider_session_client_secret_reference_configured"] is False
        assert body["settings"]["auth_provider_session_token_header"] == "authorization"
        assert body["settings"]["auth_provider_session_user_claim"] == "sub"
        assert body["settings"]["auth_provider_session_groups_claim"] == "groups"
        assert body["settings"]["auth_provider_session_default_role"] == "viewer"
        assert body["settings"]["auth_provider_session_decision_log_configured"] is False
        assert body["settings"]["auth_provider_session_decision_log_limit"] == 100
        assert body["settings"]["temporal_backend_mode"] == "local"
        assert body["settings"]["temporal_signal_transport_enabled"] is False
        assert body["settings"]["publisher_automated_live_enabled"] is False
        assert body["counts"]["automated_live_publisher_targets"] == 0
        assert body["settings"]["worker_heartbeat_ttl_seconds"] == 90
        assert body["settings"]["worker_auto_start_production_runs_enabled"] is False

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_deployment_readiness_status{env="development",status="healthy"} 1'
            in metrics.text
        )
        assert "dialecticore_deployment_readiness_issues 0" in metrics.text
        assert 'dialecticore_runtime_path_ready{name="backup",required="true"} 1' in (metrics.text)
        assert (
            'dialecticore_runtime_path_state{name="backup",required="true",'
            'state="parent_exists"} 1' in metrics.text
        )
        assert (
            'dialecticore_runtime_path_state{name="backup",required="true",'
            'state="writable_target_or_parent"} 1' in metrics.text
        )
        assert (
            'dialecticore_runtime_path_ready{name="runtime_state",required="true"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_runtime_path_ready{name="object_storage_local",required="true"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_runtime_path_state{name="object_storage_local",'
            'required="true",state="free_bytes_sufficient"} 1' in metrics.text
        )
        assert (
            "dialecticore_object_storage_local_path_ready"
            f'{{backend="local",bucket="dialecticore",checked_path="{tmp_path}"}} 1' in metrics.text
        )
        assert (
            "dialecticore_object_storage_local_path_state"
            f'{{backend="local",bucket="dialecticore",checked_path="{tmp_path}",'
            'state="writable_target_or_parent"} 1' in metrics.text
        )
        assert (
            'dialecticore_runtime_path_free_bytes_sufficient{name="backup",required="true"} 1'
            in metrics.text
        )
        assert 'dialecticore_runtime_path_free_bytes{name="backup",required="true"}' in (
            metrics.text
        )
        assert 'dialecticore_publisher_target_health_status{status="healthy"} 1' in metrics.text
        assert 'dialecticore_publisher_target_count{kind="enabled"} 1' in metrics.text
        assert 'dialecticore_publisher_target_count{kind="live_enabled"} 0' in metrics.text
        assert (
            'dialecticore_publisher_target_count{kind="automated_live_capable_enabled"} 0'
            in metrics.text
        )
        assert 'dialecticore_publisher_target_count{kind="mock_enabled"} 1' in (metrics.text)
        assert 'dialecticore_publisher_target_count{kind="issues"} 0' in metrics.text
        assert 'dialecticore_production_run_health_status{status="degraded"} 1' in metrics.text
        assert 'dialecticore_production_run_count{kind="total"} 1' in metrics.text
        assert 'dialecticore_production_run_count{kind="active"} 1' in metrics.text
        assert 'dialecticore_production_run_count{kind="paused_active"} 1' in metrics.text
        assert 'dialecticore_production_run_count{kind="attention"} 1' in metrics.text
        assert (
            'dialecticore_workflow_orchestration_count{dimension="attempts",value="all"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_workflow_orchestration_count{dimension="errors",value="all"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_workflow_orchestration_count{dimension="dispatch",value="blocked"} 1'
            in metrics.text
        )
        assert (
            "dialecticore_workflow_orchestration_count"
            '{dimension="worker",value="workflow-worker"} 1' in metrics.text
        )
        assert 'dialecticore_workflow_stage_retry_count{dimension="total",value="all"} 3' in (
            metrics.text
        )
        assert 'dialecticore_workflow_stage_retry_count{dimension="history",value="all"} 4' in (
            metrics.text
        )
        assert (
            'dialecticore_workflow_stage_retry_count{dimension="history_status",value="resolved"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_workflow_stage_retry_count{dimension="status",value="scheduled"} 2'
            in metrics.text
        )
        assert (
            'dialecticore_workflow_stage_retry_count{dimension="status",value="exhausted"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_workflow_stage_retry_count{dimension="stage",value="voicebox"} 2'
            in metrics.text
        )
        assert (
            'dialecticore_workflow_stage_retry_count{dimension="schedule_status",value="due"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_workflow_stage_retry_count{dimension="schedule_status",value="backoff"} 1'
            in metrics.text
        )
        assert (
            "dialecticore_workflow_stage_retry_count"
            '{dimension="schedule_status",value="not_scheduled"} 1' in metrics.text
        )
        assert (
            "dialecticore_workflow_stage_retry_count"
            '{dimension="resolution_status",value="operator_retried"} 1' in metrics.text
        )
        assert (
            "dialecticore_workflow_stage_retry_count"
            '{dimension="resolution_stage",value="voicebox"} 1' in metrics.text
        )

        retry_backlog = client.get("/api/v1/system/workflow-retries?limit=2")
        assert retry_backlog.status_code == 200
        retry_body = retry_backlog.json()
        assert retry_body["schema_version"] == "workflow_retry_backlog.v1"
        assert retry_body["summary"]["total_retry_entries"] == 3
        assert retry_body["summary"]["historical_retry_entries"] == 4
        assert retry_body["summary"]["resolved_retry_entries"] == 1
        assert retry_body["summary"]["due_retry_entries"] == 1
        assert retry_body["summary"]["backoff_retry_entries"] == 1
        assert retry_body["limit"] == 2
        assert retry_body["truncated"] is True
        assert [entry["retry_id"] for entry in retry_body["entries"]] == [
            "retry-due",
            "retry-backoff",
        ]
        assert [entry["schedule_status"] for entry in retry_body["entries"]] == [
            "due",
            "backoff",
        ]

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        live_body = live_readiness.json()
        live_checks = {check["category"]: check for check in live_body["checks"]}
        assert live_checks["production_runs"]["status"] == "warning"
        assert live_checks["production_runs"]["details"]["schema_version"] == (
            "production_run_readiness.v1"
        )
        assert live_checks["production_runs"]["details"]["active_production_runs"] == 1
        assert live_checks["production_runs"]["details"]["paused_active_production_runs"] == 1
        assert live_checks["production_runs"]["details"]["attention_count"] == 1
        assert live_checks["production_runs"]["details"]["by_state"] == {"untracked": 1}
        assert live_checks["production_runs"]["details"]["by_attention_reason"] == {"paused": 1}
        assert live_checks["production_runs"]["details"]["readiness_checks"] == {
            "no_failed_active_production_runs": True,
            "no_cancelled_active_production_runs": True,
            "no_paused_active_production_runs": False,
            "no_running_active_production_runs": True,
            "no_completion_blocked_production_runs": True,
            "no_production_runs_waiting_for_action": True,
        }
        assert live_checks["production_runs"]["details"]["failed_readiness_checks"] == [
            "no_paused_active_production_runs"
        ]
        assert live_checks["production_runs"]["details"]["attention_runs"][0][
            "attention_reasons"
        ] == ["paused"]
        assert live_checks["production_runs"]["details"]["latest_run"]["paused"] is True
        assert live_checks["production_runs"]["details"]["latest_run"]["stage_history_count"] == 0
        assert (
            "one or more active production runs are paused"
            in live_checks["production_runs"]["warnings"]
        )
        assert live_checks["workflow_orchestration"]["status"] == "fail"
        assert (
            live_checks["workflow_orchestration"]["details"]["schema_version"]
            == "workflow_orchestration_summary.v1"
        )
        assert live_checks["workflow_orchestration"]["details"]["attempt_count"] == 1
        assert live_checks["workflow_orchestration"]["details"]["error_count"] == 1
        assert live_checks["workflow_orchestration"]["details"]["failed_stage_count"] == 1
        assert live_checks["workflow_orchestration"]["details"]["blocked_dispatch_count"] == 1
        assert (
            live_checks["workflow_orchestration"]["details"]["blocked_production_handoff_count"]
            == 1
        )
        assert live_checks["workflow_orchestration"]["details"]["by_failed_stage"] == {
            "localization": 1
        }
        assert live_checks["workflow_orchestration"]["details"]["by_blocked_dispatch_stage"] == {
            "localization": 1
        }
        assert live_checks["workflow_orchestration"]["details"]["attention_count"] == 4
        assert live_checks["workflow_orchestration"]["details"]["readiness_checks"] == {
            "no_workflow_orchestration_errors": False,
            "no_failed_workflow_stages": False,
            "no_blocked_temporal_dispatches": False,
            "no_blocked_production_handoffs": True,
            "no_production_handoffs_waiting_for_action": True,
        }
        assert live_checks["workflow_orchestration"]["details"]["failed_readiness_checks"] == [
            "no_workflow_orchestration_errors",
            "no_failed_workflow_stages",
            "no_blocked_temporal_dispatches",
        ]
        assert (
            "workflow orchestration has recorded stage errors"
            in live_checks["workflow_orchestration"]["blockers"]
        )
        assert (
            "workflow orchestration has blocked Temporal dispatches"
            in live_checks["workflow_orchestration"]["blockers"]
        )
        assert live_checks["workflow_retries"]["status"] == "fail"
        assert (
            live_checks["workflow_retries"]["details"]["schema_version"]
            == "workflow_retry_summary.v1"
        )
        assert live_checks["workflow_retries"]["details"]["total_retry_entries"] == 3
        assert live_checks["workflow_retries"]["details"]["historical_retry_entries"] == 4
        assert live_checks["workflow_retries"]["details"]["resolved_retry_entries"] == 1
        assert live_checks["workflow_retries"]["details"]["scheduled_retry_entries"] == 2
        assert live_checks["workflow_retries"]["details"]["exhausted_retry_entries"] == 1
        assert live_checks["workflow_retries"]["details"]["due_retry_entries"] == 1
        assert live_checks["workflow_retries"]["details"]["by_due_stage"] == {"voicebox": 1}
        assert live_checks["workflow_retries"]["details"]["by_backoff_stage"] == {"voicebox": 1}
        assert live_checks["workflow_retries"]["details"]["by_exhausted_stage"] == {"render": 1}
        assert live_checks["workflow_retries"]["details"]["readiness_checks"] == {
            "no_exhausted_workflow_retries": False,
            "no_scheduled_workflow_retries": False,
            "no_due_workflow_retries": False,
            "no_backoff_workflow_retries": False,
            "no_unknown_schedule_workflow_retries": True,
        }
        assert live_checks["workflow_retries"]["details"]["failed_readiness_checks"] == [
            "no_exhausted_workflow_retries",
            "no_scheduled_workflow_retries",
            "no_due_workflow_retries",
            "no_backoff_workflow_retries",
        ]
        assert live_checks["workflow_retries"]["details"]["pending_retry_entries"] == 2
        assert (
            "one or more workflow stage retries are exhausted"
            in live_checks["workflow_retries"]["blockers"]
        )
        assert live_checks["media_queues"]["status"] == "fail"
        assert live_checks["media_queues"]["details"]["schema_version"] == (
            "media_queue_readiness.v1"
        )
        assert live_checks["media_queues"]["details"]["failed_assets"] == 1
        assert live_checks["media_queues"]["details"]["failed_visual_assets"] == 1
        assert live_checks["media_queues"]["details"]["pending_audio_jobs"] == 1
        assert live_checks["media_queues"]["details"]["submitted_audio_jobs"] == 1
        assert live_checks["media_queues"]["details"]["running_audio_jobs"] == 0
        assert live_checks["media_queues"]["details"]["pending_visual_jobs"] == 0
        assert live_checks["media_queues"]["details"]["submitted_visual_jobs"] == 0
        assert live_checks["media_queues"]["details"]["running_visual_jobs"] == 0
        assert live_checks["media_queues"]["details"]["pending_subtitle_jobs"] == 0
        assert live_checks["media_queues"]["details"]["submitted_subtitle_jobs"] == 0
        assert live_checks["media_queues"]["details"]["running_subtitle_jobs"] == 0
        assert live_checks["media_queues"]["details"]["planned_audio_assets"] == 0
        assert live_checks["media_queues"]["details"]["planned_visual_assets"] == 0
        assert live_checks["media_queues"]["details"]["planned_subtitle_assets"] == 0
        assert live_checks["media_queues"]["details"]["failed_subtitle_assets"] == 0
        assert (
            live_checks["media_queues"]["details"]["live_readiness_policy"]
            == "current failed audio, subtitle, and visual media assets block live runs; "
            "current pending media jobs warn; render attempts have their own lifecycle and "
            "terminal cancelled or failed episode assets remain in historical counts only"
        )
        assert live_checks["media_queues"]["details"]["pending_job_count"] == 1
        assert live_checks["media_queues"]["details"]["attention_count"] == 2
        assert live_checks["media_queues"]["details"]["readiness_checks"] == {
            "no_failed_media_assets": False,
            "no_pending_audio_jobs": False,
            "no_pending_visual_jobs": True,
            "no_pending_subtitle_jobs": True,
        }
        assert live_checks["media_queues"]["details"]["failed_readiness_checks"] == [
            "no_failed_media_assets",
            "no_pending_audio_jobs",
        ]
        assert "one or more media assets are failed" in live_checks["media_queues"]["blockers"]
        assert (
            "one or more media jobs are pending or running"
            in live_checks["media_queues"]["warnings"]
        )

        orchestration = client.get("/api/v1/system/workflow-orchestration?limit=1")
        assert orchestration.status_code == 200
        orchestration_body = orchestration.json()
        assert orchestration_body["schema_version"] == "workflow_orchestration_evidence.v1"
        assert orchestration_body["summary"]["attempt_count"] == 1
        assert orchestration_body["summary"]["blocked_dispatch_count"] == 1
        assert orchestration_body["summary"]["blocked_production_handoff_count"] == 1
        assert orchestration_body["limit"] == 1
        assert orchestration_body["truncated"] is False
        assert orchestration_body["attempts"][0]["summary_id"] == "summary-1"
        assert orchestration_body["attempts"][0]["failed_stage_count"] == 1
        assert orchestration_body["attempts"][0]["production_handoff"]["status"] == "blocked"
        assert orchestration_body["dispatches"][0]["dispatch_id"] == "dispatch-blocked"
        assert orchestration_body["dispatches"][0]["status"] == "blocked"
    finally:
        app.dependency_overrides.clear()


def test_workflow_orchestration_readiness_ignores_resolved_historical_attempts(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        earlier = datetime.now(UTC) - timedelta(minutes=5)
        later = datetime.now(UTC)
        episode.workflow_control = {
            "worker_orchestration_log": [
                {
                    "schema_version": "workflow_worker_orchestration_attempt.v1",
                    "summary_id": "summary-blocked",
                    "attempt_sequence": 1,
                    "recorded_at": earlier.isoformat(),
                    "worker_id": "workflow-worker",
                    "policy": "local_stage_worker_orchestrator_v1",
                    "progressed_stage_count": 0,
                    "error_count": 1,
                    "stage_attempts": [
                        {
                            "stage": "audio",
                            "status": "failed",
                            "progress_count": 0,
                            "error_count": 1,
                        },
                    ],
                    "temporal_dispatch_count": 1,
                    "production_handoff": {
                        "schema_version": "talkshow_production_handoff.v1",
                        "episode_id": str(episode.id),
                        "status": "blocked",
                        "blocking_reasons": ["completed_audio_missing"],
                    },
                },
                {
                    "schema_version": "workflow_worker_orchestration_attempt.v1",
                    "summary_id": "summary-delivery-ready",
                    "attempt_sequence": 2,
                    "recorded_at": later.isoformat(),
                    "worker_id": "workflow-worker",
                    "policy": "local_stage_worker_orchestrator_v1",
                    "progressed_stage_count": 2,
                    "error_count": 0,
                    "stage_attempts": [
                        {
                            "stage": "audio",
                            "status": "progressed",
                            "progress_count": 1,
                            "error_count": 0,
                        },
                        {
                            "stage": "completion",
                            "status": "progressed",
                            "progress_count": 1,
                            "error_count": 0,
                        },
                    ],
                    "temporal_dispatch_count": 0,
                    "production_handoff": {
                        "schema_version": "talkshow_production_handoff.v1",
                        "episode_id": str(episode.id),
                        "status": "delivery_ready",
                        "blocking_reasons": [],
                    },
                },
            ],
            "temporal_stage_dispatch_log": [
                {
                    "schema_version": "temporal_stage_dispatch.v1",
                    "dispatch_id": "dispatch-blocked",
                    "dispatch_sequence": 1,
                    "requested_at": (earlier + timedelta(seconds=5)).isoformat(),
                    "requested_by": "workflow-worker",
                    "status": "blocked",
                    "stage": "audio",
                    "activity_name": "dialecticore.production.audio",
                    "namespace": "default",
                    "task_queue": "",
                }
            ],
        }
        repository.save(episode)

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        orchestration = components["workflow_orchestration"]
        assert orchestration["status"] == "healthy"
        assert orchestration["details"]["error_count"] == 1
        assert orchestration["details"]["failed_stage_count"] == 1
        assert orchestration["details"]["blocked_dispatch_count"] == 1
        assert orchestration["details"]["blocked_production_handoff_count"] == 1
        assert orchestration["details"]["current_error_count"] == 0
        assert orchestration["details"]["current_failed_stage_count"] == 0
        assert orchestration["details"]["current_blocked_dispatch_count"] == 0
        assert orchestration["details"]["current_blocked_production_handoff_count"] == 0
        assert orchestration["details"]["readiness_checks"] == {
            "no_workflow_orchestration_errors": True,
            "no_failed_workflow_stages": True,
            "no_blocked_temporal_dispatches": True,
            "no_blocked_production_handoffs": True,
            "no_production_handoffs_waiting_for_action": True,
        }
        assert orchestration["details"]["failed_readiness_checks"] == []

        live = client.get("/api/v1/system/live-provider-readiness")
        assert live.status_code == 200
        live_checks = {check["category"]: check for check in live.json()["checks"]}
        assert live_checks["workflow_orchestration"]["status"] == "pass"
        assert live_checks["workflow_orchestration"]["blockers"] == []
        assert live_checks["workflow_orchestration"]["details"]["error_count"] == 1
        assert live_checks["workflow_orchestration"]["details"]["current_error_count"] == 0
    finally:
        app.dependency_overrides.clear()


def test_workflow_orchestration_readiness_ignores_terminal_cancelled_attempts(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        episode.status = EpisodeStatus.cancelled
        recorded_at = datetime.now(UTC)
        episode.workflow_control = {
            "worker_orchestration_log": [
                {
                    "schema_version": "workflow_worker_orchestration_attempt.v1",
                    "summary_id": "summary-cancelled-blocked",
                    "attempt_sequence": 1,
                    "recorded_at": recorded_at.isoformat(),
                    "worker_id": "workflow-worker",
                    "policy": "local_stage_worker_orchestrator_v1",
                    "progressed_stage_count": 0,
                    "error_count": 1,
                    "stage_attempts": [
                        {
                            "stage": "audio",
                            "status": "failed",
                            "progress_count": 0,
                            "error_count": 1,
                        },
                    ],
                    "temporal_dispatch_count": 1,
                    "production_handoff": {
                        "schema_version": "talkshow_production_handoff.v1",
                        "episode_id": str(episode.id),
                        "status": "blocked",
                        "blocking_reasons": ["completed_audio_missing"],
                    },
                },
            ],
            "temporal_stage_dispatch_log": [
                {
                    "schema_version": "temporal_stage_dispatch.v1",
                    "dispatch_id": "dispatch-cancelled-blocked",
                    "dispatch_sequence": 1,
                    "requested_at": recorded_at.isoformat(),
                    "requested_by": "workflow-worker",
                    "status": "blocked",
                    "stage": "audio",
                    "activity_name": "dialecticore.production.audio",
                    "namespace": "default",
                    "task_queue": "",
                }
            ],
        }
        repository.save(episode)

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        orchestration = components["workflow_orchestration"]
        assert orchestration["status"] == "healthy"
        assert orchestration["details"]["error_count"] == 1
        assert orchestration["details"]["failed_stage_count"] == 1
        assert orchestration["details"]["blocked_dispatch_count"] == 1
        assert orchestration["details"]["blocked_production_handoff_count"] == 1
        assert orchestration["details"]["current_attempt_count"] == 0
        assert orchestration["details"]["current_error_count"] == 0
        assert orchestration["details"]["current_failed_stage_count"] == 0
        assert orchestration["details"]["current_blocked_dispatch_count"] == 0
        assert orchestration["details"]["current_blocked_production_handoff_count"] == 0

        live = client.get("/api/v1/system/live-provider-readiness")
        assert live.status_code == 200
        live_checks = {check["category"]: check for check in live.json()["checks"]}
        assert live_checks["workflow_orchestration"]["status"] == "pass"
        assert live_checks["workflow_orchestration"]["blockers"] == []
        assert live_checks["workflow_orchestration"]["details"]["error_count"] == 1
        assert live_checks["workflow_orchestration"]["details"]["current_error_count"] == 0
    finally:
        app.dependency_overrides.clear()


def test_system_health_reports_database_migration_revision(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'dialecticore.db'}"
    _stamp_alembic_revision(database_url, "0011_language_profile_records")
    repository = _persistent_repository(database_url)
    settings = Settings(
        database_url=database_url,
        object_storage_local_path=str(tmp_path / "object-store"),
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        migrations = components["database_migrations"]
        assert migrations["status"] == "healthy"
        assert migrations["details"]["schema_version"] == ("database_migrations_readiness.v1")
        assert migrations["details"]["current_revisions"] == ["0011_language_profile_records"]
        assert migrations["details"]["head_revisions"] == ["0011_language_profile_records"]
        assert migrations["details"]["readiness_checks"] == {
            "migration_revision_check_available": True,
            "migration_heads_configured": True,
            "database_revision_present": True,
            "database_schema_at_head": True,
        }
        assert migrations["details"]["failed_readiness_checks"] == []

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_database_migration_status"
            '{status="healthy",enforced="false",'
            'current="0011_language_profile_records",'
            'head="0011_language_profile_records"} 1'
        ) in metrics.text
    finally:
        app.dependency_overrides.clear()


def test_production_health_and_live_readiness_fail_when_database_revision_is_missing(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'unmigrated.db'}"
    repository = _persistent_repository(database_url)
    settings = Settings(
        env="production",
        database_url=database_url,
        object_storage_local_path=str(tmp_path / "object-store"),
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        migrations = components["database_migrations"]
        assert migrations["status"] == "degraded"
        assert migrations["details"]["enforced"] is True
        assert migrations["details"]["current_revisions"] == []
        assert migrations["details"]["head_revisions"] == ["0011_language_profile_records"]
        assert migrations["details"]["readiness_checks"]["database_revision_present"] is False
        assert migrations["details"]["readiness_checks"]["database_schema_at_head"] is False
        assert migrations["details"]["failed_readiness_checks"] == [
            "database_revision_present",
            "database_schema_at_head",
        ]

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        database_check = next(
            check
            for check in live_readiness.json()["checks"]
            if check["category"] == "database_migrations"
        )
        assert database_check["status"] == "fail"
        assert "database has no Alembic revision recorded" in live_readiness.json()["blockers"]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_database_migration_status"
            '{status="degraded",enforced="true",current="",'
            'head="0011_language_profile_records"} 0'
        ) in metrics.text
    finally:
        app.dependency_overrides.clear()


def test_production_health_and_live_readiness_fail_when_database_revision_is_behind(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'behind-head.db'}"
    repository = _persistent_repository(database_url)
    _stamp_alembic_revision(database_url, "0010_asset_records")
    settings = Settings(
        env="production",
        database_url=database_url,
        object_storage_local_path=str(tmp_path / "object-store"),
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        migrations = components["database_migrations"]
        assert migrations["status"] == "degraded"
        assert migrations["details"]["enforced"] is True
        assert migrations["details"]["current_revisions"] == ["0010_asset_records"]
        assert migrations["details"]["head_revisions"] == ["0011_language_profile_records"]
        assert migrations["details"]["readiness_checks"] == {
            "migration_revision_check_available": True,
            "migration_heads_configured": True,
            "database_revision_present": True,
            "database_schema_at_head": False,
        }
        assert migrations["details"]["failed_readiness_checks"] == ["database_schema_at_head"]
        assert (
            migrations["details"]["reason"]
            == "database migration revision does not match Alembic head"
        )

        deployment = components["deployment_readiness"]
        assert deployment["details"]["checks"]["database_schema_at_head"] is False
        assert "database_schema_at_head" in deployment["details"]["failed_readiness_checks"]
        assert (
            "production database schema must match the current Alembic head"
            in (deployment["details"]["issues"])
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        live_body = live_readiness.json()
        database_check = next(
            check for check in live_body["checks"] if check["category"] == "database_migrations"
        )
        assert database_check["status"] == "fail"
        assert "database migration revision does not match Alembic head" in live_body["blockers"]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_database_migration_status"
            '{status="degraded",enforced="true",current="0010_asset_records",'
            'head="0011_language_profile_records"} 0'
        ) in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="database_schema_at_head",status="fail"} 1'
        ) in metrics.text
    finally:
        app.dependency_overrides.clear()


def test_system_health_degrades_endpoint_collections_with_unhealthy_or_unknown_records(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository.upsert_model_endpoint(
        ModelEndpoint(
            id="unhealthy-model",
            name="Unhealthy Model",
            health_status="unhealthy",
            enabled=True,
        )
    )
    repository.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="unknown-voicebox",
            name="Unknown Voicebox",
            health_status="unknown",
            enabled=True,
        )
    )
    repository.upsert_comfyui_endpoint(
        ComfyUiEndpoint(
            id="failed-comfyui",
            name="Failed ComfyUI",
            health_status="failed",
            enabled=True,
        )
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
        components = {component["name"]: component for component in response.json()["components"]}

        model_endpoints = components["model_endpoints"]
        assert model_endpoints["status"] == "degraded"
        assert model_endpoints["details"]["unhealthy"] == 1
        assert model_endpoints["details"]["unknown"] == 0
        assert model_endpoints["details"]["readiness_checks"] == {
            "endpoints_configured": True,
            "enabled_endpoint_available": True,
            "enabled_endpoints_not_unhealthy": False,
            "enabled_endpoints_health_known": True,
        }
        assert model_endpoints["details"]["failed_readiness_checks"] == [
            "enabled_endpoints_not_unhealthy"
        ]

        voicebox_endpoints = components["voicebox_endpoints"]
        assert voicebox_endpoints["status"] == "degraded"
        assert voicebox_endpoints["details"]["unhealthy"] == 0
        assert voicebox_endpoints["details"]["unknown"] == 1
        assert voicebox_endpoints["details"]["readiness_checks"] == {
            "endpoints_configured": True,
            "enabled_endpoint_available": True,
            "enabled_endpoints_not_unhealthy": True,
            "enabled_endpoints_health_known": False,
        }
        assert voicebox_endpoints["details"]["failed_readiness_checks"] == [
            "enabled_endpoints_health_known"
        ]

        comfyui_endpoints = components["comfyui_endpoints"]
        assert comfyui_endpoints["status"] == "degraded"
        assert comfyui_endpoints["details"]["unhealthy"] == 1
        assert comfyui_endpoints["details"]["unknown"] == 0
        assert comfyui_endpoints["details"]["readiness_checks"] == {
            "endpoints_configured": True,
            "enabled_endpoint_available": True,
            "enabled_endpoints_not_unhealthy": False,
            "enabled_endpoints_health_known": True,
        }
        assert comfyui_endpoints["details"]["failed_readiness_checks"] == [
            "enabled_endpoints_not_unhealthy"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_component_readiness_check"
            '{component="model_endpoints",check="enabled_endpoints_not_unhealthy",'
            'status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_component_readiness_check"
            '{component="voicebox_endpoints",check="enabled_endpoints_health_known",'
            'status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_component_readiness_check"
            '{component="comfyui_endpoints",check="enabled_endpoint_available",'
            'status="pass"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_live_provider_readiness_passes_worker_registry_when_roles_are_covered(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    worker_status = WorkerStatusService(settings)
    for role in configured_worker_roles(settings):
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role=role,
                worker_id=f"{role}-worker",
                status="running",
            )
        )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: worker_status
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    client = TestClient(app)

    try:
        response = client.get("/api/v1/system/live-provider-readiness")

        assert response.status_code == 200
        body = response.json()
        checks = {check["category"]: check for check in body["checks"]}
        worker_registry = checks["worker_registry"]
        assert worker_registry["status"] == "pass"
        assert worker_registry["warnings"] == []
        assert worker_registry["blockers"] == []
        assert worker_registry["details"]["failed_readiness_checks"] == []
        assert worker_registry["details"]["active_roles"] == len(configured_worker_roles(settings))
        assert worker_registry["details"]["missing_role_names"] == []
        assert "worker registry has active heartbeat evidence" not in body["warnings"]
    finally:
        app.dependency_overrides.clear()


def test_system_health_and_metrics_report_production_deployment_readiness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    database_url = f"sqlite:///{tmp_path / 'dialecticore.db'}"
    _stamp_alembic_revision(database_url, "0011_language_profile_records")
    monkeypatch.setenv("DIALECTICORE_API_KEY", "change-me-before-enabling-auth")
    monkeypatch.setenv("MINIO_ROOT_USER", "dialecticore")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "change-me-in-production")
    monkeypatch.setenv("POSTGRES_PASSWORD", "dialecticore")
    settings = Settings(
        env="production",
        database_url=database_url,
        object_storage_backend="local",
        object_storage_local_path=str(tmp_path / "object-store"),
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=False,
        redis_event_fanout_enabled=False,
        redis_worker_signal_enabled=False,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        deployment = next(
            component
            for component in health.json()["components"]
            if component["name"] == "deployment_readiness"
        )
        assert deployment["status"] == "degraded"
        assert deployment["details"]["schema_version"] == "deployment_readiness.v1"
        assert deployment["details"]["production_mode"] is True
        assert deployment["details"]["database_driver"] == "sqlite"
        assert deployment["details"]["object_storage_backend"] == "local"
        assert deployment["details"]["temporal_runtime_contract"]["configured"] is True
        assert deployment["details"]["issue_count"] == 11
        assert deployment["details"]["unsafe_default_secret_labels"] == [
            "DIALECTICORE_API_KEY",
            "MINIO_ROOT_PASSWORD",
            "MINIO_ROOT_USER",
            "POSTGRES_PASSWORD",
        ]
        assert deployment["details"]["runtime_paths"]["backup"]["writable_target_or_parent"] is True
        assert (
            deployment["details"]["runtime_paths"]["runtime_state"]["writable_target_or_parent"]
            is True
        )
        assert deployment["details"]["checks"]["database_persistent"] is False
        assert deployment["details"]["checks"]["database_schema_at_head"] is True
        assert deployment["details"]["checks"]["cors_origin_restricted"] is False
        assert deployment["details"]["checks"]["auth_enabled"] is False
        assert deployment["details"]["checks"]["auth_mode_configured"] is False
        assert deployment["details"]["checks"]["initial_admin_path_configured"] is True
        assert deployment["details"]["checks"]["object_storage_remote"] is False
        assert deployment["details"]["checks"]["object_storage_endpoint_configured"] is True
        assert deployment["details"]["checks"]["object_storage_bucket_configured"] is True
        assert deployment["details"]["checks"]["object_storage_credential_pair_configured"] is True
        assert deployment["details"]["checks"]["redis_runtime_enabled"] is False
        assert deployment["details"]["checks"]["redis_url_configured"] is True
        assert deployment["details"]["checks"]["redis_runtime_channels_configured"] is True
        assert deployment["details"]["checks"]["worker_heartbeat_ttl_covers_poll_interval"] is True
        assert deployment["details"]["checks"]["worker_lease_ttl_covers_poll_interval"] is True
        assert deployment["details"]["checks"]["temporal_runtime_contract_valid"] is True
        assert deployment["details"]["checks"]["temporal_runtime_contract_configured"] is True
        assert deployment["details"]["checks"]["publisher_automated_live_target_available"] is True
        assert deployment["details"]["checks"]["publisher_target_enabled"] is True
        assert deployment["details"]["checks"]["publisher_live_target_enabled"] is False
        assert deployment["details"]["checks"]["publisher_targets_not_unhealthy"] is True
        assert deployment["details"]["checks"]["publisher_target_health_known"] is True
        assert deployment["details"]["publisher_target_summary"] == {
            "configured": 2,
            "enabled": 1,
            "live_enabled": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert deployment["details"]["checks"]["model_provider_endpoint_enabled"] is True
        assert deployment["details"]["checks"]["model_provider_remote_endpoint_enabled"] is False
        assert deployment["details"]["checks"]["model_provider_remote_endpoint_configured"] is True
        assert deployment["details"]["checks"]["model_provider_endpoints_not_unhealthy"] is True
        assert deployment["details"]["checks"]["model_provider_endpoint_health_known"] is True
        assert deployment["details"]["model_provider_summary"] == {
            "configured": 1,
            "enabled": 1,
            "remote_enabled": 0,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert deployment["details"]["checks"]["voicebox_endpoint_enabled"] is True
        assert deployment["details"]["checks"]["voicebox_remote_endpoint_enabled"] is False
        assert deployment["details"]["checks"]["voicebox_remote_endpoint_configured"] is True
        assert deployment["details"]["checks"]["voicebox_endpoints_not_unhealthy"] is True
        assert deployment["details"]["checks"]["voicebox_endpoint_health_known"] is True
        assert deployment["details"]["voicebox_summary"] == {
            "configured": 1,
            "enabled": 1,
            "remote_enabled": 0,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert deployment["details"]["checks"]["comfyui_endpoint_enabled"] is True
        assert deployment["details"]["checks"]["comfyui_remote_endpoint_enabled"] is False
        assert deployment["details"]["checks"]["comfyui_remote_endpoint_configured"] is True
        assert deployment["details"]["checks"]["comfyui_endpoints_not_unhealthy"] is True
        assert deployment["details"]["checks"]["comfyui_endpoint_health_known"] is True
        assert deployment["details"]["comfyui_summary"] == {
            "configured": 1,
            "enabled": 1,
            "remote_enabled": 0,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert deployment["details"]["checks"]["backup_path_writable"] is True
        assert deployment["details"]["checks"]["runtime_state_path_writable"] is True
        assert deployment["details"]["checks"]["object_storage_local_path_writable"] is True
        assert deployment["details"]["checks"]["unsafe_default_secrets_replaced"] is False
        assert deployment["details"]["readiness_checks"] == {
            "database_url_resolved": True,
            "database_persistent": False,
            "database_schema_at_head": True,
            "cors_origin_restricted": False,
            "auth_enabled": False,
            "auth_mode_configured": False,
            "auth_api_key_reference_available": True,
            "initial_admin_path_configured": True,
            "object_storage_remote": False,
            "object_storage_endpoint_configured": True,
            "object_storage_bucket_configured": True,
            "object_storage_credential_pair_configured": True,
            "redis_runtime_enabled": False,
            "redis_url_configured": True,
            "redis_runtime_channels_configured": True,
            "worker_heartbeat_ttl_covers_poll_interval": True,
            "worker_lease_ttl_covers_poll_interval": True,
            "backup_path_configured": True,
            "runtime_state_path_configured": True,
            "backup_path_writable": True,
            "runtime_state_path_writable": True,
            "runtime_paths_free_space_sufficient": True,
            "object_storage_local_path_writable": True,
            "unsafe_default_secrets_replaced": False,
            "temporal_runtime_contract_valid": True,
            "temporal_runtime_contract_configured": True,
            "publisher_automated_live_target_available": True,
            "publisher_target_enabled": True,
            "publisher_live_target_enabled": False,
            "publisher_targets_not_unhealthy": True,
            "publisher_target_health_known": True,
            "model_provider_endpoint_enabled": True,
            "model_provider_remote_endpoint_enabled": False,
            "model_provider_remote_endpoint_configured": True,
            "model_provider_endpoints_not_unhealthy": True,
            "model_provider_endpoint_health_known": True,
            "voicebox_endpoint_enabled": True,
            "voicebox_remote_endpoint_enabled": False,
            "voicebox_remote_endpoint_configured": True,
            "voicebox_endpoints_not_unhealthy": True,
            "voicebox_endpoint_health_known": True,
            "comfyui_endpoint_enabled": True,
            "comfyui_remote_endpoint_enabled": False,
            "comfyui_remote_endpoint_configured": True,
            "comfyui_endpoints_not_unhealthy": True,
            "comfyui_endpoint_health_known": True,
        }
        assert deployment["details"]["failed_readiness_checks"] == [
            "database_persistent",
            "cors_origin_restricted",
            "auth_enabled",
            "auth_mode_configured",
            "object_storage_remote",
            "redis_runtime_enabled",
            "unsafe_default_secrets_replaced",
            "publisher_live_target_enabled",
            "model_provider_remote_endpoint_enabled",
            "voicebox_remote_endpoint_enabled",
            "comfyui_remote_endpoint_enabled",
        ]
        assert "production deployments should not use sqlite" in deployment["details"]["issues"]
        assert (
            "production deployments should enable authentication"
            in (deployment["details"]["issues"])
        )
        assert (
            "production deployments must replace placeholder/default secrets"
            in (deployment["details"]["issues"])
        )
        assert (
            "production API CORS origins should be restricted" in (deployment["details"]["issues"])
        )
        assert (
            "production model routing needs an enabled non-mock model endpoint"
            in deployment["details"]["issues"]
        )
        assert (
            "production audio synthesis needs an enabled non-mock Voicebox endpoint"
            in deployment["details"]["issues"]
        )
        assert (
            "production visual generation needs an enabled non-mock ComfyUI endpoint"
            in deployment["details"]["issues"]
        )
        assert (
            "production publishing needs an enabled non-mock, non-dry-run publisher target"
            in deployment["details"]["issues"]
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_deployment_readiness_status{env="production",status="degraded"} 1'
            in metrics.text
        )
        assert "dialecticore_deployment_readiness_issues 11" in metrics.text
        assert (
            'dialecticore_deployment_readiness_check{check="database_persistent",status="fail"} 1'
            in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="database_schema_at_head",status="pass"} 1' in metrics.text
        )
        assert (
            'dialecticore_deployment_readiness_check{check="database_url_resolved",status="pass"} 1'
            in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="cors_origin_restricted",status="fail"} 1' in metrics.text
        )
        assert (
            'dialecticore_deployment_readiness_check{check="redis_runtime_enabled",status="fail"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_deployment_readiness_check{check="redis_url_configured",status="pass"} 1'
            in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="redis_runtime_channels_configured",status="pass"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="worker_heartbeat_ttl_covers_poll_interval",status="pass"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="worker_lease_ttl_covers_poll_interval",status="pass"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="temporal_runtime_contract_configured",status="pass"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="unsafe_default_secrets_replaced",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="publisher_live_target_enabled",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="model_provider_remote_endpoint_enabled",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="voicebox_remote_endpoint_enabled",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="comfyui_remote_endpoint_enabled",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        live_body = live_readiness.json()
        assert live_body["schema_version"] == "live_provider_readiness.v1"
        assert live_body["status"] == "fail"
        assert live_body["summary"]["fail_count"] >= 2
        assert live_body["summary"]["blocker_count"] >= 2
        checks = {check["category"]: check for check in live_body["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["readiness_checks"] == {
            "database_url_resolved": True,
            "database_persistent": False,
            "database_schema_at_head": True,
            "cors_origin_restricted": False,
            "auth_enabled": False,
            "auth_mode_configured": False,
            "auth_api_key_reference_available": True,
            "initial_admin_path_configured": True,
            "object_storage_remote": False,
            "object_storage_endpoint_configured": True,
            "object_storage_bucket_configured": True,
            "object_storage_credential_pair_configured": True,
            "redis_runtime_enabled": False,
            "redis_url_configured": True,
            "redis_runtime_channels_configured": True,
            "worker_heartbeat_ttl_covers_poll_interval": True,
            "worker_lease_ttl_covers_poll_interval": True,
            "backup_path_configured": True,
            "runtime_state_path_configured": True,
            "backup_path_writable": True,
            "runtime_state_path_writable": True,
            "runtime_paths_free_space_sufficient": True,
            "object_storage_local_path_writable": True,
            "unsafe_default_secrets_replaced": False,
            "temporal_runtime_contract_valid": True,
            "temporal_runtime_contract_configured": True,
            "publisher_automated_live_target_available": True,
            "publisher_target_enabled": True,
            "publisher_live_target_enabled": False,
            "publisher_targets_not_unhealthy": True,
            "publisher_target_health_known": True,
            "model_provider_endpoint_enabled": True,
            "model_provider_remote_endpoint_enabled": False,
            "model_provider_remote_endpoint_configured": True,
            "model_provider_endpoints_not_unhealthy": True,
            "model_provider_endpoint_health_known": True,
            "voicebox_endpoint_enabled": True,
            "voicebox_remote_endpoint_enabled": False,
            "voicebox_remote_endpoint_configured": True,
            "voicebox_endpoints_not_unhealthy": True,
            "voicebox_endpoint_health_known": True,
            "comfyui_endpoint_enabled": True,
            "comfyui_remote_endpoint_enabled": False,
            "comfyui_remote_endpoint_configured": True,
            "comfyui_endpoints_not_unhealthy": True,
            "comfyui_endpoint_health_known": True,
        }
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "database_persistent",
            "cors_origin_restricted",
            "auth_enabled",
            "auth_mode_configured",
            "object_storage_remote",
            "redis_runtime_enabled",
            "unsafe_default_secrets_replaced",
            "publisher_live_target_enabled",
            "model_provider_remote_endpoint_enabled",
            "voicebox_remote_endpoint_enabled",
            "comfyui_remote_endpoint_enabled",
        ]
        assert checks["runtime_paths"]["status"] == "pass"
        assert checks["runtime_paths"]["details"]["schema_version"] == "runtime_paths.v1"
        assert checks["runtime_paths"]["details"]["required_path_count"] == 3
        assert checks["runtime_paths"]["details"]["unavailable_path_count"] == 0
        assert checks["runtime_paths"]["details"]["readiness_checks"] == {
            "required_paths_configured": True,
            "required_paths_available_and_writable": True,
            "required_paths_free_space_sufficient": True,
        }
        assert checks["runtime_paths"]["details"]["failed_readiness_checks"] == []
        assert checks["backup_storage"]["status"] == "warning"
        assert checks["backup_storage"]["details"]["archive_count"] == 0
        assert checks["backup_storage"]["details"]["readiness_checks"] == {
            "backup_path_exists_or_parent_exists": True,
            "backup_path_writable": True,
            "backup_archive_available": False,
            "backup_archives_readable": True,
            "latest_archive_manifest_readable": False,
            "latest_restore_validation_current": False,
        }
        assert checks["backup_storage"]["details"]["failed_readiness_checks"] == [
            "backup_archive_available",
            "latest_archive_manifest_readable",
            "latest_restore_validation_current",
        ]
        assert "no backup archives are available" in checks["backup_storage"]["warnings"]
        assert checks["worker_registry"]["status"] == "warning"
        assert (
            checks["worker_registry"]["details"]["schema_version"] == "worker_registry_readiness.v1"
        )
        assert checks["worker_registry"]["details"]["active_workers"] == 0
        assert checks["worker_registry"]["details"]["configured_roles"] == 1
        assert checks["worker_registry"]["details"]["active_role_names"] == []
        assert checks["worker_registry"]["details"]["readiness_checks"] == {
            "worker_status_supplied": True,
            "active_worker_heartbeats_present": False,
            "configured_worker_roles_covered": False,
            "worker_heartbeats_fresh": True,
            "worker_heartbeats_not_failed": True,
            "worker_heartbeats_not_degraded": True,
            "worker_runtime_state_files_parse": True,
        }
        assert checks["worker_registry"]["details"]["failed_readiness_checks"] == [
            "active_worker_heartbeats_present",
            "configured_worker_roles_covered",
        ]
        assert checks["worker_registry"]["details"]["missing_role_names"] == [
            "workflow-worker",
        ]
        assert "no active worker heartbeats are present" in checks["worker_registry"]["warnings"]
        assert checks["model_providers"]["status"] == "warning"
        assert checks["model_providers"]["details"]["enabled"] == 1
        assert checks["model_providers"]["details"]["remote_enabled"] == 0
        assert checks["model_providers"]["details"]["remote_base_url_configured"] == 0
        assert checks["model_providers"]["details"]["missing_base_url"] == 0
        assert checks["model_providers"]["details"]["by_provider_type"] == {"mock": 1}
        assert checks["model_providers"]["details"]["by_health_status"] == {"healthy": 1}
        assert checks["model_providers"]["details"]["missing_base_url_endpoints"] == []
        assert checks["model_providers"]["details"]["unhealthy_endpoints"] == []
        assert checks["model_providers"]["details"]["unknown_health_endpoints"] == []
        assert checks["model_providers"]["details"]["readiness_checks"] == {
            "has_enabled_model_endpoint": True,
            "has_remote_model_endpoint": False,
            "remote_model_endpoints_have_base_url": True,
            "no_unhealthy_model_endpoints": True,
            "no_unknown_health_model_endpoints": True,
        }
        assert checks["model_providers"]["details"]["failed_readiness_checks"] == [
            "has_remote_model_endpoint"
        ]
        assert "only mock model endpoints are enabled" in checks["model_providers"]["warnings"]
        assert checks["voicebox"]["status"] == "fail"
        assert checks["comfyui"]["status"] == "fail"
        assert checks["voicebox"]["details"]["require_remote_base_url"] is True
        assert checks["voicebox"]["details"]["remote_base_url_configured"] == 0
        assert checks["voicebox"]["details"]["missing_base_url"] == 1
        assert checks["voicebox"]["details"]["by_adapter_type"] == {"mock": 1}
        assert checks["voicebox"]["details"]["by_health_status"] == {"healthy": 1}
        assert checks["voicebox"]["details"]["missing_base_url_endpoints"] == [
            {
                "id": "mock-voicebox",
                "name": "Deterministic Mock Voicebox",
                "adapter_type": "mock",
                "health_status": "healthy",
                "base_url_configured": False,
            }
        ]
        assert checks["voicebox"]["details"]["unhealthy_endpoints"] == []
        assert checks["voicebox"]["details"]["unknown_health_endpoints"] == []
        assert checks["voicebox"]["details"]["readiness_checks"] == {
            "has_enabled_voicebox_endpoint": True,
            "voicebox_endpoints_have_base_url": False,
            "no_unhealthy_voicebox_endpoints": True,
            "no_unknown_health_voicebox_endpoints": True,
        }
        assert checks["voicebox"]["details"]["failed_readiness_checks"] == [
            "voicebox_endpoints_have_base_url"
        ]
        assert checks["comfyui"]["details"]["require_remote_base_url"] is True
        assert checks["comfyui"]["details"]["remote_base_url_configured"] == 0
        assert checks["comfyui"]["details"]["missing_base_url"] == 1
        assert checks["comfyui"]["details"]["by_adapter_type"] == {"mock": 1}
        assert checks["comfyui"]["details"]["by_health_status"] == {"healthy": 1}
        assert checks["comfyui"]["details"]["missing_base_url_endpoints"] == [
            {
                "id": "mock-comfyui",
                "name": "Deterministic Mock ComfyUI",
                "adapter_type": "mock",
                "health_status": "healthy",
                "base_url_configured": False,
            }
        ]
        assert checks["comfyui"]["details"]["unhealthy_endpoints"] == []
        assert checks["comfyui"]["details"]["unknown_health_endpoints"] == []
        assert checks["comfyui"]["details"]["readiness_checks"] == {
            "has_enabled_comfyui_endpoint": True,
            "comfyui_endpoints_have_base_url": False,
            "no_unhealthy_comfyui_endpoints": True,
            "no_unknown_health_comfyui_endpoints": True,
        }
        assert checks["comfyui"]["details"]["failed_readiness_checks"] == [
            "comfyui_endpoints_have_base_url"
        ]
        assert (
            "one or more enabled voicebox endpoints records are missing base_url"
            in (checks["voicebox"]["blockers"])
        )
        assert (
            "one or more enabled comfyui endpoints records are missing base_url"
            in (checks["comfyui"]["blockers"])
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_mock_only_ai_media_providers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    monkeypatch.setenv("DIALECTICORE_API_KEY", "operator-api-key")
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        object_storage_endpoint="http://minio:9000",
        object_storage_bucket="dialecticore",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]

        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 4
        assert deployment["details"]["model_provider_summary"] == {
            "configured": 1,
            "enabled": 1,
            "remote_enabled": 0,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert deployment["details"]["voicebox_summary"] == {
            "configured": 1,
            "enabled": 1,
            "remote_enabled": 0,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert deployment["details"]["comfyui_summary"] == {
            "configured": 1,
            "enabled": 1,
            "remote_enabled": 0,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert deployment["details"]["publisher_target_summary"] == {
            "configured": 2,
            "enabled": 1,
            "live_enabled": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert deployment["details"]["failed_readiness_checks"] == [
            "publisher_live_target_enabled",
            "model_provider_remote_endpoint_enabled",
            "voicebox_remote_endpoint_enabled",
            "comfyui_remote_endpoint_enabled",
        ]
        assert deployment["details"]["issues"] == [
            "production publishing needs an enabled non-mock, non-dry-run publisher target",
            "production model routing needs an enabled non-mock model endpoint",
            "production audio synthesis needs an enabled non-mock Voicebox endpoint",
            "production visual generation needs an enabled non-mock ComfyUI endpoint",
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 4" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="publisher_live_target_enabled",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="model_provider_remote_endpoint_enabled",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="voicebox_remote_endpoint_enabled",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="comfyui_remote_endpoint_enabled",status="fail"} 1' in metrics.text
        )

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "publisher_live_target_enabled",
            "model_provider_remote_endpoint_enabled",
            "voicebox_remote_endpoint_enabled",
            "comfyui_remote_endpoint_enabled",
        ]
        assert checks["model_providers"]["status"] == "warning"
        assert checks["model_providers"]["details"]["failed_readiness_checks"] == [
            "has_remote_model_endpoint"
        ]

        for endpoint in _repository_with_remote_providers().list_model_endpoints():
            repository.upsert_model_endpoint(endpoint)
        for endpoint in _repository_with_remote_providers().list_voicebox_endpoints():
            repository.upsert_voicebox_endpoint(endpoint)
        for endpoint in _repository_with_remote_providers().list_comfyui_endpoints():
            repository.upsert_comfyui_endpoint(endpoint)
        for target in _repository_with_remote_providers().list_publisher_targets():
            repository.upsert_publisher_target(target)
        ready = client.get("/api/v1/system/health")
        assert ready.status_code == 200
        ready_components = {
            component["name"]: component for component in ready.json()["components"]
        }
        ready_deployment = ready_components["deployment_readiness"]
        assert ready_deployment["status"] == "healthy"
        assert ready_deployment["details"]["model_provider_summary"] == {
            "configured": 2,
            "enabled": 2,
            "remote_enabled": 1,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert ready_deployment["details"]["voicebox_summary"] == {
            "configured": 2,
            "enabled": 2,
            "remote_enabled": 1,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert ready_deployment["details"]["comfyui_summary"] == {
            "configured": 2,
            "enabled": 2,
            "remote_enabled": 1,
            "missing_base_url": 0,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert ready_deployment["details"]["publisher_target_summary"] == {
            "configured": 3,
            "enabled": 2,
            "live_enabled": 1,
            "unhealthy": 0,
            "unknown": 0,
        }
        assert (
            ready_deployment["details"]["readiness_checks"]["publisher_live_target_enabled"] is True
        )
        assert (
            ready_deployment["details"]["readiness_checks"][
                "model_provider_remote_endpoint_enabled"
            ]
            is True
        )
        assert (
            ready_deployment["details"]["readiness_checks"]["voicebox_remote_endpoint_enabled"]
            is True
        )
        assert (
            ready_deployment["details"]["readiness_checks"]["comfyui_remote_endpoint_enabled"]
            is True
        )
        assert ready_deployment["details"]["failed_readiness_checks"] == []
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_reports_temporal_contract_gaps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.setenv("DIALECTICORE_API_KEY", "operator-api-key")
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore@postgres/dialecticore",
        object_storage_backend="s3",
        object_storage_bucket="dialecticore",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
        temporal_backend_mode="external",
        temporal_backend_address=None,
        temporal_task_queue=None,
        temporal_backend_worker_enabled=False,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        deployment = next(
            component
            for component in health.json()["components"]
            if component["name"] == "deployment_readiness"
        )
        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 1
        assert deployment["details"]["checks"]["temporal_runtime_contract_valid"] is True
        assert deployment["details"]["checks"]["temporal_runtime_contract_configured"] is False
        assert deployment["details"]["readiness_checks"]["temporal_runtime_contract_valid"] is True
        assert (
            deployment["details"]["readiness_checks"]["temporal_runtime_contract_configured"]
            is False
        )
        assert (
            "temporal_runtime_contract_configured"
            in deployment["details"]["failed_readiness_checks"]
        )
        assert deployment["details"]["temporal_runtime_contract"] == {
            "mode": "external",
            "valid_mode": True,
            "configured": False,
            "missing": [
                "DIALECTICORE_TEMPORAL_BACKEND_ADDRESS",
                "DIALECTICORE_TEMPORAL_TASK_QUEUE",
                "DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED",
            ],
            "reason": (
                "external Temporal mode needs backend address, task queue, "
                "and native worker enabled"
            ),
        }
        assert (
            "external Temporal mode needs backend address, task queue, and native worker enabled"
            in deployment["details"]["issues"]
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 1" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="temporal_runtime_contract_configured",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert (
            "temporal_runtime_contract_configured"
            in checks["deployment_readiness"]["details"]["failed_readiness_checks"]
        )
        assert (
            "external Temporal mode needs backend address, task queue, and native worker enabled"
            in checks["deployment_readiness"]["blockers"]
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_missing_automated_live_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.setenv("DIALECTICORE_API_KEY", "operator-api-key")
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        object_storage_endpoint="http://minio:9000",
        object_storage_bucket="dialecticore",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
        publisher_automated_live_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]
        publishers = components["publisher_targets"]

        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 1
        assert deployment["details"]["publisher_automated_live_enabled"] is True
        assert deployment["details"]["publisher_automated_live_capable_enabled"] == 0
        assert deployment["details"]["checks"]["publisher_automated_live_target_available"] is False
        assert deployment["details"]["failed_readiness_checks"] == [
            "publisher_automated_live_target_available"
        ]
        assert deployment["details"]["issues"] == [
            "automated live publishing needs an enabled live publisher target"
        ]
        assert publishers["status"] == "degraded"
        assert "automated_live_target_available" in publishers["details"]["failed_readiness_checks"]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 1" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="publisher_automated_live_target_available",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "publisher_automated_live_target_available"
        ]
        assert checks["publisher_targets"]["status"] == "fail"
        assert (
            "automated_live_target_available"
            in checks["publisher_targets"]["details"]["failed_readiness_checks"]
        )

        repository.upsert_publisher_target(
            PublisherTarget(
                id="live-http",
                name="Live HTTP Publisher",
                platform="generic",
                adapter_type="http",
                base_url="https://publisher.example.test",
                enabled=True,
                health_status="healthy",
                capabilities={
                    "delivery_path": "/deliveries",
                    "automated_live_publish": True,
                },
            )
        )
        ready_health = client.get("/api/v1/system/health")
        assert ready_health.status_code == 200
        ready_components = {
            component["name"]: component for component in ready_health.json()["components"]
        }
        ready_deployment = ready_components["deployment_readiness"]
        assert ready_deployment["details"]["publisher_automated_live_capable_enabled"] == 1
        assert (
            ready_deployment["details"]["readiness_checks"][
                "publisher_automated_live_target_available"
            ]
            is True
        )
        assert (
            "publisher_automated_live_target_available"
            not in (ready_deployment["details"]["failed_readiness_checks"])
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_missing_redis_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.setenv("DIALECTICORE_API_KEY", "operator-api-key")
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        object_storage_bucket="dialecticore",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_url=" ",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["settings"]["redis_url_configured"] is False
        deployment = next(
            component
            for component in health.json()["components"]
            if component["name"] == "deployment_readiness"
        )
        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 1
        assert deployment["details"]["checks"]["redis_runtime_enabled"] is True
        assert deployment["details"]["checks"]["redis_url_configured"] is False
        assert deployment["details"]["readiness_checks"]["redis_url_configured"] is False
        assert deployment["details"]["failed_readiness_checks"] == ["redis_url_configured"]
        assert deployment["details"]["issues"] == [
            "production Redis runtime needs DIALECTICORE_REDIS_URL"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 1" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="redis_url_configured",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "redis_url_configured"
        ]
        assert checks["redis"]["status"] == "fail"
        assert "redis_reachable" in checks["redis"]["details"]["failed_readiness_checks"]
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_missing_s3_endpoint_and_bucket(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.setenv("DIALECTICORE_API_KEY", "operator-api-key")
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        object_storage_endpoint=" ",
        object_storage_bucket=" ",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]
        object_storage = components["object_storage"]

        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 2
        assert deployment["details"]["checks"]["object_storage_remote"] is True
        assert deployment["details"]["checks"]["object_storage_endpoint_configured"] is False
        assert deployment["details"]["checks"]["object_storage_bucket_configured"] is False
        assert deployment["details"]["failed_readiness_checks"] == [
            "object_storage_endpoint_configured",
            "object_storage_bucket_configured",
        ]
        assert deployment["details"]["issues"] == [
            "production object storage needs an S3-compatible endpoint",
            "production object storage needs a bucket name",
        ]
        assert object_storage["status"] == "degraded"
        assert object_storage["details"]["readiness_checks"]["bucket_configured"] is False

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 2" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="object_storage_endpoint_configured",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="object_storage_bucket_configured",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "object_storage_endpoint_configured",
            "object_storage_bucket_configured",
        ]
        assert checks["object_storage"]["status"] == "fail"
        assert "bucket_configured" in checks["object_storage"]["details"]["failed_readiness_checks"]
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_mismatched_s3_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.setenv("DIALECTICORE_API_KEY", "operator-api-key")
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        object_storage_endpoint="http://minio:9000",
        object_storage_bucket="dialecticore",
        object_storage_access_key_reference=" ",
        object_storage_secret_key_reference="env:MINIO_ROOT_PASSWORD",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]
        object_storage = components["object_storage"]

        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 1
        assert deployment["details"]["checks"]["object_storage_credential_pair_configured"] is False
        assert health.json()["settings"]["object_storage_access_key_reference_configured"] is False
        assert health.json()["settings"]["object_storage_secret_key_reference_configured"] is True
        assert health.json()["settings"]["object_storage_credential_pair_configured"] is False
        assert deployment["details"]["failed_readiness_checks"] == [
            "object_storage_credential_pair_configured"
        ]
        assert deployment["details"]["issues"] == [
            "production object storage access and secret key references must be configured together"
        ]
        assert object_storage["status"] == "degraded"
        assert object_storage["details"]["readiness_checks"]["credential_pair_configured"] is False
        assert object_storage["details"]["access_key_reference_configured"] is False
        assert object_storage["details"]["secret_key_reference_configured"] is True

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 1" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="object_storage_credential_pair_configured",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "object_storage_credential_pair_configured"
        ]
        assert checks["object_storage"]["status"] == "fail"
        assert (
            "credential_pair_configured"
            in checks["object_storage"]["details"]["failed_readiness_checks"]
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_missing_redis_channels(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.setenv("DIALECTICORE_API_KEY", "operator-api-key")
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        object_storage_bucket="dialecticore",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_url="redis://redis:6379/0",
        redis_event_fanout_enabled=True,
        redis_event_channel=" ",
        redis_worker_signal_enabled=True,
        redis_worker_signal_stream=" ",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        deployment = next(
            component
            for component in health.json()["components"]
            if component["name"] == "deployment_readiness"
        )
        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 1
        assert deployment["details"]["checks"]["redis_runtime_enabled"] is True
        assert deployment["details"]["checks"]["redis_url_configured"] is True
        assert deployment["details"]["checks"]["redis_runtime_channels_configured"] is False
        assert (
            deployment["details"]["readiness_checks"]["redis_runtime_channels_configured"] is False
        )
        assert deployment["details"]["failed_readiness_checks"] == [
            "redis_runtime_channels_configured"
        ]
        assert deployment["details"]["issues"] == [
            "production Redis runtime needs event channel and worker signal stream names"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 1" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="redis_runtime_channels_configured",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "redis_runtime_channels_configured"
        ]
        assert checks["redis"]["status"] == "fail"
        assert "event_channel_configured" in checks["redis"]["details"]["failed_readiness_checks"]
        assert (
            "worker_signal_stream_configured"
            in checks["redis"]["details"]["failed_readiness_checks"]
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_unavailable_api_key_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.delenv("MISSING_DIALECTICORE_API_KEY", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        object_storage_bucket="dialecticore",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:MISSING_DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]

        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 1
        assert deployment["details"]["auth_api_key_reference_status"] == {
            "status": "unavailable",
            "reference": "env:MISSING_DIALECTICORE_API_KEY",
            "error": "RuntimeError",
            "reason": "credential reference is not available",
        }
        assert (
            deployment["details"]["readiness_checks"]["auth_api_key_reference_available"] is False
        )
        assert deployment["details"]["failed_readiness_checks"] == [
            "auth_api_key_reference_available"
        ]
        assert deployment["details"]["issues"] == ["configured API-key reference is unavailable"]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 1" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="auth_api_key_reference_available",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["auth_api_key_reference_status"] == {
            "status": "unavailable",
            "reference": "env:MISSING_DIALECTICORE_API_KEY",
            "error": "RuntimeError",
            "reason": "credential reference is not available",
        }
        assert (
            "configured API-key reference is unavailable"
            in (checks["deployment_readiness"]["blockers"])
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_default_database_url_password(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.setenv("DIALECTICORE_API_KEY", "operator-api-key")
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url=("postgresql+psycopg://dialecticore:dialecticore@postgres:5432/dialecticore"),
        object_storage_backend="s3",
        object_storage_bucket="dialecticore",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        deployment = next(
            component
            for component in health.json()["components"]
            if component["name"] == "deployment_readiness"
        )
        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 1
        assert deployment["details"]["unsafe_default_secret_labels"] == [
            "DIALECTICORE_DATABASE_URL.password"
        ]
        assert deployment["details"]["checks"]["unsafe_default_secrets_replaced"] is False
        assert deployment["details"]["failed_readiness_checks"] == [
            "unsafe_default_secrets_replaced"
        ]
        assert (
            "production deployments must replace placeholder/default secrets"
            in (deployment["details"]["issues"])
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 1" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="unsafe_default_secrets_replaced",status="fail"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_placeholder_secret_references(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    monkeypatch.delenv("DIALECTICORE_API_KEY", raising=False)
    monkeypatch.delenv("MINIO_ROOT_USER", raising=False)
    monkeypatch.delenv("MINIO_ROOT_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    secret_root = tmp_path / "secrets"
    secret_root.mkdir()
    (secret_root / "dialecticore_api_key").write_text(
        "change-me-before-enabling-auth\n",
        encoding="utf-8",
    )
    (secret_root / "minio_root_password").write_text(
        "change-me-in-production\n",
        encoding="utf-8",
    )
    (secret_root / "minio_root_user").write_text("dialecticore\n", encoding="utf-8")
    (secret_root / "postgres_password").write_text("dialecticore\n", encoding="utf-8")
    secret_resolver = SecretResolver()
    secret_resolver.docker_secret_root = secret_root
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="",
        database_driver="postgresql+psycopg",
        database_host="postgres",
        database_name="dialecticore",
        database_user="dialecticore",
        database_password_reference=f"file:{secret_root / 'postgres_password'}",
        object_storage_backend="s3",
        object_storage_bucket="dialecticore",
        object_storage_access_key_reference="docker-secret:minio_root_user",
        object_storage_secret_key_reference="docker-secret:minio_root_password",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="docker-secret:dialecticore_api_key",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(
        settings,
        secret_resolver=secret_resolver,
    )
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        deployment = next(
            component
            for component in health.json()["components"]
            if component["name"] == "deployment_readiness"
        )
        assert deployment["status"] == "degraded"
        assert deployment["details"]["issue_count"] == 1
        assert deployment["details"]["unsafe_default_secret_labels"] == [
            "DIALECTICORE_AUTH_API_KEY_REFERENCE",
            "DIALECTICORE_DATABASE_PASSWORD_REFERENCE",
            "DIALECTICORE_DATABASE_URL.password",
            "DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE",
            "DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE",
        ]
        assert deployment["details"]["checks"]["database_url_resolved"] is True
        assert deployment["details"]["checks"]["unsafe_default_secrets_replaced"] is False
        assert deployment["details"]["failed_readiness_checks"] == [
            "unsafe_default_secrets_replaced"
        ]
        assert (
            "production deployments must replace placeholder/default secrets"
            in (deployment["details"]["issues"])
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_deployment_readiness_issues 1" in metrics.text
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="unsafe_default_secrets_replaced",status="fail"} 1' in metrics.text
        )

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["unsafe_default_secret_labels"] == [
            "DIALECTICORE_AUTH_API_KEY_REFERENCE",
            "DIALECTICORE_DATABASE_PASSWORD_REFERENCE",
            "DIALECTICORE_DATABASE_URL.password",
            "DIALECTICORE_OBJECT_STORAGE_ACCESS_KEY_REFERENCE",
            "DIALECTICORE_OBJECT_STORAGE_SECRET_KEY_REFERENCE",
        ]
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_wildcard_cors(tmp_path: Path) -> None:
    repository = _repository_with_remote_providers()
    settings = Settings(
        env="production",
        cors_allowed_origins="*",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        object_storage_access_key_reference=None,
        object_storage_secret_key_reference=None,
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]

        assert deployment["status"] == "degraded"
        assert deployment["details"]["readiness_checks"]["cors_origin_restricted"] is False
        assert "cors_origin_restricted" in deployment["details"]["failed_readiness_checks"]
        assert (
            "production API CORS origins should be restricted" in (deployment["details"]["issues"])
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="cors_origin_restricted",status="fail"} 1' in metrics.text
        )

        ready_settings = settings.model_copy(
            update={"cors_allowed_origins": "https://studio.example.test"}
        )
        app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(
            ready_settings
        )
        ready = client.get("/api/v1/system/health")
        assert ready.status_code == 200
        ready_components = {
            component["name"]: component for component in ready.json()["components"]
        }
        assert (
            ready_components["deployment_readiness"]["details"]["readiness_checks"][
                "cors_origin_restricted"
            ]
            is True
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_missing_admin_bootstrap_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    for name in [
        "DIALECTICORE_API_KEY",
        "MINIO_ROOT_PASSWORD",
        "POSTGRES_PASSWORD",
    ]:
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference=None,
        auth_trusted_identity_enabled=True,
        auth_trusted_default_role="viewer",
        auth_trusted_group_role_map="dialecticore-producers=producer",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]

        assert deployment["status"] == "degraded"
        assert deployment["details"]["checks"]["auth_enabled"] is True
        assert deployment["details"]["checks"]["auth_mode_configured"] is True
        assert deployment["details"]["checks"]["initial_admin_path_configured"] is False
        assert deployment["details"]["failed_readiness_checks"] == ["initial_admin_path_configured"]
        assert deployment["details"]["issues"] == [
            "production authentication needs an admin-capable bootstrap path"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="initial_admin_path_configured",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "initial_admin_path_configured"
        ]

        ready_settings = settings.model_copy(
            update={"auth_trusted_group_role_map": "dialecticore-admins=admin"}
        )
        app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(
            ready_settings
        )
        ready = client.get("/api/v1/system/health")
        assert ready.status_code == 200
        ready_components = {
            component["name"]: component for component in ready.json()["components"]
        }
        assert (
            ready_components["deployment_readiness"]["details"]["readiness_checks"][
                "initial_admin_path_configured"
            ]
            is True
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_requires_viable_auth_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = _repository_with_remote_providers()
    for name in [
        "DIALECTICORE_API_KEY",
        "MINIO_ROOT_PASSWORD",
        "POSTGRES_PASSWORD",
    ]:
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference=None,
        auth_provider_session_enabled=True,
        auth_provider_session_introspection_url="https://idp.example.test/introspect",
        auth_provider_session_default_role="admin",
        auth_provider_session_user_claim=" ",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]

        assert deployment["status"] == "degraded"
        assert deployment["details"]["checks"]["auth_enabled"] is True
        assert deployment["details"]["checks"]["auth_mode_configured"] is False
        assert deployment["details"]["checks"]["initial_admin_path_configured"] is False
        assert deployment["details"]["failed_readiness_checks"] == [
            "auth_mode_configured",
            "initial_admin_path_configured",
        ]
        assert deployment["details"]["issues"] == [
            "production authentication needs at least one configured auth mode",
            "production authentication needs an admin-capable bootstrap path",
        ]

        auth_runtime = components["auth_runtime"]
        assert auth_runtime["status"] == "degraded"
        assert auth_runtime["details"]["failed_readiness_checks"] == [
            "auth_disabled_or_mode_configured",
            "api_key_or_alternate_auth_mode_configured",
            "provider_session_user_claim_configured",
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="auth_mode_configured",status="fail"} 1' in metrics.text
        )
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="initial_admin_path_configured",status="fail"} 1' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["deployment_readiness"]["status"] == "fail"
        assert checks["deployment_readiness"]["details"]["failed_readiness_checks"] == [
            "auth_mode_configured",
            "initial_admin_path_configured",
        ]
        assert checks["deployment_readiness"]["blockers"] == [
            "production authentication needs at least one configured auth mode; "
            "production authentication needs an admin-capable bootstrap path"
        ]
        assert checks["auth_runtime"]["status"] == "fail"
        assert checks["auth_runtime"]["details"]["failed_readiness_checks"] == [
            "auth_disabled_or_mode_configured",
            "api_key_or_alternate_auth_mode_configured",
            "provider_session_user_claim_configured",
        ]
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_worker_heartbeat_timing(
    tmp_path: Path,
) -> None:
    repository = _repository_with_remote_providers()
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
        worker_poll_interval_seconds=120,
        worker_heartbeat_ttl_seconds=30,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]

        assert deployment["status"] == "degraded"
        assert (
            deployment["details"]["readiness_checks"]["worker_heartbeat_ttl_covers_poll_interval"]
            is False
        )
        assert (
            "worker_heartbeat_ttl_covers_poll_interval"
            in (deployment["details"]["failed_readiness_checks"])
        )
        assert (
            "worker heartbeat TTL should be greater than worker poll interval"
            in (deployment["details"]["issues"])
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="worker_heartbeat_ttl_covers_poll_interval",status="fail"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_production_deployment_readiness_flags_worker_lease_timing(
    tmp_path: Path,
) -> None:
    repository = _repository_with_remote_providers()
    settings = Settings(
        env="production",
        cors_allowed_origins="https://studio.example.test",
        database_url="postgresql+psycopg://dialecticore:secret@postgres:5432/dialecticore",
        object_storage_backend="s3",
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        redis_event_fanout_enabled=True,
        redis_worker_signal_enabled=True,
        worker_poll_interval_seconds=120,
        worker_heartbeat_ttl_seconds=180,
        worker_lease_ttl_seconds=30,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        deployment = components["deployment_readiness"]

        assert deployment["status"] == "degraded"
        assert (
            deployment["details"]["readiness_checks"]["worker_lease_ttl_covers_poll_interval"]
            is False
        )
        assert (
            "worker_lease_ttl_covers_poll_interval"
            in (deployment["details"]["failed_readiness_checks"])
        )
        assert (
            "worker lease TTL should be greater than worker poll interval"
            in (deployment["details"]["issues"])
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_deployment_readiness_check"
            '{check="worker_lease_ttl_covers_poll_interval",status="fail"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_live_provider_readiness_fails_on_unavailable_runtime_paths(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        backup_path=str(tmp_path / "missing-parent" / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        object_storage_backend="local",
        object_storage_local_path=str(tmp_path / "object-store"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    client = TestClient(app)

    try:
        response = client.get("/api/v1/system/live-provider-readiness")
        assert response.status_code == 200
        body = response.json()
        checks = {check["category"]: check for check in body["checks"]}
        runtime_paths = checks["runtime_paths"]
        assert runtime_paths["status"] == "fail"
        assert runtime_paths["details"]["schema_version"] == "runtime_paths.v1"
        assert runtime_paths["details"]["required_path_count"] == 3
        assert runtime_paths["details"]["unavailable_path_count"] == 1
        assert runtime_paths["details"]["paths"]["backup"]["parent_exists"] is False
        assert runtime_paths["details"]["paths"]["backup"]["writable_target_or_parent"] is False
        assert runtime_paths["details"]["readiness_checks"] == {
            "required_paths_configured": True,
            "required_paths_available_and_writable": False,
            "required_paths_free_space_sufficient": True,
        }
        assert runtime_paths["details"]["failed_readiness_checks"] == [
            "required_paths_available_and_writable"
        ]
        assert (
            "one or more required runtime paths are missing or unwritable"
            in runtime_paths["blockers"]
        )
        assert body["summary"]["blocker_count"] >= 1
        assert body["status"] == "fail"

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_runtime_path_state{name="backup",required="true",'
            'state="parent_exists"} 0' in metrics.text
        )
        assert (
            'dialecticore_runtime_path_state{name="backup",required="true",'
            'state="writable_target_or_parent"} 0' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_live_provider_readiness_fails_on_low_runtime_path_free_space(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        object_storage_backend="local",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_path_min_free_bytes=10**18,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        runtime_paths = components["runtime_paths"]
        assert runtime_paths["status"] == "degraded"
        assert runtime_paths["details"]["schema_version"] == "runtime_paths.v1"
        assert runtime_paths["details"]["required_path_count"] == 3
        assert runtime_paths["details"]["unavailable_path_count"] == 0
        assert runtime_paths["details"]["low_free_space_path_count"] == 3
        assert runtime_paths["details"]["min_free_bytes"] == 10**18
        assert runtime_paths["details"]["paths"]["backup"]["free_bytes_sufficient"] is False
        assert runtime_paths["details"]["readiness_checks"] == {
            "required_paths_configured": True,
            "required_paths_available_and_writable": True,
            "required_paths_free_space_sufficient": False,
        }
        assert runtime_paths["details"]["failed_readiness_checks"] == [
            "required_paths_free_space_sufficient"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_runtime_path_free_bytes_sufficient{name="backup",required="true"} 0'
            in metrics.text
        )
        assert (
            'dialecticore_runtime_path_state{name="backup",required="true",'
            'state="free_bytes_sufficient"} 0' in metrics.text
        )

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert checks["runtime_paths"]["status"] == "fail"
        assert checks["runtime_paths"]["details"]["readiness_checks"] == {
            "required_paths_configured": True,
            "required_paths_available_and_writable": True,
            "required_paths_free_space_sufficient": False,
        }
        assert checks["runtime_paths"]["details"]["failed_readiness_checks"] == [
            "required_paths_free_space_sufficient"
        ]
        assert (
            "one or more required runtime paths are below the free-space floor"
            in checks["runtime_paths"]["blockers"]
        )
    finally:
        app.dependency_overrides.clear()


def test_backup_storage_health_requires_writable_backup_path(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    backup_path.chmod(0o500)
    settings = Settings(
        backup_path=str(backup_path),
        runtime_state_path=str(tmp_path / "runtime-state"),
        object_storage_backend="local",
        object_storage_local_path=str(tmp_path / "object-store"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        backup_storage = components["backup_storage"]
        assert backup_storage["status"] == "degraded"
        assert backup_storage["details"]["checked_path"] == str(backup_path)
        assert backup_storage["details"]["checked_path_exists"] is True
        assert backup_storage["details"]["checked_path_is_dir"] is True
        assert backup_storage["details"]["writable_target_or_parent"] is False
        assert backup_storage["details"]["writable_parent"] is False
        assert backup_storage["details"]["readiness_checks"]["backup_path_writable"] is False
        assert backup_storage["details"]["failed_readiness_checks"] == [
            "backup_path_writable",
            "backup_archive_available",
            "latest_archive_manifest_readable",
            "latest_restore_validation_current",
        ]
        assert backup_storage["details"]["reason"] == (
            "backup path or parent directory is not writable"
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["backup_storage"]["status"] == "warning"
        assert (
            checks["backup_storage"]["details"]["readiness_checks"]["backup_path_writable"] is False
        )
        assert (
            "backup path or parent directory is not writable"
            in checks["backup_storage"]["warnings"]
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_component_readiness_check"
            '{component="backup_storage",check="backup_path_writable",status="fail"} 1'
            in metrics.text
        )
    finally:
        backup_path.chmod(0o700)
        app.dependency_overrides.clear()


def test_local_object_storage_health_requires_writable_path(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    object_store_path = tmp_path / "object-store"
    object_store_path.mkdir()
    object_store_path.chmod(0o500)
    settings = Settings(
        object_storage_backend="local",
        object_storage_local_path=str(object_store_path),
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        object_storage = components["object_storage"]
        assert object_storage["status"] == "degraded"
        assert object_storage["details"]["backend"] == "local"
        assert object_storage["details"]["checked_path"] == str(object_store_path)
        assert object_storage["details"]["checked_path_exists"] is True
        assert object_storage["details"]["checked_path_is_dir"] is True
        assert object_storage["details"]["writable_target_or_parent"] is False
        assert object_storage["details"]["writable_parent"] is False
        assert object_storage["details"]["readiness_checks"] == {
            "checked_path_exists": True,
            "checked_path_is_directory": True,
            "writable_target_or_parent": False,
        }
        assert object_storage["details"]["failed_readiness_checks"] == ["writable_target_or_parent"]
        assert object_storage["details"]["reason"] == (
            "local object-storage path or parent directory is not writable"
        )
        runtime_paths = components["runtime_paths"]
        assert runtime_paths["status"] == "degraded"
        assert (
            runtime_paths["details"]["paths"]["object_storage_local"]["writable_target_or_parent"]
            is False
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_component_health_status{component="object_storage",status="degraded"} 1'
        ) in metrics.text
        assert (
            'dialecticore_runtime_path_ready{name="object_storage_local",required="true"} 0'
            in metrics.text
        )
        assert (
            'dialecticore_runtime_path_state{name="object_storage_local",'
            'required="true",state="writable_target_or_parent"} 0' in metrics.text
        )
        assert (
            "dialecticore_object_storage_local_path_ready"
            f'{{backend="local",bucket="dialecticore",checked_path="{object_store_path}"}} 0'
            in metrics.text
        )
        assert (
            "dialecticore_object_storage_local_path_state"
            f'{{backend="local",bucket="dialecticore",checked_path="{object_store_path}",'
            'state="writable_target_or_parent"} 0' in metrics.text
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["object_storage"]["status"] == "fail"
        assert checks["object_storage"]["details"]["readiness_checks"] == {
            "checked_path_exists": True,
            "checked_path_is_directory": True,
            "writable_target_or_parent": False,
        }
        assert checks["object_storage"]["details"]["failed_readiness_checks"] == [
            "writable_target_or_parent"
        ]
        assert (
            "local object-storage path or parent directory is not writable"
            in checks["object_storage"]["blockers"]
        )
        assert checks["runtime_paths"]["status"] == "fail"
    finally:
        object_store_path.chmod(0o700)
        app.dependency_overrides.clear()


def test_live_provider_readiness_blocks_misconfigured_automated_live_publishing(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        publisher_automated_live_enabled=True,
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        health_components = {
            component["name"]: component for component in health.json()["components"]
        }
        publisher_health = health_components["publisher_targets"]
        assert publisher_health["status"] == "degraded"
        assert publisher_health["details"]["schema_version"] == "publisher_target_health.v1"
        assert publisher_health["details"]["automated_live_enabled"] is True
        assert publisher_health["details"]["automated_live_capable_enabled"] == 0
        assert publisher_health["details"]["by_adapter_type"] == {"mock": 1}
        assert publisher_health["details"]["by_health_status"] == {"healthy": 1}
        assert publisher_health["details"]["by_platform"] == {"youtube": 1}
        assert publisher_health["details"]["issue_count"] == 1
        assert (
            "automated live publishing is enabled but no enabled target declares "
            "automated_live_publish" in publisher_health["details"]["issues"]
        )
        assert publisher_health["details"]["readiness_checks"] == {
            "has_enabled_publisher_target": True,
            "has_live_publisher_target": False,
            "automated_live_target_available": False,
            "no_unhealthy_publisher_targets": True,
            "no_unknown_health_publisher_targets": True,
        }
        assert publisher_health["details"]["failed_readiness_checks"] == [
            "has_live_publisher_target",
            "automated_live_target_available",
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert 'dialecticore_publisher_target_health_status{status="degraded"} 1' in metrics.text
        assert (
            'dialecticore_publisher_target_count{kind="automated_live_capable_enabled"} 0'
            in metrics.text
        )
        assert 'dialecticore_publisher_target_count{kind="issues"} 1' in metrics.text

        response = client.get("/api/v1/system/live-provider-readiness")
        assert response.status_code == 200
        checks = {check["category"]: check for check in response.json()["checks"]}
        publishers = checks["publisher_targets"]
        assert publishers["status"] == "fail"
        assert publishers["details"]["configured"] >= 1
        assert publishers["details"]["enabled"] >= 1
        assert publishers["details"]["live_enabled"] == 0
        assert publishers["details"]["automated_live_enabled"] is True
        assert publishers["details"]["automated_live_capable_enabled"] == 0
        assert publishers["details"]["mock_enabled"] >= 1
        assert publishers["details"]["dry_run_only_enabled"] == 0
        assert publishers["details"]["healthy"] >= 1
        assert publishers["details"]["unhealthy"] == 0
        assert publishers["details"]["by_adapter_type"] == {"mock": 1}
        assert publishers["details"]["by_health_status"] == {"healthy": 1}
        assert publishers["details"]["by_platform"] == {"youtube": 1}
        assert publishers["details"]["readiness_checks"] == {
            "has_enabled_publisher_target": True,
            "has_live_publisher_target": False,
            "automated_live_target_available": False,
            "no_unhealthy_publisher_targets": True,
            "no_unknown_health_publisher_targets": True,
        }
        assert publishers["details"]["failed_readiness_checks"] == [
            "has_live_publisher_target",
            "automated_live_target_available",
        ]
        assert publishers["details"]["live_readiness_policy"] == (
            "automated live publishing requires an enabled live target with automated_live_publish"
        )
        assert (
            "automated live publishing is enabled but no enabled target declares "
            "automated_live_publish" in publishers["blockers"]
        )

        repository.upsert_publisher_target(
            PublisherTarget(
                id="live-http",
                name="Live HTTP Publisher",
                platform="generic",
                adapter_type="http",
                base_url="https://publisher.example.test",
                enabled=True,
                health_status="healthy",
                capabilities={
                    "delivery_path": "/deliveries",
                    "automated_live_publish": True,
                },
            )
        )

        ready_response = client.get("/api/v1/system/live-provider-readiness")
        assert ready_response.status_code == 200
        ready_checks = {check["category"]: check for check in ready_response.json()["checks"]}
        ready_publishers = ready_checks["publisher_targets"]
        assert ready_publishers["status"] == "pass"
        assert ready_publishers["details"]["live_enabled"] == 1
        assert ready_publishers["details"]["automated_live_capable_enabled"] == 1
        assert ready_publishers["details"]["automated_live_enabled"] is True
        assert ready_publishers["details"]["unhealthy"] == 0
        assert ready_publishers["details"]["readiness_checks"] == {
            "has_enabled_publisher_target": True,
            "has_live_publisher_target": True,
            "automated_live_target_available": True,
            "no_unhealthy_publisher_targets": True,
            "no_unknown_health_publisher_targets": True,
        }
        assert ready_publishers["details"]["failed_readiness_checks"] == []
        assert ready_publishers["blockers"] == []

        ready_health = client.get("/api/v1/system/health")
        assert ready_health.status_code == 200
        ready_components = {
            component["name"]: component for component in ready_health.json()["components"]
        }
        assert ready_components["publisher_targets"]["status"] == "healthy"
        assert (
            ready_components["publisher_targets"]["details"]["automated_live_capable_enabled"] == 1
        )
        assert ready_components["publisher_targets"]["details"]["failed_readiness_checks"] == []
    finally:
        app.dependency_overrides.clear()


def test_publisher_target_health_degrades_unknown_enabled_targets(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        backup_path=str(tmp_path / "backups"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    repository.upsert_publisher_target(
        PublisherTarget(
            id="unknown-http",
            name="Unknown HTTP Publisher",
            platform="generic",
            adapter_type="http",
            base_url="https://publisher.example.test",
            enabled=True,
            health_status="unknown",
            capabilities={"delivery_path": "/deliveries"},
        )
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        publisher_health = components["publisher_targets"]
        assert publisher_health["status"] == "degraded"
        assert publisher_health["details"]["unknown"] == 1
        assert publisher_health["details"]["issue_count"] == 1
        assert publisher_health["details"]["issues"] == [
            "one or more enabled publisher targets have unknown health"
        ]
        assert publisher_health["details"]["readiness_checks"] == {
            "has_enabled_publisher_target": True,
            "has_live_publisher_target": True,
            "automated_live_target_available": True,
            "no_unhealthy_publisher_targets": True,
            "no_unknown_health_publisher_targets": False,
        }
        assert publisher_health["details"]["failed_readiness_checks"] == [
            "no_unknown_health_publisher_targets"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert 'dialecticore_publisher_target_health_status{status="degraded"} 1' in metrics.text
        assert 'dialecticore_publisher_target_count{kind="unknown"} 1' in metrics.text
        assert 'dialecticore_publisher_target_count{kind="issues"} 1' in metrics.text
        assert (
            'dialecticore_component_readiness_check{component="publisher_targets",'
            'check="no_unknown_health_publisher_targets",status="fail"} 1'
        ) in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        publishers = checks["publisher_targets"]
        assert publishers["status"] == "warning"
        assert publishers["details"]["unknown"] == 1
        assert publishers["details"]["readiness_checks"] == {
            "has_enabled_publisher_target": True,
            "has_live_publisher_target": True,
            "automated_live_target_available": True,
            "no_unhealthy_publisher_targets": True,
            "no_unknown_health_publisher_targets": False,
        }
        assert publishers["details"]["failed_readiness_checks"] == [
            "no_unknown_health_publisher_targets"
        ]
        assert publishers["warnings"] == [
            "one or more enabled publisher targets have unknown health"
        ]
    finally:
        app.dependency_overrides.clear()


def test_system_health_and_metrics_report_credential_reference_readiness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    secret_path = tmp_path / "voicebox-token"
    secret_path.write_text("voicebox-secret\n", encoding="utf-8")
    monkeypatch.setenv("DIALECTICORE_API_KEY", "api-secret")
    monkeypatch.setenv("MODEL_TOKEN", "model-secret")
    settings = Settings(
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_API_KEY",
        object_storage_backend="s3",
        object_storage_access_key_reference="env:MODEL_TOKEN",
        object_storage_secret_key_reference="env:MISSING_S3_SECRET",
    )
    repository.upsert_model_endpoint(
        ModelEndpoint(
            id="remote-model",
            name="Remote Model",
            provider_type="openai_compatible",
            base_url="https://models.example.test",
            credential_reference="env:MODEL_TOKEN",
            enabled=True,
        )
    )
    repository.upsert_voicebox_endpoint(
        VoiceboxEndpoint(
            id="remote-voicebox",
            name="Remote Voicebox",
            base_url="https://voicebox.example.test",
            credential_reference=f"file:{secret_path}",
            enabled=True,
        )
    )
    repository.upsert_comfyui_endpoint(
        ComfyUiEndpoint(
            id="remote-comfyui",
            name="Remote ComfyUI",
            base_url="https://comfy.example.test",
            credential_reference="env:MISSING_COMFYUI_TOKEN",
            enabled=True,
        )
    )
    repository.upsert_publisher_target(
        PublisherTarget(
            id="live-youtube",
            name="Live YouTube",
            platform="youtube",
            adapter_type="youtube_resumable",
            base_url="https://youtube.example.test",
            credential_reference="env:MISSING_YOUTUBE_ACCESS_TOKEN",
            enabled=True,
            capabilities={
                "oauth_refresh_token_reference": "env:MISSING_YOUTUBE_REFRESH_TOKEN",
                "oauth_client_id_reference": "env:MODEL_TOKEN",
                "oauth_client_secret_reference": "env:MISSING_YOUTUBE_CLIENT_SECRET",
            },
        )
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        credentials = next(
            component
            for component in health.json()["components"]
            if component["name"] == "credential_references"
        )
        assert credentials["status"] == "degraded"
        assert credentials["details"]["schema_version"] == "credential_reference_readiness.v1"
        assert credentials["details"]["checked_count"] == 10
        assert credentials["details"]["resolved_count"] == 5
        assert credentials["details"]["unavailable_count"] == 5
        assert credentials["details"]["unsupported_count"] == 0
        assert credentials["details"]["by_owner_type"] == {
            "comfyui_endpoint": 1,
            "model_endpoint": 1,
            "publisher_target": 4,
            "settings": 3,
            "voicebox_endpoint": 1,
        }
        assert credentials["details"]["by_scheme"] == {"env": 9, "file": 1}
        assert credentials["details"]["readiness_checks"] == {
            "active_credential_references_resolve": False,
            "credential_reference_schemes_supported": True,
        }
        assert credentials["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve"
        ]
        unavailable = [
            item for item in credentials["details"]["references"] if item["status"] == "unavailable"
        ]
        assert {item["reference"] for item in unavailable} == {
            "env:MISSING_S3_SECRET",
            "env:MISSING_COMFYUI_TOKEN",
            "env:MISSING_YOUTUBE_ACCESS_TOKEN",
            "env:MISSING_YOUTUBE_REFRESH_TOKEN",
            "env:MISSING_YOUTUBE_CLIENT_SECRET",
        }
        provisioning_health = next(
            component
            for component in health.json()["components"]
            if component["name"] == "credential_provisioning"
        )
        assert provisioning_health["status"] == "degraded"
        assert (
            provisioning_health["details"]["schema_version"] == "credential_provisioning_health.v1"
        )
        assert provisioning_health["details"]["active_reference_count"] == 10
        assert provisioning_health["details"]["active_unavailable_count"] == 5
        assert provisioning_health["details"]["all_reference_count"] == 14
        assert provisioning_health["details"]["all_unavailable_count"] == 9
        assert provisioning_health["details"]["inactive_unavailable_count"] == 4
        assert provisioning_health["details"]["env_var_count"] == 11
        assert provisioning_health["details"]["file_count"] == 1
        assert provisioning_health["details"]["readiness_checks"] == {
            "active_credential_references_resolve": False,
            "disabled_target_credential_references_resolve": False,
            "credential_reference_schemes_supported": True,
        }
        assert provisioning_health["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve",
            "disabled_target_credential_references_resolve",
        ]
        assert {
            item["reference"]
            for item in provisioning_health["details"]["missing_active_references"]
        } == {
            "env:MISSING_S3_SECRET",
            "env:MISSING_COMFYUI_TOKEN",
            "env:MISSING_YOUTUBE_ACCESS_TOKEN",
            "env:MISSING_YOUTUBE_REFRESH_TOKEN",
            "env:MISSING_YOUTUBE_CLIENT_SECRET",
        }
        assert {
            item["reference"]
            for item in provisioning_health["details"]["missing_inactive_references"]
        } == {
            "env:YOUTUBE_OAUTH_ACCESS_TOKEN",
            "env:YOUTUBE_OAUTH_REFRESH_TOKEN",
            "env:YOUTUBE_OAUTH_CLIENT_ID",
            "env:YOUTUBE_OAUTH_CLIENT_SECRET",
        }

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_credential_reference_count{dimension="status",value="checked"} 10'
            in metrics.text
        )
        assert (
            'dialecticore_credential_reference_count{dimension="status",value="resolved"} 5'
            in metrics.text
        )
        assert (
            'dialecticore_credential_reference_count{dimension="status",value="unavailable"} 5'
            in metrics.text
        )
        assert (
            'dialecticore_credential_reference_count{dimension="owner_type",'
            'value="publisher_target"} 4'
        ) in metrics.text
        assert (
            'dialecticore_credential_reference_count{dimension="scheme",value="env"} 9'
            in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="active",kind="references"} 10'
            in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="active",kind="unavailable"} 5'
            in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="all",kind="references"} 14'
            in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="all",kind="unavailable"} 9'
            in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="inactive",kind="unavailable"} 4'
            in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="all",kind="env_vars"} 11'
            in metrics.text
        )

        provisioning = client.get("/api/v1/system/credential-provisioning")
        assert provisioning.status_code == 200
        provisioning_body = provisioning.json()
        assert provisioning_body["schema_version"] == "credential_provisioning_plan.v1"
        assert provisioning_body["status"] == "fail"
        assert provisioning_body["include_disabled"] is True
        assert provisioning_body["summary"]["reference_count"] == 14
        assert provisioning_body["summary"]["unavailable_count"] == 9
        assert "YOUTUBE_OAUTH_ACCESS_TOKEN" in provisioning_body["env_vars"]
        assert "YOUTUBE_OAUTH_REFRESH_TOKEN" in provisioning_body["env_vars"]
        assert "YOUTUBE_OAUTH_CLIENT_SECRET" in provisioning_body["env_vars"]
        assert (
            "MODEL_TOKEN=<provisioned-secret>"
            in (provisioning_body["compose_environment_examples"])
        )
        youtube_disabled_refs = [
            item
            for item in provisioning_body["references"]
            if item["owner_id"] == "youtube-resumable"
        ]
        assert len(youtube_disabled_refs) == 4
        assert {item["field"] for item in youtube_disabled_refs} == {
            "credential_reference",
            "capabilities.oauth_refresh_token_reference",
            "capabilities.oauth_client_id_reference",
            "capabilities.oauth_client_secret_reference",
        }

        active_only = client.get("/api/v1/system/credential-provisioning?include_disabled=false")
        assert active_only.status_code == 200
        active_body = active_only.json()
        assert active_body["include_disabled"] is False
        assert active_body["summary"]["reference_count"] == 10
        assert all(item["owner_id"] != "youtube-resumable" for item in active_body["references"])

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert checks["credential_references"]["status"] == "fail"
        assert checks["credential_references"]["details"]["checked_count"] == 10
        assert checks["credential_references"]["details"]["resolved_count"] == 5
        assert checks["credential_references"]["details"]["unavailable_count"] == 5
        assert checks["credential_references"]["details"]["unsupported_count"] == 0
        assert checks["credential_references"]["details"]["by_owner_type"] == {
            "comfyui_endpoint": 1,
            "model_endpoint": 1,
            "publisher_target": 4,
            "settings": 3,
            "voicebox_endpoint": 1,
        }
        assert checks["credential_references"]["details"]["by_scheme"] == {
            "env": 9,
            "file": 1,
        }
        assert len(checks["credential_references"]["details"]["references"]) == 10
        assert checks["credential_references"]["details"]["readiness_checks"] == {
            "active_credential_references_resolve": False,
            "credential_reference_schemes_supported": True,
        }
        assert checks["credential_references"]["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve"
        ]
        assert checks["credential_provisioning"]["status"] == "fail"
        assert checks["credential_provisioning"]["details"]["active_reference_count"] == 10
        assert checks["credential_provisioning"]["details"]["active_unavailable_count"] == 5
        assert checks["credential_provisioning"]["details"]["all_reference_count"] == 14
        assert checks["credential_provisioning"]["details"]["all_unavailable_count"] == 9
        assert checks["credential_provisioning"]["details"]["inactive_unavailable_count"] == 4
        assert checks["credential_provisioning"]["details"]["env_var_count"] == 11
        assert checks["credential_provisioning"]["details"]["docker_secret_count"] == 0
        assert checks["credential_provisioning"]["details"]["file_count"] == 1
        assert checks["credential_provisioning"]["details"]["unsupported_count"] == 0
        assert checks["credential_provisioning"]["details"]["attention_count"] == 9
        assert checks["credential_provisioning"]["details"]["readiness_checks"] == {
            "active_credential_references_resolve": False,
            "disabled_target_credential_references_resolve": False,
            "credential_reference_schemes_supported": True,
        }
        assert checks["credential_provisioning"]["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve",
            "disabled_target_credential_references_resolve",
        ]
        assert len(checks["credential_provisioning"]["details"]["missing_active_references"]) == 5
        assert len(checks["credential_provisioning"]["details"]["missing_inactive_references"]) == 4
        assert (
            checks["credential_provisioning"]["details"]["live_readiness_policy"]
            == "active missing credentials block live runs; missing disabled-target "
            "credentials warn for future live cutover"
        )
        assert (
            "one or more active credential references are unavailable"
            in checks["credential_provisioning"]["blockers"]
        )
        assert (
            "one or more disabled live-target credential references are unavailable"
            in checks["credential_provisioning"]["warnings"]
        )
    finally:
        app.dependency_overrides.clear()


def test_database_password_reference_is_reported_in_credential_readiness() -> None:
    repository = EpisodeRepository()
    settings = Settings(
        database_url="",
        database_host="postgres",
        database_password_reference="env:MISSING_DATABASE_PASSWORD",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        credentials = components["credential_references"]
        assert credentials["status"] == "degraded"
        assert credentials["details"]["checked_count"] == 1
        assert credentials["details"]["unavailable_count"] == 1
        assert credentials["details"]["by_owner_type"] == {"settings": 1}
        assert credentials["details"]["by_scheme"] == {"env": 1}
        assert credentials["details"]["references"] == [
            {
                "owner_type": "settings",
                "owner_id": "database",
                "field": "database_password_reference",
                "reference": "env:MISSING_DATABASE_PASSWORD",
                "scheme": "env",
                "status": "unavailable",
                "error": "RuntimeError",
                "reason": "credential reference is not available",
            }
        ]
        deployment = components["deployment_readiness"]
        assert deployment["details"]["database_resolution_error"] == (
            "credential reference is not available"
        )
        assert deployment["details"]["readiness_checks"]["database_url_resolved"] is False
        assert "database_url_resolved" in deployment["details"]["failed_readiness_checks"]

        provisioning = client.get("/api/v1/system/credential-provisioning?include_disabled=false")
        assert provisioning.status_code == 200
        provisioning_body = provisioning.json()
        assert provisioning_body["summary"]["reference_count"] == 1
        assert provisioning_body["summary"]["unavailable_count"] == 1
        assert provisioning_body["env_vars"] == ["MISSING_DATABASE_PASSWORD"]
        assert provisioning_body["references"][0]["owner_id"] == "database"
        assert provisioning_body["references"][0]["field"] == "database_password_reference"

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert checks["credential_references"]["status"] == "fail"
        assert checks["credential_references"]["details"]["checked_count"] == 1
        assert checks["credential_provisioning"]["status"] == "fail"
        assert checks["credential_provisioning"]["details"]["active_reference_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_raw_settings_credential_reference_is_not_reflected_in_health_payload() -> None:
    raw_token = "leaked-raw-auth-api-token"
    repository = EpisodeRepository()
    settings = Settings(auth_enabled=True, auth_api_key_reference=raw_token)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        body = health.text
        components = {component["name"]: component for component in health.json()["components"]}
        credentials = components["credential_references"]

        assert raw_token not in body
        assert credentials["details"]["unsupported_count"] == 0
        assert credentials["details"]["invalid_count"] == 1
        assert credentials["details"]["by_scheme"] == {"invalid": 1}
        assert credentials["details"]["references"] == [
            {
                "owner_type": "settings",
                "owner_id": "auth",
                "field": "auth_api_key_reference",
                "reference": "[invalid]",
                "scheme": "invalid",
                "status": "unavailable",
                "error": "RuntimeError",
                "reason": "credential_reference must use scheme:target syntax",
            }
        ]
        assert credentials["details"]["readiness_checks"] == {
            "active_credential_references_resolve": False,
            "credential_reference_schemes_supported": False,
        }
        assert credentials["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve",
            "credential_reference_schemes_supported",
        ]

        provisioning = components["credential_provisioning"]
        assert provisioning["details"]["unsupported_count"] == 0
        assert provisioning["details"]["invalid_count"] == 1
        assert provisioning["details"]["readiness_checks"] == {
            "active_credential_references_resolve": False,
            "disabled_target_credential_references_resolve": False,
            "credential_reference_schemes_supported": False,
        }

        plan = client.get("/api/v1/system/credential-provisioning?include_disabled=false")
        assert plan.status_code == 200
        plan_body = plan.json()
        assert raw_token not in plan.text
        assert plan_body["summary"]["unsupported_count"] == 0
        assert plan_body["summary"]["invalid_count"] == 1
        assert "one or more credential references have invalid syntax" in plan_body["blockers"]

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert raw_token not in readiness.text
        assert checks["credential_references"]["details"]["invalid_count"] == 1
        assert checks["credential_references"]["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve",
            "credential_reference_schemes_supported",
        ]
        assert checks["credential_provisioning"]["details"]["invalid_count"] == 1
        assert (
            "credential_reference_schemes_supported"
            in (checks["credential_provisioning"]["details"]["failed_readiness_checks"])
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_credential_reference_count{dimension="scheme",value="invalid"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="all",kind="invalid"} 1'
            in metrics.text
        )
        assert (
            'dialecticore_component_readiness_check{component="credential_references",'
            'check="credential_reference_schemes_supported",status="fail"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_minio_docker_secrets_are_reported_in_credential_provisioning(
    monkeypatch,
    tmp_path: Path,
) -> None:
    secret_root = tmp_path / "docker-secrets"
    secret_root.mkdir()
    (secret_root / "minio_root_user").write_text("operator-minio-user\n", encoding="utf-8")
    (secret_root / "minio_root_password").write_text(
        "operator-minio-password\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(SecretResolver, "docker_secret_root", secret_root)

    repository = EpisodeRepository()
    settings = Settings(
        object_storage_backend="s3",
        object_storage_endpoint="http://minio:9000",
        object_storage_bucket="dialecticore",
        object_storage_access_key_reference="docker-secret:minio_root_user",
        object_storage_secret_key_reference="docker-secret:minio_root_password",
    )
    health_service = SystemHealthService(settings)
    monkeypatch.setattr(
        health_service,
        "_object_storage_endpoint_tcp_probe",
        lambda endpoint: {"reachable": True, "host": "minio", "port": 9000},
    )
    monkeypatch.setattr(
        health_service,
        "_object_storage_bucket_probe",
        lambda: {
            "available": True,
            "bucket": "dialecticore",
            "probe": "head_bucket",
        },
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: health_service
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        active_only = client.get("/api/v1/system/credential-provisioning?include_disabled=false")
        assert active_only.status_code == 200
        active_body = active_only.json()
        assert active_body["status"] == "pass"
        assert active_body["summary"]["reference_count"] == 2
        assert active_body["summary"]["resolved_count"] == 2
        assert active_body["summary"]["docker_secret_count"] == 2
        assert active_body["docker_secrets"] == [
            "minio_root_password",
            "minio_root_user",
        ]
        assert active_body["docker_secret_examples"] == [
            "minio_root_password: external: true",
            "minio_root_user: external: true",
        ]
        assert {
            (item["field"], item["reference"], item["status"], item["target"])
            for item in active_body["references"]
        } == {
            (
                "object_storage_access_key_reference",
                "docker-secret:minio_root_user",
                "resolved",
                "minio_root_user",
            ),
            (
                "object_storage_secret_key_reference",
                "docker-secret:minio_root_password",
                "resolved",
                "minio_root_password",
            ),
        }

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        credentials = components["credential_references"]
        assert credentials["details"]["by_scheme"]["docker-secret"] == 2
        assert credentials["details"]["by_owner_type"]["settings"] == 2
        provisioning = components["credential_provisioning"]
        assert provisioning["details"]["active_reference_count"] == 2
        assert provisioning["details"]["active_unavailable_count"] == 0
        assert provisioning["details"]["docker_secret_count"] == 2

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert checks["credential_references"]["status"] == "pass"
        assert checks["credential_provisioning"]["status"] == "warning"
        assert checks["credential_provisioning"]["details"]["docker_secret_count"] == 2
        assert checks["credential_provisioning"]["details"]["active_unavailable_count"] == 0

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_credential_reference_count{dimension="scheme",'
            'value="docker-secret"} 2' in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="all",'
            'kind="docker_secrets"} 2' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_credential_readiness_flags_unsupported_reference_schemes() -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_backend="local")
    repository.upsert_model_endpoint(
        ModelEndpoint(
            id="vault-model",
            name="Vault Model",
            provider_type="openai_compatible",
            base_url="https://models.example.test",
            credential_reference="vault:model-token",
            enabled=True,
        )
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        credentials = components["credential_references"]
        assert credentials["status"] == "degraded"
        assert credentials["details"]["checked_count"] == 1
        assert credentials["details"]["resolved_count"] == 0
        assert credentials["details"]["unavailable_count"] == 1
        assert credentials["details"]["unsupported_count"] == 1
        assert credentials["details"]["invalid_count"] == 0
        assert credentials["details"]["by_scheme"] == {"unsupported": 1}
        assert credentials["details"]["references"] == [
            {
                "owner_type": "model_endpoint",
                "owner_id": "vault-model",
                "field": "credential_reference",
                "reference": "vault:[unsupported]",
                "scheme": "unsupported",
                "status": "unavailable",
                "error": "RuntimeError",
                "reason": "unsupported credential reference scheme",
            }
        ]
        assert credentials["details"]["readiness_checks"] == {
            "active_credential_references_resolve": False,
            "credential_reference_schemes_supported": False,
        }
        assert credentials["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve",
            "credential_reference_schemes_supported",
        ]

        provisioning = client.get("/api/v1/system/credential-provisioning?include_disabled=false")
        assert provisioning.status_code == 200
        provisioning_body = provisioning.json()
        assert provisioning_body["summary"]["unsupported_count"] == 1
        assert provisioning_body["summary"]["invalid_count"] == 0
        assert provisioning_body["references"][0]["scheme"] == "unsupported"
        assert provisioning_body["references"][0]["status"] == "unavailable"
        assert provisioning_body["references"][0]["target"] == ""
        assert (
            "one or more credential references use an unsupported scheme"
            in provisioning_body["blockers"]
        )

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert checks["credential_references"]["status"] == "fail"
        assert checks["credential_references"]["details"]["unsupported_count"] == 1
        assert checks["credential_references"]["details"]["invalid_count"] == 0
        assert checks["credential_references"]["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve",
            "credential_reference_schemes_supported",
        ]
        assert checks["credential_provisioning"]["status"] == "fail"
        assert checks["credential_provisioning"]["details"]["unsupported_count"] == 1
        assert checks["credential_provisioning"]["details"]["invalid_count"] == 0
        assert checks["credential_provisioning"]["details"]["failed_readiness_checks"] == [
            "active_credential_references_resolve",
            "disabled_target_credential_references_resolve",
            "credential_reference_schemes_supported",
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_credential_reference_count{dimension="scheme",'
            'value="unsupported"} 1' in metrics.text
        )
        assert (
            'dialecticore_credential_provisioning_count{scope="all",kind="unsupported"} 1'
            in metrics.text
        )
        assert (
            "dialecticore_component_readiness_check"
            '{component="credential_references",'
            'check="credential_reference_schemes_supported",status="fail"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_live_provider_readiness_warns_on_disabled_credential_provisioning_gap(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        auth_enabled=True,
        object_storage_backend="local",
        object_storage_local_path=str(tmp_path / "object-store"),
    )
    repository.upsert_publisher_target(
        PublisherTarget(
            id="future-live-youtube",
            name="Future Live YouTube",
            platform="youtube",
            adapter_type="youtube_resumable",
            base_url="https://youtube.example.test",
            enabled=False,
            capabilities={
                "oauth_refresh_token_reference": "env:MISSING_DISABLED_REFRESH_TOKEN",
                "oauth_client_id_reference": "env:MISSING_DISABLED_CLIENT_ID",
                "oauth_client_secret_reference": "env:MISSING_DISABLED_CLIENT_SECRET",
            },
        )
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        response = client.get("/api/v1/system/live-provider-readiness")
        assert response.status_code == 200
        checks = {check["category"]: check for check in response.json()["checks"]}
        provisioning = checks["credential_provisioning"]
        assert provisioning["status"] == "warning"
        assert provisioning["blockers"] == []
        assert (
            "one or more disabled live-target credential references are unavailable"
            in provisioning["warnings"]
        )
        assert provisioning["details"]["active_unavailable_count"] == 0
        assert provisioning["details"]["inactive_unavailable_count"] == 7
        assert provisioning["details"]["attention_count"] == 7
        assert (
            provisioning["details"]["live_readiness_policy"]
            == "active missing credentials block live runs; missing disabled-target "
            "credentials warn for future live cutover"
        )
    finally:
        app.dependency_overrides.clear()


def test_system_health_and_metrics_probe_s3_object_storage(monkeypatch) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_backend="s3",
        object_storage_endpoint="http://minio:9000",
        object_storage_bucket="dialecticore",
        object_storage_access_key_reference="env:MINIO_ROOT_USER",
        object_storage_secret_key_reference="env:MINIO_ROOT_PASSWORD",
    )
    health_service = SystemHealthService(settings)
    monkeypatch.setattr(
        health_service,
        "_object_storage_endpoint_tcp_probe",
        lambda endpoint: {"reachable": True, "host": "minio", "port": 9000},
    )
    monkeypatch.setattr(
        health_service,
        "_object_storage_bucket_probe",
        lambda: {
            "available": True,
            "bucket": "dialecticore",
            "probe": "head_bucket",
        },
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: health_service
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        object_storage = next(
            component
            for component in health.json()["components"]
            if component["name"] == "object_storage"
        )
        assert object_storage["status"] == "healthy"
        assert object_storage["details"]["backend"] == "s3"
        assert object_storage["details"]["tcp_probe"] == {
            "reachable": True,
            "host": "minio",
            "port": 9000,
        }
        assert object_storage["details"]["credentials_ready"] is True
        assert object_storage["details"]["bucket_available"] is True
        assert object_storage["details"]["bucket_probe"] == {
            "available": True,
            "bucket": "dialecticore",
            "probe": "head_bucket",
        }
        assert object_storage["details"]["readiness_checks"] == {
            "endpoint_reachable": True,
            "credential_pair_configured": True,
            "bucket_configured": True,
            "bucket_available": True,
        }
        assert object_storage["details"]["failed_readiness_checks"] == []

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_object_storage_remote_reachable"
            '{backend="s3",bucket="dialecticore",host="minio",port="9000"} 1'
        ) in metrics.text
        assert (
            "dialecticore_object_storage_bucket_available"
            '{backend="s3",bucket="dialecticore",probe="head_bucket"} 1'
        ) in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        storage_readiness = checks["object_storage"]
        assert storage_readiness["status"] == "pass"
        assert storage_readiness["details"]["backend"] == "s3"
        assert storage_readiness["details"]["bucket"] == "dialecticore"
        assert storage_readiness["details"]["endpoint"] == "http://minio:9000"
        assert storage_readiness["details"]["tcp_probe"] == {
            "reachable": True,
            "host": "minio",
            "port": 9000,
        }
        assert storage_readiness["details"]["credentials_ready"] is True
        assert storage_readiness["details"]["bucket_configured"] is True
        assert storage_readiness["details"]["bucket_available"] is True
        assert storage_readiness["details"]["bucket_probe"] == {
            "available": True,
            "bucket": "dialecticore",
            "probe": "head_bucket",
        }
        assert storage_readiness["details"]["readiness_checks"] == {
            "endpoint_reachable": True,
            "credential_pair_configured": True,
            "bucket_configured": True,
            "bucket_available": True,
        }
        assert storage_readiness["details"]["failed_readiness_checks"] == []
    finally:
        app.dependency_overrides.clear()


def test_system_health_degrades_when_s3_bucket_probe_fails(monkeypatch) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_backend="s3",
        object_storage_endpoint="http://minio:9000",
        object_storage_bucket="dialecticore",
        object_storage_access_key_reference="env:MINIO_ROOT_USER",
        object_storage_secret_key_reference="env:MINIO_ROOT_PASSWORD",
    )
    health_service = SystemHealthService(settings)
    monkeypatch.setattr(
        health_service,
        "_object_storage_endpoint_tcp_probe",
        lambda endpoint: {"reachable": True, "host": "minio", "port": 9000},
    )
    monkeypatch.setattr(
        health_service,
        "_object_storage_bucket_probe",
        lambda: {
            "available": False,
            "error": "ClientError",
            "reason": "Not Found",
        },
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: health_service
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        object_storage = next(
            component
            for component in health.json()["components"]
            if component["name"] == "object_storage"
        )
        assert object_storage["status"] == "degraded"
        assert object_storage["details"]["bucket_available"] is False
        assert object_storage["details"]["bucket_probe"]["error"] == "ClientError"
        assert object_storage["details"]["readiness_checks"] == {
            "endpoint_reachable": True,
            "credential_pair_configured": True,
            "bucket_configured": True,
            "bucket_available": False,
        }
        assert object_storage["details"]["failed_readiness_checks"] == ["bucket_available"]
        assert (
            object_storage["details"]["reason"]
            == "S3 bucket is not reachable with configured credentials"
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_object_storage_bucket_available"
            '{backend="s3",bucket="dialecticore",probe=""} 0'
        ) in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        storage_readiness = checks["object_storage"]
        assert storage_readiness["status"] == "fail"
        assert storage_readiness["details"]["backend"] == "s3"
        assert storage_readiness["details"]["tcp_probe"] == {
            "reachable": True,
            "host": "minio",
            "port": 9000,
        }
        assert storage_readiness["details"]["credentials_ready"] is True
        assert storage_readiness["details"]["bucket_configured"] is True
        assert storage_readiness["details"]["bucket_available"] is False
        assert storage_readiness["details"]["bucket_probe"]["error"] == "ClientError"
        assert storage_readiness["details"]["bucket_probe"]["reason"] == "Not Found"
        assert storage_readiness["details"]["readiness_checks"] == {
            "endpoint_reachable": True,
            "credential_pair_configured": True,
            "bucket_configured": True,
            "bucket_available": False,
        }
        assert storage_readiness["details"]["failed_readiness_checks"] == ["bucket_available"]
        assert (
            "S3 bucket is not reachable with configured credentials"
            in storage_readiness["blockers"]
        )
    finally:
        app.dependency_overrides.clear()


def test_system_health_and_metrics_probe_redis_runtime(monkeypatch, tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        redis_url="redis://redis:6379/0",
        redis_event_fanout_enabled=True,
        redis_event_channel="dialecticore:test-events",
        redis_worker_signal_enabled=True,
        redis_worker_signal_stream="dialecticore:test-signals",
        redis_worker_signal_maxlen=25,
    )
    health_service = SystemHealthService(settings)
    monkeypatch.setattr(
        health_service,
        "_redis_tcp_probe",
        lambda redis_url: {"reachable": True, "host": "redis", "port": 6379},
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: health_service
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        redis_component = next(
            component for component in health.json()["components"] if component["name"] == "redis"
        )
        assert redis_component["status"] == "healthy"
        assert redis_component["details"]["tcp_probe"] == {
            "reachable": True,
            "host": "redis",
            "port": 6379,
        }
        assert redis_component["details"]["event_fanout_enabled"] is True
        assert redis_component["details"]["worker_signal_enabled"] is True
        assert redis_component["details"]["readiness_checks"] == {
            "redis_modes_enabled": True,
            "url_configured": True,
            "event_channel_configured": True,
            "worker_signal_stream_configured": True,
            "worker_signal_maxlen_valid": True,
            "redis_reachable": True,
        }
        assert redis_component["details"]["failed_readiness_checks"] == []

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert 'dialecticore_redis_runtime_enabled{mode="event_fanout"} 1' in metrics.text
        assert 'dialecticore_redis_runtime_enabled{mode="worker_signal"} 1' in metrics.text
        assert (
            'dialecticore_redis_runtime_reachable{host="redis",port="6379",'
            'event_channel="dialecticore:test-events",'
            'worker_signal_stream="dialecticore:test-signals"} 1'
        ) in metrics.text
        assert "dialecticore_redis_worker_signal_maxlen 25" in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        redis_readiness = checks["redis"]
        assert redis_readiness["status"] == "pass"
        assert redis_readiness["details"]["url_configured"] is True
        assert redis_readiness["details"]["event_fanout_enabled"] is True
        assert redis_readiness["details"]["event_channel"] == "dialecticore:test-events"
        assert redis_readiness["details"]["worker_signal_enabled"] is True
        assert redis_readiness["details"]["worker_signal_stream"] == "dialecticore:test-signals"
        assert redis_readiness["details"]["worker_signal_maxlen"] == 25
        assert redis_readiness["details"]["tcp_probe"] == {
            "reachable": True,
            "host": "redis",
            "port": 6379,
        }
        assert redis_readiness["details"]["readiness_checks"] == {
            "redis_modes_enabled": True,
            "url_configured": True,
            "event_channel_configured": True,
            "worker_signal_stream_configured": True,
            "worker_signal_maxlen_valid": True,
            "redis_reachable": True,
        }
        assert redis_readiness["details"]["failed_readiness_checks"] == []
    finally:
        app.dependency_overrides.clear()


def test_system_health_degrades_reachable_redis_with_blank_runtime_names(
    monkeypatch, tmp_path: Path
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        redis_url="redis://redis:6379/0",
        redis_event_fanout_enabled=True,
        redis_event_channel=" ",
        redis_worker_signal_enabled=True,
        redis_worker_signal_stream="",
        redis_worker_signal_maxlen=25,
    )
    health_service = SystemHealthService(settings)
    monkeypatch.setattr(
        health_service,
        "_redis_tcp_probe",
        lambda redis_url: {"reachable": True, "host": "redis", "port": 6379},
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: health_service
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        redis_component = next(
            component for component in health.json()["components"] if component["name"] == "redis"
        )
        assert redis_component["status"] == "degraded"
        assert redis_component["details"]["tcp_probe"] == {
            "reachable": True,
            "host": "redis",
            "port": 6379,
        }
        assert redis_component["details"]["readiness_checks"] == {
            "redis_modes_enabled": True,
            "url_configured": True,
            "event_channel_configured": False,
            "worker_signal_stream_configured": False,
            "worker_signal_maxlen_valid": True,
            "redis_reachable": True,
        }
        assert redis_component["details"]["failed_readiness_checks"] == [
            "event_channel_configured",
            "worker_signal_stream_configured",
        ]
        assert (
            redis_component["details"]["reason"]
            == "Redis is reachable but runtime configuration has failed readiness checks"
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_redis_runtime_reachable{host="redis",port="6379",'
            'event_channel=" ",worker_signal_stream=""} 1'
        ) in metrics.text
        assert (
            'dialecticore_component_readiness_check{component="redis",'
            'check="event_channel_configured",status="fail"} 1'
        ) in metrics.text
        assert (
            'dialecticore_component_readiness_check{component="redis",'
            'check="worker_signal_stream_configured",status="fail"} 1'
        ) in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        redis_readiness = checks["redis"]
        assert redis_readiness["status"] == "fail"
        assert redis_readiness["details"]["readiness_checks"] == {
            "redis_modes_enabled": True,
            "url_configured": True,
            "event_channel_configured": False,
            "worker_signal_stream_configured": False,
            "worker_signal_maxlen_valid": True,
            "redis_reachable": True,
        }
        assert redis_readiness["details"]["failed_readiness_checks"] == [
            "event_channel_configured",
            "worker_signal_stream_configured",
        ]
        assert redis_readiness["blockers"] == [
            "Redis is reachable but runtime configuration has failed readiness checks"
        ]
    finally:
        app.dependency_overrides.clear()


def test_production_run_attention_count_deduplicates_overlapping_reasons(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        now = datetime.now(UTC)
        episode.status = EpisodeStatus.failed
        episode.workflow_control = {
            "paused": True,
            "paused_at": now.isoformat(),
            "run": {
                "run_id": "run-overlap",
                "state": "failed",
                "current_stage": "render",
                "updated_at": now.isoformat(),
            },
        }
        repository.save(episode)

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        body = health.json()
        components = {component["name"]: component for component in body["components"]}
        production_runs = components["production_runs"]
        assert production_runs["status"] == "degraded"
        assert production_runs["details"]["production_run_count"] == 1
        assert production_runs["details"]["active_production_runs"] == 1
        assert production_runs["details"]["paused_active_production_runs"] == 1
        assert production_runs["details"]["failed_active_production_runs"] == 1
        assert production_runs["details"]["attention_count"] == 1
        assert production_runs["details"]["by_attention_reason"] == {
            "failed": 1,
            "paused": 1,
        }
        assert len(production_runs["details"]["attention_runs"]) == 1
        assert production_runs["details"]["attention_runs"][0]["attention_reasons"] == [
            "paused",
            "failed",
        ]
        assert body["counts"]["production_runs_needing_attention"] == 1

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert 'dialecticore_production_run_count{kind="attention"} 1' in metrics.text
        assert 'dialecticore_production_run_count{kind="paused_active"} 1' in metrics.text
        assert 'dialecticore_production_run_count{kind="failed_active"} 1' in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        run_readiness = checks["production_runs"]
        assert run_readiness["status"] == "fail"
        assert run_readiness["details"]["attention_count"] == 1
        assert run_readiness["details"]["by_attention_reason"] == {
            "failed": 1,
            "paused": 1,
        }
        assert run_readiness["details"]["failed_readiness_checks"] == [
            "no_failed_active_production_runs",
            "no_paused_active_production_runs",
        ]
        assert run_readiness["blockers"] == ["one or more active production runs are failed"]
        assert run_readiness["warnings"] == ["one or more active production runs are paused"]
    finally:
        app.dependency_overrides.clear()


def test_media_queue_readiness_warns_on_submitted_and_running_work(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        episode.assets.extend(
            [
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.audio,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-a",
                    status="submitted",
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.video,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-v",
                    status="running",
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.subtitle,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id="turn-s",
                    status="submitted",
                ),
            ]
        )
        repository.save(episode)

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        media_queues = checks["media_queues"]
        assert media_queues["status"] == "warning"
        assert media_queues["details"]["failed_assets"] == 0
        assert media_queues["details"]["pending_audio_jobs"] == 1
        assert media_queues["details"]["submitted_audio_jobs"] == 1
        assert media_queues["details"]["pending_visual_jobs"] == 1
        assert media_queues["details"]["running_visual_jobs"] == 1
        assert media_queues["details"]["pending_subtitle_jobs"] == 1
        assert media_queues["details"]["submitted_subtitle_jobs"] == 1
        assert media_queues["details"]["pending_job_count"] == 3
        assert media_queues["details"]["attention_count"] == 3
        assert media_queues["details"]["readiness_checks"] == {
            "no_failed_media_assets": True,
            "no_pending_audio_jobs": False,
            "no_pending_visual_jobs": False,
            "no_pending_subtitle_jobs": False,
        }
        assert media_queues["details"]["failed_readiness_checks"] == [
            "no_pending_audio_jobs",
            "no_pending_visual_jobs",
            "no_pending_subtitle_jobs",
        ]
        assert media_queues["blockers"] == []
        assert media_queues["warnings"] == ["one or more media jobs are pending or running"]
    finally:
        app.dependency_overrides.clear()


def test_media_queue_readiness_ignores_terminal_cancelled_failed_assets(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        episode.status = EpisodeStatus.cancelled
        episode.assets.append(
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.video,
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id="turn-v",
                status="failed",
            )
        )
        repository.save(episode)

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        media_queues = checks["media_queues"]
        assert media_queues["status"] == "pass"
        assert media_queues["details"]["failed_assets"] == 1
        assert media_queues["details"]["failed_visual_assets"] == 1
        assert media_queues["details"]["current_failed_assets"] == 0
        assert media_queues["details"]["current_failed_visual_assets"] == 0
        assert media_queues["details"]["readiness_checks"] == {
            "no_failed_media_assets": True,
            "no_pending_audio_jobs": True,
            "no_pending_visual_jobs": True,
            "no_pending_subtitle_jobs": True,
        }
        assert media_queues["blockers"] == []
    finally:
        app.dependency_overrides.clear()


def test_live_readiness_comfyui_endpoint_includes_prompt_admission_reason(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        endpoint = repository.get_comfyui_endpoint("mock-comfyui")
        endpoint.enabled = False
        repository.upsert_comfyui_endpoint(endpoint)
        repository.upsert_comfyui_endpoint(
            ComfyUiEndpoint(
                id="b1-comfyui",
                name="B1 Native ComfyUI",
                adapter_type="comfyui_http",
                base_url="https://comfy.ai.b1.germering",
                credential_reference="env:B1_API_KEY",
                enabled=True,
                health_status="unhealthy",
                capabilities={
                    "native_comfyui": True,
                    "prompt_admission_ready": False,
                    "prompt_admission_probe": {
                        "ready": False,
                        "status_code": 503,
                        "response": {
                            "detail": {
                                "code": "hardware_resource_policy",
                                "message": ("GPU admission blocked by hardware resource policy"),
                                "hardware_resource_policy": {
                                    "detail": (
                                        "largest GPU free VRAM is 825 MiB; policy reserve "
                                        "requires 1024 MiB"
                                    )
                                },
                            }
                        },
                    },
                },
            )
        )

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        unhealthy = checks["comfyui"]["details"]["unhealthy_endpoints"]
        assert unhealthy == [
            {
                "id": "b1-comfyui",
                "name": "B1 Native ComfyUI",
                "adapter_type": "comfyui_http",
                "health_status": "unhealthy",
                "base_url_configured": True,
                "prompt_admission": {
                    "ready": False,
                    "status_code": 503,
                    "code": "hardware_resource_policy",
                    "message": "GPU admission blocked by hardware resource policy",
                    "detail": "largest GPU free VRAM is 825 MiB; policy reserve requires 1024 MiB",
                },
            }
        ]
    finally:
        app.dependency_overrides.clear()


def test_live_readiness_includes_failed_managed_media_smoke(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    smoke_path = tmp_path / "b1-managed-media-smoke.json"
    smoke_path.write_text(
        json.dumps(
            {
                "schema_version": "b1_managed_media_smoke_evidence.v1",
                "status": "runner_failed",
                "model": "image-default",
                "operation": "image-generation",
                "terminal": {
                    "state": "failed",
                    "failure_category": "gpu_runner_error",
                    "failure_message": "ValueError",
                    "artifact_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        b1_managed_media_smoke_evidence_path=str(smoke_path),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        smoke = checks["managed_media_smoke"]
        assert smoke["status"] == "fail"
        assert smoke["blockers"] == ["latest B1 managed media smoke did not complete successfully"]
        assert smoke["details"]["status"] == "runner_failed"
        assert smoke["details"]["failure_category"] == "gpu_runner_error"
        assert smoke["details"]["action"] == ("fix_b1_managed_media_runner_then_rerun_smoke")
        assert smoke["details"]["failed_readiness_checks"] == ["managed_media_smoke_passed"]
    finally:
        app.dependency_overrides.clear()


def test_publish_readiness_warns_on_package_missing_production_manifest() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(Settings())
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        package = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            source_entity_type="render_asset",
            source_entity_id="render-final",
            storage_uri="object://dialecticore/exports/package.zip",
            checksum="sha256:package",
            status="completed",
        )
        episode.assets.append(package)
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="export_package_asset",
                target_id=str(package.id),
                check_type="youtube_package_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        repository.save(episode)

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["counts"]["completed_export_packages"] == 1
        assert health.json()["counts"]["production_manifest_assets"] == 0
        assert health.json()["counts"]["packages_missing_package_qc"] == 0
        assert health.json()["counts"]["packages_failing_package_qc"] == 0
        assert health.json()["counts"]["packages_missing_production_manifest"] == 1

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        publish_jobs = checks["publish_jobs"]
        assert publish_jobs["status"] == "warning"
        assert publish_jobs["details"]["completed_export_packages"] == 1
        assert publish_jobs["details"]["production_manifest_assets"] == 0
        assert publish_jobs["details"]["packages_missing_package_qc"] == 0
        assert publish_jobs["details"]["packages_failing_package_qc"] == 0
        assert publish_jobs["details"]["packages_missing_production_manifest"] == 1
        assert publish_jobs["details"]["attention_count"] == 1
        assert publish_jobs["details"]["readiness_checks"] == {
            "no_failed_publish_jobs": True,
            "no_packages_missing_package_qc": True,
            "no_packages_failing_package_qc": True,
            "no_packages_missing_thumbnails": True,
            "no_packages_missing_subtitles": True,
            "no_submitted_publish_jobs": True,
            "no_invalid_production_manifests": True,
            "no_packages_missing_production_manifest": False,
        }
        assert publish_jobs["details"]["failed_readiness_checks"] == [
            "no_packages_missing_production_manifest"
        ]
        assert publish_jobs["details"]["latest_package_missing_production_manifest"] == {
            "episode_id": str(episode.id),
            "package_asset_id": str(package.id),
            "source_entity_type": "render_asset",
            "source_entity_id": "render-final",
            "language": None,
            "storage_uri_present": True,
            "checksum_present": True,
            "created_at": package.created_at.isoformat(),
        }
        assert publish_jobs["warnings"] == [
            "one or more completed export packages are missing production manifests"
        ]
    finally:
        app.dependency_overrides.clear()


def test_publish_readiness_warns_on_invalid_production_manifest() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(Settings())
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        package = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            source_entity_type="render_asset",
            source_entity_id="render-final",
            storage_uri="object://dialecticore/exports/package.zip",
            checksum="sha256:package",
            status="completed",
        )
        invalid_manifest = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            source_entity_type="export_package",
            source_entity_id=str(package.id),
            storage_uri="object://dialecticore/manifests/production.json",
            checksum="sha256:production-manifest",
            status="completed",
            generation_metadata={"production_manifest": {"schema_version": "draft"}},
        )
        episode.assets.extend([package, invalid_manifest])
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="export_package_asset",
                target_id=str(package.id),
                check_type="youtube_package_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        repository.save(episode)

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["counts"]["completed_export_packages"] == 1
        assert health.json()["counts"]["production_manifest_assets"] == 0
        assert health.json()["counts"]["invalid_production_manifest_assets"] == 1
        assert health.json()["counts"]["packages_missing_package_qc"] == 0
        assert health.json()["counts"]["packages_failing_package_qc"] == 0
        assert health.json()["counts"]["packages_missing_production_manifest"] == 1

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        publish_jobs = checks["publish_jobs"]
        assert publish_jobs["status"] == "warning"
        assert publish_jobs["details"]["invalid_production_manifest_assets"] == 1
        assert publish_jobs["details"]["attention_count"] == 2
        assert publish_jobs["details"]["readiness_checks"] == {
            "no_failed_publish_jobs": True,
            "no_packages_missing_package_qc": True,
            "no_packages_failing_package_qc": True,
            "no_packages_missing_thumbnails": True,
            "no_packages_missing_subtitles": True,
            "no_submitted_publish_jobs": True,
            "no_invalid_production_manifests": False,
            "no_packages_missing_production_manifest": False,
        }
        assert publish_jobs["details"]["failed_readiness_checks"] == [
            "no_invalid_production_manifests",
            "no_packages_missing_production_manifest",
        ]
        assert publish_jobs["details"]["latest_invalid_production_manifest"] == {
            "episode_id": str(episode.id),
            "manifest_asset_id": str(invalid_manifest.id),
            "package_asset_id": str(package.id),
            "language": None,
            "storage_uri_present": True,
            "checksum_present": True,
            "created_at": invalid_manifest.created_at.isoformat(),
            "reason": "embedded production_manifest schema_version is invalid",
        }
        assert publish_jobs["warnings"] == [
            "one or more production manifest assets are invalid",
            "one or more completed export packages are missing production manifests",
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="invalid_production_manifest_assets"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_publish_readiness_warns_on_unlinked_production_manifest() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(Settings())
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        package = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            source_entity_type="render_asset",
            source_entity_id="render-final",
            storage_uri="object://dialecticore/exports/package.zip",
            checksum="sha256:package",
            status="completed",
        )
        invalid_manifest = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            source_entity_type="export_package",
            source_entity_id=str(package.id),
            storage_uri="object://dialecticore/manifests/production.json",
            checksum="sha256:production-manifest",
            status="completed",
            generation_metadata={
                "production_manifest": {
                    "schema_version": "production_manifest.v1",
                    "delivery_package": {},
                }
            },
        )
        episode.assets.extend([package, invalid_manifest])
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="export_package_asset",
                target_id=str(package.id),
                check_type="youtube_package_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        repository.save(episode)

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        publish_jobs = checks["publish_jobs"]
        assert publish_jobs["status"] == "warning"
        assert publish_jobs["details"]["production_manifest_assets"] == 0
        assert publish_jobs["details"]["invalid_production_manifest_assets"] == 1
        assert publish_jobs["details"]["packages_missing_production_manifest"] == 1
        assert publish_jobs["details"]["latest_invalid_production_manifest"]["reason"] == (
            "embedded delivery package asset_id is missing"
        )
        assert publish_jobs["details"]["failed_readiness_checks"] == [
            "no_invalid_production_manifests",
            "no_packages_missing_production_manifest",
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="invalid_production_manifest_assets"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_publish_readiness_warns_on_missing_manifest_talkshow_visual_handoff() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(Settings())
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        package = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            source_entity_type="render_asset",
            source_entity_id="render-final",
            storage_uri="object://dialecticore/exports/package.zip",
            checksum="sha256:package",
            status="completed",
        )
        manifest = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            source_entity_type="export_package",
            source_entity_id=str(package.id),
            storage_uri="object://dialecticore/manifests/production.json",
            checksum="sha256:production-manifest",
            status="completed",
            generation_metadata={
                "production_manifest": {
                    "schema_version": "production_manifest.v1",
                    "delivery_package": {"asset_id": str(package.id)},
                    "timeline_segments": [
                        {
                            "id": "segment-1",
                            "studio_scene_asset_id": "studio-scene",
                            "visual_layers": [
                                {"role": "reaction_loop", "asset_id": "reaction-loop"}
                            ],
                        }
                    ],
                }
            },
        )
        episode.assets.extend([package, manifest])
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="export_package_asset",
                target_id=str(package.id),
                check_type="youtube_package_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        repository.save(episode)

        readiness = client.get("/api/v1/system/live-provider-readiness")

        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        publish_jobs = checks["publish_jobs"]
        assert publish_jobs["details"]["invalid_production_manifest_assets"] == 1
        assert publish_jobs["details"]["latest_invalid_production_manifest"]["reason"] == (
            "embedded talkshow visual handoff is missing"
        )
        assert (
            publish_jobs["details"]["readiness_checks"]["no_invalid_production_manifests"] is False
        )
    finally:
        app.dependency_overrides.clear()


def test_publish_readiness_blocks_on_package_missing_qc() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(Settings())
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        package = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            source_entity_type="render_asset",
            source_entity_id="render-final",
            storage_uri="object://dialecticore/exports/package.zip",
            checksum="sha256:package",
            status="completed",
        )
        manifest = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            source_entity_type="export_package",
            source_entity_id=str(package.id),
            storage_uri="object://dialecticore/manifests/production.json",
            checksum="sha256:production-manifest",
            status="completed",
            generation_metadata={
                "production_manifest": {
                    "schema_version": "production_manifest.v1",
                    "delivery_package": {"asset_id": str(package.id)},
                }
            },
        )
        episode.assets.extend([package, manifest])
        repository.save(episode)

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        publish_jobs = checks["publish_jobs"]
        assert publish_jobs["status"] == "fail"
        assert publish_jobs["details"]["packages_missing_package_qc"] == 1
        assert publish_jobs["details"]["packages_failing_package_qc"] == 0
        assert publish_jobs["details"]["attention_count"] == 1
        assert publish_jobs["details"]["readiness_checks"] == {
            "no_failed_publish_jobs": True,
            "no_packages_missing_package_qc": False,
            "no_packages_failing_package_qc": True,
            "no_packages_missing_thumbnails": True,
            "no_packages_missing_subtitles": True,
            "no_submitted_publish_jobs": True,
            "no_invalid_production_manifests": True,
            "no_packages_missing_production_manifest": True,
        }
        assert publish_jobs["details"]["failed_readiness_checks"] == [
            "no_packages_missing_package_qc"
        ]
        assert publish_jobs["details"]["latest_package_missing_package_qc"] == {
            "episode_id": str(episode.id),
            "package_asset_id": str(package.id),
            "source_entity_type": "render_asset",
            "source_entity_id": "render-final",
            "language": None,
            "storage_uri_present": True,
            "checksum_present": True,
            "created_at": package.created_at.isoformat(),
        }
        assert publish_jobs["blockers"] == [
            "one or more completed export packages are missing package QC"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="packages_missing_package_qc"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_publish_readiness_blocks_on_package_failing_qc() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(Settings())
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        package = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            source_entity_type="render_asset",
            source_entity_id="render-final",
            storage_uri="object://dialecticore/exports/package.zip",
            checksum="sha256:package",
            status="completed",
        )
        manifest = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            source_entity_type="export_package",
            source_entity_id=str(package.id),
            storage_uri="object://dialecticore/manifests/production.json",
            checksum="sha256:production-manifest",
            status="completed",
            generation_metadata={
                "production_manifest": {
                    "schema_version": "production_manifest.v1",
                    "delivery_package": {"asset_id": str(package.id)},
                }
            },
        )
        qc = QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.fail,
            status="fail",
            details={"failure_count": 1},
        )
        episode.assets.extend([package, manifest])
        episode.quality_results.append(qc)
        repository.save(episode)

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["counts"]["packages_missing_package_qc"] == 0
        assert health.json()["counts"]["packages_failing_package_qc"] == 1

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        publish_jobs = checks["publish_jobs"]
        assert publish_jobs["status"] == "fail"
        assert publish_jobs["details"]["packages_missing_package_qc"] == 0
        assert publish_jobs["details"]["packages_failing_package_qc"] == 1
        assert publish_jobs["details"]["attention_count"] == 1
        assert publish_jobs["details"]["readiness_checks"] == {
            "no_failed_publish_jobs": True,
            "no_packages_missing_package_qc": True,
            "no_packages_failing_package_qc": False,
            "no_packages_missing_thumbnails": True,
            "no_packages_missing_subtitles": True,
            "no_submitted_publish_jobs": True,
            "no_invalid_production_manifests": True,
            "no_packages_missing_production_manifest": True,
        }
        assert publish_jobs["details"]["failed_readiness_checks"] == [
            "no_packages_failing_package_qc"
        ]
        assert publish_jobs["details"]["latest_package_failing_package_qc"] == {
            "episode_id": str(episode.id),
            "package_asset_id": str(package.id),
            "quality_result_id": str(qc.id),
            "quality_result_status": "fail",
            "quality_result_severity": "fail",
            "source_entity_type": "render_asset",
            "source_entity_id": "render-final",
            "language": None,
            "storage_uri_present": True,
            "checksum_present": True,
            "created_at": package.created_at.isoformat(),
            "quality_result_created_at": qc.created_at.isoformat(),
        }
        assert publish_jobs["blockers"] == [
            "one or more completed export packages have failing package QC"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="packages_failing_package_qc"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_publish_readiness_blocks_on_package_missing_thumbnail_and_subtitles() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(Settings())
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(Settings())
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(Settings())
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        transcript = TranscriptVersion(
            episode_id=episode.id,
            version=1,
            language="en",
            type=TranscriptType.broadcast,
            status="approved",
            turns=[
                TranscriptTurn(
                    source_discussion_turn_ids=[],
                    speaker_participant_id="host",
                    text="A package evidence check.",
                    status="approved",
                )
            ],
        )
        timeline = Asset(
            episode_id=episode.id,
            asset_type=AssetType.timeline,
            language="en",
            source_entity_type="transcript_version",
            source_entity_id=str(transcript.id),
            storage_uri="object://dialecticore/timelines/timeline.json",
            checksum="sha256:timeline",
            status="completed",
            generation_metadata={"transcript_version_id": str(transcript.id)},
        )
        render = Asset(
            episode_id=episode.id,
            asset_type=AssetType.render,
            language="en",
            source_entity_type="timeline_asset",
            source_entity_id=str(timeline.id),
            storage_uri="object://dialecticore/renders/final.mp4",
            checksum="sha256:render",
            status="completed",
            generation_metadata={
                "render_type": "final",
                "timeline_asset_id": str(timeline.id),
            },
        )
        thumbnail = Asset(
            episode_id=episode.id,
            asset_type=AssetType.thumbnail,
            language="en",
            source_entity_type="render_asset",
            source_entity_id=str(render.id),
            storage_uri="object://dialecticore/thumbnails/final.jpg",
            checksum="sha256:thumbnail",
            status="completed",
        )
        subtitle = Asset(
            episode_id=episode.id,
            asset_type=AssetType.subtitle,
            language="en",
            source_entity_type="transcript_version",
            source_entity_id=str(transcript.id),
            storage_uri="object://dialecticore/subtitles/en.vtt",
            checksum="sha256:subtitle",
            status="completed",
        )
        package = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            language="en",
            source_entity_type="render_asset",
            source_entity_id=str(render.id),
            storage_uri="object://dialecticore/exports/package.zip",
            checksum="sha256:package",
            status="completed",
            generation_metadata={
                "included_files": ["youtube-package.json", "video/render.mp4"],
                "youtube_package_manifest": {},
            },
        )
        manifest = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            source_entity_type="export_package",
            source_entity_id=str(package.id),
            storage_uri="object://dialecticore/manifests/production.json",
            checksum="sha256:production-manifest",
            status="completed",
            generation_metadata={
                "production_manifest": {
                    "schema_version": "production_manifest.v1",
                    "delivery_package": {"asset_id": str(package.id)},
                }
            },
        )
        qc = QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            details={"failure_count": 0, "warning_count": 0},
        )
        episode.transcripts.append(transcript)
        episode.assets.extend([timeline, render, thumbnail, subtitle, package, manifest])
        episode.quality_results.append(qc)
        repository.save(episode)

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["counts"]["packages_missing_thumbnail"] == 1
        assert health.json()["counts"]["packages_missing_subtitles"] == 1

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        publish_jobs = checks["publish_jobs"]
        assert publish_jobs["status"] == "fail"
        assert publish_jobs["details"]["packages_missing_thumbnail"] == 1
        assert publish_jobs["details"]["packages_missing_subtitles"] == 1
        assert publish_jobs["details"]["attention_count"] == 2
        assert publish_jobs["details"]["readiness_checks"] == {
            "no_failed_publish_jobs": True,
            "no_packages_missing_package_qc": True,
            "no_packages_failing_package_qc": True,
            "no_packages_missing_thumbnails": False,
            "no_packages_missing_subtitles": False,
            "no_submitted_publish_jobs": True,
            "no_invalid_production_manifests": True,
            "no_packages_missing_production_manifest": True,
        }
        assert publish_jobs["details"]["failed_readiness_checks"] == [
            "no_packages_missing_thumbnails",
            "no_packages_missing_subtitles",
        ]
        assert publish_jobs["details"]["latest_package_missing_thumbnail"] == {
            "episode_id": str(episode.id),
            "package_asset_id": str(package.id),
            "thumbnail_asset_id": str(thumbnail.id),
            "source_entity_type": "render_asset",
            "source_entity_id": str(render.id),
            "language": "en",
            "storage_uri_present": True,
            "checksum_present": True,
            "created_at": package.created_at.isoformat(),
        }
        assert publish_jobs["details"]["latest_package_missing_subtitles"] == {
            "episode_id": str(episode.id),
            "package_asset_id": str(package.id),
            "subtitle_asset_id": str(subtitle.id),
            "source_entity_type": "render_asset",
            "source_entity_id": str(render.id),
            "language": "en",
            "storage_uri_present": True,
            "checksum_present": True,
            "created_at": package.created_at.isoformat(),
        }
        assert publish_jobs["blockers"] == [
            "one or more completed export packages are missing thumbnails",
            "one or more completed export packages are missing subtitles",
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="packages_missing_thumbnail"} 1' in metrics.text
        )
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="packages_missing_subtitles"} 1' in metrics.text
        )
    finally:
        app.dependency_overrides.clear()


def test_system_health_reports_temporal_backend_configuration_gaps(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        temporal_backend_mode="external",
        temporal_backend_address=" ",
        temporal_namespace="dialecticore",
        temporal_task_queue=" ",
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    client = TestClient(app)

    try:
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
        body = response.json()
        components = {component["name"]: component for component in body["components"]}
        temporal = components["temporal_runtime"]
        assert temporal["status"] == "degraded"
        assert temporal["details"]["mode"] == "external"
        assert temporal["details"]["namespace"] == "dialecticore"
        assert temporal["details"]["execution_policy"] == ("external_temporal_backend_requested")
        assert temporal["details"]["missing"] == [
            "DIALECTICORE_TEMPORAL_BACKEND_ADDRESS",
            "DIALECTICORE_TEMPORAL_TASK_QUEUE",
        ]
        assert body["settings"]["temporal_backend_address_configured"] is False
        assert (
            temporal["details"]["readiness_checks"]["external_backend_address_configured"] is False
        )
        assert temporal["details"]["readiness_checks"]["external_task_queue_configured"] is False
        assert (
            "external_backend_address_configured"
            in (temporal["details"]["failed_readiness_checks"])
        )
        assert "external_task_queue_configured" in (temporal["details"]["failed_readiness_checks"])
    finally:
        app.dependency_overrides.clear()


def test_system_health_requires_external_temporal_execution_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        temporal_backend_mode="external",
        temporal_backend_address="temporal:7233",
        temporal_backend_worker_enabled=True,
        temporal_namespace="dialecticore",
        temporal_task_queue="production",
    )
    service = SystemHealthService(settings)
    monkeypatch.setattr(
        service,
        "_temporal_backend_tcp_probe",
        lambda address: {
            "address": address,
            "host": "temporal",
            "port": 7233,
            "reachable": True,
            "tls_enabled": False,
        },
    )
    worker_status = WorkerStatusService(settings)
    redis_bus = RedisBusService(settings)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: service
    app.dependency_overrides[get_worker_status_service] = lambda: worker_status
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    app.dependency_overrides[get_redis_bus_service] = lambda: redis_bus
    client = TestClient(app)

    try:
        heartbeat = client.post(
            "/api/v1/system/workers/heartbeat",
            json={
                "role": "temporal-worker",
                "worker_id": "temporal-1",
                "status": "running",
                "details": {"backend_mode": "external"},
            },
        )
        assert heartbeat.status_code == 200
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
        body = response.json()
        components = {component["name"]: component for component in body["components"]}
        temporal = components["temporal_runtime"]
        assert temporal["status"] == "degraded"
        assert temporal["details"]["temporal_worker_active"] is True
        assert temporal["details"]["temporal_worker_execution"]["status"] == "missing"
        assert temporal["details"]["readiness_checks"] == {
            "temporal_mode_valid": True,
            "bridge_signal_transport_configured": True,
            "bridge_signal_endpoint_configured": True,
            "external_backend_address_configured": True,
            "external_task_queue_configured": True,
            "external_backend_reachable": True,
            "external_native_worker_enabled": True,
            "external_temporal_worker_active": True,
            "external_temporal_worker_execution_running": False,
        }
        assert temporal["details"]["failed_readiness_checks"] == [
            "external_temporal_worker_execution_running"
        ]
        assert temporal["details"]["reason"] == (
            "temporal-worker heartbeat is active, but no "
            "temporal_worker_execution_summary.v1 evidence is present"
        )
        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        readiness_checks = {check["category"]: check for check in readiness.json()["checks"]}
        temporal_readiness = readiness_checks["temporal_runtime"]
        assert temporal_readiness["status"] == "fail"
        assert temporal_readiness["details"]["mode"] == "external"
        assert temporal_readiness["details"]["namespace"] == "dialecticore"
        assert temporal_readiness["details"]["task_queue"] == "production"
        assert temporal_readiness["details"]["backend_address"] == "temporal:7233"
        assert temporal_readiness["details"]["backend_address_configured"] is True
        assert temporal_readiness["details"]["native_worker_enabled"] is True
        assert temporal_readiness["details"]["temporal_worker_active"] is True
        assert temporal_readiness["details"]["readiness_checks"] == {
            "temporal_mode_valid": True,
            "bridge_signal_transport_configured": True,
            "bridge_signal_endpoint_configured": True,
            "external_backend_address_configured": True,
            "external_task_queue_configured": True,
            "external_backend_reachable": True,
            "external_native_worker_enabled": True,
            "external_temporal_worker_active": True,
            "external_temporal_worker_execution_running": False,
        }
        assert temporal_readiness["details"]["failed_readiness_checks"] == [
            "external_temporal_worker_execution_running"
        ]
        assert temporal_readiness["details"]["tcp_probe"] == {
            "address": "temporal:7233",
            "host": "temporal",
            "port": 7233,
            "reachable": True,
            "tls_enabled": False,
        }
        assert temporal_readiness["details"]["temporal_worker_execution"]["status"] == "missing"
        assert (
            "temporal_worker_execution_summary.v1 evidence is present"
            in temporal_readiness["blockers"][0]
        )
        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_temporal_worker_execution_status"
            '{status="missing",namespace="dialecticore",task_queue="production",'
            'worker_id=""} 1'
        ) in metrics.text
        assert (
            'dialecticore_temporal_worker_execution_count{kind="progressed_stages"} 0'
        ) in metrics.text

        heartbeat = client.post(
            "/api/v1/system/workers/heartbeat",
            json={
                "role": "temporal-worker",
                "worker_id": "temporal-1",
                "status": "running",
                "details": {
                    "schema_version": "temporal_worker_execution_summary.v1",
                    "policy": "external_temporal_stage_activity_worker_v1",
                    "status": "running",
                    "reason": (
                        "external Temporal stage activities executed by Docker temporal-worker"
                    ),
                    "activity_order": ["research", "discussion"],
                    "progressed_stage_count": 1,
                    "error_count": 0,
                },
            },
        )
        assert heartbeat.status_code == 200
        response = client.get("/api/v1/system/health")
        assert response.status_code == 200
        temporal = {component["name"]: component for component in response.json()["components"]}[
            "temporal_runtime"
        ]
        assert temporal["status"] == "healthy"
        assert temporal["details"]["temporal_worker_execution"]["status"] == "running"
        assert temporal["details"]["temporal_worker_execution"]["progressed_stage_count"] == 1
        assert temporal["details"]["temporal_worker_execution"]["activity_count"] == 2
        ready_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert ready_readiness.status_code == 200
        ready_checks = {check["category"]: check for check in ready_readiness.json()["checks"]}
        ready_temporal = ready_checks["temporal_runtime"]
        assert ready_temporal["status"] == "pass"
        assert ready_temporal["details"]["mode"] == "external"
        assert ready_temporal["details"]["tcp_probe"]["reachable"] is True
        assert ready_temporal["details"]["readiness_checks"] == {
            "temporal_mode_valid": True,
            "bridge_signal_transport_configured": True,
            "bridge_signal_endpoint_configured": True,
            "external_backend_address_configured": True,
            "external_task_queue_configured": True,
            "external_backend_reachable": True,
            "external_native_worker_enabled": True,
            "external_temporal_worker_active": True,
            "external_temporal_worker_execution_running": True,
        }
        assert ready_temporal["details"]["failed_readiness_checks"] == []
        assert ready_temporal["details"]["temporal_worker_execution"]["status"] == "running"
        assert ready_temporal["details"]["temporal_worker_execution"]["progressed_stage_count"] == 1
        assert ready_temporal["details"]["temporal_worker_execution"]["activity_count"] == 2
        assert ready_temporal["blockers"] == []
        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            "dialecticore_temporal_worker_execution_status"
            '{status="running",namespace="dialecticore",task_queue="production",'
            'worker_id="temporal-1"} 1'
        ) in metrics.text
        assert (
            'dialecticore_temporal_worker_execution_count{kind="progressed_stages"} 1'
        ) in metrics.text
        assert 'dialecticore_temporal_worker_execution_count{kind="activities"} 2' in (metrics.text)
    finally:
        app.dependency_overrides.clear()


def test_publisher_targets_and_episode_publish_api(monkeypatch) -> None:
    repository = EpisodeRepository()
    requests: list[str] = []
    monkeypatch.setenv("YOUTUBE_TEST_TOKEN", "youtube-token")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path == "/ready":
            return httpx.Response(200, json={"capabilities": {"package_upload": True}})
        if request.url.path == "/youtube/v3/channels":
            assert request.headers["authorization"] == "Bearer youtube-token"
            return httpx.Response(
                200,
                json={"capabilities": {"channel_verified": True}},
            )
        if request.url.path == "/deliveries":
            payload = json.loads(request.read().decode("utf-8"))
            assert payload["schema_version"] == "publish_delivery_payload.v1"
            assert payload["title"] == "API Publish Episode"
            return httpx.Response(
                202,
                json={
                    "job_id": "api-upload-123",
                    "publish_url": "https://publisher.test/watch/api-upload-123",
                },
            )
        return httpx.Response(404)

    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_publisher_service] = lambda: PublisherService(
        transport=httpx.MockTransport(handler)
    )
    client = TestClient(app)
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id="render-test",
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
        generation_metadata={
            "youtube_package_manifest": {
                "title": "API Publish Episode",
                "description": "Ready for upload.",
                "tags": ["dialecticore"],
                "language": "en",
                "render_uri": "object://dialecticore/renders/final.mp4",
                "subtitles": [{"language": "en", "path": "subtitles/en.vtt"}],
                "chapters": [{"title": "Opening", "start_ms": 0}],
                "evidence_lineage": {"citation_links": [{"source_id": "source-a"}]},
            }
        },
    )
    episode.assets.append(package_asset)
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.pass_,
            status="pass",
            details={"failure_count": 0, "warning_count": 0},
        )
    )
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            language="en",
            source_entity_type="export_package",
            source_entity_id=str(package_asset.id),
            storage_uri="object://dialecticore/manifests/production.json",
            mime_type="application/vnd.dialecticore.production-manifest+json",
            checksum="sha256:manifest",
            status="completed",
            generation_metadata={
                "production_manifest": {
                    "schema_version": "production_manifest.v1",
                    "delivery_package": {"asset_id": str(package_asset.id)},
                }
            },
        )
    )
    repository.save(episode)

    try:
        targets = client.get("/api/v1/publisher-targets")
        assert targets.status_code == 200
        assert targets.json()[0]["id"] == "mock-youtube"

        created_target = client.post(
            "/api/v1/publisher-targets",
            json={
                "id": "mock-secondary",
                "name": "Secondary Mock Publisher",
                "platform": "youtube",
                "adapter_type": "mock",
                "channel_id": "secondary",
                "privacy_status": "private",
                "default_language": "en",
                "enabled": True,
            },
        )
        assert created_target.status_code == 200
        assert created_target.json()["credential_reference"] is None

        health = client.post("/api/v1/publisher-targets/mock-secondary/health")
        assert health.status_code == 200
        assert health.json()["health_status"] == "healthy"

        created_http_target = client.post(
            "/api/v1/publisher-targets",
            json={
                "id": "http-delivery",
                "name": "HTTP Delivery Publisher",
                "platform": "generic",
                "adapter_type": "http",
                "base_url": "https://publisher.test",
                "privacy_status": "private",
                "default_language": "en",
                "enabled": True,
                "capabilities": {
                    "health_path": "/ready",
                    "delivery_path": "/deliveries",
                },
            },
        )
        assert created_http_target.status_code == 200

        http_health = client.post("/api/v1/publisher-targets/http-delivery/health")
        assert http_health.status_code == 200
        assert http_health.json()["health_status"] == "healthy"
        assert http_health.json()["capabilities"]["package_upload"] is True

        created_youtube_target = client.post(
            "/api/v1/publisher-targets",
            json={
                "id": "youtube-live",
                "name": "YouTube Live",
                "platform": "youtube",
                "adapter_type": "youtube_resumable",
                "base_url": "https://youtube.test",
                "credential_reference": "env:YOUTUBE_TEST_TOKEN",
                "privacy_status": "unlisted",
                "default_language": "en",
                "enabled": True,
                "capabilities": {"youtube_category_id": "28"},
            },
        )
        assert created_youtube_target.status_code == 200
        assert created_youtube_target.json()["credential_reference"] == ("env:YOUTUBE_TEST_TOKEN")

        youtube_health = client.post("/api/v1/publisher-targets/youtube-live/health")
        assert youtube_health.status_code == 200
        assert youtube_health.json()["health_status"] == "healthy"
        assert youtube_health.json()["capabilities"]["resumable_upload"] is True
        assert youtube_health.json()["capabilities"]["channel_verified"] is True

        published = client.post(
            f"/api/v1/episodes/{episode.id}/publish",
            json={
                "publisher_target_id": "mock-youtube",
                "package_asset_id": str(package_asset.id),
                "dry_run": True,
                "user_id": "tester",
            },
        )
        assert published.status_code == 200
        body = published.json()
        assert body["publish_jobs"][-1]["status"] == "completed"
        assert body["publish_jobs"][-1]["dry_run"] is True
        assert body["quality_results"][-1]["check_type"] == "publish_delivery_integrity"
        assert body["audit_events"][-2]["event_type"] == "publisher.job.completed"

        jobs = client.get(f"/api/v1/episodes/{episode.id}/publish-jobs")
        assert jobs.status_code == 200
        assert jobs.json()[-1]["publisher_target_id"] == "mock-youtube"

        http_published = client.post(
            f"/api/v1/episodes/{episode.id}/publish",
            json={
                "publisher_target_id": "http-delivery",
                "package_asset_id": str(package_asset.id),
                "dry_run": False,
                "user_id": "tester",
            },
        )
        assert http_published.status_code == 200
        http_body = http_published.json()
        assert http_body["publish_jobs"][-1]["publisher_target_id"] == "http-delivery"
        assert http_body["publish_jobs"][-1]["status"] == "completed"
        assert http_body["publish_jobs"][-1]["dry_run"] is False
        assert http_body["publish_jobs"][-1]["remote_job_id"] == "api-upload-123"
        assert http_body["quality_results"][-1]["status"] == "pass"
        assert "https://publisher.test/ready" in requests
        assert "https://publisher.test/deliveries" in requests
        assert "https://youtube.test/youtube/v3/channels?part=id&mine=true" in requests
    finally:
        app.dependency_overrides.clear()


def test_system_workers_and_metrics_report_heartbeats(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
        redis_event_fanout_enabled=True,
        redis_event_channel="dialecticore:test-events",
        redis_worker_signal_enabled=True,
        redis_worker_signal_stream="dialecticore:test-signals",
        redis_worker_signal_maxlen=25,
    )
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    redis_client = FakeRedisClient()
    redis_bus = RedisBusService(settings, client=redis_client)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: worker_status
    app.dependency_overrides[get_worker_lease_service] = lambda: worker_lease
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    app.dependency_overrides[get_redis_bus_service] = lambda: redis_bus
    client = TestClient(app)

    try:
        episode = repository.create(EpisodeCreateRequest(definition=definition()))
        package = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            storage_uri="memory://package.zip",
            status="completed",
        )
        episode.assets.append(package)
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="export_package_asset",
                target_id=str(package.id),
                check_type="youtube_package_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        episode.assets.append(
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.production_manifest,
                source_entity_type="export_package",
                source_entity_id=str(package.id),
                storage_uri="memory://production-manifest.json",
                checksum="sha256:production-manifest",
                status="completed",
                generation_metadata={
                    "production_manifest": {
                        "schema_version": "production_manifest.v1",
                        "delivery_package": {"asset_id": str(package.id)},
                    }
                },
            )
        )
        episode.publish_jobs.extend(
            [
                PublishJob(
                    episode_id=episode.id,
                    publisher_target_id="mock-youtube",
                    platform="youtube",
                    package_asset_id=package.id,
                    status="completed",
                    dry_run=True,
                    publish_url="mock://youtube/test",
                ),
                PublishJob(
                    episode_id=episode.id,
                    publisher_target_id="live-youtube",
                    platform="youtube",
                    package_asset_id=package.id,
                    status="failed",
                    dry_run=False,
                ),
                PublishJob(
                    episode_id=episode.id,
                    publisher_target_id="mock-youtube",
                    platform="youtube",
                    package_asset_id=package.id,
                    status="replaced",
                    dry_run=True,
                ),
            ]
        )
        repository.save(episode)

        heartbeat = client.post(
            "/api/v1/system/workers/heartbeat",
            json={
                "role": "voicebox-adapter",
                "worker_id": "worker-1",
                "status": "running",
                "details": {"episodes_scanned": 2, "pending_audio_assets": 1},
            },
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["stale"] is False
        lease = worker_lease.acquire("voicebox-adapter", "worker-1")
        assert lease is not None
        expired_lease = lease.model_copy(
            update={
                "role": "comfyui-adapter",
                "worker_id": "worker-old",
                "last_renewed_at": datetime.now(UTC) - timedelta(days=2),
                "expires_at": datetime.now(UTC) - timedelta(days=2),
            }
        )
        worker_lease._path_for("comfyui-adapter").write_text(
            expired_lease.model_dump_json(),
            encoding="utf-8",
        )
        malformed_heartbeat = worker_status.root / "render-worker--bad-json.json"
        malformed_heartbeat.write_text("{not-json", encoding="utf-8")
        malformed_lease = worker_lease.root / "render-worker.json"
        malformed_lease.write_text("{not-json", encoding="utf-8")

        workers = client.get("/api/v1/system/workers")
        assert workers.status_code == 200
        worker_body = workers.json()
        assert worker_body["status"] == "degraded"
        assert worker_body["counts"]["active_workers"] == 1
        assert worker_body["counts"]["active_leases"] == 1
        assert worker_body["counts"]["expired_leases"] == 0
        assert worker_body["counts"]["pruned_expired_leases"] == 1
        assert worker_body["counts"]["retained_heartbeats"] == 1
        assert worker_body["counts"]["malformed_heartbeats"] == 1
        assert worker_body["counts"]["malformed_leases"] == 1
        assert worker_body["counts"]["pruned_malformed_heartbeats"] == 0
        assert worker_body["counts"]["pruned_malformed_leases"] == 0
        assert worker_body["runtime_state_retention_seconds"] == 86400
        assert worker_body["workers"][0]["role"] == "voicebox-adapter"
        assert worker_body["leases"][0]["role"] == "voicebox-adapter"
        assert worker_body["workers"][0]["details"]["pending_audio_assets"] == 1

        signal = client.post(
            "/api/v1/system/workers/signals",
            json={
                "target_role": "voicebox-adapter",
                "signal_type": "drain",
                "reason": "maintenance",
                "user_id": "operator",
            },
        )
        assert signal.status_code == 200
        assert signal.json()["status"] == "queued"
        assert signal.json()["redis_stream_id"] == "1700000000000-0"
        assert signal.json()["redis_stream_maxlen"] == 25
        assert redis_client.streams[0][0] == "dialecticore:test-signals"
        assert redis_client.xadd_options[0] == {"maxlen": 25, "approximate": True}

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        assert health.json()["settings"]["redis_worker_signal_maxlen"] == 25
        assert health.json()["settings"]["worker_runtime_state_retention_seconds"] == 86400
        assert health.json()["counts"]["publish_jobs"] == 2
        assert health.json()["counts"]["completed_publish_jobs"] == 1
        assert health.json()["counts"]["failed_publish_jobs"] == 1
        assert health.json()["counts"]["dry_run_publish_jobs"] == 1
        assert health.json()["counts"]["live_publish_jobs"] == 1
        health_components = {
            component["name"]: component for component in health.json()["components"]
        }
        assert health_components["worker_signals"]["status"] == "degraded"
        assert health_components["worker_signals"]["details"]["recent_count"] == 1
        assert health_components["worker_signals"]["details"]["blocking_count"] == 1
        assert health_components["worker_signals"]["details"]["active_blocking_target_roles"] == [
            "voicebox-adapter"
        ]
        assert health_components["worker_signals"]["details"]["by_active_blocking_target_role"] == {
            "voicebox-adapter": 1
        }
        assert health_components["worker_signals"]["details"]["failed_count"] == 0
        assert health_components["worker_signals"]["details"]["attention_count"] == 1
        assert health_components["worker_signals"]["details"]["readiness_checks"] == {
            "worker_signal_summary_supplied": True,
            "worker_signal_delivery_not_failed": True,
            "worker_signals_not_blocking": False,
        }
        assert health_components["worker_signals"]["details"]["failed_readiness_checks"] == [
            "worker_signals_not_blocking"
        ]
        assert health_components["worker_signals"]["details"]["by_signal_type"] == {"drain": 1}
        assert health_components["worker_signals"]["details"]["by_target_role"] == {
            "voicebox-adapter": 1
        }
        assert health_components["worker_signals"]["details"]["by_delivery_source"] == {
            "runtime_state": 1,
            "redis_stream": 1,
        }

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        live_checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        assert live_checks["publish_jobs"]["status"] == "fail"
        assert live_checks["publish_jobs"]["details"]["schema_version"] == (
            "publish_job_summary.v1"
        )
        assert live_checks["publish_jobs"]["details"]["publish_jobs"] == 2
        assert live_checks["publish_jobs"]["details"]["completed_publish_jobs"] == 1
        assert live_checks["publish_jobs"]["details"]["failed_publish_jobs"] == 1
        assert live_checks["publish_jobs"]["details"]["submitted_publish_jobs"] == 0
        assert live_checks["publish_jobs"]["details"]["live_publish_jobs"] == 1
        assert live_checks["publish_jobs"]["details"]["attention_count"] == 1
        assert live_checks["publish_jobs"]["details"]["readiness_checks"] == {
            "no_failed_publish_jobs": False,
            "no_packages_missing_package_qc": True,
            "no_packages_failing_package_qc": True,
            "no_packages_missing_thumbnails": True,
            "no_packages_missing_subtitles": True,
            "no_submitted_publish_jobs": True,
            "no_invalid_production_manifests": True,
            "no_packages_missing_production_manifest": True,
        }
        assert live_checks["publish_jobs"]["details"]["failed_readiness_checks"] == [
            "no_failed_publish_jobs"
        ]
        assert (
            live_checks["publish_jobs"]["details"]["live_readiness_policy"]
            == "failed publish jobs, missing/failing package QC, or missing package "
            "thumbnail/subtitle evidence block live runs; submitted jobs and invalid or "
            "missing production manifests warn"
        )
        assert (
            live_checks["publish_jobs"]["details"]["latest_failed_job"]["publisher_target_id"]
            == "live-youtube"
        )
        assert live_checks["publish_jobs"]["details"]["latest_failed_job"]["status"] == "failed"
        assert live_checks["publish_jobs"]["details"]["latest_failed_job"]["dry_run"] is False
        assert live_checks["publish_jobs"]["details"]["latest_submitted_job"] is None
        assert "one or more publish jobs have failed" in live_checks["publish_jobs"]["blockers"]
        assert live_checks["worker_signals"]["status"] == "fail"
        assert live_checks["worker_signals"]["details"]["schema_version"] == (
            "worker_signal_readiness.v1"
        )
        assert live_checks["worker_signals"]["details"]["recent_count"] == 1
        assert live_checks["worker_signals"]["details"]["blocking_count"] == 1
        assert live_checks["worker_signals"]["details"]["active_blocking_target_roles"] == [
            "voicebox-adapter"
        ]
        assert live_checks["worker_signals"]["details"]["by_active_blocking_target_role"] == {
            "voicebox-adapter": 1
        }
        assert live_checks["worker_signals"]["details"]["failed_count"] == 0
        assert live_checks["worker_signals"]["details"]["attention_count"] == 1
        assert live_checks["worker_signals"]["details"]["readiness_checks"] == {
            "worker_signal_summary_supplied": True,
            "worker_signal_delivery_not_failed": True,
            "worker_signals_not_blocking": False,
        }
        assert live_checks["worker_signals"]["details"]["failed_readiness_checks"] == [
            "worker_signals_not_blocking"
        ]
        assert live_checks["worker_signals"]["details"]["by_status"] == {"queued": 1}
        assert live_checks["worker_signals"]["details"]["by_signal_type"] == {"drain": 1}
        assert live_checks["worker_signals"]["details"]["by_target_role"] == {"voicebox-adapter": 1}
        assert live_checks["worker_signals"]["details"]["by_delivery_source"] == {
            "runtime_state": 1,
            "redis_stream": 1,
        }
        assert live_checks["worker_signals"]["details"]["latest_signal"]["signal_type"] == "drain"
        assert (
            "one or more active worker control signals block live runs"
            in live_checks["worker_signals"]["blockers"]
        )
        assert live_checks["media_queues"]["status"] == "pass"
        assert live_checks["media_queues"]["details"]["schema_version"] == (
            "media_queue_readiness.v1"
        )
        assert live_checks["media_queues"]["details"]["failed_assets"] == 0
        assert live_checks["media_queues"]["details"]["pending_job_count"] == 0

        metrics_expired_lease = lease.model_copy(
            update={
                "role": "timeline-worker",
                "worker_id": "worker-old",
                "last_renewed_at": datetime.now(UTC) - timedelta(days=2),
                "expires_at": datetime.now(UTC) - timedelta(days=2),
            }
        )
        worker_lease._path_for("timeline-worker").write_text(
            metrics_expired_lease.model_dump_json(),
            encoding="utf-8",
        )
        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "text/plain" in metrics.headers["content-type"]
        text = metrics.text
        assert (
            'dialecticore_temporal_runtime_status{mode="local",status="healthy",'
            'namespace="default",task_queue="",native_worker_enabled="false"} 1'
        ) in text
        assert "dialecticore_publisher_automated_live_enabled 0" in text
        assert 'dialecticore_publish_job_count{kind="total"} 2' in text
        assert 'dialecticore_publish_job_count{kind="completed"} 1' in text
        assert 'dialecticore_publish_job_count{kind="failed"} 1' in text
        assert 'dialecticore_publish_job_count{kind="dry_run"} 1' in text
        assert 'dialecticore_publish_job_count{kind="live"} 1' in text
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="completed_export_packages"} 1' in text
        )
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="production_manifest_assets"} 1' in text
        )
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="invalid_production_manifest_assets"} 0' in text
        )
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="packages_missing_package_qc"} 0' in text
        )
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="packages_failing_package_qc"} 0' in text
        )
        assert (
            "dialecticore_publish_package_manifest_count"
            '{kind="packages_missing_production_manifest"} 0' in text
        )
        assert 'dialecticore_worker_count{kind="active_workers"} 1' in text
        assert 'dialecticore_worker_count{kind="pruned_expired_leases"} 1' in text
        assert 'dialecticore_worker_count{kind="malformed_heartbeats"} 1' in text
        assert 'dialecticore_worker_count{kind="malformed_leases"} 1' in text
        assert 'dialecticore_worker_runtime_seconds{kind="heartbeat_ttl"} 90' in text
        assert 'dialecticore_worker_runtime_seconds{kind="lease_ttl"} 45' in text
        assert 'dialecticore_worker_runtime_seconds{kind="runtime_state_retention"} 86400' in text
        assert (
            "dialecticore_worker_heartbeat_age_seconds"
            '{role="voicebox-adapter",worker_id="worker-1",status="running",stale="false"}'
        ) in text
        assert 'dialecticore_queue_count{kind="pending_audio_jobs"}' in text
        assert 'dialecticore_queue_count{kind="submitted_visual_jobs"}' in text
        assert 'dialecticore_queue_count{kind="running_subtitle_jobs"}' in text
        assert 'dialecticore_queue_count{kind="failed_subtitle_assets"}' in text
        assert 'dialecticore_worker_count{kind="active_leases"} 1' in text
        assert 'dialecticore_episode_count{kind="recent_worker_signals"} 1' in text
        assert ('dialecticore_worker_signal_count{dimension="signal_type",value="drain"} 1') in text
        assert (
            'dialecticore_worker_signal_count{dimension="delivery_source",value="redis_stream"} 1'
        ) in text
        assert (
            "dialecticore_worker_signal_count"
            '{dimension="active_blocking_target_role",value="voicebox-adapter"} 1'
        ) in text
        assert "dialecticore_worker_lease_expires_in_seconds" in text

        event_expired_lease = lease.model_copy(
            update={
                "role": "qc-worker",
                "worker_id": "worker-old",
                "last_renewed_at": datetime.now(UTC) - timedelta(days=2),
                "expires_at": datetime.now(UTC) - timedelta(days=2),
            }
        )
        worker_lease._path_for("qc-worker").write_text(
            event_expired_lease.model_dump_json(),
            encoding="utf-8",
        )
        events = client.get("/api/v1/system/events?once=true&audit_limit=2")
        assert events.status_code == 200
        assert "text/event-stream" in events.headers["content-type"]
        assert "id: system.snapshot:" in events.text
        assert "event: system.snapshot" in events.text
        event_payload = json.loads(
            next(
                line.removeprefix("data: ")
                for line in events.text.splitlines()
                if line.startswith("data: ")
            )
        )
        assert event_payload["schema_version"] == "system_status_event.v1"
        event_id = next(
            line.removeprefix("id: ")
            for line in events.text.splitlines()
            if line.startswith("id: ")
        )
        assert event_id == f"system.snapshot:{event_payload['health']['checked_at']}"
        assert event_payload["health"]["status"] in {"healthy", "degraded", "unhealthy"}
        assert event_payload["workers"]["counts"]["active_workers"] == 1
        assert event_payload["workers"]["counts"]["malformed_heartbeats"] == 1
        assert event_payload["workers"]["counts"]["malformed_leases"] == 1
        assert event_payload["workers"]["counts"]["pruned_expired_leases"] == 1
        assert event_payload["workers"]["stale_worker_count"] == 0
        assert event_payload["workers"]["active_lease_count"] == 1
        assert event_payload["workers"]["retained_heartbeat_count"] == 1
        assert event_payload["workers"]["pruned_stale_heartbeat_count"] == 0
        assert event_payload["workers"]["heartbeat_ttl_seconds"] == 90
        assert event_payload["workers"]["lease_ttl_seconds"] == 45
        assert event_payload["workers"]["runtime_state_retention_seconds"] == 86400
        assert event_payload["redis_fanout"]["status"] == "published"
        assert "pending_audio_jobs" in event_payload["health"]["queues"]
        assert "submitted_visual_jobs" in event_payload["health"]["queues"]
        assert "running_subtitle_jobs" in event_payload["health"]["queues"]
        assert "failed_subtitle_assets" in event_payload["health"]["queues"]
        assert event_payload["health"]["worker_signals"]["recent_count"] == 1
        assert event_payload["health"]["worker_signals"]["by_signal_type"]["drain"] == 1
        assert isinstance(event_payload["audit_events"], list)
        assert redis_client.published[0][0] == "dialecticore:test-events"
        signals = client.get("/api/v1/system/workers/signals")
        assert signals.status_code == 200
        assert signals.json()["signals"][0]["target_role"] == "voicebox-adapter"
        audit = client.get("/api/v1/audit-events?limit=5")
        assert audit.status_code == 200
        worker_audit = next(
            event for event in audit.json() if event["event_type"] == "worker.signal.recorded"
        )
        assert worker_audit["actor"] == "operator"
        assert worker_audit["details"]["schema_version"] == "worker_signal_audit.v1"
        assert worker_audit["details"]["target_role"] == "voicebox-adapter"
        assert worker_audit["details"]["signal_type"] == "drain"
        assert worker_audit["details"]["status"] == "queued"
        assert worker_audit["details"]["redis_stream_maxlen"] == 25
        assert worker_audit["details"]["redis_stream_id"] == "1700000000000-0"
        assert worker_audit["details"]["delivery_sources"] == [
            "runtime_state",
            "redis_stream",
        ]

        resume_signal = client.post(
            "/api/v1/system/workers/signals",
            json={
                "target_role": "voicebox-adapter",
                "signal_type": "resume",
                "reason": "maintenance complete",
                "user_id": "operator",
            },
        )
        assert resume_signal.status_code == 200
        assert resume_signal.json()["status"] == "queued"
        assert resume_signal.json()["redis_stream_id"] == "1700000000000-0"

        cleared_health = client.get("/api/v1/system/health")
        assert cleared_health.status_code == 200
        cleared_components = {
            component["name"]: component for component in cleared_health.json()["components"]
        }
        cleared_worker_signals = cleared_components["worker_signals"]
        assert cleared_worker_signals["status"] == "healthy"
        assert cleared_worker_signals["details"]["recent_count"] == 2
        assert cleared_worker_signals["details"]["blocking_count"] == 0
        assert cleared_worker_signals["details"]["active_blocking_target_roles"] == []
        assert cleared_worker_signals["details"]["by_active_blocking_target_role"] == {}
        assert cleared_worker_signals["details"]["readiness_checks"] == {
            "worker_signal_summary_supplied": True,
            "worker_signal_delivery_not_failed": True,
            "worker_signals_not_blocking": True,
        }
        assert cleared_worker_signals["details"]["failed_readiness_checks"] == []
        assert cleared_worker_signals["details"]["by_signal_type"] == {
            "drain": 1,
            "resume": 1,
        }

        cleared_live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert cleared_live_readiness.status_code == 200
        cleared_live_checks = {
            check["category"]: check for check in cleared_live_readiness.json()["checks"]
        }
        assert cleared_live_checks["worker_signals"]["status"] == "pass"
        assert cleared_live_checks["worker_signals"]["details"]["blocking_count"] == 0
        assert (
            cleared_live_checks["worker_signals"]["details"]["active_blocking_target_roles"] == []
        )
        assert (
            cleared_live_checks["worker_signals"]["details"]["by_active_blocking_target_role"] == {}
        )
        assert cleared_live_checks["worker_signals"]["blockers"] == []
    finally:
        app.dependency_overrides.clear()


def test_system_backup_api_creates_lists_and_validates_restore(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(
        backup_path=str(tmp_path / "backups"),
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    redis_bus = RedisBusService(settings)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_backup_service] = lambda: BackupService(settings)
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: worker_status
    app.dependency_overrides[get_worker_lease_service] = lambda: worker_lease
    app.dependency_overrides[get_redis_bus_service] = lambda: redis_bus
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        repository.create(EpisodeCreateRequest(definition=definition()))
        object_file = (
            Path(settings.object_storage_local_path)
            / settings.object_storage_bucket
            / "audio"
            / "api.wav"
        )
        object_file.parent.mkdir(parents=True)
        object_file.write_bytes(b"audio")
        runtime_file = Path(settings.runtime_state_path) / "workers" / "api-worker.json"
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text('{"role":"workflow-worker"}', encoding="utf-8")
        created = client.post(
            "/api/v1/system/backups",
            json={"label": "api-smoke", "user_id": "tester"},
        )
        assert created.status_code == 200
        created_body = created.json()
        assert created_body["database"]["record_counts"]["episode_records"] == 1

        listed = client.get("/api/v1/system/backups")
        assert listed.status_code == 200
        assert listed.json()["backups"][0]["backup_id"] == created_body["backup_id"]
        assert listed.json()["backups"][0]["restore_validation"]["validated"] is False
        assert listed.json()["backups"][0]["restore_validation"]["status"] == "missing"

        validated = client.post(
            "/api/v1/system/backups/restore",
            json={
                "backup_path": created_body["archive"]["filename"],
                "apply": False,
                "restore_database": False,
                "restore_object_storage": True,
                "restore_runtime_state": True,
                "user_id": "tester",
            },
        )
        assert validated.status_code == 200
        expected_record_count = created_body["database"]["total_records"]
        assert validated.json()["status"] == "validated"
        assert validated.json()["restore_plan"]["schema_version"] == "backup_restore_plan.v1"
        assert validated.json()["restore_plan"]["backup_id"] == created_body["backup_id"]
        assert validated.json()["restore_plan"]["database"]["will_restore"] is False
        assert (
            validated.json()["restore_plan"]["database"]["total_records"] == expected_record_count
        )
        assert validated.json()["restore_plan"]["object_storage"]["will_restore"] is True
        assert (
            validated.json()["restore_plan"]["object_storage"]["archive_validation"][
                "schema_version"
            ]
            == "file_storage_restore_validation.v1"
        )
        assert (
            validated.json()["restore_plan"]["object_storage"]["archive_validation"][
                "checksum_verified_count"
            ]
            == 1
        )
        assert validated.json()["restore_plan"]["runtime_state"]["will_restore"] is True
        assert (
            validated.json()["restore_plan"]["runtime_state"]["archive_validation"][
                "schema_version"
            ]
            == "file_storage_restore_validation.v1"
        )
        assert (
            validated.json()["restore_plan"]["runtime_state"]["archive_validation"][
                "checksum_verified_count"
            ]
            == 1
        )
        assert validated.json()["restore_plan"]["summary"]["target_scope_count"] == 2
        assert validated.json()["restore_plan"]["summary"]["target_record_count"] == 0
        assert validated.json()["restore_plan"]["summary"]["target_file_count"] == 2
        assert validated.json()["restored"] == {}
        audit_events = client.get("/api/v1/audit-events?limit=2")
        assert audit_events.status_code == 200
        assert audit_events.json()[0]["event_type"] == "backup.restore_validated"
        assert (
            audit_events.json()[0]["details"]["restore_plan"]["schema_version"]
            == "backup_restore_plan.v1"
        )
        listed_after_validation = client.get("/api/v1/system/backups")
        assert listed_after_validation.status_code == 200
        listed_backup = listed_after_validation.json()["backups"][0]
        assert listed_backup["backup_id"] == created_body["backup_id"]
        assert listed_backup["restore_validation"]["validated"] is True
        assert listed_backup["restore_validation"]["status"] == "validated"
        assert (
            listed_backup["restore_validation"]["restore_plan_schema_version"]
            == "backup_restore_plan.v1"
        )
        assert listed_backup["restore_validation"]["target_record_count"] == 0
        assert listed_backup["restore_validation"]["target_file_count"] == 2

        for index in range(210):
            repository.record_global_audit_event(
                AuditEvent(
                    event_type="workflow.worker.orchestration_recorded",
                    actor="workflow-worker",
                    details={"index": index},
                )
            )
        recent_audit_events = client.get("/api/v1/audit-events?limit=200")
        assert recent_audit_events.status_code == 200
        assert all(
            event["event_type"] != "backup.restore_validated"
            for event in recent_audit_events.json()
        )

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        backup_component = next(
            component
            for component in health.json()["components"]
            if component["name"] == "backup_storage"
        )
        assert backup_component["status"] == "healthy"
        assert backup_component["details"]["archive_count"] == 1
        assert backup_component["details"]["readiness_checks"] == {
            "backup_path_exists_or_parent_exists": True,
            "backup_path_writable": True,
            "backup_archive_available": True,
            "backup_archives_readable": True,
            "latest_archive_manifest_readable": True,
            "latest_restore_validation_current": True,
        }
        assert backup_component["details"]["failed_readiness_checks"] == []
        assert backup_component["details"]["latest_archive"]["manifest_readable"] is True
        assert (
            backup_component["details"]["latest_archive"]["backup_id"] == created_body["backup_id"]
        )
        latest_validation = backup_component["details"]["latest_restore_validation"]
        assert backup_component["details"]["readable_archive_count"] == 1
        assert backup_component["details"]["restore_validated_archive_count"] == 1
        assert backup_component["details"]["restore_unvalidated_archive_count"] == 0
        assert backup_component["details"]["unreadable_archive_count"] == 0
        assert latest_validation["validated"] is True
        assert latest_validation["status"] == "validated"
        assert latest_validation["backup_id"] == created_body["backup_id"]
        assert latest_validation["restore_plan_schema_version"] == "backup_restore_plan.v1"
        assert latest_validation["target_record_count"] == 0
        assert latest_validation["target_file_count"] == 2
        assert latest_validation["object_storage_archive_validation"] == {
            "validated": True,
            "status": "validated",
            "schema_version": "file_storage_restore_validation.v1",
            "will_restore": True,
            "expected_count": 1,
            "archive_count": 1,
            "total_bytes": len(b"audio"),
            "size_verified_count": 1,
            "checksum_verified_count": 1,
        }
        assert latest_validation["runtime_state_archive_validation"] == {
            "validated": True,
            "status": "validated",
            "schema_version": "file_storage_restore_validation.v1",
            "will_restore": True,
            "expected_count": 1,
            "archive_count": 1,
            "total_bytes": len(b'{"role":"workflow-worker"}'),
            "size_verified_count": 1,
            "checksum_verified_count": 1,
        }

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        live_checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        backup_readiness = live_checks["backup_storage"]
        assert backup_readiness["status"] == "pass"
        assert backup_readiness["details"]["archive_count"] == 1
        assert backup_readiness["details"]["readiness_checks"] == {
            "backup_path_exists_or_parent_exists": True,
            "backup_path_writable": True,
            "backup_archive_available": True,
            "backup_archives_readable": True,
            "latest_archive_manifest_readable": True,
            "latest_restore_validation_current": True,
        }
        assert backup_readiness["details"]["failed_readiness_checks"] == []
        assert backup_readiness["details"]["readable_archive_count"] == 1
        assert backup_readiness["details"]["restore_validated_archive_count"] == 1
        assert backup_readiness["details"]["restore_unvalidated_archive_count"] == 0
        assert backup_readiness["details"]["unreadable_archive_count"] == 0
        assert (
            backup_readiness["details"]["latest_archive"]["backup_id"] == created_body["backup_id"]
        )
        assert backup_readiness["details"]["latest_archive"]["manifest_readable"] is True
        assert backup_readiness["details"]["latest_restore_validation"]["status"] == "validated"
        assert (
            backup_readiness["details"]["latest_restore_validation"]["restore_plan_schema_version"]
            == "backup_restore_plan.v1"
        )
        assert (
            backup_readiness["details"]["latest_restore_validation"][
                "object_storage_archive_validation"
            ]["schema_version"]
            == "file_storage_restore_validation.v1"
        )
        assert (
            backup_readiness["details"]["latest_restore_validation"][
                "runtime_state_archive_validation"
            ]["checksum_verified_count"]
            == 1
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert "dialecticore_backup_archive_count 1" in metrics.text
        assert (
            "dialecticore_backup_latest_archive_info"
            f'{{filename="{created_body["archive"]["filename"]}",manifest_readable="true"}} 1'
        ) in metrics.text
        assert "dialecticore_backup_latest_age_seconds" in metrics.text
        assert "dialecticore_backup_latest_size_bytes" in metrics.text
        assert 'dialecticore_backup_archive_validation_count{status="readable"} 1' in metrics.text
        assert 'dialecticore_backup_archive_validation_count{status="validated"} 1' in metrics.text
        assert (
            'dialecticore_backup_archive_validation_count{status="unvalidated"} 0' in metrics.text
        )
        assert 'dialecticore_backup_archive_validation_count{status="unreadable"} 0' in metrics.text
        assert (
            "dialecticore_backup_latest_restore_validated"
            f'{{backup_id="{created_body["backup_id"]}",status="validated",'
            'restore_plan_schema_version="backup_restore_plan.v1"} 1'
        ) in metrics.text
        assert "dialecticore_backup_latest_restore_validation_age_seconds" in metrics.text
        assert (
            "dialecticore_backup_latest_content_validation"
            '{scope="object_storage",status="validated",'
            'schema_version="file_storage_restore_validation.v1"} 1'
        ) in metrics.text
        assert (
            "dialecticore_backup_latest_content_validation"
            '{scope="runtime_state",status="validated",'
            'schema_version="file_storage_restore_validation.v1"} 1'
        ) in metrics.text

        archive_path = Path(created_body["archive"]["path"])
        with tarfile.open(archive_path, "w:gz") as archive:
            manifest_payload = json.dumps(
                {key: value for key, value in created_body.items() if key != "archive"},
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            manifest_info = tarfile.TarInfo("manifest.json")
            manifest_info.size = len(manifest_payload)
            archive.addfile(manifest_info, io.BytesIO(manifest_payload))
            database_payload = json.dumps(repository.export_backup_data()).encode("utf-8")
            database_info = tarfile.TarInfo("database.json")
            database_info.size = len(database_payload)
            archive.addfile(database_info, io.BytesIO(database_payload))
            tamper_payload = b"archive changed after validation"
            tamper_info = tarfile.TarInfo("tamper.txt")
            tamper_info.size = len(tamper_payload)
            archive.addfile(tamper_info, io.BytesIO(tamper_payload))

        listed_after_tamper = client.get("/api/v1/system/backups")
        assert listed_after_tamper.status_code == 200
        tampered_backup = listed_after_tamper.json()["backups"][0]
        assert tampered_backup["restore_validation"]["validated"] is False
        assert tampered_backup["restore_validation"]["status"] == "checksum_mismatch"
        assert (
            tampered_backup["restore_validation"]["validated_archive_checksum"]
            == latest_validation["archive_checksum"]
        )

        health_after_tamper = client.get("/api/v1/system/health")
        assert health_after_tamper.status_code == 200
        backup_component_after_tamper = next(
            component
            for component in health_after_tamper.json()["components"]
            if component["name"] == "backup_storage"
        )
        assert backup_component_after_tamper["status"] == "degraded"
        assert (
            backup_component_after_tamper["details"]["readiness_checks"][
                "latest_restore_validation_current"
            ]
            is False
        )
        assert (
            backup_component_after_tamper["details"]["latest_restore_validation"]["status"]
            == "checksum_mismatch"
        )

        metrics_after_tamper = client.get("/api/v1/system/metrics")
        assert metrics_after_tamper.status_code == 200
        assert (
            "dialecticore_backup_latest_restore_validated"
            f'{{backup_id="{created_body["backup_id"]}",status="checksum_mismatch",'
            'restore_plan_schema_version=""} 0'
        ) in metrics_after_tamper.text
    finally:
        app.dependency_overrides.clear()


def test_system_health_and_metrics_report_auth_runtime_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "auth-runtime-secret")
    revocation_path = tmp_path / "provider-session-revocations.json"
    decision_path = tmp_path / "provider-session-decisions.json"
    revocation_path.write_text(
        json.dumps(
            {
                "schema_version": "provider_session_revocation_registry.v1",
                "revocations": [
                    {
                        "revocation_id": "active-revocation",
                        "token_sha256": "sha256:" + ("a" * 64),
                        "reason": "operator logout",
                        "created_at": now.isoformat(),
                        "expires_at": (now + timedelta(hours=1)).isoformat(),
                    },
                    {
                        "revocation_id": "expired-revocation",
                        "subject": "expired@example.test",
                        "reason": "old session",
                        "created_at": (now - timedelta(days=2)).isoformat(),
                        "expires_at": (now - timedelta(days=1)).isoformat(),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    decision_path.write_text(
        json.dumps(
            {
                "schema_version": "provider_session_decision_log.v1",
                "retention_limit": 10,
                "decisions": [
                    {"decision_id": "accepted", "status": "accepted"},
                    {"decision_id": "denied", "status": "denied"},
                    {"decision_id": "error", "status": "error"},
                ],
            }
        ),
        encoding="utf-8",
    )
    repository = EpisodeRepository()
    settings = Settings(
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        auth_provider_session_enabled=True,
        auth_provider_session_introspection_url="https://idp.example.test/introspect",
        auth_provider_session_revocation_path=str(revocation_path),
        auth_provider_session_decision_log_path=str(decision_path),
        auth_provider_session_decision_log_limit=10,
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    redis_bus = RedisBusService(settings)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: worker_status
    app.dependency_overrides[get_worker_lease_service] = lambda: worker_lease
    app.dependency_overrides[get_redis_bus_service] = lambda: redis_bus
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        auth_runtime = next(
            component
            for component in health.json()["components"]
            if component["name"] == "auth_runtime"
        )
        assert auth_runtime["status"] == "healthy"
        assert auth_runtime["details"]["readiness_checks"] == {
            "auth_disabled_or_mode_configured": True,
            "api_key_or_alternate_auth_mode_configured": True,
            "api_key_header_configured": True,
            "role_header_configured": True,
            "user_header_configured": True,
            "api_key_reference_resolves": True,
            "trusted_identity_header_configured": True,
            "trusted_email_header_configured": True,
            "trusted_groups_header_configured": True,
            "trusted_identity_default_role_valid": True,
            "provider_session_introspection_configured": True,
            "provider_session_token_header_configured": True,
            "provider_session_user_claim_configured": True,
            "provider_session_groups_claim_configured": True,
            "provider_session_introspection_url_secure": True,
            "provider_session_default_role_valid": True,
            "provider_session_client_credentials_ready": True,
            "provider_session_revocation_registry_readable": True,
            "provider_session_decision_log_readable": True,
        }
        assert auth_runtime["details"]["failed_readiness_checks"] == []
        assert auth_runtime["details"]["api_key_reference_status"] == {
            "status": "resolved",
            "reference": "env:DIALECTICORE_TEST_API_KEY",
        }
        assert auth_runtime["details"]["provider_session_revocations"]["active_count"] == 1
        assert auth_runtime["details"]["provider_session_revocations"]["expired_count"] == 1
        assert auth_runtime["details"]["provider_session_decisions"]["retained_count"] == 3
        assert auth_runtime["details"]["provider_session_decisions"]["accepted_count"] == 1
        assert auth_runtime["details"]["provider_session_decisions"]["denied_count"] == 1
        assert auth_runtime["details"]["provider_session_decisions"]["error_count"] == 1

        live_readiness = client.get("/api/v1/system/live-provider-readiness")
        assert live_readiness.status_code == 200
        live_checks = {check["category"]: check for check in live_readiness.json()["checks"]}
        auth_readiness = live_checks["auth_runtime"]
        assert auth_readiness["status"] == "pass"
        assert auth_readiness["details"]["auth_enabled"] is True
        assert auth_readiness["details"]["readiness_checks"] == {
            "auth_disabled_or_mode_configured": True,
            "api_key_or_alternate_auth_mode_configured": True,
            "api_key_header_configured": True,
            "role_header_configured": True,
            "user_header_configured": True,
            "api_key_reference_resolves": True,
            "trusted_identity_header_configured": True,
            "trusted_email_header_configured": True,
            "trusted_groups_header_configured": True,
            "trusted_identity_default_role_valid": True,
            "provider_session_introspection_configured": True,
            "provider_session_token_header_configured": True,
            "provider_session_user_claim_configured": True,
            "provider_session_groups_claim_configured": True,
            "provider_session_introspection_url_secure": True,
            "provider_session_default_role_valid": True,
            "provider_session_client_credentials_ready": True,
            "provider_session_revocation_registry_readable": True,
            "provider_session_decision_log_readable": True,
        }
        assert auth_readiness["details"]["failed_readiness_checks"] == []
        assert auth_readiness["details"]["api_key_reference_configured"] is True
        assert auth_readiness["details"]["provider_session_enabled"] is True
        assert auth_readiness["details"]["provider_session_introspection_configured"] is True
        assert auth_readiness["details"]["provider_session_revocations"]["active_count"] == 1
        assert auth_readiness["details"]["provider_session_revocations"]["expired_count"] == 1
        assert auth_readiness["details"]["provider_session_decisions"]["retained_count"] == 3
        assert auth_readiness["details"]["provider_session_decisions"]["denied_count"] == 1
        assert auth_readiness["details"]["provider_session_decisions"]["error_count"] == 1

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        text = metrics.text
        assert 'dialecticore_auth_mode_enabled{mode="api_key"} 1' in text
        assert 'dialecticore_auth_mode_enabled{mode="provider_session"} 1' in text
        assert ('dialecticore_auth_provider_session_count{kind="active_revocations"} 1') in text
        assert ('dialecticore_auth_provider_session_count{kind="expired_revocations"} 1') in text
        assert ('dialecticore_auth_provider_session_count{kind="retained_decisions"} 3') in text
        assert ('dialecticore_auth_provider_session_count{kind="accepted_decisions"} 1') in text
        assert ('dialecticore_auth_provider_session_count{kind="denied_decisions"} 1') in text
        assert 'dialecticore_auth_provider_session_count{kind="error_decisions"} 1' in text
    finally:
        app.dependency_overrides.clear()


def test_system_health_auth_runtime_flags_unavailable_api_key_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    monkeypatch.delenv("MISSING_DIALECTICORE_API_KEY", raising=False)
    settings = Settings(
        auth_enabled=True,
        auth_api_key_reference="env:MISSING_DIALECTICORE_API_KEY",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_redis_bus_service] = lambda: RedisBusService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        auth_runtime = components["auth_runtime"]
        assert auth_runtime["status"] == "degraded"
        assert auth_runtime["details"]["api_key_reference_status"] == {
            "status": "unavailable",
            "reference": "env:MISSING_DIALECTICORE_API_KEY",
            "error": "RuntimeError",
            "reason": "credential reference is not available",
        }
        assert auth_runtime["details"]["readiness_checks"] == {
            "auth_disabled_or_mode_configured": True,
            "api_key_or_alternate_auth_mode_configured": True,
            "api_key_header_configured": True,
            "role_header_configured": True,
            "user_header_configured": True,
            "api_key_reference_resolves": False,
            "trusted_identity_header_configured": True,
            "trusted_email_header_configured": True,
            "trusted_groups_header_configured": True,
            "trusted_identity_default_role_valid": True,
            "provider_session_introspection_configured": True,
            "provider_session_token_header_configured": True,
            "provider_session_user_claim_configured": True,
            "provider_session_groups_claim_configured": True,
            "provider_session_introspection_url_secure": True,
            "provider_session_default_role_valid": True,
            "provider_session_client_credentials_ready": True,
            "provider_session_revocation_registry_readable": True,
            "provider_session_decision_log_readable": True,
        }
        assert auth_runtime["details"]["failed_readiness_checks"] == ["api_key_reference_resolves"]
        assert auth_runtime["details"]["reason"] == ("configured API-key reference is unavailable")

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="api_key_reference_resolves",status="fail"} 1'
        ) in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        auth_readiness = checks["auth_runtime"]
        assert auth_readiness["status"] == "fail"
        assert auth_readiness["details"]["failed_readiness_checks"] == [
            "api_key_reference_resolves"
        ]
        assert auth_readiness["blockers"] == ["configured API-key reference is unavailable"]
    finally:
        app.dependency_overrides.clear()


def test_system_health_auth_runtime_flags_blank_header_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "secret")
    settings = Settings(
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        auth_api_key_header=" ",
        auth_role_header="",
        auth_user_header=" ",
        auth_trusted_identity_enabled=True,
        auth_trusted_identity_header="",
        auth_trusted_email_header=" ",
        auth_trusted_groups_header=" ",
        auth_provider_session_enabled=True,
        auth_provider_session_introspection_url="https://idp.example.test/introspect",
        auth_provider_session_token_header="",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_redis_bus_service] = lambda: RedisBusService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        auth_runtime = components["auth_runtime"]
        assert auth_runtime["status"] == "degraded"
        assert auth_runtime["details"]["failed_readiness_checks"] == [
            "auth_disabled_or_mode_configured",
            "api_key_or_alternate_auth_mode_configured",
            "api_key_header_configured",
            "role_header_configured",
            "user_header_configured",
            "trusted_identity_header_configured",
            "trusted_email_header_configured",
            "trusted_groups_header_configured",
            "provider_session_token_header_configured",
        ]
        assert auth_runtime["details"]["reason"] == (
            "API-key auth header name is not configured; "
            "auth role header name is not configured; "
            "auth user header name is not configured; "
            "trusted identity header name is not configured; "
            "trusted email header name is not configured; "
            "trusted groups header name is not configured; "
            "provider session token header name is not configured; "
            "authentication is enabled but no viable auth mode is configured"
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="api_key_header_configured",status="fail"} 1'
        ) in metrics.text
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="trusted_identity_header_configured",status="fail"} 1'
        ) in metrics.text
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="trusted_email_header_configured",status="fail"} 1'
        ) in metrics.text
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="provider_session_token_header_configured",status="fail"} 1'
        ) in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        auth_readiness = checks["auth_runtime"]
        assert auth_readiness["status"] == "fail"
        assert auth_readiness["details"]["failed_readiness_checks"] == [
            "auth_disabled_or_mode_configured",
            "api_key_or_alternate_auth_mode_configured",
            "api_key_header_configured",
            "role_header_configured",
            "user_header_configured",
            "trusted_identity_header_configured",
            "trusted_email_header_configured",
            "trusted_groups_header_configured",
            "provider_session_token_header_configured",
        ]
        assert auth_readiness["blockers"] == [
            "API-key auth header name is not configured; "
            "auth role header name is not configured; "
            "auth user header name is not configured; "
            "trusted identity header name is not configured; "
            "trusted email header name is not configured; "
            "trusted groups header name is not configured; "
            "provider session token header name is not configured; "
            "authentication is enabled but no viable auth mode is configured"
        ]
    finally:
        app.dependency_overrides.clear()


def test_system_health_auth_runtime_flags_blank_provider_session_claim_names(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "secret")
    settings = Settings(
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        auth_provider_session_enabled=True,
        auth_provider_session_introspection_url="https://idp.example.test/introspect",
        auth_provider_session_user_claim=" ",
        auth_provider_session_groups_claim="",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_redis_bus_service] = lambda: RedisBusService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        auth_runtime = components["auth_runtime"]
        assert auth_runtime["status"] == "degraded"
        assert auth_runtime["details"]["provider_session_user_claim_configured"] is False
        assert auth_runtime["details"]["provider_session_groups_claim_configured"] is False
        assert auth_runtime["details"]["failed_readiness_checks"] == [
            "provider_session_user_claim_configured",
            "provider_session_groups_claim_configured",
        ]
        assert auth_runtime["details"]["reason"] == (
            "provider session user claim name is not configured; "
            "provider session groups claim name is not configured"
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="provider_session_user_claim_configured",status="fail"} 1'
        ) in metrics.text
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="provider_session_groups_claim_configured",status="fail"} 1'
        ) in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        auth_readiness = checks["auth_runtime"]
        assert auth_readiness["status"] == "fail"
        assert auth_readiness["details"]["failed_readiness_checks"] == [
            "provider_session_user_claim_configured",
            "provider_session_groups_claim_configured",
        ]
        assert auth_readiness["blockers"] == [
            "provider session user claim name is not configured; "
            "provider session groups claim name is not configured"
        ]
    finally:
        app.dependency_overrides.clear()


def test_system_health_auth_runtime_flags_mismatched_provider_session_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "secret")
    settings = Settings(
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        auth_provider_session_enabled=True,
        auth_provider_session_introspection_url="https://idp.example.test/introspect",
        auth_provider_session_client_id_reference=" ",
        auth_provider_session_client_secret_reference="env:OIDC_CLIENT_SECRET",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_redis_bus_service] = lambda: RedisBusService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        auth_runtime = components["auth_runtime"]
        assert auth_runtime["status"] == "degraded"
        assert auth_runtime["details"]["provider_session_client_credentials_status"] == {
            "status": "mismatched",
            "client_id_reference_configured": False,
            "client_secret_reference_configured": True,
            "configured_references": ["env:OIDC_CLIENT_SECRET"],
            "reason": ("provider session client ID and secret references must both be configured"),
        }
        assert (
            health.json()["settings"]["auth_provider_session_client_id_reference_configured"]
            is False
        )
        assert (
            health.json()["settings"]["auth_provider_session_client_secret_reference_configured"]
            is True
        )
        assert auth_runtime["details"]["failed_readiness_checks"] == [
            "provider_session_client_credentials_ready"
        ]
        assert auth_runtime["details"]["reason"] == (
            "provider session client credentials are incomplete"
        )

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert checks["auth_runtime"]["status"] == "fail"
        assert checks["auth_runtime"]["blockers"] == [
            "provider session client credentials are incomplete"
        ]
    finally:
        app.dependency_overrides.clear()


def test_system_health_auth_runtime_flags_insecure_provider_session_introspection_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "secret")
    settings = Settings(
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        auth_provider_session_enabled=True,
        auth_provider_session_introspection_url="http://idp.example.test/introspect",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_redis_bus_service] = lambda: RedisBusService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        auth_runtime = components["auth_runtime"]
        assert auth_runtime["status"] == "degraded"
        assert auth_runtime["details"]["provider_session_introspection_url_scheme"] == "http"
        assert (
            auth_runtime["details"]["readiness_checks"]["provider_session_introspection_url_secure"]
            is False
        )
        assert auth_runtime["details"]["failed_readiness_checks"] == [
            "provider_session_introspection_url_secure"
        ]
        assert auth_runtime["details"]["reason"] == (
            "provider session introspection URL must use HTTPS"
        )

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="provider_session_introspection_url_secure",status="fail"} 1'
        ) in metrics.text

        readiness = client.get("/api/v1/system/live-provider-readiness")
        assert readiness.status_code == 200
        checks = {check["category"]: check for check in readiness.json()["checks"]}
        assert checks["auth_runtime"]["status"] == "fail"
        assert checks["auth_runtime"]["blockers"] == [
            "provider session introspection URL must use HTTPS"
        ]
    finally:
        app.dependency_overrides.clear()


def test_system_health_auth_runtime_flags_unavailable_provider_session_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = EpisodeRepository()
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "secret")
    monkeypatch.setenv("OIDC_CLIENT_ID", "dialecticore")
    monkeypatch.delenv("MISSING_OIDC_CLIENT_SECRET", raising=False)
    settings = Settings(
        auth_enabled=True,
        auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        auth_provider_session_enabled=True,
        auth_provider_session_introspection_url="https://idp.example.test/introspect",
        auth_provider_session_client_id_reference="env:OIDC_CLIENT_ID",
        auth_provider_session_client_secret_reference="env:MISSING_OIDC_CLIENT_SECRET",
        object_storage_local_path=str(tmp_path / "object-store"),
        runtime_state_path=str(tmp_path / "runtime-state"),
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_system_health_service] = lambda: SystemHealthService(settings)
    app.dependency_overrides[get_worker_status_service] = lambda: WorkerStatusService(settings)
    app.dependency_overrides[get_worker_lease_service] = lambda: WorkerLeaseService(settings)
    app.dependency_overrides[get_redis_bus_service] = lambda: RedisBusService(settings)
    app.dependency_overrides[get_system_metrics_service] = lambda: SystemMetricsService()
    client = TestClient(app)

    try:
        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        components = {component["name"]: component for component in health.json()["components"]}
        auth_runtime = components["auth_runtime"]
        assert auth_runtime["status"] == "degraded"
        assert auth_runtime["details"]["provider_session_client_credentials_status"] == {
            "status": "unavailable",
            "client_id_reference_configured": True,
            "client_secret_reference_configured": True,
            "configured_references": [
                "env:OIDC_CLIENT_ID",
                "env:MISSING_OIDC_CLIENT_SECRET",
            ],
            "error": "RuntimeError",
            "reason": "credential reference is not available",
        }
        assert auth_runtime["details"]["failed_readiness_checks"] == [
            "provider_session_client_credentials_ready"
        ]

        metrics = client.get("/api/v1/system/metrics")
        assert metrics.status_code == 200
        assert (
            'dialecticore_component_readiness_check{component="auth_runtime",'
            'check="provider_session_client_credentials_ready",status="fail"} 1'
        ) in metrics.text
    finally:
        app.dependency_overrides.clear()


def test_rbac_middleware_enforces_configured_roles(monkeypatch) -> None:
    monkeypatch.setenv("DIALECTICORE_TEST_API_KEY", "secret")
    original_auth_service = main_module.auth_service
    main_module.auth_service = AuthService(
        Settings(
            auth_enabled=True,
            auth_api_key_reference="env:DIALECTICORE_TEST_API_KEY",
        )
    )
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        anonymous = client.get("/api/v1/system/auth-policy")
        assert anonymous.status_code == 403

        preflight = client.options("/api/v1/episodes")
        assert preflight.status_code in {200, 405}
        assert preflight.status_code != 403

        policy = client.get(
            "/api/v1/system/auth-policy",
            headers={
                "x-dialecticore-api-key": "secret",
                "x-dialecticore-role": "viewer",
            },
        )
        assert policy.status_code == 200
        assert policy.json()["enabled"] is True
        assert policy.json()["api_key_reference_configured"] is True
        assert "backup_restore" in policy.json()["permissions"]
        assert "configuration_write" in policy.json()["permissions"]

        viewer_create = client.post(
            "/api/v1/episodes",
            json=payload,
            headers={
                "x-dialecticore-api-key": "secret",
                "x-dialecticore-role": "viewer",
            },
        )
        assert viewer_create.status_code == 403
        assert "episode_write" in viewer_create.json()["detail"]

        producer_create = client.post(
            "/api/v1/episodes",
            json=payload,
            headers={
                "x-dialecticore-api-key": "secret",
                "x-dialecticore-role": "producer",
                "x-dialecticore-user": "producer-1",
            },
        )
        assert producer_create.status_code == 200

        producer_config = client.post(
            "/api/v1/model-endpoints",
            json={
                "id": "remote",
                "name": "Remote",
                "provider_type": "openai_compatible",
                "base_url": "https://models.example.test",
                "credential_reference": "env:REMOTE_TOKEN",
            },
            headers={
                "x-dialecticore-api-key": "secret",
                "x-dialecticore-role": "producer",
            },
        )
        assert producer_config.status_code == 403
        assert "configuration_write" in producer_config.json()["detail"]
    finally:
        main_module.auth_service = original_auth_service
        app.dependency_overrides.clear()


def test_rbac_middleware_accepts_trusted_identity_headers() -> None:
    original_auth_service = main_module.auth_service
    main_module.auth_service = AuthService(
        Settings(
            auth_enabled=True,
            auth_trusted_identity_enabled=True,
            auth_trusted_group_role_map="dialecticore-producers=producer",
        )
    )
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        policy = client.get(
            "/api/v1/system/auth-policy",
            headers={"x-forwarded-user": "producer@example.test"},
        )
        assert policy.status_code == 200
        assert policy.json()["trusted_identity_enabled"] is True
        assert policy.json()["authentication_modes"]["trusted_identity"]["enabled"] is True
        assert (
            policy.json()["authentication_modes"]["trusted_identity"]["group_role_map_configured"]
            is True
        )

        viewer_create = client.post(
            "/api/v1/episodes",
            json=payload,
            headers={"x-forwarded-user": "viewer@example.test"},
        )
        assert viewer_create.status_code == 403
        assert "episode_write" in viewer_create.json()["detail"]

        producer_create = client.post(
            "/api/v1/episodes",
            json=payload,
            headers={
                "x-forwarded-user": "producer@example.test",
                "x-forwarded-groups": "other,dialecticore-producers",
            },
        )
        assert producer_create.status_code == 200
    finally:
        main_module.auth_service = original_auth_service
        app.dependency_overrides.clear()


def test_rbac_middleware_accepts_and_revokes_provider_managed_session(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = request.content.decode("utf-8").split("token=", 1)[1].split("&", 1)[0]
        groups = ["dialecticore-admins"] if token == "admin-token" else ["dialecticore-producers"]
        return httpx.Response(
            200,
            json={
                "active": True,
                "sub": f"{token}@example.test",
                "groups": groups,
            },
        )

    original_auth_service = main_module.auth_service
    main_module.auth_service = AuthService(
        Settings(
            auth_enabled=True,
            auth_provider_session_enabled=True,
            auth_provider_session_introspection_url="https://idp.example.test/introspect",
            auth_provider_session_group_role_map=(
                "dialecticore-producers=producer,dialecticore-admins=admin"
            ),
            auth_provider_session_revocation_path=str(tmp_path / "revocations.json"),
            auth_provider_session_decision_log_path=str(tmp_path / "decisions.json"),
        ),
        transport=httpx.MockTransport(handler),
    )
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        policy = client.get(
            "/api/v1/system/auth-policy",
            headers={"authorization": "Bearer provider-token"},
        )
        assert policy.status_code == 200
        assert policy.json()["provider_session_enabled"] is True
        assert policy.json()["authentication_modes"]["provider_session"]["enabled"] is True
        assert (
            policy.json()["authentication_modes"]["provider_session"][
                "introspection_url_configured"
            ]
            is True
        )
        assert (
            policy.json()["authentication_modes"]["provider_session"]["decision_log"][
                "retention_limit"
            ]
            == 100
        )

        producer_create = client.post(
            "/api/v1/episodes",
            json=payload,
            headers={"authorization": "Bearer provider-token"},
        )
        assert producer_create.status_code == 200

        token_sha256 = "sha256:" + hashlib.sha256(b"provider-token").hexdigest()
        revoked = client.post(
            "/api/v1/system/auth/provider-session/revocations",
            json={
                "token_sha256": token_sha256,
                "reason": "operator logout",
                "user_id": "admin@example.test",
            },
            headers={"authorization": "Bearer admin-token"},
        )
        assert revoked.status_code == 200
        assert revoked.json()["token_sha256"] == token_sha256
        assert revoked.json()["reason"] == "operator logout"

        listed = client.get(
            "/api/v1/system/auth/provider-session/revocations",
            headers={"authorization": "Bearer admin-token"},
        )
        assert listed.status_code == 200
        assert listed.json()["revocations"][0]["token_sha256"] == token_sha256

        blocked = client.get(
            "/api/v1/episodes",
            headers={"authorization": "Bearer provider-token"},
        )
        assert blocked.status_code == 403
        assert "revoked" in blocked.json()["detail"]

        decisions = client.get(
            "/api/v1/system/auth/provider-session/decisions?limit=10",
            headers={"authorization": "Bearer admin-token"},
        )
        assert decisions.status_code == 200
        decision_records = decisions.json()["decisions"]
        assert any(
            decision["status"] == "accepted"
            and decision["subject"] == "provider-token@example.test"
            for decision in decision_records
        )
        assert any(
            decision["status"] == "denied" and "revoked" in decision["reason"]
            for decision in decision_records
        )
    finally:
        main_module.auth_service = original_auth_service
        app.dependency_overrides.clear()


def test_research_api_builds_reads_and_checks_claims(tmp_path: Path, monkeypatch) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    research_service = ResearchService(settings)
    monkeypatch.setattr(
        research_service,
        "_fetch_retrieval_target",
        lambda uri: {
            "http_status": 200,
            "content_type": "text/plain; charset=utf-8",
            "byte_count": 130,
            "payload": (
                b"AI assistants improved productivity by 19 percent when review "
                b"ownership was explicit in software maintenance teams."
            ),
        },
    )
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_research_service] = lambda: research_service
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        settings,
    )
    client = TestClient(app)

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": research_definition().model_dump(mode="json")},
        ).json()["id"]
        built = client.post(
            f"/api/v1/episodes/{episode_id}/research/build",
            json={
                "user_id": "tester",
                "sources": [
                    {
                        "title": "AI Governance Source",
                        "uri": "https://example.edu/ai-governance",
                        "source_type": "academic_paper",
                        "published_at": "2026",
                        "content": (
                            "AI assistants improved productivity by 27 percent in a controlled "
                            "software maintenance study. Teams may face risk when generated code "
                            "is merged without ownership."
                        ),
                    }
                ],
                "retrieval_targets": [
                    {
                        "title": "Fetched AI Governance Source",
                        "uri": "https://research.example.org/ai-governance",
                        "source_type": "government_report",
                    }
                ],
            },
        )
        assert built.status_code == 200
        built_body = built.json()
        assert built_body["status"] == "RESEARCH_REVIEW"
        evidence_assets = [
            asset for asset in built_body["assets"] if asset["asset_type"] == "evidence_pack"
        ]
        assert evidence_assets[-1]["status"] == "completed"
        evidence_qc = [
            result
            for result in built_body["quality_results"]
            if result["check_type"] == "evidence_pack_integrity"
        ][-1]
        assert evidence_qc["status"] == "pass"
        assert evidence_qc["details"]["retrieved_source_count"] == 2
        assert evidence_qc["details"]["source_ranking_count"] >= 2
        assert evidence_qc["details"]["strong_source_count"] >= 1
        assert evidence_qc["details"]["source_agreement_count"] >= 1
        assert evidence_assets[-1]["generation_metadata"]["retrieval_success_count"] == 1
        assert evidence_assets[-1]["generation_metadata"]["strong_source_count"] >= 1
        assert evidence_assets[-1]["generation_metadata"]["source_agreement_count"] >= 1

        research = client.get(f"/api/v1/episodes/{episode_id}/research")
        assert research.status_code == 200
        assert research.json()["evidence_pack"]["schema_version"] == "evidence_pack.v1"
        assert research.json()["evidence_pack_asset"]["id"] == evidence_assets[-1]["id"]
        assert research.json()["evidence_pack"]["important_statistics"]
        assert research.json()["evidence_pack"]["source_rankings"]
        assert research.json()["evidence_pack"]["source_agreements"]
        sources = client.get(f"/api/v1/episodes/{episode_id}/research/sources")
        assert sources.status_code == 200
        source_body = sources.json()
        assert any(source["source_type"] == "academic_paper" for source in source_body)
        assert all(source["episode_id"] == episode_id for source in source_body)
        ranked_source = next(source for source in source_body if source["credibility_score"] >= 0.7)
        assert ranked_source["metadata"]["evidence_pack_asset_id"] == evidence_assets[-1]["id"]
        claims = client.get(f"/api/v1/episodes/{episode_id}/research/claims")
        assert claims.status_code == 200
        claim_body = claims.json()
        assert any(claim["status"] == "supported" for claim in claim_body)
        assert all(claim["episode_id"] == episode_id for claim in claim_body)
        assert all(claim["statement"] == claim["text"] for claim in claim_body)
        reviewed_source_id = next(
            source["id"]
            for source in research.json()["evidence_pack"]["source_index"]
            if source["source_type"] == "academic_paper"
        )
        source_review = client.post(
            f"/api/v1/episodes/{episode_id}/research/source-review",
            json={
                "user_id": "reviewer",
                "source_id": reviewed_source_id,
                "decision": "approved",
                "notes": "Accepted source for research review.",
            },
        )
        assert source_review.status_code == 200
        source_review_body = source_review.json()
        reviewed_asset = [
            asset
            for asset in source_review_body["assets"]
            if asset["asset_type"] == "evidence_pack"
        ][-1]
        source_review_qc = [
            result
            for result in source_review_body["quality_results"]
            if result["check_type"] == "research_source_review_integrity"
        ][-1]
        assert reviewed_asset["generation_metadata"]["source_review_policy"] == (
            "human_source_review_v1"
        )
        assert reviewed_asset["generation_metadata"]["source_review_count"] == 1
        assert source_review_qc["details"]["approved_source_count"] == 1
        assert source_review_body["audit_events"][-1]["event_type"] == (
            "research.source_review.recorded"
        )

        blocked_production = client.post(f"/api/v1/episodes/{episode_id}/produce")
        assert blocked_production.status_code == 422
        assert "research approval" in blocked_production.json()["detail"]

        approval_id = built_body["approvals"][-1]["id"]
        approved = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/{approval_id}/decision",
            json={"decision": "approved", "user_id": "tester"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "DRAFT"

        produced = client.post(f"/api/v1/episodes/{episode_id}/produce")
        assert produced.status_code == 200
        checked = client.post(
            f"/api/v1/episodes/{episode_id}/research/claim-qc",
            json={"user_id": "tester"},
        )
        assert checked.status_code == 200
        claim_qc = [
            result
            for result in checked.json()["quality_results"]
            if result["check_type"] == "claim_citation_integrity"
        ][-1]
        assert claim_qc["status"] == "pass"
        assert claim_qc["details"]["cited_claim_count"] == claim_qc["details"]["claim_count"]
    finally:
        app.dependency_overrides.clear()


def test_transcript_approval_blocks_failing_semantic_qc() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        Settings(),
    )
    client = TestClient(app)
    payload = {"definition": definition().model_dump(mode="json")}

    try:
        episode_id = client.post("/api/v1/episodes", json=payload).json()["id"]
        client.post(f"/api/v1/episodes/{episode_id}/produce")
        episode = repository.get(episode_id)
        canonical = next(
            transcript
            for transcript in episode.transcripts
            if transcript.id == episode.canonical_transcript_version_id
        )
        canonical.turns[0].source_discussion_turn_ids = []
        episode.quality_results.append(
            DiscussionEngine(ModelGateway(), Settings())._transcript_semantic_qc(
                episode,
                canonical,
            )
        )
        repository.save(episode)

        approval_id = episode.approvals[0].id
        decided = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/{approval_id}/decision",
            json={"decision": "approved", "comment": "Should block.", "user_id": "tester"},
        )
        assert decided.status_code == 422
        assert "semantic fidelity QC" in decided.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_localization_and_audio_asset_planning_api(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        settings,
    )
    app.dependency_overrides[get_voicebox_service] = lambda: VoiceboxService(settings)
    app.dependency_overrides[get_comfyui_service] = lambda: ComfyUiService(settings)
    client = TestClient(app)
    localized_definition = definition().model_dump(mode="json")
    localized_definition["languages"] = {
        "source_language": "en",
        "outputs": [
            {"language": "en", "mode": "canonical"},
            {"language": "de", "mode": "localized_reperformance"},
        ],
        "semantic_fidelity_threshold": 0.92,
        "allow_new_claims": False,
    }

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": localized_definition},
        ).json()["id"]
        client.post(f"/api/v1/episodes/{episode_id}/produce")
        approvals = client.get(f"/api/v1/episodes/{episode_id}/approvals")
        approval_id = approvals.json()["approvals"][0]["id"]
        approved = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/{approval_id}/decision",
            json={"decision": "approved", "comment": "Ready.", "user_id": "tester"},
        )
        assert approved.status_code == 200

        localized = client.post(
            f"/api/v1/episodes/{episode_id}/localize",
            json={"languages": ["de"], "user_id": "tester"},
        )
        assert localized.status_code == 200
        localized_body = localized.json()
        localized_transcripts = [
            transcript
            for transcript in localized_body["transcripts"]
            if transcript["type"] == "localized" and transcript["language"] == "de"
        ]
        assert len(localized_transcripts) == 1
        assert (
            localized_transcripts[0]["parent_version_id"]
            == (localized_body["canonical_transcript_version_id"])
        )
        assert localized_transcripts[0]["localization_metadata"] == {
            "schema_version": "transcript_localization_metadata.v1",
            "mode": "localized_reperformance",
            "source_language": "en",
            "target_language": "de",
            "source_transcript_version_id": localized_body["canonical_transcript_version_id"],
            "localization_adapter": "deterministic_scaffold",
            "source_bound": True,
            "claim_policy": "preserve_source_claims",
            "allows_new_claims": False,
            "supports_dubbing": True,
            "supports_video_reperformance": True,
            "requires_human_review": True,
        }
        assert all(
            turn["transcript_version_id"] == localized_transcripts[0]["id"]
            for turn in localized_transcripts[0]["turns"]
        )
        assert all(turn["source_discussion_turn_ids"] for turn in localized_transcripts[0]["turns"])
        assert all(
            turn["pronunciation_markup"]
            for turn in localized_transcripts[0]["turns"]
            if turn["status"] != "excluded"
        )
        policy_qc = [
            result
            for result in localized_body["quality_results"]
            if result["check_type"] == "localized_transcript_semantic_fidelity"
        ][-1]
        assert policy_qc["details"]["allow_new_claims"] is False
        assert policy_qc["details"]["semantic_fidelity_threshold"] == 0.92
        assert policy_qc["details"]["localization_metadata"]["mode"] == ("localized_reperformance")

        localized_episode = repository.get(episode_id)
        canonical_version = next(
            transcript
            for transcript in localized_episode.transcripts
            if transcript.id == localized_episode.canonical_transcript_version_id
        )
        localized_version = next(
            transcript
            for transcript in localized_episode.transcripts
            if transcript.language == "de"
        )
        localized_version.turns[0].claims.append(
            Claim(
                text="A new localized claim that is not present in the canonical turn.",
                claim_type="fact",
                confidence=0.8,
            )
        )
        claim_qc = LocalizationService()._localized_transcript_qc(
            localized_episode,
            canonical_version,
            localized_version,
            "localized_reperformance",
        )
        assert claim_qc.status == "fail"
        assert claim_qc.details["failure_count"] >= 1
        assert any(
            failure["issue"] == "localized_new_claim_detected"
            for failure in claim_qc.details["failures"]
        )

        pending_audio_plan = client.post(
            f"/api/v1/episodes/{episode_id}/audio-assets/plan",
            json={"language": "de", "user_id": "tester"},
        )
        assert pending_audio_plan.status_code == 422
        assert "no transcript found for language de" in pending_audio_plan.json()["detail"]

        localized_version.turns[0].claims.pop()
        repository.save(localized_episode)
        localized_approval = next(
            approval
            for approval in repository.get(episode_id).approvals
            if approval.stage == "localized_transcript_review"
            and approval.target_id == str(localized_version.id)
        )
        approved_localized = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/{localized_approval.id}/decision",
            json={
                "decision": "approved",
                "comment": "German script is approved.",
                "user_id": "tester",
            },
        )
        assert approved_localized.status_code == 200
        approved_localized_transcript = next(
            transcript
            for transcript in approved_localized.json()["transcripts"]
            if transcript["id"] == str(localized_version.id)
        )
        assert approved_localized_transcript["status"] == "approved"

        audio_plan = client.post(
            f"/api/v1/episodes/{episode_id}/audio-assets/plan",
            json={"language": "de", "user_id": "tester"},
        )
        assert audio_plan.status_code == 200
        audio_body = audio_plan.json()
        playable_turn_count = sum(
            1 for turn in localized_transcripts[0]["turns"] if turn["status"] != "excluded"
        )
        audio_assets = [
            asset
            for asset in audio_body["assets"]
            if asset["asset_type"] == "audio" and asset["language"] == "de"
        ]
        assert len(audio_assets) == playable_turn_count
        assert {asset["source_entity_type"] for asset in audio_assets} == {"transcript_turn"}
        assert all(asset["generation_metadata"]["adapter"] == "voicebox" for asset in audio_assets)
        assert all(
            asset["generation_metadata"]["transcript_type"] == "localized"
            and asset["generation_metadata"]["localization"]["mode"] == "localized_reperformance"
            and asset["generation_metadata"]["localization"]["supports_dubbing"] is True
            for asset in audio_assets
        )
        listed_assets = client.get(f"/api/v1/episodes/{episode_id}/assets")
        assert listed_assets.status_code == 200
        listed_audio_assets = [
            asset
            for asset in listed_assets.json()
            if asset["asset_type"] == "audio" and asset["language"] == "de"
        ]
        assert len(listed_audio_assets) == playable_turn_count
        assert {asset["id"] for asset in listed_audio_assets} == {
            asset["id"] for asset in audio_assets
        }

        episode_for_cancel = repository.get(episode_id)
        asset_for_cancel = next(
            asset
            for asset in episode_for_cancel.assets
            if asset.asset_type == "audio" and asset.language == "de"
        )
        asset_for_cancel.status = "submitted"
        asset_for_cancel.generation_metadata = {
            **asset_for_cancel.generation_metadata,
            "status": "submitted",
            "voicebox_endpoint_id": "mock-voicebox",
            "remote_job_id": "api-cancel-job-1",
        }
        repository.save(episode_for_cancel)
        cancelled = client.post(
            f"/api/v1/episodes/{episode_id}/audio-assets/cancel",
            json={
                "language": "de",
                "asset_ids": [str(asset_for_cancel.id)],
                "user_id": "tester",
            },
        )
        assert cancelled.status_code == 200
        cancelled_body = cancelled.json()
        cancelled_asset = next(
            asset for asset in cancelled_body["assets"] if asset["id"] == str(asset_for_cancel.id)
        )
        assert cancelled_asset["status"] == "planned"
        assert cancelled_asset["generation_metadata"]["cancelled_remote_job_id"] == (
            "api-cancel-job-1"
        )
        assert cancelled_body["audit_events"][-1]["event_type"] == "audio.jobs.cancelled"

        generated = client.post(
            f"/api/v1/episodes/{episode_id}/audio-assets/generate",
            json={"language": "de", "user_id": "tester"},
        )
        assert generated.status_code == 200
        generated_body = generated.json()
        generated_audio_assets = [
            asset
            for asset in generated_body["assets"]
            if asset["asset_type"] == "audio" and asset["language"] == "de"
        ]
        assert len(generated_audio_assets) == playable_turn_count
        assert {asset["status"] for asset in generated_audio_assets} == {"completed"}
        assert all(
            asset["storage_uri"].startswith("object://dialecticore/audio/")
            for asset in generated_audio_assets
        )
        assert all(asset["checksum"] for asset in generated_audio_assets)
        assert all(
            asset["generation_metadata"]["word_timestamps"] for asset in generated_audio_assets
        )
        assert all(
            asset["generation_metadata"]["phoneme_timing"]["ready_for_lipsync"]
            for asset in generated_audio_assets
        )
        assert all(
            asset["generation_metadata"]["phoneme_timing"]["source"]
            == "estimated_from_word_timestamps"
            for asset in generated_audio_assets
        )
        assert all(
            asset["generation_metadata"]["normalized_phoneme_timestamps"]
            for asset in generated_audio_assets
        )
        assert all(
            asset["generation_metadata"]["storage_backend"] == "local_object_store"
            for asset in generated_audio_assets
        )
        assert all(
            asset["generation_metadata"]["media_probe"]["duration_ms"] == asset["duration_ms"]
            for asset in generated_audio_assets
        )
        assert all(
            asset["generation_metadata"]["media_probe"]["clipping_detected"] is False
            for asset in generated_audio_assets
        )
        assert all(
            asset["generation_metadata"]["media_probe"]["silence_ratio"] < 0.35
            for asset in generated_audio_assets
        )
        assert all(
            -28 <= asset["generation_metadata"]["media_probe"]["loudness_lufs"] <= -12
            for asset in generated_audio_assets
        )
        assert all(
            Path(asset["generation_metadata"]["object_storage_path"]).exists()
            for asset in generated_audio_assets
        )
        generated_turns = [
            turn
            for turn in generated_body["discussion_session"]["turns"]
            if turn["status"] != "excluded"
        ]
        assert len(generated_turns) == playable_turn_count
        assert all(turn["actual_audio_duration_seconds"] for turn in generated_turns)
        assert all(
            turn["generation_metadata"]["actual_audio_duration_seconds_by_language"]["de"]
            == turn["actual_audio_duration_seconds"]
            for turn in generated_turns
        )
        speaker_balance = generated_body["discussion_session"]["speaker_balance_state"]
        assert (
            sum(participant["actual_speaking_seconds"] for participant in speaker_balance.values())
            > 0
        )
        assert (
            generated_body["discussion_session"]["controller_state"]["actual_audio_timing"][
                "language"
            ]
            == "de"
        )
        audio_media_qc = [
            result
            for result in generated_body["quality_results"]
            if result["check_type"] == "audio_media_integrity"
        ][-1]
        assert audio_media_qc["status"] == "pass"
        assert audio_media_qc["details"]["checked_audio_asset_count"] == playable_turn_count
        assert audio_media_qc["details"]["probed_audio_asset_count"] == playable_turn_count
        assert (
            audio_media_qc["details"]["waveform_analyzed_audio_asset_count"] == playable_turn_count
        )
        assert audio_media_qc["details"]["phoneme_timed_audio_asset_count"] == playable_turn_count
        assert (
            audio_media_qc["details"]["estimated_phoneme_timed_audio_asset_count"]
            == playable_turn_count
        )
        assert audio_media_qc["details"]["phoneme_timing_missing_count"] == 0
        assert audio_media_qc["details"]["issue_count"] == 0

        selected_asset_id = generated_audio_assets[0]["id"]
        regenerated = client.post(
            f"/api/v1/episodes/{episode_id}/audio-assets/generate",
            json={
                "language": "de",
                "asset_ids": [selected_asset_id],
                "regenerate": True,
                "user_id": "tester",
            },
        )
        assert regenerated.status_code == 200
        regenerated_body = regenerated.json()
        regenerated_asset = next(
            asset for asset in regenerated_body["assets"] if asset["id"] == selected_asset_id
        )
        assert regenerated_asset["status"] == "completed"
        assert regenerated_asset["generation_metadata"]["generation_attempt_count"] == 2
        selective_qc = [
            result
            for result in regenerated_body["quality_results"]
            if result["check_type"] == "audio_media_integrity"
        ][-1]
        assert selective_qc["status"] == "pass"
        assert selective_qc["details"]["checked_audio_asset_count"] == 1
        assert selective_qc["details"]["selection"]["asset_ids"] == [selected_asset_id]

        audio_qc = client.post(
            f"/api/v1/episodes/{episode_id}/audio-assets/qc",
            json={"language": "de", "user_id": "tester"},
        )
        assert audio_qc.status_code == 200
        audio_qc_body = audio_qc.json()
        explicit_audio_qc = [
            result
            for result in audio_qc_body["quality_results"]
            if result["check_type"] == "audio_media_integrity"
        ][-1]
        assert explicit_audio_qc["status"] == "pass"
        assert explicit_audio_qc["details"]["checked_audio_asset_count"] == playable_turn_count

        audio_sync = client.post(
            f"/api/v1/episodes/{episode_id}/audio-assets/sync",
            json={"language": "de", "include_completed": True, "user_id": "tester"},
        )
        assert audio_sync.status_code == 200
        audio_sync_body = audio_sync.json()
        sync_audit = [
            event
            for event in audio_sync_body["audit_events"]
            if event["event_type"] == "audio.jobs.synced"
        ][-1]
        assert sync_audit["details"]["skipped_count"] == playable_turn_count
        assert sync_audit["details"]["checked_count"] == playable_turn_count

        subtitled = client.post(
            f"/api/v1/episodes/{episode_id}/subtitles/generate",
            json={"language": "de", "user_id": "tester"},
        )
        assert subtitled.status_code == 200
        subtitled_body = subtitled.json()
        subtitle_assets = [
            asset
            for asset in subtitled_body["assets"]
            if asset["asset_type"] == "subtitle" and asset["language"] == "de"
        ]
        assert len(subtitle_assets) == 1
        assert subtitle_assets[0]["status"] == "completed"
        assert subtitle_assets[0]["mime_type"] == "text/vtt"
        assert subtitle_assets[0]["storage_uri"].startswith("object://dialecticore/")
        assert subtitle_assets[0]["generation_metadata"]["storage_backend"] == "local_object_store"
        assert Path(subtitle_assets[0]["generation_metadata"]["object_storage_path"]).exists()
        assert subtitle_assets[0]["generation_metadata"]["subtitle_text"].startswith("WEBVTT")
        assert subtitle_assets[0]["generation_metadata"]["cue_count"] >= playable_turn_count
        assert subtitle_assets[0]["generation_metadata"]["missing_audio_count"] == 0
        assert subtitle_assets[0]["generation_metadata"]["word_timed_cue_count"] > 0
        assert subtitle_assets[0]["generation_metadata"]["transcript_type"] == "localized"
        assert (
            subtitle_assets[0]["generation_metadata"]["localization"]["mode"]
            == "localized_reperformance"
        )

        visual_plan = client.post(
            f"/api/v1/episodes/{episode_id}/visual-assets/plan",
            json={"language": "de", "user_id": "tester"},
        )
        assert visual_plan.status_code == 200
        visual_body = visual_plan.json()
        visual_assets = [
            asset
            for asset in visual_body["assets"]
            if asset["asset_type"] == "video"
            and asset["language"] == "de"
            and asset["generation_metadata"]["visual_role"] == "video_primary"
        ]
        broll_assets = [
            asset
            for asset in visual_body["assets"]
            if asset["asset_type"] == "broll"
            and asset["language"] == "de"
            and asset["generation_metadata"]["visual_role"] == "broll"
        ]
        reaction_loop_assets = [
            asset
            for asset in visual_body["assets"]
            if asset["asset_type"] == "reaction_loop"
            and asset["language"] == "de"
            and asset["source_entity_type"] == "participant_profile"
        ]
        studio_scene_assets = [
            asset
            for asset in visual_body["assets"]
            if asset["asset_type"] == "studio_scene"
            and asset["language"] == "de"
            and asset["source_entity_type"] == "episode"
        ]
        assert len(visual_assets) == playable_turn_count
        assert len(broll_assets) >= 1
        assert len(reaction_loop_assets) == 4
        assert len(studio_scene_assets) == 2
        assert {
            asset["generation_metadata"]["visual_role"] for asset in studio_scene_assets
        } == {"studio_scene", "studio_group_cutaway"}
        assert {
            asset["status"]
            for asset in (visual_assets + broll_assets + reaction_loop_assets + studio_scene_assets)
        } == {"planned"}
        assert all(
            asset["generation_metadata"]["comfyui_endpoint_id"] == "mock-comfyui"
            for asset in visual_assets
        )
        assert all(
            asset["generation_metadata"]["transcript_type"] == "localized"
            and asset["generation_metadata"]["localization"]["mode"] == "localized_reperformance"
            and asset["generation_metadata"]["localization"]["supports_video_reperformance"] is True
            for asset in visual_assets
        )
        assert all(asset["generation_metadata"]["shot_plan"] for asset in visual_assets)

        qc_types = {result["check_type"] for result in visual_body["quality_results"]}
        assert "localized_transcript_semantic_fidelity" in qc_types
        assert "audio_asset_plan_completeness" in qc_types
        assert "audio_generation_completeness" in qc_types
        assert "audio_media_integrity" in qc_types
        assert "subtitle_generation_completeness" in qc_types
        assert "visual_asset_plan_completeness" in qc_types
        subtitle_qc = [
            result
            for result in visual_body["quality_results"]
            if result["check_type"] == "subtitle_generation_completeness"
        ][-1]
        assert subtitle_qc["status"] == "pass"
        assert subtitle_qc["details"]["required_turn_count"] == playable_turn_count
        assert subtitle_qc["details"]["covered_turn_count"] == playable_turn_count
        assert subtitle_qc["details"]["cue_count"] >= playable_turn_count
        assert subtitle_qc["details"]["missing_audio_count"] == 0
        assert subtitle_qc["details"]["word_timed_cue_count"] > 0
        assert subtitle_qc["details"]["timing_overlap_count"] == 0
        assert (
            subtitle_qc["details"]["max_sync_error_ms"]
            <= (subtitle_qc["details"]["sync_error_threshold_ms"])
        )
        visual_qc = [
            result
            for result in visual_body["quality_results"]
            if result["check_type"] == "visual_asset_plan_completeness"
        ][-1]
        assert visual_qc["status"] == "pass"
        assert visual_qc["details"]["required_visual_turn_count"] == playable_turn_count
        assert visual_qc["details"]["planned_primary_visual_asset_count"] == (playable_turn_count)
        assert visual_qc["details"]["planned_reaction_loop_asset_count"] == len(
            reaction_loop_assets
        )
        assert visual_qc["details"]["planned_studio_scene_asset_count"] == 1
        assert visual_qc["details"]["shot_planned_turn_count"] == playable_turn_count

        visual_generation = client.post(
            f"/api/v1/episodes/{episode_id}/visual-assets/generate",
            json={"language": "de", "user_id": "tester"},
        )
        assert visual_generation.status_code == 200
        visual_generation_body = visual_generation.json()
        generated_visual_assets = [
            asset
            for asset in visual_generation_body["assets"]
            if asset["asset_type"] in {"video", "broll", "reaction_loop", "studio_scene"}
            and asset["language"] == "de"
            and asset["status"] != "replaced"
        ]
        planned_visual_assets = (
            visual_assets + broll_assets + reaction_loop_assets + studio_scene_assets
        )
        assert len(generated_visual_assets) == len(planned_visual_assets)
        generated_reaction_loop_assets = [
            asset for asset in generated_visual_assets if asset["asset_type"] == "reaction_loop"
        ]
        generated_studio_scene_assets = [
            asset for asset in generated_visual_assets if asset["asset_type"] == "studio_scene"
        ]
        assert len(generated_reaction_loop_assets) == len(reaction_loop_assets)
        assert len(generated_studio_scene_assets) == len(studio_scene_assets)
        assert {asset["status"] for asset in generated_visual_assets} == {"completed"}
        assert all(
            asset["storage_uri"].startswith("object://dialecticore/visual/")
            for asset in generated_visual_assets
        )
        assert all(asset["checksum"] for asset in generated_visual_assets)
        assert all(
            Path(asset["generation_metadata"]["object_storage_path"]).exists()
            for asset in generated_visual_assets
        )
        assert all(
            asset["generation_metadata"]["media_probe"]["probe_tool"] == "svg_header"
            for asset in generated_visual_assets
        )
        assert all(
            asset["generation_metadata"]["deterministic_mock_visual"] is True
            for asset in generated_visual_assets
        )
        assert all(
            asset["generation_metadata"]["render_ready"] is True
            for asset in generated_visual_assets
        )
        visual_generation_qc = [
            result
            for result in visual_generation_body["quality_results"]
            if result["check_type"] == "visual_generation_completeness"
        ][-1]
        assert visual_generation_qc["status"] == "pass"
        assert visual_generation_qc["details"]["completed_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_generation_qc["details"]["stored_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_generation_qc["details"]["probed_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_generation_qc["details"]["render_ready_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_generation_qc["details"]["render_suitable_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_generation_qc["details"]["fallback_visual_asset_count"] == 0

        visual_media_qc = client.post(
            f"/api/v1/episodes/{episode_id}/visual-assets/qc",
            json={"language": "de", "user_id": "tester"},
        )
        assert visual_media_qc.status_code == 200
        visual_media_qc_body = visual_media_qc.json()
        visual_media_result = [
            result
            for result in visual_media_qc_body["quality_results"]
            if result["check_type"] == "visual_media_integrity"
        ][-1]
        assert visual_media_result["status"] == "pass", visual_media_result["details"]["issues"]
        assert visual_media_result["details"]["checked_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_media_result["details"]["completed_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_media_result["details"]["placeholder_visual_asset_count"] == 0
        assert visual_media_result["details"]["lip_sync_ready_visual_asset_count"] == (
            playable_turn_count
        )
        assert visual_media_result["details"]["lip_sync_missing_visual_asset_count"] == 0
        assert visual_media_result["details"]["render_suitable_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_media_result["details"]["pixel_analyzed_visual_asset_count"] == (
            len(generated_visual_assets)
        )
        assert visual_media_result["details"]["pixel_warning_visual_asset_count"] == 0

        audit = client.get("/api/v1/audit-events?limit=100")
        event_types = {event["event_type"] for event in audit.json()}
        assert "localization.transcript.created" in event_types
        assert "audio.assets.planned" in event_types
        assert "audio.assets.generated" in event_types
        assert "audio.assets.regenerated" in event_types
        assert "audio.qc.completed" in event_types
        assert "audio.jobs.synced" in event_types
        assert "subtitle.asset.generated" in event_types
        assert "visual.assets.planned" in event_types
        assert "visual.assets.generated" in event_types
        assert "visual.qc.completed" in event_types
    finally:
        app.dependency_overrides.clear()


def test_speech_production_api_plans_and_generates_approved_transcript(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        settings,
    )
    app.dependency_overrides[get_voicebox_service] = lambda: VoiceboxService(settings)
    client = TestClient(app)

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]
        client.post(f"/api/v1/episodes/{episode_id}/produce")
        approvals = client.get(f"/api/v1/episodes/{episode_id}/approvals")
        approval_id = approvals.json()["approvals"][0]["id"]
        approved = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/{approval_id}/decision",
            json={"decision": "approved", "comment": "Ready.", "user_id": "tester"},
        )
        assert approved.status_code == 200

        speech = client.post(
            f"/api/v1/episodes/{episode_id}/speech/produce",
            json={"user_id": "tester"},
        )

        assert speech.status_code == 200
        body = speech.json()
        canonical_id = body["canonical_transcript_version_id"]
        canonical = next(
            transcript for transcript in body["transcripts"] if transcript["id"] == canonical_id
        )
        playable_turn_count = sum(1 for turn in canonical["turns"] if turn["status"] != "excluded")
        audio_assets = [
            asset
            for asset in body["assets"]
            if asset["asset_type"] == "audio" and asset["language"] == canonical["language"]
        ]
        assert len(audio_assets) == playable_turn_count
        assert {asset["status"] for asset in audio_assets} == {"completed"}
        assert all(asset["generation_metadata"]["voice_profile_id"] for asset in audio_assets)
        assert all(
            asset["generation_metadata"]["transcript_version_id"] == canonical_id
            for asset in audio_assets
        )
        event_types = [event["event_type"] for event in body["audit_events"]]
        assert "audio.assets.planned" in event_types
        assert "audio.assets.generated" in event_types
    finally:
        app.dependency_overrides.clear()


def test_visual_production_api_plans_and_generates_after_speech(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        settings,
    )
    app.dependency_overrides[get_voicebox_service] = lambda: VoiceboxService(settings)
    app.dependency_overrides[get_comfyui_service] = lambda: ComfyUiService(settings)
    client = TestClient(app)

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]
        client.post(f"/api/v1/episodes/{episode_id}/produce")
        approvals = client.get(f"/api/v1/episodes/{episode_id}/approvals")
        approval_id = approvals.json()["approvals"][0]["id"]
        approved = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/{approval_id}/decision",
            json={"decision": "approved", "comment": "Ready.", "user_id": "tester"},
        )
        assert approved.status_code == 200
        speech = client.post(
            f"/api/v1/episodes/{episode_id}/speech/produce",
            json={"user_id": "tester"},
        )
        assert speech.status_code == 200

        visuals = client.post(
            f"/api/v1/episodes/{episode_id}/visuals/produce",
            json={"user_id": "tester"},
        )

        assert visuals.status_code == 200
        body = visuals.json()
        canonical_id = body["canonical_transcript_version_id"]
        canonical = next(
            transcript for transcript in body["transcripts"] if transcript["id"] == canonical_id
        )
        playable_turn_count = sum(1 for turn in canonical["turns"] if turn["status"] != "excluded")
        primary_visual_assets = [
            asset
            for asset in body["assets"]
            if asset["asset_type"] == "video"
            and asset["language"] == canonical["language"]
            and asset["source_entity_type"] == "transcript_turn"
            and asset["generation_metadata"].get("visual_role") == "video_primary"
        ]
        assert len(primary_visual_assets) == playable_turn_count
        assert {asset["status"] for asset in primary_visual_assets} == {"completed"}
        assert all(
            asset["generation_metadata"]["transcript_version_id"] == canonical_id
            for asset in primary_visual_assets
        )
        assert all(
            "portrait_reference_image_uri" in asset["generation_metadata"]["prompt_inputs"]
            and "full_body_reference_image_uri" in asset["generation_metadata"]["prompt_inputs"]
            and "wardrobe_reference_image_uri" in asset["generation_metadata"]["prompt_inputs"]
            for asset in primary_visual_assets
        )
        event_types = [event["event_type"] for event in body["audit_events"]]
        assert "visual.assets.planned" in event_types
        assert "visual.assets.generated" in event_types
    finally:
        app.dependency_overrides.clear()


def test_workflow_advance_api_runs_selected_episode_stage_chain(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        settings,
    )
    app.dependency_overrides[get_research_service] = lambda: ResearchService(settings)
    app.dependency_overrides[get_production_control_service] = lambda: ProductionControlService(
        settings
    )
    app.dependency_overrides[get_localization_service] = lambda: LocalizationService()
    app.dependency_overrides[get_voicebox_service] = lambda: VoiceboxService(settings)
    app.dependency_overrides[get_subtitle_service] = lambda: SubtitleService(settings)
    app.dependency_overrides[get_comfyui_service] = lambda: ComfyUiService(settings)
    app.dependency_overrides[get_timeline_service] = lambda: TimelineService(settings)
    app.dependency_overrides[get_render_service] = lambda: FakeWorkflowRenderService(settings)
    app.dependency_overrides[get_publisher_service] = lambda: PublisherService()
    client = TestClient(app)

    try:
        selected_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]
        untouched_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]
        client.post(f"/api/v1/episodes/{selected_id}/produce")
        approvals = client.get(f"/api/v1/episodes/{selected_id}/approvals")
        approval_id = approvals.json()["approvals"][0]["id"]
        approved = client.post(
            f"/api/v1/episodes/{selected_id}/approvals/{approval_id}/decision",
            json={"decision": "approved", "comment": "Ready.", "user_id": "tester"},
        )
        assert approved.status_code == 200
        response = client.post(f"/api/v1/episodes/{selected_id}/workflow/advance")

        assert response.status_code == 200
        body = response.json()
        assert body["episode"]["id"] == selected_id
        summary = body["summary"]
        assert summary["schema_version"] == "workflow_worker_orchestration_summary.v1"
        assert summary["batch_limit"] == 1
        assert summary["stage_order"] == [
            "research",
            "discussion",
            "localization",
            "qc",
            "audio",
            "voicebox",
            "subtitles",
            "visuals",
            "comfyui",
            "timeline",
            "render",
            "publishing",
            "completion",
        ]
        assert summary["stages"]["audio"]["completed_audio_assets"] > 0
        assert summary["stages"]["subtitles"]["subtitles_generated"] > 0
        assert summary["stages"]["visuals"]["completed_visual_assets"] > 0
        assert summary["stages"]["timeline"]["timelines_built"] == 1
        assert summary["stages"]["render"]["preview_render_requests_submitted"] == 1
        assert summary["orchestration_records"][0]["episode_id"] == selected_id
        handoff = summary["production_handoffs"][0]
        assert handoff["schema_version"] == "talkshow_production_handoff.v1"
        assert handoff["episode_id"] == selected_id
        assert handoff["status"] == "review_ready"
        assert handoff["blocking_reasons"] == []
        assert handoff["playable_turn_count"] > 0
        assert handoff["character_configuration"]["schema_version"] == (
            "character_configuration_handoff.v1"
        )
        assert handoff["character_configuration"]["ready"] is True
        assert handoff["character_configuration"]["active_speaker_count"] >= 1
        assert (
            handoff["character_configuration"]["configured_model_speaker_count"]
            == handoff["character_configuration"]["active_speaker_count"]
        )
        assert (
            handoff["character_configuration"]["configured_voice_speaker_count"]
            == handoff["character_configuration"]["active_speaker_count"]
        )
        assert (
            handoff["character_configuration"]["configured_visual_speaker_count"]
            == handoff["character_configuration"]["active_speaker_count"]
        )
        assert handoff["speech"]["ready"] is True
        assert handoff["character_animation"]["ready"] is True
        assert handoff["subtitles"]["ready"] is True
        assert handoff["timeline"]["ready"] is True
        assert handoff["timeline"]["linked_turn_count"] == handoff["playable_turn_count"]
        assert (
            handoff["turn_handoffs"]["completed_audio_turn_count"] == handoff["playable_turn_count"]
        )
        assert (
            handoff["turn_handoffs"]["completed_primary_visual_turn_count"]
            == handoff["playable_turn_count"]
        )
        assert handoff["render"]["preview_render_asset_id"] is not None
        assert handoff["render"]["final_render_asset_id"] is None

        updated = body["episode"]
        assert updated["workflow_control"]["worker_orchestration_log"][0]["worker_id"] == (
            "workflow-worker"
        )
        persisted_handoff = updated["workflow_control"]["worker_orchestration_log"][0][
            "production_handoff"
        ]
        assert persisted_handoff["status"] == "review_ready"
        assert persisted_handoff["episode_id"] == selected_id
        assert (
            updated["workflow_control"]["run"]["last_worker_orchestration"]["production_handoff"][
                "status"
            ]
            == "review_ready"
        )
        asset_types = {asset["asset_type"] for asset in updated["assets"]}
        assert {"audio", "subtitle", "video", "timeline", "render"}.issubset(asset_types)

        final_response = client.post(f"/api/v1/episodes/{selected_id}/workflow/advance")
        assert final_response.status_code == 200
        final_body = final_response.json()
        assert final_body["summary"]["stages"]["render"]["final_renders_created"] == 0
        preview_render = next(
            asset
            for asset in final_body["episode"]["assets"]
            if asset["asset_type"] == "render"
            and asset["generation_metadata"].get("render_type") == "preview"
        )
        preview_approval = next(
            approval
            for approval in final_body["episode"]["approvals"]
            if approval["stage"] == "preview_render_review"
            and approval["target_id"] == preview_render["id"]
        )
        approved_preview = client.post(
            f"/api/v1/episodes/{selected_id}/approvals/{preview_approval['id']}/decision",
            json={
                "decision": "approved",
                "comment": "Preview render is ready for final rendering.",
                "user_id": "tester",
            },
        )
        assert approved_preview.status_code == 200

        final_response = client.post(f"/api/v1/episodes/{selected_id}/workflow/advance")
        assert final_response.status_code == 200
        final_body = final_response.json()
        assert final_body["summary"]["stages"]["render"]["final_render_requests_submitted"] == 1
        final_render = next(
            asset
            for asset in final_body["episode"]["assets"]
            if asset["asset_type"] == "render"
            and asset["generation_metadata"].get("render_type") == "final"
        )
        final_approval = next(
            approval
            for approval in final_body["episode"]["approvals"]
            if approval["stage"] == "final_render_review"
            and approval["target_id"] == final_render["id"]
        )
        approved_final = client.post(
            f"/api/v1/episodes/{selected_id}/approvals/{final_approval['id']}/decision",
            json={
                "decision": "approved",
                "comment": "Final render is ready.",
                "user_id": "tester",
            },
        )
        assert approved_final.status_code == 200

        publishing_response = client.post(f"/api/v1/episodes/{selected_id}/workflow/advance")
        assert publishing_response.status_code == 200
        publishing_body = publishing_response.json()
        assert publishing_body["summary"]["stages"]["publishing"]["thumbnails_created"] == 1
        assert publishing_body["summary"]["stages"]["publishing"]["youtube_packages_created"] == 1
        publishing_stage = publishing_body["summary"]["stages"]["publishing"]
        assert publishing_stage["production_manifests_created"] == 1
        assert publishing_stage["production_manifests_refreshed"] == 1
        assert publishing_stage["dry_run_publish_jobs_created"] == 1
        completion_stage = publishing_body["summary"]["stages"]["completion"]
        assert completion_stage["episodes_completed"] == 1
        assert completion_stage["completed_episode_ids"] == [selected_id]
        delivery_handoff = publishing_body["summary"]["production_handoffs"][0]
        assert delivery_handoff["status"] == "delivery_ready"
        assert delivery_handoff["render"]["final_render_asset_id"] is not None
        assert delivery_handoff["render"]["delivery_package_asset_id"] is not None
        assert delivery_handoff["render"]["production_manifest_asset_id"] is not None
        finished_asset_types = {
            asset["asset_type"] for asset in publishing_body["episode"]["assets"]
        }
        assert {"thumbnail", "export_package", "production_manifest"}.issubset(finished_asset_types)
        assert publishing_body["episode"]["publish_jobs"][0]["dry_run"] is True
        current_manifest = [
            asset
            for asset in publishing_body["episode"]["assets"]
            if asset["asset_type"] == "production_manifest" and asset["status"] == "completed"
        ][-1]["generation_metadata"]["production_manifest"]
        assert (
            current_manifest["publish_jobs"][0]["id"]
            == publishing_body["episode"]["publish_jobs"][0]["id"]
        )
        assert len(publishing_body["episode"]["workflow_control"]["worker_orchestration_log"]) == 4
        persisted_delivery_handoff = publishing_body["episode"]["workflow_control"][
            "worker_orchestration_log"
        ][-1]["production_handoff"]
        assert persisted_delivery_handoff["status"] == "delivery_ready"
        assert (
            publishing_body["episode"]["workflow_control"]["run"]["last_worker_orchestration"][
                "production_handoff"
            ]["status"]
            == "delivery_ready"
        )
        assert publishing_body["episode"]["status"] == "COMPLETED"
        completed_run = publishing_body["episode"]["workflow_control"]["run"]
        assert completed_run["state"] == "completed"
        assert completed_run["current_stage"] == "COMPLETED"
        assert completed_run["completion_gate"]["status"] == "pass"
        assert completed_run["signals"][-1]["signal_type"] == "complete"
        completed_event_types = [
            event["event_type"] for event in publishing_body["episode"]["audit_events"]
        ]
        assert "workflow.completed" in completed_event_types
        assert "workflow.stage.changed" in completed_event_types

        readiness = client.get(f"/api/v1/episodes/{selected_id}/workflow/completion-readiness")
        assert readiness.status_code == 200
        assert readiness.json()["status"] == "pass"
        production_test_report = client.get(
            f"/api/v1/episodes/{selected_id}/production-test-report"
        )
        assert production_test_report.status_code == 200
        report = production_test_report.json()
        assert report["schema_version"] == "production_test_report.v1"
        assert report["status"] == "pass"
        assert report["acceptance_summary"]["schema_version"] == (
            "production_acceptance_summary.v1"
        )
        assert report["acceptance_summary"]["status"] == "pass"
        assert report["acceptance_summary"]["production_test_status"] == "pass"
        assert report["acceptance_summary"]["artifact_download_status"] == "pass"
        assert report["acceptance_summary"]["deliverables"]["final_render"] == {
            "asset_id": report["deliverables"]["final_render"]["asset_id"],
            "status": "completed",
            "checksum": report["deliverables"]["final_render"]["checksum"],
            "mime_type": report["deliverables"]["final_render"]["mime_type"],
            "downloadable": True,
            "file_size_bytes": report["deliverables"]["final_render"]["file_size_bytes"],
            "download_missing_reason": None,
        }
        assert "download_url" not in report["acceptance_summary"]["deliverables"]["final_render"]
        assert report["acceptance_summary"]["package"]["manifest_schema_version"] == (
            "youtube_package.v1"
        )
        assert report["acceptance_summary"]["package"]["manifest_matches_asset_metadata"] is True
        assert report["audio_first_test_ready"] is True
        assert report["production_target_satisfied"] is True
        assert report["real_life_test_readiness"]["schema_version"] == (
            "production_real_life_test_readiness.v1"
        )
        assert report["real_life_test_readiness"]["ready"] is False
        assert report["real_life_test_readiness"]["audio_first_ready"] is False
        assert report["real_life_test_readiness"]["live_provider_preflight_ready"] is False
        assert report["real_life_test_readiness"]["audio_first_blockers"] == [
            "run_live_provider_preflight_before_real_life_test"
        ]
        assert report["media_readiness"]["schema_version"] == "production_media_readiness.v1"
        assert report["media_readiness"]["audio_first_ready"] is True
        assert report["media_readiness"]["native_visual_ready"] is False
        assert report["media_readiness"]["native_visual_config_ready"] is False
        assert report["media_readiness"]["managed_media_catalog_ready"] is True
        assert report["media_readiness"]["native_prompt_admission_ready"] is True
        assert report["media_readiness"]["managed_media_required_endpoints"] == []
        assert report["media_readiness"]["managed_media_missing_preset_endpoints"] == []
        assert report["deliverables"]["final_render"]["status"] == "completed"
        assert report["deliverables"]["final_render"]["downloadable"] is True
        assert report["deliverables"]["final_render"]["download_url"]
        assert report["deliverables"]["export_package"]["checksum"]
        assert report["deliverables"]["export_package"]["downloadable"] is True
        assert report["deliverables"]["export_package"]["file_size_bytes"] > 0
        assert report["deliverables"]["production_manifest"]["status"] == "completed"
        assert report["deliverables"]["production_manifest"]["downloadable"] is True
        assert report["package_inspection"]["status"] == "pass"
        assert "youtube-package.json" in report["package_inspection"]["included_files"]
        assert report["package_inspection"]["manifest_schema_version"] == "youtube_package.v1"
        assert report["package_inspection"]["manifest_matches_asset_metadata"] is True
        assert report["publish"]["status"] == "completed"
        assert report["publish"]["dry_run"] is True
        assert report["operator_next_action"] == "inspect_export_package_and_publish_evidence"
        package_asset = next(
            asset
            for asset in repository.get(selected_id).assets
            if asset.asset_type == AssetType.export_package and asset.status == "completed"
        )
        package_path = create_object_store(settings).path_for_uri(package_asset.storage_uri or "")
        assert package_path is not None
        package_path.unlink()
        missing_package_report = client.get(
            f"/api/v1/episodes/{selected_id}/production-test-report"
        )
        assert missing_package_report.status_code == 200
        assert missing_package_report.json()["status"] == "warning"
        assert missing_package_report.json()["acceptance_summary"]["status"] == "fail"
        assert (
            missing_package_report.json()["acceptance_summary"]["artifact_download_status"]
            == "fail"
        )
        assert "export_package_not_downloadable" in missing_package_report.json()["blockers"]
        assert "export_package_not_inspectable" in missing_package_report.json()["blockers"]
        assert (
            missing_package_report.json()["deliverables"]["export_package"][
                "download_missing_reason"
            ]
            == "stored_object_not_found"
        )
        assert missing_package_report.json()["package_inspection"]["issues"] == [
            "package_file_missing"
        ]
        replay = client.get(f"/api/v1/episodes/{selected_id}/workflow/replay")
        assert replay.status_code == 200
        assert replay.json()["status"] == "pass"
        assert replay.json()["replayed"]["state"] == "completed"
        assert replay.json()["replayed"]["current_stage"] == "COMPLETED"

        untouched = client.get(f"/api/v1/episodes/{untouched_id}").json()
        assert untouched["status"] == "DRAFT"
        assert untouched["assets"] == []
        assert untouched["workflow_control"].get("worker_orchestration_log") is None
    finally:
        app.dependency_overrides.clear()


def test_workflow_run_until_blocked_stops_at_human_review_gates(
    tmp_path: Path,
) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        settings,
    )
    app.dependency_overrides[get_research_service] = lambda: ResearchService(settings)
    app.dependency_overrides[get_production_control_service] = lambda: ProductionControlService(
        settings
    )
    app.dependency_overrides[get_localization_service] = lambda: LocalizationService()
    app.dependency_overrides[get_voicebox_service] = lambda: VoiceboxService(settings)
    app.dependency_overrides[get_subtitle_service] = lambda: SubtitleService(settings)
    app.dependency_overrides[get_comfyui_service] = lambda: ComfyUiService(settings)
    app.dependency_overrides[get_timeline_service] = lambda: TimelineService(settings)
    app.dependency_overrides[get_render_service] = lambda: FakeWorkflowRenderService(settings)
    app.dependency_overrides[get_publisher_service] = lambda: PublisherService()
    client = TestClient(app)

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]

        first = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/run-until-blocked",
            json={"max_passes": 4, "user_id": "tester"},
        )
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["status"] == "awaiting_approval"
        assert first_body["stop_reason"] == "pending_approval"
        assert first_body["pass_count"] == 1
        assert first_body["pending_approvals"][0]["stage"] == "transcript_review"
        assert first_body["episode"]["workflow_control"]["run"]["state"] == "running"
        first_evidence = first_body["episode"]["workflow_control"]["last_run_until_blocked"]
        assert first_evidence["schema_version"] == "workflow_run_until_blocked_evidence.v1"
        assert first_evidence["status"] == "awaiting_approval"
        assert first_evidence["stop_reason"] == "pending_approval"
        assert first_evidence["pass_count"] == 1
        assert first_evidence["pending_approval_stages"] == ["transcript_review"]
        assert first_evidence["completion_status"] == "fail"
        assert first_body["handoff"]["status"] == "blocked"
        assert first_body["handoff"]["next_handoff_action"] == "approve_broadcast_transcript"
        assert first_body["handoff"]["blocking_reasons"][0] == "transcript_not_approved"
        assert first_evidence["handoff"] == first_body["handoff"]
        assert "summaries" not in first_evidence
        assert (
            first_body["episode"]["workflow_control"]["run"]["last_run_until_blocked"]
            == first_evidence
        )

        approved_transcript = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/"
            f"{first_body['pending_approvals'][0]['id']}/decision",
            json={"decision": "approved", "comment": "Transcript ready.", "user_id": "tester"},
        )
        assert approved_transcript.status_code == 200

        preview = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/run-until-blocked",
            json={"max_passes": 4, "user_id": "tester"},
        )
        assert preview.status_code == 200
        preview_body = preview.json()
        assert preview_body["status"] == "awaiting_approval"
        assert preview_body["stop_reason"] == "pending_approval"
        assert preview_body["pending_approvals"][0]["stage"] == "preview_render_review"
        assert preview_body["handoff"]["status"] == "review_ready"
        assert preview_body["handoff"]["next_handoff_action"] == "review_preview_render"
        assert preview_body["handoff"]["stage_readiness"]["speech"] is True
        assert preview_body["handoff"]["stage_readiness"]["character_animation"] is True
        assert (
            preview_body["summaries"][0]["stages"]["render"]["preview_render_requests_submitted"]
            == 1
        )
        assert any(
            asset["asset_type"] == "render"
            and asset["generation_metadata"].get("render_type") == "preview"
            for asset in preview_body["episode"]["assets"]
        )

        approved_preview = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/"
            f"{preview_body['pending_approvals'][0]['id']}/decision",
            json={"decision": "approved", "comment": "Preview ready.", "user_id": "tester"},
        )
        assert approved_preview.status_code == 200

        final = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/run-until-blocked",
            json={"max_passes": 4, "user_id": "tester"},
        )
        assert final.status_code == 200
        final_body = final.json()
        assert final_body["status"] == "awaiting_approval"
        assert final_body["stop_reason"] == "pending_approval"
        assert final_body["pending_approvals"][0]["stage"] == "final_render_review"
        assert final_body["handoff"]["status"] == "review_ready"
        assert final_body["handoff"]["next_handoff_action"] == "review_final_render"
        assert (
            final_body["summaries"][0]["stages"]["render"]["final_render_requests_submitted"] == 1
        )

        approved_final = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/"
            f"{final_body['pending_approvals'][0]['id']}/decision",
            json={"decision": "approved", "comment": "Final ready.", "user_id": "tester"},
        )
        assert approved_final.status_code == 200

        completed = client.post(
            f"/api/v1/episodes/{episode_id}/workflow/run-until-blocked",
            json={"max_passes": 4, "user_id": "tester"},
        )
        assert completed.status_code == 200
        completed_body = completed.json()
        assert completed_body["status"] == "completed"
        assert completed_body["stop_reason"] == "completed"
        assert completed_body["episode"]["status"] == "COMPLETED"
        assert completed_body["completion_readiness"]["status"] == "pass"
        completed_evidence = completed_body["episode"]["workflow_control"]["last_run_until_blocked"]
        assert completed_evidence["status"] == "completed"
        assert completed_evidence["stop_reason"] == "completed"
        assert completed_evidence["pending_approval_count"] == 0
        assert completed_evidence["completion_status"] == "pass"
        assert completed_body["handoff"]["status"] == "delivery_ready"
        assert completed_body["handoff"]["next_handoff_action"] == (
            "complete_workflow_or_inspect_publish_evidence"
        )
        assert completed_evidence["handoff"] == completed_body["handoff"]
        assert (
            completed_body["summaries"][0]["stages"]["publishing"]["youtube_packages_created"] == 1
        )
        assert completed_body["summaries"][0]["stages"]["completion"]["episodes_completed"] == 1
        assert "workflow.run_until_blocked.recorded" in [
            event["event_type"] for event in completed_body["episode"]["audit_events"]
        ]
        report_response = client.get(f"/api/v1/episodes/{episode_id}/production-test-report")
        assert report_response.status_code == 200
        report = report_response.json()
        assert report["workflow_run_until_blocked"] == {
            "schema_version": "production_workflow_run_until_blocked_summary.v1",
            "source_schema_version": "workflow_run_until_blocked_evidence.v1",
            "recorded_at": completed_evidence["recorded_at"],
            "status": "completed",
            "stop_reason": "completed",
            "pass_count": completed_evidence["pass_count"],
            "progressed_stage_count": completed_evidence["progressed_stage_count"],
            "pending_approval_count": 0,
            "pending_approval_stages": [],
            "completion_status": "pass",
            "completion_failed_checks": [],
            "handoff": completed_evidence["handoff"],
            "orchestration_attempt_count": len(completed_evidence["orchestration_attempt_ids"]),
            "orchestration_attempt_ids": completed_evidence["orchestration_attempt_ids"],
        }
        assert (
            report["acceptance_summary"]["workflow_run_until_blocked"]
            == report["workflow_run_until_blocked"]
        )
        assert "summaries" not in report["workflow_run_until_blocked"]
    finally:
        app.dependency_overrides.clear()


def test_pending_episode_approvals_ignore_stale_retry_stage_approvals() -> None:
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    stale = Approval(
        episode_id=episode.id,
        stage="transcript_review",
        decision="pending",
        comment="stale transcript review",
        created_at=datetime.now(UTC),
    )
    episode.approvals.append(stale)
    episode.status = EpisodeStatus.discussing
    episode.workflow_control = {
        "run": {
            "schema_version": "production_workflow_run.v1",
            "state": "running",
            "current_stage": EpisodeStatus.discussing.value,
        }
    }

    assert _pending_episode_approvals(episode) == []

    broadcast = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language=episode.source_language,
        status="pending_review",
        turns=[
            TranscriptTurn(
                transcript_version_id=stale.id,
                speaker_participant_id="host",
                text="Ready for review.",
                source_discussion_turn_ids=[],
            )
        ],
    )
    episode.transcripts.append(broadcast)

    assert _pending_episode_approvals(episode) == [stale]
    handoff = _pending_transcript_review_handoff(episode, [stale])
    assert handoff is not None
    assert handoff["status"] == "blocked"
    assert handoff["next_handoff_action"] == "approve_broadcast_transcript"
    assert handoff["blocking_reasons"] == ["transcript_not_approved"]
    assert handoff["transcript_version_id"] == str(broadcast.id)
    assert handoff["playable_turn_count"] == 1

    episode.transcripts.clear()
    episode.status = EpisodeStatus.transcript_review
    episode.workflow_control["run"]["current_stage"] = EpisodeStatus.transcript_review.value

    assert _pending_episode_approvals(episode) == [stale]


def test_primer_render_approval_is_not_an_actionable_talkshow_gate() -> None:
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    primer_timeline = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language=episode.source_language,
        source_entity_type="primer_production",
        source_entity_id="primer-1",
        status="completed",
    )
    primer_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(primer_timeline.id),
        status="completed",
        generation_metadata={"render_type": "preview"},
    )
    approval = Approval(
        episode_id=episode.id,
        stage="preview_render_review",
        target_type="render_asset",
        target_id=str(primer_render.id),
        decision="pending",
    )
    episode.assets.extend([primer_timeline, primer_render])
    episode.approvals.append(approval)
    repository.save(episode)

    assert _pending_episode_approvals(episode) == []
    summary = repository.list_summaries()[0]
    assert summary.pending_approval_count == 0
    assert summary.pending_approvals == []


def test_episode_summary_list_can_exclude_archived_episodes() -> None:
    repository = EpisodeRepository()
    current = repository.create(EpisodeCreateRequest(definition=definition()))
    completed = repository.create(EpisodeCreateRequest(definition=definition()))
    cancelled = repository.create(EpisodeCreateRequest(definition=definition()))
    completed.status = EpisodeStatus.completed
    cancelled.status = EpisodeStatus.cancelled
    repository.save(completed)
    repository.save(cancelled)

    current_summaries = repository.list_summaries(include_archived=False)
    all_summaries = repository.list_summaries(include_archived=True)

    assert [summary.id for summary in current_summaries] == [current.id]
    assert {summary.id for summary in all_summaries} == {
        current.id,
        completed.id,
        cancelled.id,
    }


def test_only_latest_current_talkshow_render_approval_is_actionable() -> None:
    repository = EpisodeRepository()
    episode = repository.create(EpisodeCreateRequest(definition=definition()))
    canonical_transcript_id = uuid4()
    episode.canonical_transcript_version_id = canonical_transcript_id
    timeline = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language=episode.source_language,
        source_entity_type="transcript_version",
        source_entity_id=str(canonical_transcript_id),
        status="completed",
    )
    replaced_timeline = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language=episode.source_language,
        source_entity_type="transcript_version",
        source_entity_id=str(canonical_transcript_id),
        status="replaced",
    )
    earlier_preview = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(replaced_timeline.id),
        status="completed",
        generation_metadata={"render_type": "preview"},
    )
    current_preview = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language=episode.source_language,
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline.id),
        status="completed",
        generation_metadata={"render_type": "preview"},
    )
    obsolete_approval = Approval(
        episode_id=episode.id,
        stage="preview_render_review",
        target_type="render_asset",
        target_id=str(earlier_preview.id),
        decision="pending",
    )
    current_approval = Approval(
        episode_id=episode.id,
        stage="preview_render_review",
        target_type="render_asset",
        target_id=str(current_preview.id),
        decision="pending",
    )
    episode.assets.extend([replaced_timeline, earlier_preview, timeline, current_preview])
    episode.approvals.extend([obsolete_approval, current_approval])
    repository.save(episode)

    assert _pending_episode_approvals(episode) == [current_approval]
    summary = repository.list_summaries()[0]
    assert summary.pending_approval_count == 1
    assert summary.pending_approvals == [current_approval]


def test_timeline_api_builds_reads_and_edits_timeline(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    render_service = RenderService(settings)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_discussion_engine] = lambda: DiscussionEngine(
        ModelGateway(),
        settings,
    )
    app.dependency_overrides[get_timeline_service] = lambda: TimelineService(settings)
    app.dependency_overrides[get_render_service] = lambda: render_service
    client = TestClient(app)

    try:
        episode_id = client.post(
            "/api/v1/episodes",
            json={"definition": definition().model_dump(mode="json")},
        ).json()["id"]
        client.post(f"/api/v1/episodes/{episode_id}/produce")
        episode = repository.get(episode_id)
        transcript = next(
            transcript
            for transcript in episode.transcripts
            if transcript.id == episode.canonical_transcript_version_id
        )
        transcript.status = "approved"
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        episode.assets.append(
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.subtitle,
                language=transcript.language,
                source_entity_type="transcript_version",
                source_entity_id=str(transcript.id),
                storage_uri="mock://subtitles/en/test.vtt",
                mime_type="text/vtt",
                duration_ms=24_000,
                checksum="sha256:subtitle",
                status="completed",
                generation_metadata={
                    "format": "vtt",
                    "transcript_version_id": str(transcript.id),
                    "subtitle_text": (
                        "WEBVTT\n\n"
                        "1\n"
                        "00:00:00.000 --> 00:00:01.000\n"
                        "Moderator: API render caption one.\n\n"
                        "2\n"
                        "00:00:01.000 --> 00:00:02.000\n"
                        "The Optimist: API render caption two.\n"
                    ),
                },
            )
        )
        for index, turn in enumerate(playable_turns, start=1):
            episode.assets.extend(
                [
                    Asset(
                        episode_id=episode.id,
                        asset_type=AssetType.audio,
                        language=transcript.language,
                        source_entity_type="transcript_turn",
                        source_entity_id=str(turn.id),
                        storage_uri=f"object://dialecticore/audio/{turn.id}.wav",
                        mime_type="audio/wav",
                        duration_ms=1000,
                        checksum=f"sha256:audio-{index}",
                        status="completed",
                        generation_metadata={"transcript_version_id": str(transcript.id)},
                    ),
                    Asset(
                        episode_id=episode.id,
                        asset_type=AssetType.video,
                        language=transcript.language,
                        source_entity_type="transcript_turn",
                        source_entity_id=str(turn.id),
                        storage_uri=f"object://dialecticore/video/{turn.id}.mp4",
                        mime_type="video/mp4",
                        duration_ms=1000,
                        width=1920,
                        height=1080,
                        fps=30,
                        checksum=f"sha256:video-{index}",
                        status="completed",
                        generation_metadata={
                            "transcript_version_id": str(transcript.id),
                            "visual_role": "video_primary",
                            "render_ready": True,
                            "shot_plan": {"camera_transition": "cut"},
                        },
                    ),
                ]
            )
        repository.save(episode)

        built = client.post(
            f"/api/v1/episodes/{episode_id}/timeline/build",
            json={"transcript_version_id": str(transcript.id), "user_id": "tester"},
        )
        assert built.status_code == 200
        built_body = built.json()
        timeline_asset = [
            asset for asset in built_body["assets"] if asset["asset_type"] == "timeline"
        ][-1]
        assert timeline_asset["status"] == "completed"
        timeline_qc = [
            result
            for result in built_body["quality_results"]
            if result["check_type"] == "timeline_integrity"
        ][-1]
        assert timeline_qc["status"] == "pass"
        assert timeline_qc["details"]["segment_count"] == len(playable_turns)

        fetched = client.get(
            f"/api/v1/episodes/{episode_id}/timeline",
            params={"transcript_version_id": str(transcript.id)},
        )
        assert fetched.status_code == 200
        timeline = fetched.json()["timeline"]
        timeline_entity = fetched.json()["timeline_entity"]
        assert timeline["schema_version"] == "episode_timeline.v2"
        assert len(timeline["segments"]) == len(playable_turns)
        assert [segment["source_discussion_turn_ids"] for segment in timeline["segments"]] == [
            [str(turn_id) for turn_id in turn.source_discussion_turn_ids] for turn in playable_turns
        ]
        assert timeline_entity["id"] == timeline["id"]
        assert timeline_entity["episode_id"] == episode_id
        assert timeline_entity["language"] == transcript.language
        assert timeline_entity["version"] == 1
        assert timeline_entity["status"] == "completed"
        assert timeline_entity["duration_ms"] == timeline["duration_ms"]
        assert timeline_entity["timeline_json"]["id"] == timeline["id"]

        timeline["segments"][0]["camera_transition"] = "dissolve"
        timeline["segments"][0]["end_ms"] = timeline["segments"][0]["start_ms"] + 1800
        edited = client.put(
            f"/api/v1/episodes/{episode_id}/timeline",
            json={
                "timeline": timeline,
                "user_id": "tester",
                "comment": "Adjust opening transition.",
            },
        )
        assert edited.status_code == 200
        edited_assets = [
            asset for asset in edited.json()["assets"] if asset["asset_type"] == "timeline"
        ]
        assert len(edited_assets) == 2
        assert edited_assets[0]["status"] == "replaced"
        assert edited_assets[-1]["generation_metadata"]["edit_version"] == 2
        assert (
            edited_assets[-1]["generation_metadata"]["timeline_json"]["segments"][0][
                "camera_transition"
            ]
            == "dissolve"
        )
        assert (
            edited_assets[-1]["generation_metadata"]["timeline_json"]["segments"][0]["duration_ms"]
            == 1800
        )

        render_presets = client.get("/api/v1/render-presets")
        assert render_presets.status_code == 200
        preset_ids = {preset["id"] for preset in render_presets.json()}
        assert {
            "youtube-1080p",
            "youtube-1440p",
            "youtube-4k",
            "preview-low-bitrate",
            "audio-only",
            "short-promotional-clip",
        }.issubset(preset_ids)

        if shutil.which("ffmpeg") is not None:
            queued_render = client.post(
                f"/api/v1/episodes/{episode_id}/renders",
                json={
                    "timeline_asset_id": edited_assets[-1]["id"],
                    "preset_id": "preview-low-bitrate",
                    "user_id": "tester",
                },
            )
            assert queued_render.status_code == 202
            render_assets = [
                asset for asset in queued_render.json()["assets"] if asset["asset_type"] == "render"
            ]
            assert len(render_assets) == 1
            assert render_assets[0]["status"] == "submitted"
            assert (
                render_assets[0]["generation_metadata"]["render_request"]["preset_id"]
                == "preview-low-bitrate"
            )
            listed_renders = client.get(f"/api/v1/episodes/{episode_id}/renders")
            assert listed_renders.status_code == 200
            assert len(listed_renders.json()) == 1

        episode = repository.get(episode_id)
        stored_render = render_service.object_store.put_bytes(
            key=f"renders/{episode_id}/api-final-test.mp4",
            payload=b"fake mp4 api package payload",
            content_type="video/mp4",
        )
        subtitle_asset = next(
            asset for asset in episode.assets if asset.asset_type == AssetType.subtitle
        )
        subtitle_asset.generation_metadata["format"] = "vtt"
        subtitle_asset.generation_metadata["subtitle_text"] = (
            "WEBVTT\n\n00:00.000 --> 00:02.000\nOpening\n"
        )
        final_render_asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.render,
            language=transcript.language,
            source_entity_type="timeline_asset",
            source_entity_id=edited_assets[-1]["id"],
            storage_uri=stored_render.uri,
            mime_type=stored_render.content_type,
            duration_ms=24_000,
            width=1920,
            height=1080,
            fps=30,
            checksum=stored_render.checksum,
            status="completed",
            generation_metadata={
                "render_id": "api-final-test",
                "render_type": "final",
                "preset_id": "youtube-1080p",
                "timeline_asset_id": edited_assets[-1]["id"],
                "object_storage_path": str(stored_render.path),
                "render_manifest": {
                    "id": "api-final-test",
                    "schema_version": "render_manifest.v1",
                    "source_assets": [
                        {
                            "asset_id": edited_assets[-1]["id"],
                            "asset_type": AssetType.timeline.value,
                        },
                        {
                            "asset_id": str(subtitle_asset.id),
                            "asset_type": AssetType.subtitle.value,
                        },
                    ],
                },
            },
        )
        stored_thumbnail = render_service.object_store.put_bytes(
            key=f"thumbnails/{episode_id}/api-thumbnail-test.jpg",
            payload=b"fake jpg api package payload",
            content_type="image/jpeg",
        )
        thumbnail_asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.thumbnail,
            language=transcript.language,
            source_entity_type="render_asset",
            source_entity_id=str(final_render_asset.id),
            storage_uri=stored_thumbnail.uri,
            mime_type=stored_thumbnail.content_type,
            width=1920,
            height=1080,
            checksum=stored_thumbnail.checksum,
            status="completed",
            generation_metadata={
                "thumbnail_id": "api-thumbnail-test",
                "render_asset_id": str(final_render_asset.id),
                "object_storage_path": str(stored_thumbnail.path),
            },
        )
        episode.assets.extend([final_render_asset, thumbnail_asset])
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="render_asset",
                target_id=str(final_render_asset.id),
                check_type="render_final_integrity",
                severity=QualitySeverity.pass_,
                status="pass",
                score=1.0,
                details={"failure_count": 0, "warning_count": 0},
            )
        )
        final_render_approval = Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(final_render_asset.id),
        )
        episode.approvals.append(final_render_approval)
        repository.save(episode)

        blocked_export = client.post(
            f"/api/v1/episodes/{episode_id}/youtube-package/export",
            json={"user_id": "tester"},
        )
        assert blocked_export.status_code == 422
        assert "final render must be approved" in blocked_export.json()["detail"]

        decided_render = client.post(
            f"/api/v1/episodes/{episode_id}/approvals/{final_render_approval.id}/decision",
            json={
                "decision": "approved",
                "comment": "Final render is ready for delivery.",
                "user_id": "tester",
            },
        )
        assert decided_render.status_code == 200
        approved_render_asset = next(
            asset
            for asset in decided_render.json()["assets"]
            if asset["id"] == str(final_render_asset.id)
        )
        assert approved_render_asset["generation_metadata"]["approval_status"] == "approved"

        exported = client.post(
            f"/api/v1/episodes/{episode_id}/youtube-package/export",
            json={"user_id": "tester"},
        )
        assert exported.status_code == 200
        package_assets = [
            asset for asset in exported.json()["assets"] if asset["asset_type"] == "export_package"
        ]
        assert package_assets[-1]["status"] == "completed"
        package_qc = [
            result
            for result in exported.json()["quality_results"]
            if result["check_type"] == "youtube_package_integrity"
        ][-1]
        assert package_qc["status"] == "pass"
        assert package_qc["details"]["included_file_count"] == 4

        manifested = client.post(
            f"/api/v1/episodes/{episode_id}/production-manifest",
            json={"user_id": "tester"},
        )
        assert manifested.status_code == 200
        manifest_assets = [
            asset
            for asset in manifested.json()["assets"]
            if asset["asset_type"] == "production_manifest"
        ]
        assert manifest_assets[-1]["status"] == "completed"
        assert manifest_assets[-1]["source_entity_type"] == "export_package"
        assert manifest_assets[-1]["source_entity_id"] == package_assets[-1]["id"]
        production_manifest = manifest_assets[-1]["generation_metadata"]["production_manifest"]
        assert production_manifest["schema_version"] == "production_manifest.v1"
        assert production_manifest["render"]["asset_id"] == str(final_render_asset.id)
        assert production_manifest["render"]["manifest"]["schema_version"] == ("render_manifest.v1")
        assert production_manifest["delivery_package"]["asset_id"] == package_assets[-1]["id"]
        manifest_transcript = next(
            item for item in production_manifest["transcripts"] if item["id"] == str(transcript.id)
        )
        assert manifest_transcript["localization_metadata"] == transcript.localization_metadata
        assert manifest_transcript["turn_lineage"] == [
            {
                "transcript_turn_id": str(turn.id),
                "speaker_participant_id": turn.speaker_participant_id,
                "source_discussion_turn_ids": [
                    str(turn_id) for turn_id in turn.source_discussion_turn_ids
                ],
                "claim_count": len(turn.claims),
                "status": turn.status,
            }
            for turn in transcript.turns
        ]
        assert production_manifest["timeline"]["segment_count"] == len(playable_turns)
        assert "chapters" in production_manifest["timeline"]
        assert len(production_manifest["timeline_segments"]) == len(playable_turns)
        assert [
            segment["source_discussion_turn_ids"]
            for segment in production_manifest["timeline_segments"]
        ] == [
            [str(turn_id) for turn_id in turn.source_discussion_turn_ids] for turn in playable_turns
        ]

        duplicate_manifest = client.post(
            f"/api/v1/episodes/{episode_id}/production-manifest",
            json={"user_id": "tester"},
        )
        assert duplicate_manifest.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_model_endpoint_crud_api() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    try:
        listed = client.get("/api/v1/model-endpoints")
        assert listed.status_code == 200
        assert [endpoint["id"] for endpoint in listed.json()] == ["mock"]

        created = client.post(
            "/api/v1/model-endpoints",
            json={
                "id": "ollama-local",
                "name": "Ollama Local",
                "provider_type": "ollama",
                "base_url": "http://ollama:11434",
                "credential_reference": "env:OLLAMA_TOKEN",
                "health_status": "unknown",
            },
        )
        assert created.status_code == 200
        assert created.json()["id"] == "ollama-local"
        assert created.json()["credential_reference"] == "env:OLLAMA_TOKEN"

        updated = client.put(
            "/api/v1/model-endpoints/ollama-local",
            json={
                "id": "ignored-by-route",
                "name": "Ollama Local Updated",
                "provider_type": "ollama",
                "base_url": "http://ollama:11434",
                "enabled": False,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["id"] == "ollama-local"
        assert updated.json()["enabled"] is False

        deleted = client.delete("/api/v1/model-endpoints/ollama-local")
        assert deleted.status_code == 204
        assert client.get("/api/v1/model-endpoints/ollama-local").status_code == 404

        audit = client.get("/api/v1/audit-events")
        event_types = [event["event_type"] for event in audit.json()]
        assert event_types.count("model_endpoint.upserted") == 2
        assert "model_endpoint.deleted" in event_types
    finally:
        app.dependency_overrides.clear()


def test_endpoint_configuration_rejects_base_url_userinfo() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    cases = [
        (
            "/api/v1/model-endpoints",
            "model-userinfo",
            {
                "id": "model-userinfo",
                "name": "Model Userinfo",
                "provider_type": "openai_compatible",
                "base_url": "https://model-user:leaked-model-password@models.example.test/v1",
            },
        ),
        (
            "/api/v1/voicebox-endpoints",
            "voicebox-userinfo",
            {
                "id": "voicebox-userinfo",
                "name": "Voicebox Userinfo",
                "adapter_type": "voicebox_http",
                "base_url": "https://voice-user:leaked-voice-password@voicebox.example.test",
            },
        ),
        (
            "/api/v1/comfyui-endpoints",
            "comfyui-userinfo",
            {
                "id": "comfyui-userinfo",
                "name": "ComfyUI Userinfo",
                "adapter_type": "comfyui_http",
                "base_url": "https://comfy-user:leaked-comfy-password@comfyui.example.test",
            },
        ),
        (
            "/api/v1/publisher-targets",
            "publisher-userinfo",
            {
                "id": "publisher-userinfo",
                "name": "Publisher Userinfo",
                "platform": "generic",
                "adapter_type": "http",
                "base_url": (
                    "https://publisher-user:leaked-publisher-password@publisher.example.test"
                ),
            },
        ),
    ]

    try:
        for path, item_id, payload in cases:
            created = client.post(path, json=payload)
            assert created.status_code == 422
            assert "base_url must not include username or password" in created.text
            assert f"leaked-{item_id.split('-', 1)[0]}-password" not in created.text
            assert client.get(f"{path}/{item_id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_endpoint_configuration_rejects_raw_credential_reference_values() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    cases = [
        (
            "/api/v1/model-endpoints",
            "model-raw-credential",
            {
                "id": "model-raw-credential",
                "name": "Model Raw Credential",
                "provider_type": "openai_compatible",
                "base_url": "https://models.example.test/v1",
                "credential_reference": "leaked-model-token",
            },
            "leaked-model-token",
        ),
        (
            "/api/v1/voicebox-endpoints",
            "voicebox-raw-credential",
            {
                "id": "voicebox-raw-credential",
                "name": "Voicebox Raw Credential",
                "adapter_type": "voicebox_http",
                "base_url": "https://voicebox.example.test",
                "credential_reference": "leaked-voicebox-token",
            },
            "leaked-voicebox-token",
        ),
        (
            "/api/v1/comfyui-endpoints",
            "comfyui-raw-credential",
            {
                "id": "comfyui-raw-credential",
                "name": "ComfyUI Raw Credential",
                "adapter_type": "comfyui_http",
                "base_url": "https://comfyui.example.test",
                "credential_reference": "leaked-comfyui-token",
            },
            "leaked-comfyui-token",
        ),
        (
            "/api/v1/publisher-targets",
            "publisher-raw-credential",
            {
                "id": "publisher-raw-credential",
                "name": "Publisher Raw Credential",
                "platform": "generic",
                "adapter_type": "http",
                "base_url": "https://publisher.example.test",
                "credential_reference": "leaked-publisher-token",
            },
            "leaked-publisher-token",
        ),
    ]

    try:
        for path, item_id, payload, leaked_value in cases:
            created = client.post(path, json=payload)
            assert created.status_code == 422
            assert "credential_reference must use scheme:target syntax" in created.text
            assert leaked_value not in created.text
            assert client.get(f"{path}/{item_id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_endpoint_configuration_redacts_secret_shaped_capabilities() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    try:
        model_endpoint = client.post(
            "/api/v1/model-endpoints",
            json={
                "id": "model-capability-secret",
                "name": "Model Capability Secret",
                "provider_type": "openai_compatible",
                "base_url": "https://models.example.test/v1",
                "credential_reference": "env:MODEL_TOKEN",
                "capabilities": {
                    "health_path": "/models",
                    "apiKey": "leaked-model-capability-key",
                    "nested": {"clientSecret": "leaked-model-capability-secret"},
                },
            },
        )
        assert model_endpoint.status_code == 200
        model_capabilities = model_endpoint.json()["capabilities"]
        assert model_capabilities["health_path"] == "/models"
        assert model_capabilities["apiKey"] == "[redacted]"
        assert model_capabilities["nested"]["clientSecret"] == "[redacted]"

        publisher_target = client.post(
            "/api/v1/publisher-targets",
            json={
                "id": "publisher-capability-secret",
                "name": "Publisher Capability Secret",
                "platform": "youtube",
                "adapter_type": "youtube_resumable",
                "base_url": "https://youtube.example.test",
                "capabilities": {
                    "api_key": "leaked-publisher-capability-key",
                    "oauth_refresh_token_reference": "env:YOUTUBE_REFRESH_TOKEN",
                    "oauth_client_id_reference": "env:YOUTUBE_CLIENT_ID",
                    "oauth_client_secret_reference": "env:YOUTUBE_CLIENT_SECRET",
                },
            },
        )
        assert publisher_target.status_code == 200
        publisher_capabilities = publisher_target.json()["capabilities"]
        assert publisher_capabilities["api_key"] == "[redacted]"
        assert (
            publisher_capabilities["oauth_refresh_token_reference"] == "env:YOUTUBE_REFRESH_TOKEN"
        )
        assert publisher_capabilities["oauth_client_id_reference"] == "env:YOUTUBE_CLIENT_ID"
        assert (
            publisher_capabilities["oauth_client_secret_reference"] == "env:YOUTUBE_CLIENT_SECRET"
        )

        body_json = json.dumps(
            {
                "model": model_endpoint.json(),
                "publisher": publisher_target.json(),
            },
            sort_keys=True,
        )
        assert "leaked-model-capability-key" not in body_json
        assert "leaked-model-capability-secret" not in body_json
        assert "leaked-publisher-capability-key" not in body_json
    finally:
        app.dependency_overrides.clear()


def test_model_endpoint_health_api_persists_discovered_capabilities(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_MODEL_TOKEN", "remote-token")
    repository = EpisodeRepository()
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "data": [{"id": "mistral-large-test"}],
                "capabilities": {"region": "local-test"},
            },
        )

    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_model_endpoint_service] = lambda: ModelEndpointService(
        transport=httpx.MockTransport(handler)
    )
    client = TestClient(app)

    try:
        created = client.post(
            "/api/v1/model-endpoints",
            json={
                "id": "mistral-remote",
                "name": "Mistral Remote",
                "provider_type": "mistral_compatible",
                "base_url": "https://models.example.test/v1",
                "credential_reference": "env:REMOTE_MODEL_TOKEN",
            },
        )
        assert created.status_code == 200

        checked = client.post("/api/v1/model-endpoints/mistral-remote/health")
        assert checked.status_code == 200
        payload = checked.json()
        assert seen["path"] == "/v1/models"
        assert seen["authorization"] == "Bearer remote-token"
        assert payload["health_status"] == "healthy"
        assert payload["credential_reference"] == "env:REMOTE_MODEL_TOKEN"
        assert payload["capabilities"]["chat_completions"] is True
        assert payload["capabilities"]["json_object_response"] is True
        assert payload["capabilities"]["model_ids"] == ["mistral-large-test"]
        assert payload["capabilities"]["region"] == "local-test"

        reloaded = client.get("/api/v1/model-endpoints/mistral-remote")
        assert reloaded.status_code == 200
        assert reloaded.json()["health_status"] == "healthy"
        assert reloaded.json()["capabilities"]["model_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_participant_profile_crud_api() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    try:
        listed = client.get("/api/v1/participant-profiles")
        assert listed.status_code == 200
        assert {profile["id"] for profile in listed.json()} >= {
            "host",
            "optimist",
            "skeptic",
            "practitioner",
        }

        created = client.post(
            "/api/v1/participant-profiles",
            json={
                "id": "researcher",
                "name": "researcher",
                "display_name": "Researcher",
                "participant_type": "fact_checker",
                "model_endpoint_id": "mock",
                "model_id": "mock-researcher-v1",
                "system_prompt_template": "fact_checker_v1",
                "perspective": "check claims against evidence",
                "expertise": "source evaluation",
                "speaking_style": "careful and concise",
            },
        )
        assert created.status_code == 200
        assert created.json()["participant_type"] == "fact_checker"

        updated = client.put(
            "/api/v1/participant-profiles/researcher",
            json={
                "id": "ignored-by-route",
                "name": "researcher",
                "display_name": "Researcher Updated",
                "participant_type": "fact_checker",
                "model_endpoint_id": "mock",
                "model_id": "mock-researcher-v2",
                "system_prompt_template": "fact_checker_v1",
                "perspective": "check claims against evidence",
                "expertise": "source evaluation",
                "speaking_style": "careful and concise",
                "enabled": False,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["id"] == "researcher"
        assert updated.json()["enabled"] is False

        deleted = client.delete("/api/v1/participant-profiles/researcher")
        assert deleted.status_code == 204
        assert client.get("/api/v1/participant-profiles/researcher").status_code == 404

        audit = client.get("/api/v1/audit-events")
        event_types = [event["event_type"] for event in audit.json()]
        assert event_types.count("participant_profile.upserted") == 2
        assert "participant_profile.deleted" in event_types
    finally:
        app.dependency_overrides.clear()


def test_voicebox_endpoint_and_voice_profile_crud_api(tmp_path: Path, monkeypatch) -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    try:
        listed_endpoints = client.get("/api/v1/voicebox-endpoints")
        assert listed_endpoints.status_code == 200
        assert [endpoint["id"] for endpoint in listed_endpoints.json()] == ["mock-voicebox"]

        checked = client.post("/api/v1/voicebox-endpoints/mock-voicebox/health")
        assert checked.status_code == 200
        assert checked.json()["health_status"] == "healthy"
        assert checked.json()["capabilities"]["tts"] is True

        created_endpoint = client.post(
            "/api/v1/voicebox-endpoints",
            json={
                "id": "voicebox-remote",
                "name": "Voicebox Remote",
                "adapter_type": "voicebox_http",
                "base_url": "https://voicebox.example.test",
                "credential_reference": "env:VOICEBOX_TOKEN",
                "health_status": "unknown",
            },
        )
        assert created_endpoint.status_code == 200
        assert created_endpoint.json()["id"] == "voicebox-remote"
        assert created_endpoint.json()["credential_reference"] == "env:VOICEBOX_TOKEN"

        cert_bytes = b"public b1 ca"
        expected_sha256 = "b62fe909c8bd114a911356f26c0dcf3d0509b4360ae379bd0e9cf2c826f491d6"
        bootstrap_requests: list[httpx.Request] = []

        def b1_handler(request: httpx.Request) -> httpx.Response:
            bootstrap_requests.append(request)
            assert request.url.path == "/.well-known/b1-ai-hub/caddy-root.crt"
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                content=cert_bytes,
                headers={"x-b1-sha256": expected_sha256},
            )

        voicebox_service = VoiceboxService(
            Settings(runtime_state_path=str(tmp_path / "runtime-state")),
            transport=httpx.MockTransport(b1_handler),
        )
        app.dependency_overrides[get_voicebox_service] = lambda: voicebox_service
        monkeypatch.setenv("B1_API_KEY", "b1-token")
        created_b1_endpoint = client.post(
            "/api/v1/voicebox-endpoints",
            json={
                "id": "b1-voicebox",
                "name": "B1 Voicebox",
                "adapter_type": "b1_voice_stream",
                "base_url": "https://voice.ai.b1.germering",
                "credential_reference": "env:B1_API_KEY",
                "capabilities": {
                    "ca_cert_bootstrap_url": (
                        "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
                    ),
                    "ca_cert_sha256": expected_sha256,
                    "tls_ca_cert_path": "./b1-ai-hub-caddy-root.crt",
                },
            },
        )
        assert created_b1_endpoint.status_code == 200
        bootstrapped_b1_endpoint = client.post(
            "/api/v1/voicebox-endpoints/b1-voicebox/ca-certificate/bootstrap"
        )
        assert bootstrapped_b1_endpoint.status_code == 200
        b1_payload = bootstrapped_b1_endpoint.json()
        assert b1_payload["health_status"] == "healthy"
        assert b1_payload["capabilities"]["ca_cert_bootstrap"]["stored"] is True
        assert b1_payload["capabilities"]["ca_cert_bootstrap"]["sha256_matches"] is True
        stored_cert_path = Path(b1_payload["capabilities"]["tls_ca_cert_path"])
        assert stored_cert_path == tmp_path / "runtime-state" / "certificates" / (
            "b1-ai-hub-caddy-root.crt"
        )
        assert stored_cert_path.read_bytes() == cert_bytes
        assert len(bootstrap_requests) == 2

        updated_endpoint = client.put(
            "/api/v1/voicebox-endpoints/voicebox-remote",
            json={
                "id": "ignored-by-route",
                "name": "Voicebox Remote Updated",
                "adapter_type": "voicebox_http",
                "base_url": "https://voicebox.example.test",
                "enabled": False,
            },
        )
        assert updated_endpoint.status_code == 200
        assert updated_endpoint.json()["id"] == "voicebox-remote"
        assert updated_endpoint.json()["enabled"] is False

        created_profile = client.post(
            "/api/v1/voice-profiles",
            json={
                "id": "narrator-de",
                "name": "Narrator German",
                "voicebox_endpoint_id": "voicebox-remote",
                "voice_id": "voice-de-1",
                "language": "de",
                "speaker_label": "Narrator",
                "model_id": "voicebox-v1",
                "prosody": {"style": "clear"},
            },
        )
        assert created_profile.status_code == 200
        assert created_profile.json()["language"] == "de"

        updated_profile = client.put(
            "/api/v1/voice-profiles/narrator-de",
            json={
                "id": "ignored-by-route",
                "name": "Narrator German Updated",
                "voicebox_endpoint_id": "voicebox-remote",
                "voice_id": "voice-de-2",
                "language": "de",
                "enabled": False,
            },
        )
        assert updated_profile.status_code == 200
        assert updated_profile.json()["id"] == "narrator-de"
        assert updated_profile.json()["enabled"] is False

        blocked_endpoint_delete = client.delete("/api/v1/voicebox-endpoints/voicebox-remote")
        assert blocked_endpoint_delete.status_code == 422

        deleted_profile = client.delete("/api/v1/voice-profiles/narrator-de")
        assert deleted_profile.status_code == 204
        assert client.get("/api/v1/voice-profiles/narrator-de").status_code == 404

        deleted_endpoint = client.delete("/api/v1/voicebox-endpoints/voicebox-remote")
        assert deleted_endpoint.status_code == 204
        assert client.get("/api/v1/voicebox-endpoints/voicebox-remote").status_code == 404
        deleted_b1_endpoint = client.delete("/api/v1/voicebox-endpoints/b1-voicebox")
        assert deleted_b1_endpoint.status_code == 204
        assert client.get("/api/v1/voicebox-endpoints/b1-voicebox").status_code == 404

        audit = client.get("/api/v1/audit-events")
        audit_events = audit.json()
        event_types = [event["event_type"] for event in audit_events]
        assert event_types.count("voicebox_endpoint.upserted") >= 5
        assert event_types.count("voice_profile.upserted") == 2
        bootstrap_event = next(
            event
            for event in audit_events
            if event["event_type"] == "voicebox_endpoint.ca_certificate_bootstrapped"
        )
        assert bootstrap_event["details"] == {
            "endpoint_id": "b1-voicebox",
            "health_status": "healthy",
            "ca_cert_stored": True,
            "ca_cert_sha256_matches": True,
            "tls_ca_cert_path_configured": True,
        }
        assert "voice.ai.b1.germering" not in json.dumps(bootstrap_event)
        assert expected_sha256 not in json.dumps(bootstrap_event)
        assert "b1-token" not in json.dumps(bootstrap_event)
        assert "voice_profile.deleted" in event_types
        assert "voicebox_endpoint.deleted" in event_types
    finally:
        app.dependency_overrides.clear()


def test_b1_german_voice_preset_provisioning_is_missing_only_and_audited() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)

    try:
        created_b1_endpoint = client.post(
            "/api/v1/voicebox-endpoints",
            json={
                "id": "b1-voicebox",
                "name": "B1 Voicebox",
                "adapter_type": "b1_voice_stream",
                "base_url": "https://voice.ai.b1.germering",
                "credential_reference": "env:B1_API_KEY",
            },
        )
        assert created_b1_endpoint.status_code == 200

        provisioned = client.post(
            "/api/v1/voicebox-endpoints/b1-voicebox/b1-german-voice-presets/provision",
            json={"assign_participants": True},
        )
        assert provisioned.status_code == 200
        payload = provisioned.json()
        assert payload["created_voice_profile_ids"] == [
            "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
            "67a00466-17ba-4f26-812e-60c13119be9e",
            "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
            "1418bd8c-1c39-4317-91a0-92d62e5fd9c0",
            "7476947f-5836-480b-9a95-67bf66575c2a",
            "1865b646-41ca-4140-ba9d-1a40d9fe623a",
        ]
        assert payload["existing_voice_profile_ids"] == []
        assert payload["assigned_participants"] == {
            "chatgpt": "1865b646-41ca-4140-ba9d-1a40d9fe623a",
            "claude": "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
            "deepseek": "67a00466-17ba-4f26-812e-60c13119be9e",
            "gemini": "1418bd8c-1c39-4317-91a0-92d62e5fd9c0",
            "grok": "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
            "mistral": "7476947f-5836-480b-9a95-67bf66575c2a",
        }
        assert payload["reassign_participants"] is False
        assert payload["reassigned_participant_ids"] == []
        assert set(payload["preserved_assigned_participant_ids"]) == {
            "host",
            "optimist",
            "practitioner",
            "skeptic",
        }

        voice_profiles = {
            profile["id"]: profile for profile in client.get("/api/v1/voice-profiles").json()
        }
        assert voice_profiles["0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5"] == {
            "id": "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
            "name": "A_DE_Claude",
            "voicebox_endpoint_id": "b1-voicebox",
            "voice_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
            "language": "de",
            "speaker_label": "A_DE_Claude",
            "model_id": "chatterbox",
            "prosody": {
                "engine": "chatterbox",
                "normalize": False,
                "effects_chain": [],
            },
            "rate": 1.0,
            "pitch": 0.0,
            "pronunciation_dictionary": {},
            "enabled": True,
        }
        assert (
            client.get("/api/v1/participant-profiles/claude").json()["voice_profile_id"]
            == "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5"
        )
        assert (
            client.get("/api/v1/participant-profiles/host").json()["voice_profile_id"]
            == "voice-host"
        )

        reprovisioned = client.post(
            "/api/v1/voicebox-endpoints/b1-voicebox/b1-german-voice-presets/provision",
            json={"assign_participants": True},
        )
        assert reprovisioned.status_code == 200
        assert reprovisioned.json()["created_voice_profile_ids"] == []
        assert reprovisioned.json()["assigned_participants"] == {}
        assert reprovisioned.json()["reassign_participants"] is False
        assert reprovisioned.json()["reassigned_participant_ids"] == []

        claude_profile = client.get("/api/v1/participant-profiles/claude").json()
        claude_profile["voice_profile_id"] = "voice-host"
        updated_claude = client.put(
            "/api/v1/participant-profiles/claude",
            json=claude_profile,
        )
        assert updated_claude.status_code == 200
        assert updated_claude.json()["voice_profile_id"] == "voice-host"

        reassigned = client.post(
            "/api/v1/voicebox-endpoints/b1-voicebox/b1-german-voice-presets/provision",
            json={"assign_participants": True, "reassign_participants": True},
        )
        assert reassigned.status_code == 200
        reassigned_payload = reassigned.json()
        assert reassigned_payload["created_voice_profile_ids"] == []
        assert reassigned_payload["assigned_participants"] == {
            "claude": B1_CHARACTER_VOICE_ASSIGNMENTS["claude"]
        }
        assert reassigned_payload["reassign_participants"] is True
        assert reassigned_payload["reassigned_participant_ids"] == ["claude"]
        assert (
            client.get("/api/v1/participant-profiles/claude").json()["voice_profile_id"]
            == B1_CHARACTER_VOICE_ASSIGNMENTS["claude"]
        )

        audit = client.get("/api/v1/audit-events?limit=20")
        audit_events = audit.json()
        provisioning_events = [
            event
            for event in audit_events
            if event["event_type"] == "voice_profile.b1_presets_provisioned"
        ]
        assert len(provisioning_events) == 3
        assert provisioning_events[0]["details"]["reassign_participants"] is True
        assert provisioning_events[0]["details"]["reassigned_participant_ids"] == ["claude"]
        assert any(
            event["details"]["created_voice_profile_count"] == 6 for event in provisioning_events
        )
        assert "bd4e9bf1-482b-4900-97c1-48275d1ba28c" not in json.dumps(provisioning_events)

        wrong_adapter = client.post(
            "/api/v1/voicebox-endpoints/mock-voicebox/b1-german-voice-presets/provision",
            json={"assign_participants": True},
        )
        assert wrong_adapter.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_openrouter_model_preset_provisioning_creates_endpoint_and_assigns_characters() -> None:
    repository = EpisodeRepository()
    app.dependency_overrides[get_repository] = lambda: repository
    client = TestClient(app)
    assignments = {
        "chatgpt": ("ChatGPT", "panelist", "panelist_v1", "openai/gpt-4.1-mini"),
        "claude": ("Claude", "host", "moderator_v1", "anthropic/claude-sonnet-5"),
        "deepseek": ("DeepSeek", "panelist", "panelist_v1", "deepseek/deepseek-v3.2"),
        "gemini": ("Gemini", "panelist", "panelist_v1", "google/gemini-3.6-flash"),
        "grok": ("Grok", "panelist", "panelist_v1", "x-ai/grok-4.3"),
        "mistral": ("Mistral", "panelist", "panelist_v1", "mistralai/mistral-large-2512"),
    }

    try:
        assert client.get("/api/v1/model-endpoints/openrouter").status_code == 404

        for participant_id, (display_name, participant_type, template_id, _) in assignments.items():
            created = client.post(
                "/api/v1/participant-profiles",
                json={
                    "id": participant_id,
                    "name": display_name,
                    "display_name": display_name,
                    "participant_type": participant_type,
                    "model_endpoint_id": "mock",
                    "model_id": "mock-panelist-v1",
                    "system_prompt_template": template_id,
                    "perspective": f"{display_name} perspective",
                    "expertise": f"{display_name} expertise",
                    "speaking_style": "concise",
                    "sampling_settings": {
                        "temperature": 0.6,
                        "top_p": 0.95,
                        "max_tokens": 500,
                    },
                    "tool_policy_id": "no_tools",
                    "voice_profile_id": None,
                    "visual_profile_id": None,
                    "enabled": True,
                },
            )
            assert created.status_code == 200

        provisioned = client.post(
            "/api/v1/model-endpoints/openrouter/presets/provision",
            json={"assign_participants": True},
        )
        assert provisioned.status_code == 200
        payload = provisioned.json()
        assert payload["model_endpoint_id"] == "openrouter"
        assert payload["created_endpoint"] is True
        assert payload["updated_endpoint"] is False
        assert payload["missing_participant_ids"] == []
        assert payload["assigned_participants"] == {
            participant_id: model_id for participant_id, (_, _, _, model_id) in assignments.items()
        }

        endpoint = client.get("/api/v1/model-endpoints/openrouter").json()
        assert endpoint["provider_type"] == "openai_compatible"
        assert endpoint["base_url"] == "https://openrouter.ai/api/v1"
        assert endpoint["credential_reference"] == "env:OPENROUTER_API_KEY"
        assert endpoint["capabilities"]["model_presets"] == OPENROUTER_MODEL_PRESETS
        assert set(OPENROUTER_CHARACTER_MODEL_ASSIGNMENTS.values()) <= set(
            endpoint["capabilities"]["model_presets"]
        )

        for participant_id, (_, _, _, model_id) in assignments.items():
            profile = client.get(f"/api/v1/participant-profiles/{participant_id}").json()
            assert profile["model_endpoint_id"] == "openrouter"
            assert profile["model_id"] == model_id

        reprovisioned = client.post(
            "/api/v1/model-endpoints/openrouter/presets/provision",
            json={"assign_participants": False},
        )
        assert reprovisioned.status_code == 200
        assert reprovisioned.json()["created_endpoint"] is False
        assert reprovisioned.json()["updated_endpoint"] is True
        assert reprovisioned.json()["assigned_participants"] == {}

        audit = client.get("/api/v1/audit-events?limit=20")
        provisioning_events = [
            event
            for event in audit.json()
            if event["event_type"] == "model_endpoint.openrouter_presets_provisioned"
        ]
        assert len(provisioning_events) == 2
        assert provisioning_events[-1]["details"]["preset_model_count"] == 6
        assert provisioning_events[-1]["details"]["credential_reference_scheme"] == "env"
        assert "OPENROUTER_API_KEY" not in json.dumps(provisioning_events)
        assert "sk-or" not in json.dumps(provisioning_events)
    finally:
        app.dependency_overrides.clear()


def test_comfyui_endpoint_workflow_and_visual_profile_crud_api(tmp_path: Path) -> None:
    repository = EpisodeRepository()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_comfyui_service] = lambda: ComfyUiService(settings)
    client = TestClient(app)

    try:
        listed_endpoints = client.get("/api/v1/comfyui-endpoints")
        assert listed_endpoints.status_code == 200
        assert [endpoint["id"] for endpoint in listed_endpoints.json()] == ["mock-comfyui"]

        checked = client.post("/api/v1/comfyui-endpoints/mock-comfyui/health")
        assert checked.status_code == 200
        assert checked.json()["health_status"] == "healthy"
        assert checked.json()["capabilities"]["video"] is True

        listed_workflows = client.get("/api/v1/comfyui-workflows")
        assert listed_workflows.status_code == 200
        default_talking_head = next(
            workflow
            for workflow in listed_workflows.json()
            if workflow["id"] == "workflow-talking-head-v1"
        )
        assert default_talking_head["api_workflow"]["10"]["class_type"] == "CreateVideo"
        assert (
            default_talking_head["prompt_template"]["node_input_bindings"]["6.inputs.text"]
            == "positive_prompt"
        )

        created_endpoint = client.post(
            "/api/v1/comfyui-endpoints",
            json={
                "id": "comfyui-remote",
                "name": "ComfyUI Remote",
                "adapter_type": "comfyui_http",
                "base_url": "https://comfyui.example.test",
                "credential_reference": "env:COMFYUI_TOKEN",
                "health_status": "unknown",
            },
        )
        assert created_endpoint.status_code == 200
        assert created_endpoint.json()["id"] == "comfyui-remote"

        created_workflow = client.post(
            "/api/v1/comfyui-workflows",
            json={
                "id": "remote-talking-head",
                "name": "Remote Talking Head",
                "workflow_type": "talking_head",
                "comfyui_endpoint_id": "comfyui-remote",
                "output_asset_type": "video",
                "default_parameters": {"width": 1280, "height": 720, "fps": 24},
            },
        )
        assert created_workflow.status_code == 200
        assert created_workflow.json()["comfyui_endpoint_id"] == "comfyui-remote"

        created_profile = client.post(
            "/api/v1/visual-profiles",
            json={
                "id": "remote-visual",
                "name": "Remote Visual",
                "character_name": "Remote Character",
                "primary_workflow_id": "remote-talking-head",
                "style_prompt": "studio lit participant",
            },
        )
        assert created_profile.status_code == 200
        assert created_profile.json()["primary_workflow_id"] == "remote-talking-head"

        png_payload = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+"
            "M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        uploaded_reference = client.post(
            "/api/v1/visual-profiles/remote-visual/reference-image",
            json={
                "filename": "speaker.png",
                "content_type": "image/png",
                "image_base64": base64.b64encode(png_payload).decode(),
                "reference_type": "full_body",
                "user_id": "tester",
            },
        )
        assert uploaded_reference.status_code == 200
        assert uploaded_reference.json()["reference_image_uri"].startswith(
            "object://dialecticore/visual-profiles/remote-visual/reference-images/"
        )
        assert uploaded_reference.json()["reference_images"][0]["reference_type"] == "full_body"
        assert uploaded_reference.json()["reference_images"][0]["uri"].startswith(
            "object://dialecticore/visual-profiles/remote-visual/reference-images/full_body/"
        )
        downloaded_reference = client.get(
            "/api/v1/visual-profiles/remote-visual/reference-images/full_body/download"
        )
        assert downloaded_reference.status_code == 200
        assert downloaded_reference.headers["content-type"] == "image/png"
        assert downloaded_reference.content == png_payload
        assert "remote-visual-full_body.png" in downloaded_reference.headers["content-disposition"]

        replacement_payload = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAD0lEQVR42mNk+M9Qz0AE"
            "YAEQABp9AkoAAAAASUVORK5CYII="
        )
        replaced_reference = client.post(
            "/api/v1/visual-profiles/remote-visual/reference-image",
            json={
                "filename": "speaker-replacement.png",
                "content_type": "image/png",
                "image_base64": base64.b64encode(replacement_payload).decode(),
                "reference_type": "full_body",
                "user_id": "tester",
            },
        )
        assert replaced_reference.status_code == 200
        replaced_full_body_references = [
            reference
            for reference in replaced_reference.json()["reference_images"]
            if reference["reference_type"] == "full_body"
        ]
        assert len(replaced_full_body_references) == 1
        assert replaced_full_body_references[0]["filename"] == "speaker-replacement.png"
        assert (
            replaced_full_body_references[0]["uri"]
            != uploaded_reference.json()["reference_images"][0]["uri"]
        )
        downloaded_replacement = client.get(
            "/api/v1/visual-profiles/remote-visual/reference-images/full_body/download"
        )
        assert downloaded_replacement.status_code == 200
        assert downloaded_replacement.content == replacement_payload
        missing_reference = client.get(
            "/api/v1/visual-profiles/remote-visual/reference-images/portrait/download"
        )
        assert missing_reference.status_code == 404
        removed_reference = client.delete(
            "/api/v1/visual-profiles/remote-visual/reference-images/full_body"
        )
        assert removed_reference.status_code == 200
        assert removed_reference.json()["reference_images"] == []
        removed_reference_download = client.get(
            "/api/v1/visual-profiles/remote-visual/reference-images/full_body/download"
        )
        assert removed_reference_download.status_code == 404
        missing_reference_delete = client.delete(
            "/api/v1/visual-profiles/remote-visual/reference-images/full_body"
        )
        assert missing_reference_delete.status_code == 404

        uploaded_scene_reference = client.post(
            "/api/v1/show-media/scene-reference-image",
            json={
                "filename": "studio.png",
                "content_type": "image/png",
                "image_base64": base64.b64encode(png_payload).decode(),
                "user_id": "tester",
            },
        )
        assert uploaded_scene_reference.status_code == 200
        scene_payload = uploaded_scene_reference.json()
        assert scene_payload["scene_reference_image_uri"].startswith(
            "object://dialecticore/show-media/scene-reference-images/"
        )
        assert scene_payload["content_type"] == "image/png"
        assert scene_payload["checksum"].startswith("sha256:")
        assert scene_payload["size_bytes"] == len(png_payload)
        assert scene_payload["object_key"].startswith("show-media/scene-reference-images/")
        downloaded_scene_reference = client.get(
            "/api/v1/show-media/scene-reference-image/download",
            params={"uri": scene_payload["scene_reference_image_uri"]},
        )
        assert downloaded_scene_reference.status_code == 200
        assert downloaded_scene_reference.headers["content-type"] == "image/png"
        assert downloaded_scene_reference.content == png_payload
        assert (
            "show-scene-reference.png" in downloaded_scene_reference.headers["content-disposition"]
        )
        rejected_scene_reference_download = client.get(
            "/api/v1/show-media/scene-reference-image/download",
            params={
                "uri": uploaded_reference.json()["reference_images"][0]["uri"],
            },
        )
        assert rejected_scene_reference_download.status_code == 422

        rejected_scene_reference = client.post(
            "/api/v1/show-media/scene-reference-image",
            json={
                "filename": "studio.png",
                "content_type": "image/png",
                "image_base64": base64.b64encode(b"not a png").decode(),
                "user_id": "tester",
            },
        )
        assert rejected_scene_reference.status_code == 422

        rejected_reference = client.post(
            "/api/v1/visual-profiles/remote-visual/reference-image",
            json={
                "filename": "speaker.png",
                "content_type": "image/png",
                "image_base64": base64.b64encode(b"not a png").decode(),
                "user_id": "tester",
            },
        )
        assert rejected_reference.status_code == 422

        first_wardrobe_payload = png_payload
        second_wardrobe_payload = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAD0lEQVR42mNk+M9Qz0AE"
            "YAEQABp9AkoAAAAASUVORK5CYII="
        )
        first_wardrobe = client.post(
            "/api/v1/visual-profiles/remote-visual/reference-image",
            json={
                "filename": "jacket.png",
                "content_type": "image/png",
                "image_base64": base64.b64encode(first_wardrobe_payload).decode(),
                "reference_type": "wardrobe",
                "user_id": "tester",
            },
        )
        assert first_wardrobe.status_code == 200
        second_wardrobe = client.post(
            "/api/v1/visual-profiles/remote-visual/reference-image",
            json={
                "filename": "scarf.png",
                "content_type": "image/png",
                "image_base64": base64.b64encode(second_wardrobe_payload).decode(),
                "reference_type": "wardrobe",
                "user_id": "tester",
            },
        )
        assert second_wardrobe.status_code == 200
        wardrobe_references = [
            reference
            for reference in second_wardrobe.json()["reference_images"]
            if reference["reference_type"] == "wardrobe"
        ]
        assert [reference["filename"] for reference in wardrobe_references] == [
            "jacket.png",
            "scarf.png",
        ]
        downloaded_first_wardrobe = client.get(
            "/api/v1/visual-profiles/remote-visual/reference-images/wardrobe/download",
            params={"uri": wardrobe_references[0]["uri"]},
        )
        assert downloaded_first_wardrobe.status_code == 200
        assert downloaded_first_wardrobe.content == first_wardrobe_payload
        removed_first_wardrobe = client.delete(
            "/api/v1/visual-profiles/remote-visual/reference-images/wardrobe",
            params={"uri": wardrobe_references[0]["uri"]},
        )
        assert removed_first_wardrobe.status_code == 200
        remaining_wardrobe_references = [
            reference
            for reference in removed_first_wardrobe.json()["reference_images"]
            if reference["reference_type"] == "wardrobe"
        ]
        assert [reference["filename"] for reference in remaining_wardrobe_references] == [
            "scarf.png"
        ]

        blocked_workflow_delete = client.delete("/api/v1/comfyui-workflows/remote-talking-head")
        assert blocked_workflow_delete.status_code == 422

        deleted_profile = client.delete("/api/v1/visual-profiles/remote-visual")
        assert deleted_profile.status_code == 204
        assert client.get("/api/v1/visual-profiles/remote-visual").status_code == 404

        deleted_workflow = client.delete("/api/v1/comfyui-workflows/remote-talking-head")
        assert deleted_workflow.status_code == 204
        assert client.get("/api/v1/comfyui-workflows/remote-talking-head").status_code == 404

        deleted_endpoint = client.delete("/api/v1/comfyui-endpoints/comfyui-remote")
        assert deleted_endpoint.status_code == 204
        assert client.get("/api/v1/comfyui-endpoints/comfyui-remote").status_code == 404

        audit = client.get("/api/v1/audit-events")
        event_types = [event["event_type"] for event in audit.json()]
        assert event_types.count("comfyui_endpoint.upserted") >= 2
        assert "comfyui_workflow.upserted" in event_types
        assert "visual_profile.upserted" in event_types
        assert "show_media.scene_reference_image_uploaded" in event_types
        reference_event = next(
            event
            for event in audit.json()
            if event["event_type"] == "visual_profile.reference_image_uploaded"
            and event["details"]["reference_type"] == "full_body"
        )
        assert reference_event["details"]["profile_id"] == "remote-visual"
        assert reference_event["details"]["reference_type"] == "full_body"
        assert reference_event["details"]["content_type"] == "image/png"
        assert reference_event["details"]["reference_image_uri_present"] is True
        assert "iVBOR" not in json.dumps(reference_event)
        removal_event = next(
            event
            for event in audit.json()
            if event["event_type"] == "visual_profile.reference_image_removed"
            and event["details"]["reference_type"] == "full_body"
        )
        assert removal_event["details"]["profile_id"] == "remote-visual"
        assert removal_event["details"]["reference_type"] == "full_body"
        assert removal_event["details"]["stored_object_retained"] is True
        assert "iVBOR" not in json.dumps(removal_event)
        scene_reference_event = next(
            event
            for event in audit.json()
            if event["event_type"] == "show_media.scene_reference_image_uploaded"
        )
        assert scene_reference_event["details"]["content_type"] == "image/png"
        assert scene_reference_event["details"]["scene_reference_image_uri_present"] is True
        assert "studio.png" not in json.dumps(scene_reference_event)
        assert "visual_profile.deleted" in event_types
        assert "comfyui_workflow.deleted" in event_types
        assert "comfyui_endpoint.deleted" in event_types
    finally:
        app.dependency_overrides.clear()
