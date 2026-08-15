import base64
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from app.core.config import Settings
from app.domain.defaults import (
    default_comfyui_endpoints,
    default_comfyui_workflows,
    default_model_endpoints,
    default_participants,
    default_publisher_targets,
    default_visual_profiles,
    default_voice_profiles,
    default_voicebox_endpoints,
)
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity, TranscriptType, TurnType
from app.domain.schemas import (
    Approval,
    ApprovalDecisionRequest,
    Asset,
    ComfyUiEndpoint,
    ComfyUiWorkflow,
    DiscussionSession,
    DiscussionTurn,
    Episode,
    EpisodeCreateRequest,
    EpisodeDefinition,
    ParticipantProfile,
    ProductionManifestRequest,
    PublisherTarget,
    PublishJob,
    PublishRequest,
    QualityResult,
    RenderRequest,
    ResearchBuildRequest,
    StructuredTurnOutput,
    TranscriptTurn,
    TranscriptVersion,
    VisualAssetPlanRequest,
    VoiceboxEndpoint,
    VoiceProfile,
    WorkflowActionRequest,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.comfyui_service import ComfyUiService
from app.services.discussion_engine import DiscussionEngine
from app.services.localization_service import LocalizationService
from app.services.model_gateway import ModelGateway
from app.services.production_control_service import (
    ProductionControlService,
    worker_stage_progress_count,
)
from app.services.research_service import ResearchService
from app.services.subtitle_service import SubtitleService
from app.services.timeline_service import TimelineService
from app.services.voicebox_service import VoiceboxService
from app.services.worker_status_service import WORKER_ROLES, WorkerStatusService
from app.workflows.worker_placeholder import (
    _ActiveWorkflowRunRepository,
    _CachedWorkflowWorkerRepository,
    _discussion_worker_can_start,
    _production_manifest_validity,
    _production_transcript_candidates,
    _target_transcript_needing_visuals,
    _visual_generation_target_asset_ids,
    run_audio_production_worker_once,
    run_comfyui_adapter_once,
    run_completion_worker_once,
    run_discussion_worker_once,
    run_localization_worker_once,
    run_publishing_worker_once,
    run_qc_worker_once,
    run_render_worker_once,
    run_research_worker_once,
    run_subtitle_worker_once,
    run_temporal_worker_once,
    run_timeline_worker_once,
    run_unsupported_worker,
    run_visual_production_worker_once,
    run_voicebox_adapter_once,
    run_workflow_worker_once,
    supported_worker_roles,
)
from tests.test_discussion_engine import definition

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
    "WjR9awAAAABJRU5ErkJggg=="
)


def test_worker_entrypoint_roles_match_worker_status_registry() -> None:
    assert sorted(supported_worker_roles()) == sorted(WORKER_ROLES)


def test_cached_workflow_worker_repository_discards_stale_worker_write() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = SnapshotEpisodeRepository(episode)
    cached = _CachedWorkflowWorkerRepository(repository)
    stale_worker_episode = cached.get(episode.id)

    operator_episode = repository.get(episode.id)
    operator_episode.title = "Operator-directed revision"
    operator_episode.updated_at = operator_episode.updated_at + timedelta(seconds=1)
    repository.save(operator_episode)

    stale_worker_episode.title = "Stale worker revision"
    saved = cached.save(stale_worker_episode)

    assert saved.title == "Operator-directed revision"
    assert repository.episode.title == "Operator-directed revision"
    assert len(repository.saved) == 1


def test_active_workflow_run_discards_mid_pass_studio_layout_change() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    running_episode = ProductionControlService(Settings()).begin_run(
        episode,
        user_id="tester",
    )
    repository = SnapshotEpisodeRepository(running_episode)
    cached = _CachedWorkflowWorkerRepository(repository)
    active_run = _ActiveWorkflowRunRepository(cached)
    stale_worker_episode = cached.get(running_episode.id)

    operator_episode = repository.get(running_episode.id)
    operator_episode.definition.media.directing.studio_layout = "seated_panel"
    operator_episode.definition.media.directing.seating_plan = {"host": 1}
    operator_episode.updated_at = operator_episode.updated_at + timedelta(seconds=1)
    repository.save(operator_episode)

    stale_worker_episode.definition.media.directing.studio_layout = "legacy_overlay"
    saved = active_run.save(stale_worker_episode)

    assert saved.definition.media.directing.studio_layout == "seated_panel"
    assert repository.episode.definition.media.directing.studio_layout == "seated_panel"
    assert len(repository.saved) == 1


def test_worker_targets_canonical_transcript_before_superseded_approved_versions() -> None:
    episode, canonical, turn = worker_episode_with_transcript()
    superseded = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language=canonical.language,
        status="approved",
        created_at=canonical.created_at - timedelta(seconds=1),
    )
    superseded.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=list(turn.source_discussion_turn_ids),
            speaker_participant_id=turn.speaker_participant_id,
            text="Superseded discussion text",
            status="accepted",
        )
    )
    episode.transcripts.insert(0, superseded)

    candidates = _production_transcript_candidates(episode)

    assert [transcript.id for transcript in candidates] == [canonical.id]
    assert _target_transcript_needing_visuals(episode) == canonical


def test_publishing_stage_progress_counts_production_manifest_handoff() -> None:
    assert (
        worker_stage_progress_count(
            "publishing",
            {
                "thumbnails_created": 0,
                "youtube_packages_created": 0,
                "production_manifests_created": 1,
                "dry_run_publish_jobs_created": 0,
                "live_publish_jobs_created": 0,
            },
        )
        == 1
    )


def test_production_manifest_validity_requires_delivery_package_chapters() -> None:
    package_asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.export_package,
        source_entity_type="render_asset",
        source_entity_id="render-final",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=package_asset.episode_id,
        asset_type=AssetType.production_manifest,
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "timeline": {
                    "chapter_count": 1,
                    "chapters": [{"title": "Opening", "start_ms": 0}],
                },
                "delivery_package": {
                    "asset_id": str(package_asset.id),
                    "manifest": {"schema_version": "youtube_package.v1", "chapters": []},
                },
            }
        },
    )

    validity = _production_manifest_validity(manifest_asset, package_asset)

    assert validity == {
        "valid": False,
        "reason": "embedded delivery package chapters do not match timeline chapters",
    }


def test_production_manifest_validity_rejects_stale_delivery_package_checksum() -> None:
    package_asset = Asset(
        episode_id=uuid4(),
        asset_type=AssetType.export_package,
        source_entity_type="render_asset",
        source_entity_id="render-final",
        storage_uri="object://dialecticore/exports/package.zip",
        checksum="sha256:current-package",
        status="completed",
    )
    manifest_asset = Asset(
        episode_id=package_asset.episode_id,
        asset_type=AssetType.production_manifest,
        source_entity_type="export_package",
        source_entity_id=str(package_asset.id),
        status="completed",
        generation_metadata={
            "production_manifest": {
                "schema_version": "production_manifest.v1",
                "delivery_package": {
                    "asset_id": str(package_asset.id),
                    "storage_uri": package_asset.storage_uri,
                    "checksum": "sha256:older-package",
                },
            }
        },
    )

    validity = _production_manifest_validity(manifest_asset, package_asset)

    assert validity == {
        "valid": False,
        "reason": "embedded delivery package checksum does not match package asset",
    }


def test_worker_orchestration_persists_media_repair_counts() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ProductionControlService(Settings())
    episode = service.begin_run(episode, user_id="tester")

    updated = service.record_worker_orchestration(
        episode,
        {
            "schema_version": "workflow_worker_orchestration_summary.v1",
            "policy": "local_stage_worker_orchestrator_v1",
            "batch_limit": 1,
            "stage_order": ["audio", "visuals"],
            "stages": {
                "audio": {
                    "episodes_scanned": 1,
                    "episodes_generated": 1,
                    "completed_audio_assets": 1,
                    "targeted_audio_assets": 2,
                    "repair_audio_assets": 1,
                    "workflow_blocked": 0,
                    "skipped": 0,
                    "error_count": 0,
                },
                "visuals": {
                    "episodes_scanned": 1,
                    "episodes_generated": 1,
                    "completed_visual_assets": 1,
                    "targeted_visual_assets": 3,
                    "repair_visual_assets": 2,
                    "workflow_blocked": 0,
                    "skipped": 0,
                    "error_count": 0,
                },
            },
            "progressed_stage_count": 2,
            "error_count": 0,
            "production_handoffs": [],
        },
    )

    attempts = updated.workflow_control["worker_orchestration_log"][0]["stage_attempts"]
    audio_attempt = next(attempt for attempt in attempts if attempt["stage"] == "audio")
    visuals_attempt = next(attempt for attempt in attempts if attempt["stage"] == "visuals")
    assert audio_attempt["targeted_audio_assets"] == 2
    assert audio_attempt["repair_audio_assets"] == 1
    assert audio_attempt["workflow_blocked"] == 0
    assert visuals_attempt["targeted_visual_assets"] == 3
    assert visuals_attempt["repair_visual_assets"] == 2


def test_worker_orchestration_records_distinct_idle_passes_and_bounds_history() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    service = ProductionControlService(Settings())
    episode = service.begin_run(episode, user_id="tester")
    control = episode.workflow_control
    control["worker_orchestration_log"] = [
        {
            "summary_id": f"historic-{index}",
            "attempt_sequence": index,
        }
        for index in range(1, 121)
    ]
    control["run"]["worker_orchestration_attempt_count"] = 120
    episode.workflow_control = control
    summary = {
        "schema_version": "workflow_worker_orchestration_summary.v1",
        "orchestration_attempt_id": "new-observation",
        "policy": "local_stage_worker_orchestrator_v1",
        "batch_limit": 1,
        "stage_order": ["audio"],
        "stages": {
            "audio": {
                "episodes_scanned": 1,
                "episodes_generated": 0,
                "workflow_blocked": 0,
                "skipped": 1,
                "error_count": 0,
                "errors": [],
            }
        },
        "progressed_stage_count": 0,
        "error_count": 0,
        "production_handoffs": [],
    }

    updated = service.record_worker_orchestration(episode, summary)
    retained_log = updated.workflow_control["worker_orchestration_log"]

    assert len(retained_log) == service.worker_orchestration_log_retention_limit
    assert retained_log[0]["attempt_sequence"] == 22
    assert retained_log[-1]["attempt_sequence"] == 121
    assert updated.workflow_control["run"]["worker_orchestration_attempt_count"] == 121
    assert updated.workflow_control["worker_orchestration_log_retention"] == {
        "retained_attempt_count": 100,
        "first_retained_attempt_sequence": 22,
        "last_retained_attempt_sequence": 121,
        "dropped_attempt_count": 21,
    }

    audit_count = len(updated.audit_events)
    next_observation = service.record_worker_orchestration(
        updated,
        {**summary, "orchestration_attempt_id": "same-operational-state"},
    )

    assert len(next_observation.workflow_control["worker_orchestration_log"]) == 100
    assert next_observation.workflow_control["worker_orchestration_log"][0][
        "attempt_sequence"
    ] == 23
    assert next_observation.workflow_control["worker_orchestration_log"][-1][
        "attempt_sequence"
    ] == 122
    assert next_observation.workflow_control["run"]["worker_orchestration_attempt_count"] == 122
    assert len(next_observation.audit_events) == audit_count + 1

    duplicate = service.record_worker_orchestration(
        next_observation,
        {**summary, "orchestration_attempt_id": "same-operational-state"},
    )

    assert duplicate.workflow_control["run"]["worker_orchestration_attempt_count"] == 122
    assert len(duplicate.audit_events) == audit_count + 1


def test_unsupported_worker_role_records_failed_heartbeat(tmp_path: Path) -> None:
    settings = Settings(runtime_state_path=str(tmp_path / "runtime-state"))

    summary = run_unsupported_worker("typo-worker", settings)
    workers = WorkerStatusService(settings).summary().workers

    assert summary["schema_version"] == "unsupported_worker_role.v1"
    assert summary["status"] == "failed"
    assert "workflow-worker" in summary["supported_roles"]
    assert "typo-worker" not in supported_worker_roles()
    assert len(workers) == 1
    assert workers[0].role == "typo-worker"
    assert workers[0].status == "failed"
    assert workers[0].details["reason"] == (
        "DIALECTICORE_WORKER_ROLE is not a supported worker role"
    )


def worker_research_definition() -> EpisodeDefinition:
    return EpisodeDefinition.model_validate(
        {
            "title": "Evidence grounded AI panel",
            "topic": {
                "central_question": "How should AI assistants be governed in software teams?",
                "required_dimensions": ["productivity", "risk", "quality"],
            },
            "format": {"target_duration_minutes": 4, "participant_count": 4},
            "participants": [
                {"participant_profile_id": "host", "role": "moderator"},
                {"participant_profile_id": "optimist", "role": "panelist"},
                {"participant_profile_id": "skeptic", "role": "panelist"},
                {"participant_profile_id": "practitioner", "role": "panelist"},
            ],
            "research": {
                "enabled": True,
                "depth": "standard",
                "require_source_links": True,
                "approval_required": True,
            },
        }
    )


def worker_localized_definition() -> EpisodeDefinition:
    payload = definition().model_dump(mode="json")
    payload["languages"] = {
        "source_language": "en",
        "outputs": [
            {"language": "en", "mode": "canonical"},
            {"language": "de", "mode": "literal"},
        ],
    }
    return EpisodeDefinition.model_validate(payload)


class FakeVoiceboxRepository:
    def __init__(self, episode: Episode) -> None:
        self.episode = episode
        self.saved: list[Episode] = []
        self.endpoint = VoiceboxEndpoint(
            id="voicebox-remote",
            name="Voicebox Remote",
            adapter_type="voicebox_http",
            base_url="https://voicebox.example.test",
            capabilities={"formats": ["audio/wav"], "sample_rates": [48000]},
        )
        self.profile = VoiceProfile(
            id="voice-host",
            name="Host Voice",
            voicebox_endpoint_id="voicebox-remote",
            voice_id="voice-1",
            language="en",
        )

    def list(self) -> list[Episode]:
        return [self.episode]

    def save(self, episode: Episode) -> Episode:
        self.episode = episode
        self.saved.append(episode)
        return episode

    def list_voicebox_endpoints(self) -> list[VoiceboxEndpoint]:
        return [self.endpoint]

    def list_voice_profiles(self) -> list[VoiceProfile]:
        return [self.profile]


class FakeComfyUiRepository:
    def __init__(self, episode: Episode) -> None:
        self.episode = episode
        self.saved: list[Episode] = []
        self.endpoint = ComfyUiEndpoint(
            id="comfyui-remote",
            name="ComfyUI Remote",
            adapter_type="comfyui_http",
            base_url="https://comfyui.example.test",
        )
        self.workflows = [
            workflow.model_copy(update={"comfyui_endpoint_id": "comfyui-remote"})
            for workflow in default_comfyui_workflows()
        ]

    def list(self) -> list[Episode]:
        return [self.episode]

    def save(self, episode: Episode) -> Episode:
        self.episode = episode
        self.saved.append(episode)
        return episode

    def list_comfyui_endpoints(self) -> list[ComfyUiEndpoint]:
        return [self.endpoint]

    def list_comfyui_workflows(self) -> list[ComfyUiWorkflow]:
        return self.workflows


class FakeEpisodeRepository:
    def __init__(self, episode: Episode) -> None:
        self.episode = episode
        self.saved: list[Episode] = []

    def list(self) -> list[Episode]:
        return [self.episode]

    def get(self, episode_id: UUID) -> Episode:
        if episode_id != self.episode.id:
            raise KeyError(episode_id)
        return self.episode

    def save(self, episode: Episode) -> Episode:
        self.episode = episode
        self.saved.append(episode)
        return episode


class SnapshotEpisodeRepository(FakeEpisodeRepository):
    """Return detached episodes to reproduce a worker snapshot versus API write."""

    def list(self) -> list[Episode]:
        return [self.episode.model_copy(deep=True)]

    def get(self, episode_id: UUID) -> Episode:
        if episode_id != self.episode.id:
            raise KeyError(episode_id)
        return self.episode.model_copy(deep=True)

    def save(self, episode: Episode) -> Episode:
        self.episode = episode.model_copy(deep=True)
        self.saved.append(self.episode)
        return self.episode.model_copy(deep=True)


class FakePublishingRepository(FakeEpisodeRepository):
    def __init__(self, episode: Episode, publisher_targets=None) -> None:
        super().__init__(episode)
        self.publisher_targets = (
            default_publisher_targets() if publisher_targets is None else publisher_targets
        )

    def list_publisher_targets(self):
        return self.publisher_targets


class FakeWorkflowRepository(FakePublishingRepository):
    def list_voicebox_endpoints(self) -> list[VoiceboxEndpoint]:
        return default_voicebox_endpoints()

    def list_voice_profiles(self) -> list[VoiceProfile]:
        return default_voice_profiles()

    def list_comfyui_endpoints(self) -> list[ComfyUiEndpoint]:
        return default_comfyui_endpoints()

    def list_comfyui_workflows(self) -> list[ComfyUiWorkflow]:
        return default_comfyui_workflows()

    def list_visual_profiles(self):
        return default_visual_profiles()


class FakeRenderService:
    def enqueue_render(self, episode: Episode, request, presets):
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.render,
            language=request.language or "en",
            source_entity_type="timeline_asset",
            source_entity_id=str(request.timeline_asset_id),
            status="submitted",
            generation_metadata={
                "render_type": request.render_type,
                "preset_id": request.preset_id,
                "timeline_asset_id": str(request.timeline_asset_id),
                "render_request": request.model_dump(mode="json"),
            },
        )
        episode.assets.append(asset)
        return episode

    def start_queued_render(self, episode: Episode, render_asset_id, *, actor: str):
        asset = next(asset for asset in episode.assets if asset.id == render_asset_id)
        asset.status = "running"
        return episode

    def fail_queued_render(self, episode: Episode, render_asset_id, *, actor: str, error: str):
        asset = next(asset for asset in episode.assets if asset.id == render_asset_id)
        asset.status = "failed"
        return episode

    def render_episode(self, episode: Episode, request, presets, queued_render_asset_id=None):
        asset = (
            next(asset for asset in episode.assets if asset.id == queued_render_asset_id)
            if queued_render_asset_id is not None
            else Asset(
                episode_id=episode.id,
                asset_type=AssetType.render,
                language=request.language or "en",
                source_entity_type="timeline_asset",
                source_entity_id=str(request.timeline_asset_id),
            )
        )
        asset.storage_uri = f"object://dialecticore/renders/{request.render_type}.mp4"
        asset.mime_type = "video/mp4"
        asset.checksum = f"sha256:{request.render_type}"
        asset.status = "completed"
        asset.generation_metadata.update(
            {
                "render_type": request.render_type,
                "preset_id": request.preset_id,
                "timeline_asset_id": str(request.timeline_asset_id),
            }
        )
        if queued_render_asset_id is None:
            episode.assets.append(asset)
        if request.render_type == "preview":
            approval = Approval(
                episode_id=episode.id,
                stage="preview_render_review",
                target_type="render_asset",
                target_id=str(asset.id),
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

    def generate_thumbnail(self, episode: Episode, request):
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.thumbnail,
            language="en",
            source_entity_type="render_asset",
            source_entity_id=str(request.render_asset_id),
            storage_uri="object://dialecticore/thumbnails/thumb.jpg",
            mime_type="image/jpeg",
            checksum="sha256:thumbnail",
            status="completed",
        )
        episode.assets.append(asset)
        return episode

    def export_youtube_package(self, episode: Episode, request):
        included_files = ["youtube-package.json", "video/render.mp4"]
        youtube_manifest = {}
        if request.thumbnail_asset_id:
            included_files.append("thumbnail/thumbnail.jpg")
            youtube_manifest["thumbnail_asset_id"] = str(request.thumbnail_asset_id)
        subtitle_asset = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.subtitle and asset.status == "completed"
            ),
            None,
        )
        if subtitle_asset is not None:
            subtitle_path = f"subtitles/{subtitle_asset.language}.vtt"
            included_files.append(subtitle_path)
            youtube_manifest["subtitles"] = [
                {
                    "asset_id": str(subtitle_asset.id),
                    "language": subtitle_asset.language,
                    "path": subtitle_path,
                }
            ]
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.export_package,
            language="en",
            source_entity_type="render_asset",
            source_entity_id=str(request.render_asset_id),
            storage_uri="object://dialecticore/exports/package.zip",
            mime_type="application/zip",
            checksum="sha256:package",
            status="completed",
            generation_metadata={
                "render_asset_id": str(request.render_asset_id),
                "thumbnail_asset_id": (
                    str(request.thumbnail_asset_id) if request.thumbnail_asset_id else None
                ),
                "included_files": included_files,
                "youtube_package_manifest": youtube_manifest,
            },
        )
        episode.assets.append(asset)
        return episode

    def generate_production_manifest(
        self,
        episode: Episode,
        request: ProductionManifestRequest,
    ):
        if request.regenerate:
            for existing in episode.assets:
                if (
                    existing.asset_type == AssetType.production_manifest
                    and existing.status == "completed"
                    and existing.source_entity_type == "export_package"
                    and existing.source_entity_id == str(request.package_asset_id)
                ):
                    existing.status = "replaced"
        publish_jobs = [
            {
                "id": str(job.id),
                "package_asset_id": str(job.package_asset_id),
                "status": job.status,
            }
            for job in episode.publish_jobs
            if str(job.package_asset_id) == str(request.package_asset_id)
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
        package_asset = next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.export_package
                and asset.id == request.package_asset_id
            ),
            None,
        )
        delivery_package = {"asset_id": str(request.package_asset_id)}
        if package_asset is not None:
            package_manifest = package_asset.generation_metadata.get("youtube_package_manifest")
            included_files = package_asset.generation_metadata.get("included_files")
            if isinstance(package_manifest, dict):
                delivery_package["manifest"] = dict(package_manifest)
            if isinstance(included_files, list):
                delivery_package["included_files"] = list(included_files)
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.production_manifest,
            language="en",
            source_entity_type="export_package",
            source_entity_id=str(request.package_asset_id),
            storage_uri="object://dialecticore/manifests/production.json",
            mime_type="application/vnd.dialecticore.production-manifest+json",
            checksum="sha256:production-manifest",
            status="completed",
            generation_metadata={
                "production_manifest": {
                    "schema_version": "production_manifest.v1",
                    "delivery_package": delivery_package,
                    "publish_jobs": publish_jobs,
                    "quality_results": publish_quality_results,
                }
            },
        )
        episode.assets.append(asset)
        return episode


class FakePublisherService:
    def __init__(self) -> None:
        self.requests: list[PublishRequest] = []

    def publish_package(self, episode: Episode, request: PublishRequest, targets):
        self.requests.append(request)
        assert any(
            asset.asset_type == AssetType.production_manifest
            and asset.status == "completed"
            and asset.source_entity_type == "export_package"
            and asset.source_entity_id == str(request.package_asset_id)
            for asset in episode.assets
        )
        job = PublishJob(
            episode_id=episode.id,
            publisher_target_id=request.publisher_target_id,
            platform="youtube",
            package_asset_id=request.package_asset_id,
            status="completed",
            dry_run=request.dry_run,
            remote_job_id="dry-run-job",
            publish_url="mock://youtube/dry-run-job",
        )
        episode.publish_jobs.append(job)
        episode.quality_results.append(
            QualityResult(
                episode_id=episode.id,
                target_type="publish_job",
                target_id=str(job.id),
                check_type="publish_delivery_integrity",
                severity=QualitySeverity.warning if request.dry_run else QualitySeverity.pass_,
                status="warning" if request.dry_run else "pass",
                score=1.0,
                details={
                    "dry_run": request.dry_run,
                    "failure_count": 0,
                    "warning_count": 1 if request.dry_run else 0,
                },
            )
        )
        return episode


class PackageQcBlockedRenderService(FakeRenderService):
    def generate_production_manifest(
        self,
        episode: Episode,
        request: ProductionManifestRequest,
    ):
        raise ValueError(
            "YouTube package QC is required before production manifest generation"
        )


class ProductionManifestBlockedPublisherService(FakePublisherService):
    def publish_package(self, episode: Episode, request: PublishRequest, targets):
        raise ValueError(
            "valid production_manifest.v1 asset is required before live publishing"
        )


class PassingCompletionControl(ProductionControlService):
    def completion_readiness(self, episode: Episode) -> dict:
        return {
            "status": "pass",
            "failed_checks": [],
            "final_render_asset_id": "render-final",
            "export_package_asset_id": "package-final",
            "production_manifest_asset_id": "manifest-final",
        }


class FailingLocalizationService:
    def create_language_variants(self, episode: Episode, request):
        raise ValueError("translation provider unavailable")


@pytest.mark.asyncio
async def test_workflow_handoff_blocks_missing_shot_planned_reusable_visuals(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    audio_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/audio/turn.wav",
        mime_type="audio/wav",
        duration_ms=1000,
        checksum="sha256:audio",
        status="completed",
        generation_metadata={"transcript_version_id": str(transcript.id)},
    )
    planned_reaction = Asset(
        episode_id=episode.id,
        asset_type=AssetType.reaction_loop,
        language="en",
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
    planned_studio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        language="en",
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
    visual_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
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
            "render_ready": True,
            "shot_plan": {
                "reusable_reaction_asset_id": str(planned_reaction.id),
                "studio_scene_asset_id": str(planned_studio.id),
            },
        },
    )
    subtitle_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language="en",
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
        language="en",
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
    episode.assets.extend(
        [
            audio_asset,
            planned_reaction,
            planned_studio,
            visual_asset,
            subtitle_asset,
            timeline_asset,
        ]
    )
    production_control = ProductionControlService()
    episode = production_control.begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode, publisher_targets=[])

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        render_service=FakeRenderService(),
        batch_limit=10,
    )

    handoff = summary["production_handoffs"][0]
    assert handoff["status"] == "blocked"
    assert "shot_planned_reaction_loop_missing" in handoff["blocking_reasons"]
    assert "shot_planned_studio_scene_missing" in handoff["blocking_reasons"]
    assert handoff["character_configuration"]["ready"] is True
    assert handoff["character_configuration"]["active_speaker_count"] == 1
    assert handoff["character_configuration"]["configured_model_speaker_count"] == 1
    assert handoff["character_configuration"]["configured_voice_speaker_count"] == 1
    assert handoff["character_configuration"]["configured_visual_speaker_count"] == 1
    assert handoff["character_configuration"]["participants"][0]["participant_id"] == "host"
    assert handoff["character_configuration"]["participants"][0]["model_endpoint_id"] == "mock"
    assert handoff["character_configuration"]["participants"][0]["model_id"] == "mock-host-v1"
    assert handoff["character_configuration"]["participants"][0]["voice_profile_id"] == (
        "voice-host"
    )
    assert handoff["character_configuration"]["participants"][0]["visual_profile_id"] == (
        "visual-host"
    )
    assert handoff["turn_handoffs"]["stale_voice_asset_turn_ids"] == []
    assert handoff["turn_handoffs"]["stale_visual_asset_turn_ids"] == []
    assert handoff["character_animation"]["ready"] is False
    assert handoff["character_animation"]["expected_reaction_loop_segment_count"] == 1
    assert handoff["character_animation"]["linked_reaction_loop_segment_count"] == 0
    assert handoff["character_animation"]["missing_reaction_loop_turn_ids"] == [
        str(turn.id)
    ]
    assert handoff["studio_scene"]["ready"] is False
    assert handoff["studio_scene"]["expected_studio_scene_segment_count"] == 1
    assert handoff["studio_scene"]["linked_studio_scene_segment_count"] == 0
    assert handoff["studio_scene"]["missing_studio_scene_turn_ids"] == [str(turn.id)]
    assert handoff["timeline"]["ready"] is False


@pytest.mark.asyncio
async def test_workflow_handoff_blocks_stale_character_media_after_profile_change(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    audio_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
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
        language="en",
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
        language="en",
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
        language="en",
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
    episode.participants[0].voice_profile_id = "voice-host-updated"
    episode.participants[0].visual_profile_id = "visual-host-updated"
    production_control = ProductionControlService()
    episode = production_control.begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode, publisher_targets=[])

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        render_service=FakeRenderService(),
        batch_limit=10,
    )

    handoff = summary["production_handoffs"][0]
    assert handoff["status"] == "blocked"
    assert "character_voice_asset_stale" in handoff["blocking_reasons"]
    assert "character_visual_asset_stale" in handoff["blocking_reasons"]
    assert handoff["turn_handoffs"]["completed_audio_turn_count"] == 1
    assert handoff["turn_handoffs"]["completed_primary_visual_turn_count"] == 1
    assert handoff["turn_handoffs"]["stale_voice_asset_turn_ids"] == [str(turn.id)]
    assert handoff["turn_handoffs"]["stale_visual_asset_turn_ids"] == [str(turn.id)]
    assert handoff["speech"]["ready"] is False
    assert handoff["character_animation"]["ready"] is False


@pytest.mark.asyncio
async def test_workflow_handoff_blocks_stale_model_turn_after_model_change(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    discussion_session = DiscussionSession(
        episode_id=episode.id,
        status="completed",
    )
    discussion_turn = DiscussionTurn(
        id=turn.source_discussion_turn_ids[0],
        discussion_session_id=discussion_session.id,
        sequence_number=1,
        speaker_participant_id=turn.speaker_participant_id,
        turn_type=TurnType.host_opening,
        spoken_text=turn.text,
        intent="open",
        estimated_duration_seconds=1,
        structured_output=StructuredTurnOutput(
            spoken_text=turn.text,
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
    episode.participants[0].model_id = "mock-host-v2"
    production_control = ProductionControlService()
    episode = production_control.begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode, publisher_targets=[])

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        render_service=FakeRenderService(),
        batch_limit=10,
    )

    handoff = summary["production_handoffs"][0]
    assert handoff["status"] == "blocked"
    assert "character_model_turn_stale" in handoff["blocking_reasons"]
    assert handoff["turn_handoffs"]["stale_model_turn_ids"] == [str(turn.id)]


@pytest.mark.asyncio
async def test_workflow_handoff_blocks_incomplete_character_configuration(
    tmp_path: Path,
) -> None:
    episode, _, _ = worker_episode_with_transcript()
    episode.participants[0].model_id = ""
    episode.participants[0].voice_profile_id = None
    episode.participants[0].visual_profile_id = None
    production_control = ProductionControlService()
    episode = production_control.begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode, publisher_targets=[])

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        render_service=FakeRenderService(),
        batch_limit=10,
    )

    handoff = summary["production_handoffs"][0]
    assert handoff["status"] == "blocked"
    assert "character_model_missing" in handoff["blocking_reasons"]
    assert "character_voice_missing" in handoff["blocking_reasons"]
    assert "character_visual_missing" in handoff["blocking_reasons"]
    assert handoff["character_configuration"] == {
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
                "display_name": "Host",
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


def worker_episode_with_transcript() -> tuple[Episode, TranscriptVersion, TranscriptTurn]:
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000100",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000101"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Worker Automation",
        slug="worker-automation",
        subject="Worker Automation",
        central_question="How should queued workers progress production?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
                visual_profile_id="visual-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
    )
    return episode, transcript, turn


def test_completion_worker_closes_running_episode_after_readiness_passes() -> None:
    production_control = PassingCompletionControl()
    episode, _transcript, _turn = worker_episode_with_transcript()
    episode = production_control.begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode)

    summary = run_completion_worker_once(
        repository=repository,
        production_control=production_control,
        batch_limit=10,
        user_id="workflow-worker",
    )

    assert summary["episodes_completed"] == 1
    assert summary["completed_episode_ids"] == [str(episode.id)]
    assert summary["readiness_blocked"] == 0
    assert repository.episode.status == EpisodeStatus.completed
    run = repository.episode.workflow_control["run"]
    assert run["state"] == "completed"
    assert run["current_stage"] == EpisodeStatus.completed.value
    assert run["completion_gate"]["status"] == "pass"
    assert run["signals"][-1]["signal_type"] == "complete"


def test_worker_orchestration_records_blocked_completion_handoff() -> None:
    production_control = ProductionControlService()
    episode, _transcript, _turn = worker_episode_with_transcript()
    episode = production_control.begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode)
    summary = {
        "schema_version": "workflow_worker_orchestration_summary.v1",
        "policy": "local_stage_worker_orchestrator_v1",
        "batch_limit": 10,
        "stage_order": ["completion"],
        "stages": {
            "completion": {
                "episodes_scanned": 1,
                "episodes_completed": 0,
                "completed_episode_ids": [],
                "readiness_blocked": 1,
                "readiness_blockers": [
                    {
                        "episode_id": str(episode.id),
                        "failed_checks": ["publish_job_missing"],
                    }
                ],
                "skipped": 1,
                "error_count": 0,
                "errors": [],
            }
        },
        "progressed_stage_count": 0,
        "error_count": 0,
        "production_handoffs": [],
    }

    updated = production_control.record_worker_orchestration(
        repository.episode,
        summary,
        worker_id="workflow-worker",
    )
    repository.save(updated)

    orchestration = repository.episode.workflow_control["worker_orchestration_log"][-1]
    assert orchestration["completion_handoff"] == {
        "schema_version": "workflow_completion_handoff.v1",
        "episode_id": str(episode.id),
        "status": "blocked",
        "failed_checks": ["publish_job_missing"],
    }
    assert repository.episode.workflow_control["run"]["last_worker_orchestration"][
        "completion_handoff"
    ]["status"] == "blocked"


@pytest.mark.asyncio
async def test_voicebox_adapter_worker_syncs_submitted_remote_jobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/tts/jobs/job-worker-1"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "job_id": "job-worker-1",
                "storage_uri": "s3://dialecticore/audio/worker.wav",
                "mime_type": "audio/wav",
                "duration_ms": 1200,
                "checksum": "sha256:worker",
                "sample_rate": 48000,
                "channels": 1,
                "detected_language": "en",
                "peak_dbfs": -4,
                "loudness_lufs": -18,
                "silence_ratio": 0.05,
                "word_timestamps": [
                    {"word": "Hello", "start_ms": 0, "end_ms": 400},
                    {"word": "there", "start_ms": 450, "end_ms": 1200},
                ],
            },
        )

    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Worker Sync",
        slug="worker-sync",
        subject="Worker Sync",
        central_question="How should remote Voicebox jobs be synchronized?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                mime_type="audio/wav",
                status="submitted",
                generation_metadata={
                    "voicebox_endpoint_id": "voicebox-remote",
                    "voice_profile_id": "voice-host",
                    "remote_job_id": "job-worker-1",
                },
            )
        ],
    )
    repository = FakeVoiceboxRepository(episode)

    summary = await run_voicebox_adapter_once(
        repository=repository,
        voicebox_service=VoiceboxService(Settings(), transport=httpx.MockTransport(handler)),
        batch_limit=10,
        user_id="test-worker",
    )

    synced_asset = repository.episode.assets[0]
    assert summary["episodes_scanned"] == 1
    assert summary["episodes_synced"] == 1
    assert summary["pending_audio_assets"] == 1
    assert summary["error_count"] == 0
    assert len(repository.saved) == 1
    assert synced_asset.status == "completed"
    assert synced_asset.storage_uri == "s3://dialecticore/audio/worker.wav"
    assert synced_asset.generation_metadata["sync_attempt_count"] == 1
    assert repository.episode.quality_results[-1].check_type == "audio_media_integrity"
    assert repository.episode.quality_results[-1].status == "pass"
    assert repository.episode.audit_events[-1].event_type == "audio.jobs.synced"
    assert repository.episode.audit_events[-1].actor == "test-worker"


@pytest.mark.asyncio
async def test_b1_voice_stream_tts_uses_native_payload_and_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "authorization": request.headers.get("authorization"),
                "accept": request.headers.get("accept"),
                "payload": request.read(),
            }
        )
        return httpx.Response(
            200,
            content=b"RIFF....WAVEfmt ",
            headers={"content-type": "audio/wav"},
        )

    monkeypatch.setenv("B1_API_KEY", "b1-test-token")
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
        credential_reference="env:B1_API_KEY",
        capabilities={"stream_generation_path": "/generate/stream"},
    )
    voice_profile = VoiceProfile(
        id="voice-claude",
        name="A_DE_Claude",
        voicebox_endpoint_id="b1-voicebox",
        voice_id="bd4e9bf1-482b-4900-97c1-48275d1ba28c",
        language="de",
        model_id="chatterbox",
        prosody={"engine": "chatterbox", "normalize": False, "effects_chain": []},
    )
    transcript = TranscriptVersion(
        episode_id=uuid4(),
        type=TranscriptType.localized,
        language="de",
        status="approved",
        localization_metadata={"mode": "localized_reperformance"},
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=[uuid4()],
        speaker_participant_id="claude",
        text="Guten Tag.",
        status="accepted",
    )
    asset = Asset(
        episode_id=transcript.episode_id,
        asset_type=AssetType.audio,
        language="de",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        mime_type="audio/wav",
        generation_metadata={},
    )

    result = await VoiceboxService(
        Settings(),
        transport=httpx.MockTransport(handler),
    )._submit_audio_stream_tts(endpoint, voice_profile, transcript, turn, asset)

    assert result.status == "completed"
    assert result.mime_type == "audio/wav"
    assert result.audio_bytes == b"RIFF....WAVEfmt "
    assert requests[0]["method"] == "POST"
    assert requests[0]["path"] == "/generate/stream"
    assert requests[0]["authorization"] == "Bearer b1-test-token"
    assert requests[0]["accept"] == "audio/wav"
    assert json.loads(requests[0]["payload"]) == {
        "profile_id": "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
        "text": "Guten Tag.",
        "language": "de",
        "engine": "chatterbox",
        "normalize": False,
        "effects_chain": [],
    }
    assert result.metadata["remote_profile_id"] == "bd4e9bf1-482b-4900-97c1-48275d1ba28c"
    assert result.metadata["engine"] == "chatterbox"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx.Response(200, content=b"", headers={"content-type": "audio/wav"}),
            "empty audio content",
        ),
        (
            httpx.Response(200, json={"error": "not audio"}),
            "non-audio content type application/json",
        ),
    ],
)
async def test_b1_voice_stream_tts_rejects_invalid_successful_streams(
    response: httpx.Response,
    message: str,
) -> None:
    endpoint = VoiceboxEndpoint(
        id="b1-voicebox",
        name="B1 Voicebox",
        adapter_type="b1_voice_stream",
        base_url="https://voice.ai.b1.germering",
    )
    voice_profile = VoiceProfile(
        id="voice-claude",
        name="A_DE_Claude",
        voicebox_endpoint_id="b1-voicebox",
        voice_id="bd4e9bf1-482b-4900-97c1-48275d1ba28c",
        language="de",
        model_id="chatterbox",
    )
    transcript = TranscriptVersion(
        episode_id=uuid4(),
        type=TranscriptType.localized,
        language="de",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=[uuid4()],
        speaker_participant_id="claude",
        text="Guten Tag.",
        status="accepted",
    )
    asset = Asset(
        episode_id=transcript.episode_id,
        asset_type=AssetType.audio,
        language="de",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        mime_type="audio/wav",
        generation_metadata={},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return response

    with pytest.raises(ValueError, match=message):
        await VoiceboxService(
            Settings(),
            transport=httpx.MockTransport(handler),
        )._submit_audio_stream_tts(endpoint, voice_profile, transcript, turn, asset)


@pytest.mark.asyncio
async def test_audio_production_worker_plans_and_generates_approved_transcript(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    repository = FakeVoiceboxRepository(episode)
    repository.endpoint = repository.endpoint.model_copy(
        update={"adapter_type": "mock", "base_url": None}
    )

    summary = await run_audio_production_worker_once(
        repository=repository,
        voicebox_service=VoiceboxService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-worker",
    )

    audio_assets = [
        asset
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.audio
        and asset.source_entity_id == str(turn.id)
        and asset.language == transcript.language
    ]
    assert summary["episodes_scanned"] == 1
    assert summary["episodes_planned"] == 1
    assert summary["episodes_generated"] == 1
    assert summary["completed_audio_assets"] == 1
    assert summary["submitted_audio_assets"] == 0
    assert summary["error_count"] == 0
    assert len(repository.saved) == 1
    assert len(audio_assets) == 1
    assert audio_assets[0].status == "completed"
    assert audio_assets[0].generation_metadata["voice_profile_id"] == "voice-host"
    assert repository.episode.quality_results[-1].check_type == "audio_media_integrity"
    assert repository.episode.quality_results[-1].status == "pass"


@pytest.mark.asyncio
async def test_audio_production_worker_waits_for_localized_transcript_approval(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language=transcript.language,
            source_entity_type="transcript_turn",
            source_entity_id=str(turn.id),
            storage_uri="object://dialecticore/audio/en-turn.wav",
            mime_type="audio/wav",
            duration_ms=1200,
            checksum="sha256:audio-en",
            status="completed",
            generation_metadata={
                "transcript_version_id": str(transcript.id),
                "voice_profile_id": "voice-host",
            },
        )
    )
    localized = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.localized,
        language="de",
        status="pending_review",
        parent_version_id=transcript.id,
        semantic_fidelity_score=1,
    )
    localized_turn = TranscriptTurn(
        transcript_version_id=localized.id,
        source_discussion_turn_ids=[turn.source_discussion_turn_ids[0]],
        speaker_participant_id=turn.speaker_participant_id,
        text="Hallo dort",
        status="accepted",
    )
    localized.turns.append(localized_turn)
    episode.transcripts.append(localized)
    repository = FakeVoiceboxRepository(episode)
    repository.endpoint = repository.endpoint.model_copy(
        update={"adapter_type": "mock", "base_url": None}
    )

    pending_summary = await run_audio_production_worker_once(
        repository=repository,
        voicebox_service=VoiceboxService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-worker",
    )

    assert pending_summary["episodes_scanned"] == 1
    assert pending_summary["episodes_generated"] == 0
    assert pending_summary["skipped"] == 1
    assert not [
        asset
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.audio
        and asset.language == "de"
        and asset.source_entity_id == str(localized_turn.id)
    ]

    localized.status = "approved"
    approved_summary = await run_audio_production_worker_once(
        repository=repository,
        voicebox_service=VoiceboxService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-worker",
    )
    localized_audio_assets = [
        asset
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.audio
        and asset.language == "de"
        and asset.source_entity_id == str(localized_turn.id)
    ]

    assert approved_summary["episodes_generated"] == 1
    assert approved_summary["completed_audio_assets"] == 1
    assert len(localized_audio_assets) == 1
    assert localized_audio_assets[0].status == "completed"


@pytest.mark.asyncio
async def test_audio_production_worker_skips_paused_workflow_run(tmp_path: Path) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    control = ProductionControlService(settings)
    episode = control.begin_run(episode, user_id="producer-1")
    episode = control.pause(
        episode,
        WorkflowActionRequest(
            action="pause",
            user_id="producer-1",
            comment="hold before audio production",
        ),
    )
    repository = FakeVoiceboxRepository(episode)
    repository.endpoint = repository.endpoint.model_copy(
        update={"adapter_type": "mock", "base_url": None}
    )

    summary = await run_audio_production_worker_once(
        repository=repository,
        voicebox_service=VoiceboxService(settings),
        batch_limit=10,
        user_id="test-worker",
    )

    assert summary["episodes_scanned"] == 1
    assert summary["workflow_blocked"] == 1
    assert summary["episodes_planned"] == 0
    assert summary["episodes_generated"] == 0
    assert summary["completed_audio_assets"] == 0
    assert summary["error_count"] == 0
    assert repository.saved == []
    assert not [
        asset
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.audio
        and asset.source_entity_id == str(turn.id)
        and asset.language == transcript.language
    ]


@pytest.mark.asyncio
async def test_audio_production_worker_repairs_failed_turn_asset(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language=transcript.language,
            source_entity_type="transcript_turn",
            source_entity_id=str(turn.id),
            mime_type="audio/wav",
            duration_ms=1200,
            checksum=None,
            status="failed",
            generation_metadata={
                "adapter": "voicebox",
                "status": "failed",
                "failure": "previous remote TTS job failed",
                "transcript_version_id": str(transcript.id),
                "speaker_participant_id": turn.speaker_participant_id,
                "voice_profile_id": "voice-host",
                "ready_for_retry": True,
            },
        )
    )
    repository = FakeVoiceboxRepository(episode)
    repository.endpoint = repository.endpoint.model_copy(
        update={"adapter_type": "mock", "base_url": None}
    )

    summary = await run_audio_production_worker_once(
        repository=repository,
        voicebox_service=VoiceboxService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-worker",
    )

    repaired = repository.episode.assets[0]
    assert summary["episodes_scanned"] == 1
    assert summary["episodes_planned"] == 0
    assert summary["episodes_generated"] == 1
    assert summary["targeted_audio_assets"] == 1
    assert summary["repair_audio_assets"] == 1
    assert summary["completed_audio_assets"] == 1
    assert repaired.status == "completed"
    assert repaired.generation_metadata["generation_attempt_count"] == 1
    assert "failure" not in repaired.generation_metadata
    assert "audio.assets.regenerated" in [
        event.event_type for event in repository.episode.audit_events
    ]


@pytest.mark.asyncio
async def test_audio_production_worker_skips_failed_turn_asset_until_retry_ready(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language=transcript.language,
            source_entity_type="transcript_turn",
            source_entity_id=str(turn.id),
            mime_type="audio/wav",
            duration_ms=1200,
            checksum=None,
            status="failed",
            generation_metadata={
                "adapter": "voicebox",
                "status": "failed",
                "failure": "provider returned 500",
                "transcript_version_id": str(transcript.id),
                "speaker_participant_id": turn.speaker_participant_id,
                "voice_profile_id": "voice-host",
            },
        )
    )
    repository = FakeVoiceboxRepository(episode)

    summary = await run_audio_production_worker_once(
        repository=repository,
        voicebox_service=VoiceboxService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-worker",
    )

    assert summary["episodes_scanned"] == 1
    assert summary["episodes_generated"] == 0
    assert summary["targeted_audio_assets"] == 0
    assert summary["repair_audio_assets"] == 0
    assert repository.episode.assets[0].status == "failed"


def test_subtitle_worker_generates_after_audio(tmp_path: Path) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language=transcript.language,
            source_entity_type="transcript_turn",
            source_entity_id=str(turn.id),
            storage_uri="object://dialecticore/audio/turn.wav",
            mime_type="audio/wav",
            duration_ms=1200,
            checksum="sha256:audio",
            status="completed",
            generation_metadata={
                "transcript_version_id": str(transcript.id),
                "voice_profile_id": "voice-host",
                "word_timestamps": [
                    {"word": "Hello", "start_ms": 0, "end_ms": 400},
                    {"word": "there", "start_ms": 450, "end_ms": 1200},
                ],
            },
        )
    )
    repository = FakeEpisodeRepository(episode)

    summary = run_subtitle_worker_once(
        repository=repository,
        subtitle_service=SubtitleService(Settings(object_storage_local_path=str(tmp_path))),
        batch_limit=10,
        user_id="test-worker",
    )

    subtitle_assets = [
        asset
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.subtitle
        and asset.source_entity_id == str(transcript.id)
        and asset.language == transcript.language
    ]
    assert summary["episodes_scanned"] == 1
    assert summary["subtitles_generated"] == 1
    assert summary["prerequisite_blocked"] == 0
    assert summary["error_count"] == 0
    assert len(repository.saved) == 1
    assert len(subtitle_assets) == 1
    assert subtitle_assets[0].status == "completed"
    assert subtitle_assets[0].generation_metadata["transcript_version_id"] == str(transcript.id)
    assert subtitle_assets[0].generation_metadata["missing_audio_count"] == 0
    assert subtitle_assets[0].generation_metadata["word_timed_cue_count"] > 0
    assert repository.episode.quality_results[-1].check_type == (
        "subtitle_generation_completeness"
    )
    assert repository.episode.quality_results[-1].status == "pass"


@pytest.mark.asyncio
async def test_visual_production_worker_plans_and_generates_after_audio(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language=transcript.language,
            source_entity_type="transcript_turn",
            source_entity_id=str(turn.id),
            storage_uri="object://dialecticore/audio/turn.wav",
            mime_type="audio/wav",
            duration_ms=1200,
            checksum="sha256:audio",
            status="completed",
            generation_metadata={
                "transcript_version_id": str(transcript.id),
                "voice_profile_id": "voice-host",
            },
        )
    )
    repository = FakeWorkflowRepository(episode)

    summary = await run_visual_production_worker_once(
        repository=repository,
        comfyui_service=ComfyUiService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-worker",
    )

    primary_visual_assets = [
        asset
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.video
        and asset.source_entity_id == str(turn.id)
        and asset.generation_metadata.get("visual_role") == "video_primary"
    ]
    assert summary["episodes_scanned"] == 1
    assert summary["episodes_planned"] == 1
    assert summary["episodes_generated"] == 1
    assert summary["completed_visual_assets"] >= 1
    assert summary["submitted_visual_assets"] == 0
    assert summary["error_count"] == 0
    assert len(repository.saved) == 1
    assert len(primary_visual_assets) == 1
    assert primary_visual_assets[0].status == "completed"
    assert primary_visual_assets[0].generation_metadata["transcript_version_id"] == (
        str(transcript.id)
    )
    assert primary_visual_assets[0].generation_metadata["visual_profile_id"] == "visual-host"
    assert repository.episode.quality_results[-1].check_type == "visual_media_integrity"
    assert repository.episode.quality_results[-1].status in {"pass", "warning"}


@pytest.mark.asyncio
async def test_visual_production_worker_repairs_failed_character_asset(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode, transcript, turn = worker_episode_with_transcript()
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.audio,
            language=transcript.language,
            source_entity_type="transcript_turn",
            source_entity_id=str(turn.id),
            storage_uri="object://dialecticore/audio/turn.wav",
            mime_type="audio/wav",
            duration_ms=1200,
            checksum="sha256:audio",
            status="completed",
            generation_metadata={
                "transcript_version_id": str(transcript.id),
                "voice_profile_id": "voice-host",
            },
        )
    )
    repository = FakeWorkflowRepository(episode, publisher_targets=[])
    comfyui_service = ComfyUiService(settings)
    episode = comfyui_service.plan_visual_assets(
        episode,
        VisualAssetPlanRequest(
            transcript_version_id=transcript.id,
            user_id="test-worker",
        ),
        visual_profiles=repository.list_visual_profiles(),
        workflows=repository.list_comfyui_workflows(),
    )
    primary = next(
        asset
        for asset in episode.assets
        if asset.asset_type == AssetType.video
        and asset.source_entity_id == str(turn.id)
        and asset.generation_metadata.get("visual_role") == "video_primary"
    )
    primary.status = "failed"
    primary.generation_metadata = {
        **primary.generation_metadata,
        "status": "failed",
        "failure": "previous remote ComfyUI job failed",
    }
    repository.episode = episode

    summary = await run_visual_production_worker_once(
        repository=repository,
        comfyui_service=comfyui_service,
        batch_limit=10,
        user_id="test-worker",
    )

    repaired = next(asset for asset in repository.episode.assets if asset.id == primary.id)
    assert summary["episodes_scanned"] == 1
    assert summary["episodes_planned"] == 0
    assert summary["episodes_generated"] == 1
    assert summary["targeted_visual_assets"] >= 1
    assert summary["repair_visual_assets"] == 1
    assert summary["completed_visual_assets"] >= 1
    assert repaired.status == "completed"
    assert repaired.generation_metadata["generation_attempt_count"] == 1
    assert repaired.generation_metadata["render_ready"] is True
    assert "visual.assets.generated" in [
        event.event_type for event in repository.episode.audit_events
    ]


def test_visual_worker_leaves_non_retryable_failed_assets_for_manual_review() -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    episode.assets.extend(
        [
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.audio,
                language=transcript.language,
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                storage_uri="object://dialecticore/audio/turn.wav",
                mime_type="audio/wav",
                duration_ms=1200,
                checksum="sha256:audio",
                status="completed",
                generation_metadata={
                    "transcript_version_id": str(transcript.id),
                    "voice_profile_id": "voice-host",
                },
            ),
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.video,
                language=transcript.language,
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                status="failed",
                generation_metadata={
                    "transcript_version_id": str(transcript.id),
                    "visual_role": "video_primary",
                    "ready_for_retry": False,
                },
            ),
        ]
    )

    assert _visual_generation_target_asset_ids(episode, transcript) == []
    assert _target_transcript_needing_visuals(episode) is None


@pytest.mark.asyncio
async def test_comfyui_adapter_worker_syncs_submitted_remote_visual_jobs(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/history/visual-worker-job-1"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "prompt_id": "visual-worker-job-1",
                "image_base64": base64.b64encode(PNG_1X1).decode(),
                "mime_type": "image/png",
            },
        )

    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000010",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000011"],
        speaker_participant_id="host",
        text="Hello there",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Visual Worker",
        slug="visual-worker",
        subject="Visual Worker",
        central_question="How should planned ComfyUI assets be observed?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="broll",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                status="submitted",
                generation_metadata={
                    "visual_role": "broll",
                    "comfyui_endpoint_id": "comfyui-remote",
                    "comfyui_workflow_id": "workflow-topic-broll-v1",
                    "remote_job_id": "visual-worker-job-1",
                },
            )
        ],
    )
    repository = FakeComfyUiRepository(episode)

    summary = await run_comfyui_adapter_once(
        repository=repository,
        comfyui_service=ComfyUiService(
            Settings(object_storage_local_path=str(tmp_path / "object-store")),
            transport=httpx.MockTransport(handler),
        ),
        batch_limit=10,
        user_id="test-comfyui-worker",
    )

    synced_asset = repository.episode.assets[0]
    assert summary["episodes_scanned"] == 1
    assert summary["episodes_synced"] == 1
    assert summary["pending_visual_assets"] == 1
    assert summary["error_count"] == 0
    assert len(repository.saved) == 1
    assert synced_asset.status == "completed"
    assert synced_asset.storage_uri and synced_asset.storage_uri.startswith("object://")
    assert synced_asset.generation_metadata["media_probe"]["probe_tool"] == "image_header"
    assert synced_asset.generation_metadata["sync_attempt_count"] == 1
    assert summary["enabled_comfyui_endpoints"] == 1
    assert summary["enabled_comfyui_workflows"] == sum(
        workflow.enabled for workflow in default_comfyui_workflows()
    )


def test_timeline_worker_builds_only_when_media_prerequisites_are_ready(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    episode.assets.extend(
        [
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.audio,
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                storage_uri="object://dialecticore/audio/turn.wav",
                mime_type="audio/wav",
                duration_ms=1000,
                checksum="sha256:audio",
                status="completed",
                generation_metadata={"transcript_version_id": str(transcript.id)},
            ),
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.studio_scene,
                language="en",
                source_entity_type="episode",
                source_entity_id=str(episode.id),
                storage_uri="object://dialecticore/visuals/studio.png",
                mime_type="image/png",
                width=1920,
                height=1080,
                checksum="sha256:studio",
                status="completed",
                generation_metadata={
                    "transcript_version_id": str(transcript.id),
                    "visual_role": "studio_scene",
                    "render_ready": True,
                },
            ),
        ]
    )
    repository = FakeEpisodeRepository(episode)

    summary = run_timeline_worker_once(
        repository=repository,
        timeline_service=TimelineService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-timeline-worker",
    )

    assert summary["episodes_scanned"] == 1
    assert summary["timelines_built"] == 1
    assert summary["skipped_prerequisites"] == 0
    assert summary["error_count"] == 0
    assert len(repository.saved) == 1
    timeline_asset = next(
        asset for asset in repository.episode.assets if asset.asset_type == AssetType.timeline
    )
    assert timeline_asset.status == "completed"
    assert timeline_asset.source_entity_id == str(transcript.id)
    assert repository.episode.audit_events[-2].event_type == "timeline.qc.completed"


def test_timeline_worker_skips_until_audio_is_complete(tmp_path: Path) -> None:
    episode, transcript, _turn = worker_episode_with_transcript()
    episode.assets.append(
        Asset(
            episode_id=episode.id,
            asset_type=AssetType.studio_scene,
            language="en",
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            storage_uri="object://dialecticore/visuals/studio.png",
            mime_type="image/png",
            checksum="sha256:studio",
            status="completed",
            generation_metadata={
                "transcript_version_id": str(transcript.id),
                "visual_role": "studio_scene",
                "render_ready": True,
            },
        )
    )
    repository = FakeEpisodeRepository(episode)

    summary = run_timeline_worker_once(
        repository=repository,
        timeline_service=TimelineService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
    )

    assert summary["episodes_scanned"] == 1
    assert summary["timelines_built"] == 0
    assert summary["skipped_prerequisites"] == 1
    assert repository.saved == []


def test_render_worker_only_claims_explicitly_queued_requests() -> None:
    episode, transcript, _turn = worker_episode_with_transcript()
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        storage_uri="object://dialecticore/timelines/timeline.json",
        mime_type="application/vnd.dialecticore.timeline+json",
        checksum="sha256:timeline",
        status="completed",
    )
    episode.assets.append(timeline_asset)
    repository = FakeEpisodeRepository(episode)
    render_service = FakeRenderService()

    no_request = run_render_worker_once(
        repository=repository,
        render_service=render_service,
        batch_limit=10,
        user_id="test-render-worker",
    )
    assert no_request["preview_renders_created"] == 0
    assert repository.episode.assets == [timeline_asset]
    assert repository.saved == []

    render_service.enqueue_render(
        repository.episode,
        RenderRequest(
            timeline_asset_id=timeline_asset.id,
            render_type="preview",
            preset_id="preview-low-bitrate",
            user_id="tester",
        ),
        presets=[],
    )
    first = run_render_worker_once(
        repository=repository,
        render_service=render_service,
        batch_limit=10,
        user_id="test-render-worker",
    )
    second = run_render_worker_once(
        repository=repository,
        render_service=render_service,
        batch_limit=10,
        user_id="test-render-worker",
    )
    preview_render = next(
        asset
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.render
        and asset.generation_metadata.get("render_type") == "preview"
    )
    preview_approval = next(
        approval
        for approval in repository.episode.approvals
        if approval.stage == "preview_render_review"
        and approval.target_id == str(preview_render.id)
    )
    preview_approval.decision = "approved"
    preview_render.generation_metadata["approval_status"] = "approved"
    render_service.enqueue_render(
        repository.episode,
        RenderRequest(
            timeline_asset_id=timeline_asset.id,
            render_type="final",
            preset_id="youtube-1080p",
            user_id="tester",
        ),
        presets=[],
    )
    third = run_render_worker_once(
        repository=repository,
        render_service=render_service,
        batch_limit=10,
        user_id="test-render-worker",
    )
    fourth = run_render_worker_once(
        repository=repository,
        render_service=render_service,
        batch_limit=10,
        user_id="test-render-worker",
    )

    assert first["preview_renders_created"] == 1
    assert first["final_renders_created"] == 0
    assert second["preview_renders_created"] == 0
    assert second["final_renders_created"] == 0
    assert third["final_renders_created"] == 1
    assert fourth["skipped"] == 1
    render_types = [
        asset.generation_metadata["render_type"]
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.render
    ]
    assert render_types == ["preview", "final"]
    # The worker persists only the explicit request's claim and completion.
    assert len(repository.saved) == 4


def test_render_worker_can_process_explicit_preview_while_episode_is_paused() -> None:
    episode, transcript, _turn = worker_episode_with_transcript()
    episode.workflow_control = {"paused": True, "cancelled": False}
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        storage_uri="object://dialecticore/timelines/preview.json",
        mime_type="application/vnd.dialecticore.timeline+json",
        checksum="sha256:timeline-preview",
        status="completed",
    )
    episode.assets.append(timeline_asset)
    repository = FakeEpisodeRepository(episode)
    render_service = FakeRenderService()
    render_service.enqueue_render(
        repository.episode,
        RenderRequest(
            timeline_asset_id=timeline_asset.id,
            render_type="preview",
            preset_id="preview-low-bitrate",
            allow_paused_episode=True,
            user_id="tester",
        ),
        presets=[],
    )

    summary = run_render_worker_once(
        repository=repository,
        render_service=render_service,
        batch_limit=10,
        user_id="test-render-worker",
    )

    assert summary["preview_renders_created"] == 1
    preview = next(
        asset for asset in repository.episode.assets if asset.asset_type == AssetType.render
    )
    assert preview.status == "completed"
    assert repository.episode.workflow_control["paused"] is True


def test_publishing_worker_prepares_package_and_dry_run_publish() -> None:
    episode, _transcript, _turn = worker_episode_with_transcript()
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(uuid4()),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={"render_type": "final"},
    )
    episode.assets.append(render_asset)
    repository = FakePublishingRepository(episode)

    summary = run_publishing_worker_once(
        repository=repository,
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
        batch_limit=10,
        user_id="test-publishing-worker",
    )
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="tester",
        )
    )
    approved = run_publishing_worker_once(
        repository=repository,
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
        batch_limit=10,
        user_id="test-publishing-worker",
    )
    second = run_publishing_worker_once(
        repository=repository,
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
        batch_limit=10,
        user_id="test-publishing-worker",
    )

    assert summary["pending_final_render_approvals"] == 1
    assert summary["skipped"] == 1
    assert summary["error_count"] == 0
    assert approved["thumbnails_created"] == 1
    assert approved["youtube_packages_created"] == 1
    assert approved["production_manifests_created"] == 1
    assert approved["production_manifests_refreshed"] == 1
    assert approved["dry_run_publish_jobs_created"] == 1
    assert approved["live_publish_jobs_created"] == 0
    assert approved["automated_live_enabled"] is False
    assert approved["automated_live_capable_targets"] == 0
    assert approved["enabled_publisher_targets"] >= 1
    assert second["skipped"] == 1
    assert len(repository.saved) == 1
    assert any(asset.asset_type == AssetType.thumbnail for asset in repository.episode.assets)
    assert any(asset.asset_type == AssetType.export_package for asset in repository.episode.assets)
    assert any(
        asset.asset_type == AssetType.production_manifest for asset in repository.episode.assets
    )
    assert repository.episode.publish_jobs[0].dry_run is True
    manifest_assets = [
        asset
        for asset in repository.episode.assets
        if asset.asset_type == AssetType.production_manifest
    ]
    assert [asset.status for asset in manifest_assets] == ["replaced", "completed"]
    production_manifest = manifest_assets[-1].generation_metadata["production_manifest"]
    assert production_manifest["publish_jobs"][0]["id"] == str(
        repository.episode.publish_jobs[0].id
    )
    assert production_manifest["quality_results"][0]["target_id"] == str(
        repository.episode.publish_jobs[0].id
    )


@pytest.mark.asyncio
async def test_workflow_handoff_requires_final_render_approval_for_delivery_ready(
    tmp_path: Path,
) -> None:
    episode, transcript, turn = worker_episode_with_transcript()
    audio_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/audio/turn.wav",
        mime_type="audio/wav",
        duration_ms=1000,
        checksum="sha256:audio",
        status="completed",
        generation_metadata={"transcript_version_id": str(transcript.id)},
    )
    visual_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
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
        },
    )
    subtitle_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language="en",
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
        language="en",
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
    final_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={"render_type": "final", "timeline_asset_id": str(timeline_asset.id)},
    )
    thumbnail_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.thumbnail,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(final_render.id),
        storage_uri="object://dialecticore/thumbnails/final.jpg",
        mime_type="image/jpeg",
        checksum="sha256:thumbnail",
        status="completed",
        generation_metadata={"render_asset_id": str(final_render.id)},
    )
    package_included_files = [
        "youtube-package.json",
        "video/render.mp4",
        "thumbnail/thumbnail.jpg",
        "subtitles/en.vtt",
    ]
    package_manifest = {
        "thumbnail_asset_id": str(thumbnail_asset.id),
        "subtitles": [
            {
                "asset_id": str(subtitle_asset.id),
                "language": "en",
                "path": "subtitles/en.vtt",
            }
        ],
    }
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(final_render.id),
        storage_uri="object://dialecticore/exports/package.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
        generation_metadata={
            "render_asset_id": str(final_render.id),
            "thumbnail_asset_id": str(thumbnail_asset.id),
            "included_files": list(package_included_files),
            "youtube_package_manifest": deepcopy(package_manifest),
        },
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
                "delivery_package": {
                    "asset_id": str(package_asset.id),
                    "included_files": list(package_included_files),
                    "manifest": deepcopy(package_manifest),
                },
            }
        },
    )
    episode.assets.extend(
        [
            audio_asset,
            visual_asset,
            subtitle_asset,
            timeline_asset,
            final_render,
            thumbnail_asset,
            package_asset,
            manifest_asset,
        ]
    )
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
    production_control = ProductionControlService()
    episode = production_control.begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode, publisher_targets=[])

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )

    handoff = summary["production_handoffs"][0]
    assert handoff["status"] == "review_ready"
    assert handoff["blocking_reasons"] == []
    assert handoff["render"]["final_render_asset_id"] == str(final_render.id)
    assert handoff["render"]["final_render_approved"] is False
    assert handoff["render"]["final_render_qc_status"] == "pass"
    assert handoff["render"]["thumbnail_asset_id"] == str(thumbnail_asset.id)
    assert handoff["render"]["delivery_package_asset_id"] == str(package_asset.id)
    assert handoff["render"]["delivery_package_qc_status"] == "pass"
    assert handoff["render"]["delivery_package_thumbnail_included"] is True
    assert handoff["render"]["delivery_package_subtitles_included"] is True
    assert handoff["render"]["production_manifest_asset_id"] == str(manifest_asset.id)
    assert handoff["render"]["production_manifest_valid"] is True

    repository.episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(final_render.id),
            decision="approved",
            user_id="tester",
        )
    )
    approved_summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )

    approved_handoff = approved_summary["production_handoffs"][0]
    assert approved_handoff["status"] == "review_ready"
    assert approved_handoff["render"]["final_render_approved"] is True
    assert approved_handoff["publish"]["ready"] is False

    saved_package_asset = next(
        asset for asset in repository.episode.assets if asset.id == package_asset.id
    )
    saved_package_asset.generation_metadata["included_files"] = [
        name
        for name in saved_package_asset.generation_metadata["included_files"]
        if not name.startswith("subtitles/")
    ]
    saved_package_asset.generation_metadata["youtube_package_manifest"].pop(
        "subtitles", None
    )
    missing_package_subtitle_summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )
    missing_package_subtitle_handoff = missing_package_subtitle_summary[
        "production_handoffs"
    ][0]
    assert missing_package_subtitle_handoff["status"] == "blocked"
    assert "export_package_subtitles_missing" in missing_package_subtitle_handoff[
        "blocking_reasons"
    ]
    assert (
        missing_package_subtitle_handoff["render"]["delivery_package_subtitles_included"]
        is False
    )
    saved_package_asset = next(
        asset for asset in repository.episode.assets if asset.id == package_asset.id
    )
    saved_package_asset.generation_metadata["included_files"] = list(package_included_files)
    saved_package_asset.generation_metadata["youtube_package_manifest"] = deepcopy(
        package_manifest
    )

    saved_manifest_asset = next(
        asset for asset in repository.episode.assets if asset.id == manifest_asset.id
    )
    saved_manifest_asset.generation_metadata["production_manifest"]["delivery_package"][
        "manifest"
    ] = {"thumbnail_asset_id": str(thumbnail_asset.id)}
    missing_manifest_subtitle_summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )
    missing_manifest_subtitle_handoff = missing_manifest_subtitle_summary[
        "production_handoffs"
    ][0]
    assert missing_manifest_subtitle_handoff["status"] == "blocked"
    assert "production_manifest_invalid" in missing_manifest_subtitle_handoff[
        "blocking_reasons"
    ]
    assert missing_manifest_subtitle_handoff["render"]["production_manifest_valid"] is False
    assert missing_manifest_subtitle_handoff["render"][
        "production_manifest_invalid_reason"
    ] == "embedded delivery package subtitle manifest is missing"
    saved_manifest_asset = next(
        asset for asset in repository.episode.assets if asset.id == manifest_asset.id
    )
    saved_manifest_asset.generation_metadata["production_manifest"]["delivery_package"][
        "manifest"
    ] = deepcopy(package_manifest)

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
    repository.episode.publish_jobs.append(publish_job)
    missing_publish_qc_summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )

    missing_publish_qc_handoff = missing_publish_qc_summary["production_handoffs"][0]
    assert missing_publish_qc_handoff["status"] == "blocked"
    assert "publish_delivery_qc_missing" in missing_publish_qc_handoff["blocking_reasons"]
    assert missing_publish_qc_handoff["publish"]["ready"] is False

    publish_qc = QualityResult(
        episode_id=episode.id,
        target_type="publish_job",
        target_id=str(publish_job.id),
        check_type="publish_delivery_integrity",
        severity=QualitySeverity.warning,
        status="warning",
        score=1.0,
        details={"dry_run": True, "warning_count": 1, "failure_count": 0},
    )
    repository.episode.quality_results.append(publish_qc)
    stale_manifest_summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )

    stale_manifest_handoff = stale_manifest_summary["production_handoffs"][0]
    assert stale_manifest_handoff["status"] == "blocked"
    assert "production_manifest_publish_evidence_missing" in stale_manifest_handoff[
        "blocking_reasons"
    ]
    assert stale_manifest_handoff["render"]["production_manifest_publish_evidence_valid"] is False

    saved_manifest_asset = next(
        asset for asset in repository.episode.assets if asset.id == manifest_asset.id
    )
    saved_manifest_asset.generation_metadata["production_manifest"]["publish_jobs"] = [
        {
            "id": str(publish_job.id),
            "package_asset_id": str(package_asset.id),
            "status": publish_job.status,
        }
    ]
    saved_manifest_asset.generation_metadata["production_manifest"]["quality_results"] = [
        {
            "id": str(publish_qc.id),
            "target_type": "publish_job",
            "target_id": str(publish_job.id),
            "check_type": "publish_delivery_integrity",
            "status": publish_qc.status,
        }
    ]
    published_summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )

    published_handoff = published_summary["production_handoffs"][0]
    assert published_handoff["status"] == "delivery_ready"
    assert published_handoff["publish"]["ready"] is True
    assert published_handoff["publish"]["publish_job_status"] == "completed"

    repository.episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="publish_job",
            target_id=str(publish_job.id),
            check_type="publish_delivery_integrity",
            severity=QualitySeverity.fail,
            status="fail",
            score=0.0,
            details={"dry_run": False, "warning_count": 0, "failure_count": 1},
        )
    )
    blocked_publish_summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )

    blocked_publish_handoff = blocked_publish_summary["production_handoffs"][0]
    assert blocked_publish_handoff["status"] == "blocked"
    assert "publish_delivery_qc_failed" in blocked_publish_handoff["blocking_reasons"]
    assert blocked_publish_handoff["publish"]["publish_delivery_qc_status"] == "fail"

    repository.episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.fail,
            status="fail",
            score=0.0,
        )
    )
    blocked_summary = await run_workflow_worker_once(
        repository=repository,
        settings=Settings(object_storage_local_path=str(tmp_path / "objects")),
        production_control=production_control,
        batch_limit=10,
    )

    blocked_handoff = blocked_summary["production_handoffs"][0]
    assert blocked_handoff["status"] == "blocked"
    assert "export_package_qc_failed" in blocked_handoff["blocking_reasons"]
    assert blocked_handoff["render"]["delivery_package_qc_status"] == "fail"


def test_publishing_worker_can_opt_into_live_automated_publish() -> None:
    episode, _transcript, _turn = worker_episode_with_transcript()
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(uuid4()),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={"render_type": "final"},
    )
    episode.assets.append(render_asset)
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="tester",
        )
    )
    live_target = PublisherTarget(
        id="generic-live",
        name="Generic Live",
        platform="generic",
        adapter_type="generic_http",
        base_url="https://publisher.example.test",
        enabled=True,
        capabilities={
            "automated_live_publish": True,
            "metadata_upload": True,
            "package_upload": True,
        },
    )
    publisher_service = FakePublisherService()
    repository = FakePublishingRepository(episode, publisher_targets=[live_target])

    summary = run_publishing_worker_once(
        repository=repository,
        render_service=FakeRenderService(),
        publisher_service=publisher_service,
        batch_limit=10,
        user_id="test-publishing-worker",
        automated_live_enabled=True,
    )

    assert summary["thumbnails_created"] == 1
    assert summary["youtube_packages_created"] == 1
    assert summary["production_manifests_created"] == 1
    assert summary["production_manifests_refreshed"] == 1
    assert summary["dry_run_publish_jobs_created"] == 0
    assert summary["live_publish_jobs_created"] == 1
    assert summary["automated_live_enabled"] is True
    assert summary["automated_live_capable_targets"] == 1
    assert summary["enabled_publisher_targets"] == 1
    assert publisher_service.requests[0].publisher_target_id == "generic-live"
    assert publisher_service.requests[0].dry_run is False
    assert repository.episode.publish_jobs[0].dry_run is False
    assert len(repository.saved) == 1


def test_publishing_worker_reports_package_qc_blocked_manifest_handoff() -> None:
    episode, _transcript, _turn = worker_episode_with_transcript()
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(uuid4()),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={"render_type": "final"},
    )
    thumbnail_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.thumbnail,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/thumbnails/thumb.jpg",
        mime_type="image/jpeg",
        checksum="sha256:thumbnail",
        status="completed",
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
        generation_metadata={
            "render_asset_id": str(render_asset.id),
            "thumbnail_asset_id": str(thumbnail_asset.id),
        },
    )
    episode.assets.extend([render_asset, thumbnail_asset, package_asset])
    episode.approvals.append(
        Approval(
            episode_id=episode.id,
            stage="final_render_review",
            target_type="render_asset",
            target_id=str(render_asset.id),
            decision="approved",
            user_id="tester",
        )
    )
    repository = FakePublishingRepository(episode)

    summary = run_publishing_worker_once(
        repository=repository,
        render_service=PackageQcBlockedRenderService(),
        publisher_service=FakePublisherService(),
        batch_limit=10,
        user_id="test-publishing-worker",
    )

    assert summary["production_manifests_created"] == 0
    assert summary["dry_run_publish_jobs_created"] == 0
    assert summary["package_qc_blocked_handoffs"] == 1
    assert summary["production_manifest_blocked_handoffs"] == 0
    assert summary["error_count"] == 1
    assert summary["errors"][0]["error_kind"] == "package_qc_blocked"
    assert summary["errors"][0]["episode_id"] == str(episode.id)
    assert repository.saved == []
    assert repository.episode.publish_jobs == []


def test_publishing_worker_reports_production_manifest_blocked_publish_handoff() -> None:
    episode, _transcript, _turn = worker_episode_with_transcript()
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(uuid4()),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:final",
        status="completed",
        generation_metadata={"render_type": "final"},
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
        checksum="sha256:production-manifest",
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
            user_id="tester",
        )
    )
    live_target = PublisherTarget(
        id="generic-live",
        name="Generic Live",
        platform="generic",
        adapter_type="generic_http",
        base_url="https://publisher.example.test",
        enabled=True,
        capabilities={
            "automated_live_publish": True,
            "metadata_upload": True,
            "package_upload": True,
        },
    )
    repository = FakePublishingRepository(episode, publisher_targets=[live_target])

    summary = run_publishing_worker_once(
        repository=repository,
        render_service=FakeRenderService(),
        publisher_service=ProductionManifestBlockedPublisherService(),
        batch_limit=10,
        user_id="test-publishing-worker",
        automated_live_enabled=True,
    )

    assert summary["production_manifests_created"] == 0
    assert summary["live_publish_jobs_created"] == 0
    assert summary["package_qc_blocked_handoffs"] == 0
    assert summary["production_manifest_blocked_handoffs"] == 1
    assert summary["error_count"] == 1
    assert summary["errors"][0]["error_kind"] == "production_manifest_blocked"
    assert summary["errors"][0]["episode_id"] == str(episode.id)
    assert repository.saved == []
    assert repository.episode.publish_jobs == []


def test_research_worker_builds_enabled_episode_evidence_pack(tmp_path: Path) -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeEpisodeRepository(episode)

    summary = run_research_worker_once(
        repository=repository,
        research_service=ResearchService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-research-worker",
    )
    second = run_research_worker_once(
        repository=repository,
        research_service=ResearchService(
            Settings(object_storage_local_path=str(tmp_path / "object-store"))
        ),
        batch_limit=10,
        user_id="test-research-worker",
    )

    assert summary["evidence_packs_built"] == 1
    assert summary["error_count"] == 0
    assert second["skipped"] == 1
    assert len(repository.saved) == 1
    assert repository.episode.status == EpisodeStatus.research_review
    assert any(asset.asset_type == AssetType.evidence_pack for asset in repository.episode.assets)
    assert repository.episode.audit_events[-1].event_type == "research.qc.completed"


@pytest.mark.asyncio
async def test_workflow_worker_builds_research_but_respects_approval_gate(
    tmp_path: Path,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        worker_auto_start_production_runs_enabled=True,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeWorkflowRepository(episode)

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["schema_version"] == "workflow_worker_orchestration_summary.v1"
    assert UUID(summary["orchestration_attempt_id"])
    assert summary["policy"] == "local_stage_worker_orchestrator_v1"
    assert summary["workflow_run_starts"]["workflow_runs_started"] == 1
    assert summary["workflow_run_starts"]["error_count"] == 0
    assert summary["stages"]["research"]["evidence_packs_built"] == 1
    assert summary["stages"]["discussion"]["discussions_completed"] == 0
    assert summary["stages"]["discussion"]["skipped"] == 1
    assert summary["progressed_stage_count"] == 1
    assert summary["error_count"] == 0
    assert repository.episode.status == EpisodeStatus.research_review
    run = repository.episode.workflow_control["run"]
    assert run["state"] == "running"
    assert run["started_by"] == "workflow-worker"
    assert [entry["stage"] for entry in run["stage_history"]] == [
        "DRAFT",
        "RESEARCHING",
        "RESEARCH_REVIEW",
    ]
    assert repository.episode.approvals[-1].stage == "research_review"
    assert repository.episode.approvals[-1].decision == "pending"
    assert repository.episode.discussion_session is None


@pytest.mark.asyncio
async def test_discussion_worker_starts_ready_episode_with_workflow_run() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeEpisodeRepository(episode)
    settings = Settings(worker_auto_start_production_runs_enabled=True)

    summary = await run_discussion_worker_once(
        repository=repository,
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        batch_limit=10,
    )
    second = await run_discussion_worker_once(
        repository=repository,
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        batch_limit=10,
    )

    assert summary["discussions_completed"] == 1
    assert summary["error_count"] == 0
    assert second["skipped"] == 1
    assert len(repository.saved) == 1
    assert repository.episode.discussion_session is not None
    assert repository.episode.status == EpisodeStatus.transcript_review
    assert repository.episode.workflow_control["run"]["state"] == "running"


@pytest.mark.asyncio
async def test_discussion_worker_blocks_incomplete_model_configuration() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.participants[0].model_id = ""
    episode.participants[1].model_endpoint_id = "missing-provider"
    episode.model_endpoints[0].enabled = False
    repository = FakeEpisodeRepository(episode)
    settings = Settings(worker_auto_start_production_runs_enabled=True)

    summary = await run_discussion_worker_once(
        repository=repository,
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        batch_limit=10,
    )

    assert summary["discussions_completed"] == 0
    assert summary["model_configuration_blocked"] == 1
    assert summary["error_count"] == 0
    assert summary["skipped"] == 1
    assert repository.saved == []
    assert repository.episode.discussion_session is None
    assert repository.episode.status == EpisodeStatus.draft
    error = summary["errors"][0]
    assert error["error_kind"] == "discussion_model_configuration_blocked"
    assert error["model_configuration"] == {
        "schema_version": "discussion_model_configuration.v1",
        "ready": False,
        "active_participant_count": 4,
        "configured_model_participant_count": 0,
        "missing_model_participant_ids": ["host"],
        "unknown_model_endpoint_participant_ids": ["optimist"],
        "disabled_model_endpoint_participant_ids": [
            "host",
            "skeptic",
            "practitioner",
        ],
    }


def test_discussion_worker_allows_empty_failed_discussing_session_retry() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.status = EpisodeStatus.discussing
    episode.discussion_session = DiscussionSession(
        episode_id=episode.id,
        status="running",
    )

    assert _discussion_worker_can_start(episode) is True


def test_discussion_worker_allows_discussing_without_session_retry() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.status = EpisodeStatus.discussing
    episode.discussion_session = None

    assert _discussion_worker_can_start(episode) is True


def test_discussion_worker_does_not_retry_partial_discussing_session() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.status = EpisodeStatus.discussing
    episode.discussion_session = DiscussionSession(
        episode_id=episode.id,
        status="running",
    )
    episode.discussion_session.turns.append(
        DiscussionTurn(
            discussion_session_id=episode.discussion_session.id,
            sequence_number=1,
            speaker_participant_id="host",
            turn_type=TurnType.host_opening,
            spoken_text="Welcome.",
            intent="open",
            estimated_duration_seconds=1,
            structured_output=StructuredTurnOutput(
                spoken_text="Welcome.",
                intent="open",
            ),
            raw_provider_response={},
            generation_metadata={},
        )
    )

    assert _discussion_worker_can_start(episode) is False


@pytest.mark.asyncio
async def test_discussion_engine_rejects_incomplete_model_configuration() -> None:
    settings = Settings()
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.participants[0].model_id = ""
    episode.participants[1].model_endpoint_id = "missing-provider"
    episode.model_endpoints[0].enabled = False

    with pytest.raises(ValueError, match="participant model configuration is incomplete"):
        await DiscussionEngine(ModelGateway(), settings).run(episode)

    assert episode.discussion_session is None
    assert episode.status == EpisodeStatus.draft


@pytest.mark.asyncio
async def test_discussion_worker_blocks_rejected_required_research_approval(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode = ResearchService(settings).build_evidence_pack(
        episode,
        ResearchBuildRequest(user_id="researcher"),
    )
    approval = next(item for item in episode.approvals if item.stage == "research_review")
    approval.decision = "rejected"
    episode.status = EpisodeStatus.draft
    repository = FakeEpisodeRepository(episode)

    summary = await run_discussion_worker_once(
        repository=repository,
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        batch_limit=10,
    )

    assert summary["discussions_completed"] == 0
    assert summary["skipped"] == 1
    assert summary["error_count"] == 0
    assert repository.episode.discussion_session is None
    assert repository.saved == []


def test_active_workflow_repository_preserves_cancellation_over_stale_stage_save() -> None:
    production_control = ProductionControlService(Settings())
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    running = production_control.begin_run(episode, user_id="tester")
    repository = FakeEpisodeRepository(running)
    active_repository = _ActiveWorkflowRunRepository(repository)
    stale_stage_update = deepcopy(running)
    stale_stage_update.status = EpisodeStatus.transcript_review

    cancelled = production_control.cancel(
        running,
        WorkflowActionRequest(
            action="cancel",
            user_id="producer",
            comment="Cancel while a long-running stage is still executing.",
        ),
    )
    repository.episode = cancelled
    saved = active_repository.save(stale_stage_update)

    assert saved.status == EpisodeStatus.cancelled
    assert repository.episode.status == EpisodeStatus.cancelled
    assert repository.episode.workflow_control["cancelled"] is True
    assert repository.episode.workflow_control["run"]["state"] == "cancelled"
    assert repository.saved == []


def test_active_workflow_repository_preserves_pending_canonical_transcript_review() -> None:
    production_control = ProductionControlService(Settings())
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    running = production_control.begin_run(episode, user_id="tester")
    previous_canonical = TranscriptVersion(
        episode_id=running.id,
        type=TranscriptType.broadcast,
        language=running.source_language,
        status="approved",
    )
    running.transcripts.append(previous_canonical)
    running.canonical_transcript_version_id = previous_canonical.id
    stale_stage_update = deepcopy(running)
    pending_canonical = TranscriptVersion(
        episode_id=running.id,
        type=TranscriptType.broadcast,
        language=running.source_language,
        status="pending_review",
    )
    running.transcripts.append(pending_canonical)
    running.canonical_transcript_version_id = pending_canonical.id
    running.status = EpisodeStatus.transcript_review
    repository = FakeEpisodeRepository(running)
    active_repository = _ActiveWorkflowRunRepository(repository)
    stale_stage_update.status = EpisodeStatus.ready

    saved = active_repository.save(stale_stage_update)

    assert active_repository.list() == []
    assert saved.status == EpisodeStatus.transcript_review
    assert repository.episode.status == EpisodeStatus.transcript_review
    assert repository.saved == []


@pytest.mark.asyncio
async def test_workflow_worker_starts_eligible_discussion_run(tmp_path: Path) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        worker_auto_start_production_runs_enabled=True,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeWorkflowRepository(episode)

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

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
    assert summary["stages"]["research"]["skipped"] == 1
    assert summary["workflow_run_starts"]["workflow_runs_started"] == 1
    assert summary["stages"]["discussion"]["discussions_completed"] == 1
    assert summary["progressed_stage_count"] == 1
    assert summary["error_count"] == 0
    assert UUID(summary["orchestration_attempt_id"])
    assert len(repository.saved) == 3
    assert summary["orchestration_records"][0]["episode_id"] == str(repository.episode.id)
    assert summary["orchestration_records"][0]["attempt_sequence"] == 1
    assert repository.episode.discussion_session is not None
    assert repository.episode.status == EpisodeStatus.transcript_review
    assert repository.episode.workflow_control["run"]["state"] == "running"
    assert repository.episode.workflow_control["run"]["started_by"] == "workflow-worker"
    assert len(
        [
            event
            for event in repository.episode.workflow_control["workflow_event_log"]
            if event["event_type"] == "workflow.run.started"
        ]
    ) == 1
    orchestration_log = repository.episode.workflow_control["worker_orchestration_log"]
    assert orchestration_log[0]["schema_version"] == ("workflow_worker_orchestration_attempt.v1")
    assert orchestration_log[0]["summary_id"] == summary["orchestration_attempt_id"]
    assert orchestration_log[0]["stage_attempts"][1]["stage"] == "discussion"
    assert orchestration_log[0]["stage_attempts"][1]["status"] == "progressed"
    discussion_manifest = orchestration_log[0]["stage_attempts"][1]["stage_manifest"]
    assert discussion_manifest["schema_version"] == "workflow_stage_manifest.v1"
    assert discussion_manifest["stage"] == "discussion"
    assert discussion_manifest["status"] == "progressed"
    assert discussion_manifest["progress_metrics"]["discussions_completed"] == 1
    assert discussion_manifest["manifest_checksum"].startswith("sha256:")
    assert repository.episode.workflow_control["run"]["last_worker_orchestration"][
        "summary_checksum"
    ].startswith("sha256:")
    replay = ProductionControlService(settings).replay_workflow(repository.episode)
    assert replay["status"] == "pass"


@pytest.mark.asyncio
async def test_workflow_worker_does_not_auto_start_draft_runs_by_default(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeWorkflowRepository(episode)

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["workflow_run_starts"]["auto_start_disabled"] is True
    assert summary["workflow_run_starts"]["workflow_runs_started"] == 0
    assert summary["stages"]["discussion"]["discussions_completed"] == 0
    assert summary["stages"]["discussion"]["auto_start_disabled"] == 0
    assert summary["stages"]["discussion"]["episodes_scanned"] == 0
    assert summary["workflow_admission"]["missing_run_episode_ids"] == [str(episode.id)]
    assert summary["progressed_stage_count"] == 0
    assert repository.episode.workflow_control.get("run") is None
    assert repository.episode.discussion_session is None
    assert repository.episode.status == EpisodeStatus.draft


@pytest.mark.asyncio
async def test_workflow_worker_requires_running_run_before_media_stages(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode, _transcript, _turn = worker_episode_with_transcript()
    repository = FakeWorkflowRepository(episode)

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["workflow_run_starts"]["auto_start_disabled"] is True
    assert summary["workflow_admission"] == {
        "schema_version": "workflow_stage_admission_summary.v1",
        "episodes_scanned": 1,
        "active_run_episode_count": 0,
        "missing_run_episode_count": 1,
        "blocked_episode_count": 0,
        "active_run_episode_ids": [],
        "missing_run_episode_ids": [str(episode.id)],
        "blocked_episode_ids": [],
        "stage_execution_requires_running_workflow_run": True,
    }
    assert summary["stages"]["audio"]["episodes_scanned"] == 0
    assert summary["stages"]["audio"]["episodes_generated"] == 0
    assert summary["stages"]["visuals"]["episodes_scanned"] == 0
    assert summary["stages"]["timeline"]["episodes_scanned"] == 0
    assert summary["progressed_stage_count"] == 0
    assert repository.saved == []
    assert repository.episode.assets == []
    assert repository.episode.workflow_control.get("run") is None


@pytest.mark.asyncio
async def test_discussion_worker_does_not_auto_start_draft_runs_by_default(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeEpisodeRepository(episode)

    summary = await run_discussion_worker_once(
        repository=repository,
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        batch_limit=10,
    )

    assert summary["discussions_completed"] == 0
    assert summary["auto_start_disabled"] == 1
    assert summary["skipped"] == 1
    assert repository.episode.workflow_control.get("run") is None
    assert repository.episode.discussion_session is None
    assert repository.saved == []


@pytest.mark.asyncio
async def test_workflow_worker_records_idle_pass_without_advancing_paused_run(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    control = ProductionControlService(settings)
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode = control.begin_run(episode, user_id="producer-1")
    episode = control.pause(
        episode,
        WorkflowActionRequest(
            action="pause",
            user_id="producer-1",
            comment="pause before automated orchestration",
        ),
    )
    repository = FakeWorkflowRepository(episode)

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=control,
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["progressed_stage_count"] == 0
    assert summary["error_count"] == 0
    assert summary["automatic_stage_retry_count"] == 0
    assert summary["workflow_admission"]["blocked_episode_count"] == 1
    assert summary["workflow_admission"]["blocked_episode_ids"] == [str(repository.episode.id)]
    assert {
        stage: stage_summary["episodes_scanned"]
        for stage, stage_summary in summary["stages"].items()
    } == {stage: 0 for stage in summary["stage_order"]}
    assert summary["orchestration_records"][0]["episode_id"] == str(repository.episode.id)
    assert repository.episode.discussion_session is None
    assert repository.episode.status == EpisodeStatus.draft
    assert repository.episode.workflow_control["paused"] is True
    assert repository.episode.workflow_control["run"]["state"] == "running"
    assert repository.episode.workflow_control["worker_orchestration_log"][0][
        "progressed_stage_count"
    ] == 0


@pytest.mark.asyncio
async def test_workflow_worker_records_external_temporal_stage_dispatches(
    tmp_path: Path,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        temporal_backend_mode="external",
        temporal_backend_address="temporal:7233",
        temporal_backend_worker_enabled=True,
        temporal_namespace="dialecticore",
        temporal_task_queue="production",
        worker_auto_start_production_runs_enabled=True,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeWorkflowRepository(episode)

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["orchestration_records"][0]["attempt_sequence"] == 1
    control = repository.episode.workflow_control
    dispatches = control["temporal_stage_dispatch_log"]
    assert len(dispatches) == len(summary["stage_order"])
    assert [dispatch["dispatch_sequence"] for dispatch in dispatches] == list(
        range(1, len(dispatches) + 1)
    )
    assert {dispatch["status"] for dispatch in dispatches} == {"ready"}
    assert {dispatch["namespace"] for dispatch in dispatches} == {"dialecticore"}
    assert {dispatch["task_queue"] for dispatch in dispatches} == {"production"}
    assert dispatches[1]["stage"] == "discussion"
    assert dispatches[1]["activity_name"] == "dialecticore.production.discussion"
    assert dispatches[1]["target_stage"] == "DISCUSSING"
    assert dispatches[1]["idempotency_key"].startswith("sha256:")
    assert dispatches[1]["stage_attempt"]["status"] == "progressed"
    run = control["run"]
    assert run["last_worker_orchestration"]["temporal_dispatch_count"] == len(
        summary["stage_order"]
    )
    assert run["last_temporal_stage_dispatch"]["ready_count"] == len(summary["stage_order"])
    event_types = [event["event_type"] for event in control["workflow_event_log"]]
    assert "workflow.temporal.stage_dispatch_recorded" in event_types
    replay = ProductionControlService(settings).replay_workflow(repository.episode)
    assert replay["status"] == "pass"


@pytest.mark.asyncio
async def test_temporal_worker_blocks_without_external_runtime_settings(
    tmp_path: Path,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        temporal_backend_mode="external",
        temporal_backend_worker_enabled=False,
        temporal_task_queue=None,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeWorkflowRepository(episode)

    summary = await run_temporal_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["schema_version"] == "temporal_worker_execution_summary.v1"
    assert summary["status"] == "blocked"
    assert summary["progressed_stage_count"] == 0
    assert summary["orchestration_records"] == []
    assert summary["missing"] == [
        "DIALECTICORE_TEMPORAL_BACKEND_ADDRESS",
        "DIALECTICORE_TEMPORAL_TASK_QUEUE",
        "DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED",
    ]
    assert "temporal_stage_dispatch_log" not in repository.episode.workflow_control


@pytest.mark.asyncio
async def test_temporal_worker_executes_external_stage_activities(
    tmp_path: Path,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        temporal_backend_mode="external",
        temporal_backend_address="temporal:7233",
        temporal_backend_worker_enabled=True,
        temporal_namespace="dialecticore",
        temporal_task_queue="production",
        worker_auto_start_production_runs_enabled=True,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    repository = FakeWorkflowRepository(episode)

    summary = await run_temporal_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["schema_version"] == "temporal_worker_execution_summary.v1"
    assert UUID(summary["orchestration_attempt_id"])
    assert summary["policy"] == "external_temporal_stage_activity_worker_v1"
    assert summary["status"] == "running"
    assert summary["activity_order"] == [
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
    assert summary["activities"]["discussion"]["schema_version"] == (
        "temporal_stage_activity_execution.v1"
    )
    assert summary["activities"]["discussion"]["activity_name"] == (
        "dialecticore.production.discussion"
    )
    assert summary["activities"]["discussion"]["status"] == "progressed"
    assert summary["progressed_stage_count"] == 1
    assert summary["orchestration_records"][0]["episode_id"] == str(repository.episode.id)
    orchestration = repository.episode.workflow_control["worker_orchestration_log"][0]
    assert orchestration["summary_id"] == summary["orchestration_attempt_id"]
    assert orchestration["worker_id"] == "temporal-worker"
    assert orchestration["policy"] == "external_temporal_stage_activity_worker_v1"
    dispatches = repository.episode.workflow_control["temporal_stage_dispatch_log"]
    assert len(dispatches) == len(summary["activity_order"])
    assert {dispatch["status"] for dispatch in dispatches} == {"ready"}
    assert dispatches[1]["requested_by"] == "temporal-worker"
    assert dispatches[1]["stage"] == "discussion"
    replay = ProductionControlService(settings).replay_workflow(repository.episode)
    assert replay["status"] == "pass"


@pytest.mark.asyncio
async def test_workflow_worker_records_stage_retry_queue_for_failed_activity(
    tmp_path: Path,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        workflow_stage_retry_max_attempts=2,
        workflow_stage_retry_backoff_seconds=5,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_localized_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language="en",
        status="approved",
    )
    transcript.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=[uuid4()],
            speaker_participant_id="host",
            text="Hello there",
            status="accepted",
        )
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    episode = ProductionControlService(settings).begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode)

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=FailingLocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["stages"]["localization"]["error_count"] == 1
    assert summary["error_count"] == 1
    assert summary["orchestration_records"][0]["retry_queue_count"] == 1
    retry = repository.episode.workflow_control["stage_retry_queue"][0]
    assert retry["schema_version"] == "workflow_stage_retry.v1"
    assert retry["stage"] == "localization"
    assert retry["target_stage"] == "LOCALIZING"
    assert retry["status"] == "scheduled"
    assert retry["attempt_number"] == 1
    assert retry["max_attempts"] == 2
    assert retry["backoff_seconds"] == 5
    assert retry["error"] == "translation provider unavailable"
    assert repository.episode.workflow_control["failed_stage"] == "LOCALIZING"
    stage_plan = repository.episode.workflow_control["run"]["stage_plan"]
    localization_stage = next(item for item in stage_plan if item["stage"] == "LOCALIZING")
    assert localization_stage["failure_count"] == 1
    assert localization_stage["retry_status"] == "scheduled"
    assert repository.episode.workflow_control["worker_orchestration_log"][0]["error_count"] == 1


@pytest.mark.asyncio
async def test_workflow_worker_applies_due_stage_retry_before_stage_chain(
    tmp_path: Path,
) -> None:
    settings = Settings(
        object_storage_local_path=str(tmp_path / "object-store"),
        workflow_stage_retry_max_attempts=2,
        workflow_stage_retry_backoff_seconds=1,
    )
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_localized_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language="en",
        status="approved",
    )
    transcript.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=[uuid4()],
            speaker_participant_id="host",
            text="Hello there",
            status="accepted",
        )
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    service = ProductionControlService(settings)
    episode = service.begin_run(episode, user_id="tester")
    episode.status = EpisodeStatus.failed
    due_at = datetime.now(UTC) - timedelta(seconds=5)
    later_at = datetime.now(UTC) + timedelta(minutes=5)
    episode.workflow_control = {
        **episode.workflow_control,
        "failed_stage": EpisodeStatus.localizing.value,
        "stage_retry_queue": [
            {
                "schema_version": "workflow_stage_retry.v1",
                "retry_id": "retry-localization-due",
                "stage": "localization",
                "target_stage": EpisodeStatus.localizing.value,
                "source_summary_id": "summary-localization-1",
                "attempt_number": 1,
                "max_attempts": 2,
                "status": "scheduled",
                "created_at": (due_at - timedelta(seconds=1)).isoformat(),
                "next_retry_not_before": due_at.isoformat(),
                "backoff_seconds": 1,
                "error": "translation provider unavailable",
            },
            {
                "schema_version": "workflow_stage_retry.v1",
                "retry_id": "retry-localization-backoff",
                "stage": "localization",
                "target_stage": EpisodeStatus.localizing.value,
                "source_summary_id": "summary-localization-2",
                "attempt_number": 2,
                "max_attempts": 2,
                "status": "scheduled",
                "created_at": datetime.now(UTC).isoformat(),
                "next_retry_not_before": later_at.isoformat(),
                "backoff_seconds": 300,
                "error": "translation provider still unavailable",
            },
        ],
    }
    repository = FakeWorkflowRepository(episode)

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        research_service=ResearchService(settings),
        discussion_engine=DiscussionEngine(ModelGateway(), settings),
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["automatic_stage_retry_count"] == 1
    retry_record = summary["automatic_stage_retries"][0]
    assert retry_record["retry_id"] == "retry-localization-due"
    assert retry_record["target_stage"] == EpisodeStatus.localizing.value
    assert summary["stages"]["localization"]["localized_languages_created"] == 1
    retry_queue = repository.episode.workflow_control["stage_retry_queue"]
    assert retry_queue[0]["status"] == "automatic_retried"
    assert retry_queue[0]["previous_status"] == "scheduled"
    assert retry_queue[1]["status"] == "scheduled"
    assert repository.episode.workflow_control["last_stage_retry_resolution"][
        "resolution"
    ] == "automatic_retried"
    assert repository.episode.workflow_control["automatic_retry_count"] == 1
    event_types = [
        event["event_type"] for event in repository.episode.workflow_control["workflow_event_log"]
    ]
    assert "workflow.stage_retry.automatic_retry_requested" in event_types
    assert "workflow.stage_retry.resolved" in event_types
    assert "workflow.retry_automatic_requested" in [
        event.event_type for event in repository.episode.audit_events
    ]


def test_localization_worker_creates_missing_language_variants() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_localized_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language="en",
        status="approved",
    )
    transcript.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=[uuid4()],
            speaker_participant_id="host",
            text="Hello there",
            status="accepted",
        )
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    repository = FakeEpisodeRepository(episode)

    summary = run_localization_worker_once(
        repository=repository,
        localization_service=LocalizationService(),
        batch_limit=10,
        user_id="test-localization-worker",
    )
    second = run_localization_worker_once(
        repository=repository,
        localization_service=LocalizationService(),
        batch_limit=10,
        user_id="test-localization-worker",
    )

    assert summary["episodes_localized"] == 1
    assert summary["localized_languages_created"] == 1
    assert second["skipped"] == 1
    assert len(repository.saved) == 1
    localized = [
        item for item in repository.episode.transcripts if item.type == TranscriptType.localized
    ]
    assert [item.language for item in localized] == ["de"]
    assert localized[0].localization_metadata["mode"] == "literal"
    assert localized[0].localization_metadata["source_bound"] is True
    assert localized[0].localization_metadata["supports_dubbing"] is True
    assert localized[0].localization_metadata["supports_video_reperformance"] is False
    localized_approval = next(
        approval
        for approval in repository.episode.approvals
        if approval.stage == "localized_transcript_review"
    )
    assert localized_approval.target_type == "transcript_version"
    assert localized_approval.target_id == str(localized[0].id)
    assert localized_approval.decision == "pending"
    assert localized[0].status == "pending_review"


@pytest.mark.asyncio
async def test_workflow_handoff_blocks_pending_configured_localized_output(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_localized_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language="en",
        status="approved",
    )
    transcript.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=[uuid4()],
            speaker_participant_id="host",
            text="Hello there",
            status="accepted",
        )
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    episode = ProductionControlService(settings).begin_run(episode, user_id="tester")
    repository = FakeWorkflowRepository(episode, publisher_targets=[])

    summary = await run_workflow_worker_once(
        repository=repository,
        settings=settings,
        batch_limit=10,
        production_control=ProductionControlService(settings),
        localization_service=LocalizationService(),
        voicebox_service=VoiceboxService(settings),
        comfyui_service=ComfyUiService(settings),
        timeline_service=TimelineService(settings),
        render_service=FakeRenderService(),
        publisher_service=FakePublisherService(),
    )

    assert summary["stages"]["localization"]["localized_languages_created"] == 1
    handoff = summary["production_handoffs"][0]
    assert handoff["localized_outputs"]["schema_version"] == "localized_output_handoff.v1"
    assert handoff["localized_outputs"]["required"] is True
    assert handoff["localized_outputs"]["required_language_count"] == 1
    assert handoff["localized_outputs"]["approved_language_count"] == 0
    assert handoff["localized_outputs"]["missing_languages"] == []
    assert handoff["localized_outputs"]["not_approved_languages"] == ["de"]
    assert handoff["localized_outputs"]["qc_missing_languages"] == []
    assert handoff["localized_outputs"]["qc_failing_languages"] == []
    assert handoff["localized_outputs"]["outputs"][0]["language"] == "de"
    assert handoff["localized_outputs"]["outputs"][0]["transcript_status"] == (
        "pending_review"
    )
    assert handoff["localized_outputs"]["outputs"][0]["qc_status"] == "pass"
    assert "localized_output_not_approved" in handoff["blocking_reasons"]
    assert repository.episode.discussion_session is None


@pytest.mark.asyncio
async def test_qc_worker_runs_claim_citation_qc_before_transcript_approval(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode = ResearchService(settings).build_evidence_pack(episode, ResearchBuildRequest())
    episode.approvals[-1].decision = "approved"
    episode.status = EpisodeStatus.draft
    episode = await DiscussionEngine(ModelGateway(), settings).run(episode)
    repository = FakeEpisodeRepository(episode)

    summary = run_qc_worker_once(
        repository=repository,
        research_service=ResearchService(settings),
        batch_limit=10,
        user_id="test-qc-worker",
    )

    assert summary["claim_qc_completed"] == 1
    assert summary["error_count"] == 0
    assert len(repository.saved) == 1
    assert any(
        result.check_type == "claim_citation_integrity"
        for result in repository.episode.quality_results
    )


@pytest.mark.asyncio
async def test_qc_worker_runs_missing_claim_citation_qc_for_approved_transcript(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=worker_research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode = ResearchService(settings).build_evidence_pack(episode, ResearchBuildRequest())
    episode.approvals[-1].decision = "approved"
    episode.status = EpisodeStatus.draft
    episode = await DiscussionEngine(ModelGateway(), settings).run(episode)
    transcript = next(
        item for item in episode.transcripts if item.id == episode.canonical_transcript_version_id
    )
    transcript.status = "approved"
    for turn in transcript.turns:
        turn.status = "accepted"
    repository = FakeEpisodeRepository(episode)

    summary = run_qc_worker_once(
        repository=repository,
        research_service=ResearchService(settings),
        batch_limit=10,
        user_id="test-qc-worker",
    )
    second = run_qc_worker_once(
        repository=repository,
        research_service=ResearchService(settings),
        batch_limit=10,
        user_id="test-qc-worker",
    )

    assert summary["claim_qc_completed"] == 1
    assert summary["error_count"] == 0
    assert second["skipped"] == 1
    assert len(repository.saved) == 1
    claim_qc = [
        result
        for result in repository.episode.quality_results
        if result.check_type == "claim_citation_integrity"
    ][-1]
    assert claim_qc.status == "pass"
    assert repository.episode.audit_events[-1].event_type == "research.claim_qc.completed"


@pytest.mark.asyncio
async def test_approved_transcript_accepts_claim_qc_warnings_for_completion(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    repository = EpisodeRepository()
    episode = repository.create(
        EpisodeCreateRequest(
            definition=worker_research_definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode = ResearchService(settings).build_evidence_pack(episode, ResearchBuildRequest())
    episode.approvals[-1].decision = "approved"
    episode.status = EpisodeStatus.draft
    episode = await DiscussionEngine(ModelGateway(), settings).run(episode)
    transcript = next(
        item for item in episode.transcripts if item.id == episode.canonical_transcript_version_id
    )
    transcript_approval = next(
        approval
        for approval in episode.approvals
        if approval.stage == "transcript_review"
    )
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="transcript_version",
            target_id=str(transcript.id),
            check_type="claim_citation_integrity",
            severity=QualitySeverity.fail,
            status="fail",
            details={"unsupported_claim_count": 1},
        )
    )
    repository.save(episode)

    decided = repository.record_approval_decision(
        episode.id,
        transcript_approval.id,
        ApprovalDecisionRequest(decision="approved", user_id="editor"),
    )
    result = decided.quality_results[-1]

    assert transcript_approval.target_type == "transcript_version"
    assert transcript_approval.target_id == str(transcript.id)
    assert not ProductionControlService()._quality_result_blocks_completion(decided, result)
    approval_event = decided.audit_events[-1]
    assert approval_event.event_type == "approval.decision.recorded"
    assert approval_event.details["claim_qc"]["editorial_decision"] == "accepted_with_warnings"
