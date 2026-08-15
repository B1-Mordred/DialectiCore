from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from app.core.config import Settings, get_settings
from app.domain.defaults import default_render_presets
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity, TranscriptType
from app.domain.schemas import (
    Asset,
    AudioAssetPlanRequest,
    AudioGenerationRequest,
    AudioResultSyncRequest,
    ComfyUiEndpoint,
    ComfyUiWorkflow,
    Episode,
    LocalizationRequest,
    ProductionManifestRequest,
    PublisherTarget,
    PublishRequest,
    RenderRequest,
    ResearchBuildRequest,
    ResearchClaimQcRequest,
    SubtitleGenerationRequest,
    ThumbnailRequest,
    TimelineBuildRequest,
    TranscriptVersion,
    VisualAssetPlanRequest,
    VisualGenerationRequest,
    VisualProfile,
    VisualQualityRequest,
    VisualResultSyncRequest,
    VoiceboxEndpoint,
    VoiceProfile,
    WorkerHeartbeatRequest,
    WorkflowActionRequest,
    YouTubeExportRequest,
)
from app.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
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
from app.services.publisher_service import PublisherService
from app.services.redis_bus_service import RedisBusService
from app.services.render_service import RenderService
from app.services.research_service import ResearchService
from app.services.subtitle_service import SubtitleService
from app.services.timeline_service import TimelineService
from app.services.voicebox_service import VoiceboxService
from app.services.worker_lease_service import WorkerLeaseService
from app.services.worker_status_service import WorkerStatusService


class VoiceboxSyncRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...

    def list_voicebox_endpoints(self) -> list[VoiceboxEndpoint]: ...

    def list_voice_profiles(self) -> list[VoiceProfile]: ...


class AudioProductionRepository(VoiceboxSyncRepository, Protocol):
    pass


class ComfyUiPlanRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...

    def list_comfyui_endpoints(self) -> list[ComfyUiEndpoint]: ...

    def list_comfyui_workflows(self) -> list[ComfyUiWorkflow]: ...


class VisualProductionRepository(ComfyUiPlanRepository, Protocol):
    def list_visual_profiles(self) -> list[VisualProfile]: ...


class SubtitleWorkerRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...


class TimelineWorkerRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...


class RenderWorkerRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...


class PublishingWorkerRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...

    def list_publisher_targets(self) -> list[PublisherTarget]: ...


class ResearchWorkerRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...


class DiscussionWorkerRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...


class LocalizationWorkerRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...


class QcWorkerRepository(Protocol):
    def list(self) -> list[Episode]: ...

    def save(self, episode: Episode) -> Episode: ...


class WorkflowWorkerRepository(
    AudioProductionRepository,
    VoiceboxSyncRepository,
    SubtitleWorkerRepository,
    VisualProductionRepository,
    ComfyUiPlanRepository,
    TimelineWorkerRepository,
    RenderWorkerRepository,
    PublishingWorkerRepository,
    ResearchWorkerRepository,
    DiscussionWorkerRepository,
    LocalizationWorkerRepository,
    QcWorkerRepository,
    Protocol,
):
    pass


class _CachedWorkflowWorkerRepository:
    """Keep one orchestration pass internally consistent without repeated full scans."""

    def __init__(self, repository: WorkflowWorkerRepository) -> None:
        self._repository = repository
        self._episode_ids: list[UUID] = []
        self._episodes_by_id: dict[UUID, Episode] = {}
        self._snapshot_updated_at: dict[UUID, datetime] = {}
        self.refresh()

    def refresh(self) -> None:
        episodes = [episode.model_copy(deep=True) for episode in self._repository.list()]
        self._episode_ids = [episode.id for episode in episodes]
        self._episodes_by_id = {episode.id: episode for episode in episodes}
        self._snapshot_updated_at = {
            episode.id: episode.updated_at for episode in episodes
        }

    def list(self) -> list[Episode]:
        return [
            self._episodes_by_id[episode_id]
            for episode_id in self._episode_ids
            if episode_id in self._episodes_by_id
        ]

    def get(self, episode_id: UUID) -> Episode:
        try:
            return self._episodes_by_id[episode_id]
        except KeyError as exc:
            raise KeyError(episode_id) from exc

    def save(self, episode: Episode) -> Episode:
        expected_updated_at = self._snapshot_updated_at.get(episode.id)
        current = _get_repository_episode(self._repository, episode.id)
        if (
            current is not None
            and expected_updated_at is not None
            and current.updated_at > expected_updated_at
        ):
            # The coordinator operates on a bounded snapshot. Never let a
            # completed worker pass overwrite a newer operator/API revision;
            # the next snapshot refresh will safely retry against current data.
            self._episodes_by_id[current.id] = current.model_copy(deep=True)
            self._snapshot_updated_at[current.id] = current.updated_at
            return self._episodes_by_id[current.id]
        saved = self._repository.save(episode)
        # The backing repository is permitted to retain the object passed to
        # save (as simple in-memory adapters do). Keep our snapshot detached so
        # later stage mutations cannot appear as an external update before the
        # next explicit save.
        cached = saved.model_copy(deep=True)
        if saved.id not in self._episodes_by_id:
            self._episode_ids.append(saved.id)
        self._episodes_by_id[saved.id] = cached
        self._snapshot_updated_at[saved.id] = saved.updated_at
        return cached

    def list_voicebox_endpoints(self) -> list[VoiceboxEndpoint]:
        return self._repository.list_voicebox_endpoints()

    def list_voice_profiles(self) -> list[VoiceProfile]:
        return self._repository.list_voice_profiles()

    def list_comfyui_endpoints(self) -> list[ComfyUiEndpoint]:
        return self._repository.list_comfyui_endpoints()

    def list_comfyui_workflows(self) -> list[ComfyUiWorkflow]:
        return self._repository.list_comfyui_workflows()

    def list_visual_profiles(self) -> list[VisualProfile]:
        return self._repository.list_visual_profiles()

    def list_publisher_targets(self) -> list[PublisherTarget]:
        return self._repository.list_publisher_targets()


class _ActiveWorkflowRunRepository:
    def __init__(self, repository: WorkflowWorkerRepository) -> None:
        self._repository = repository
        # Stage workers operate on the cached pass snapshot, but a write must
        # always compare against the backing repository so an operator update
        # made mid-pass cannot be overwritten by a stale aggregate.
        self._backing_repository = (
            repository._repository
            if isinstance(repository, _CachedWorkflowWorkerRepository)
            else repository
        )
        snapshot = list(repository.list())
        self._snapshot_updated_at = {
            episode.id: episode.updated_at for episode in snapshot
        }
        self._admitted_episode_ids = {
            episode.id
            for episode in snapshot
            if _has_running_workflow_run(episode)
            and not _workflow_control_blocks_stage_work(episode)
        }

    def list(self) -> list[Episode]:
        return [
            episode
            for episode in self._repository.list()
            if episode.id in self._admitted_episode_ids
            and episode.status != EpisodeStatus.cancelled
            and (episode.workflow_control or {}).get("cancelled") is not True
            and not _canonical_transcript_requires_review(episode)
        ]

    def save(self, episode: Episode) -> Episode:
        current = _get_repository_episode(self._backing_repository, episode.id)
        expected_updated_at = self._snapshot_updated_at.get(episode.id)
        if (
            current is not None
            and expected_updated_at is not None
            and current.updated_at > expected_updated_at
        ):
            # An API or UI change landed while this stage was running. Refresh
            # the next pass and return the authoritative aggregate rather than
            # allowing a stale visual plan to replace the operator's settings.
            if isinstance(self._repository, _CachedWorkflowWorkerRepository):
                self._repository.refresh()
            self._snapshot_updated_at[current.id] = current.updated_at
            return current.model_copy(deep=True)
        if current is not None and (
            current.id not in self._admitted_episode_ids
            or _workflow_control_blocks_stage_work(current)
            or not _has_running_workflow_run(current)
        ):
            return current
        if current is not None and _canonical_transcript_requires_review(current):
            if episode.canonical_transcript_version_id != current.canonical_transcript_version_id:
                return current
            episode.status = EpisodeStatus.transcript_review
        if _canonical_transcript_requires_review(episode):
            episode.status = EpisodeStatus.transcript_review
        saved = self._repository.save(episode)
        self._snapshot_updated_at[saved.id] = saved.updated_at
        return saved

    def list_voicebox_endpoints(self) -> list[VoiceboxEndpoint]:
        return self._repository.list_voicebox_endpoints()

    def list_voice_profiles(self) -> list[VoiceProfile]:
        return self._repository.list_voice_profiles()

    def list_comfyui_endpoints(self) -> list[ComfyUiEndpoint]:
        return self._repository.list_comfyui_endpoints()

    def list_comfyui_workflows(self) -> list[ComfyUiWorkflow]:
        return self._repository.list_comfyui_workflows()

    def list_visual_profiles(self) -> list[VisualProfile]:
        return self._repository.list_visual_profiles()

    def list_publisher_targets(self) -> list[PublisherTarget]:
        return self._repository.list_publisher_targets()


def build_repository(settings: Settings) -> EpisodeRepository:
    engine = create_database_engine(settings)
    initialize_database(engine)
    return EpisodeRepository(create_session_factory(engine))


async def run_audio_production_worker_once(
    repository: AudioProductionRepository,
    voicebox_service: VoiceboxService,
    batch_limit: int,
    user_id: str = "audio-production-worker",
) -> dict:
    endpoints = repository.list_voicebox_endpoints()
    profiles = repository.list_voice_profiles()
    episodes = list(repository.list())
    scanned_count = 0
    planned_episode_count = 0
    generated_episode_count = 0
    completed_audio_asset_count = 0
    submitted_audio_asset_count = 0
    targeted_audio_asset_count = 0
    repair_audio_asset_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        transcript = _target_transcript_needing_audio(episode)
        if transcript is None:
            skipped_count += 1
            continue

        planned_this_episode = False
        try:
            if _transcript_has_missing_audio_assets(episode, transcript):
                episode = voicebox_service.plan_audio_assets(
                    episode,
                    AudioAssetPlanRequest(
                        transcript_version_id=transcript.id,
                        user_id=user_id,
                    ),
                )
                planned_episode_count += 1
                planned_this_episode = True

            targeted_asset_ids = _audio_generation_target_asset_ids(episode, transcript)
            repair_count = _audio_repair_target_count(episode, transcript)
            before_completed = _audio_asset_count(episode, transcript, {"completed"})
            before_submitted = _audio_asset_count(episode, transcript, {"submitted", "running"})
            episode = await voicebox_service.generate_audio_assets(
                episode,
                AudioGenerationRequest(
                    transcript_version_id=transcript.id,
                    asset_ids=targeted_asset_ids or None,
                    user_id=user_id,
                ),
                voicebox_endpoints=endpoints,
                voice_profiles=profiles,
            )
            after_completed = _audio_asset_count(episode, transcript, {"completed"})
            after_submitted = _audio_asset_count(episode, transcript, {"submitted", "running"})
        except ValueError as exc:
            if planned_this_episode:
                repository.save(episode)
            error_count += 1
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "error": str(exc),
                }
            )
            continue

        repository.save(episode)
        generated_episode_count += 1
        completed_audio_asset_count += max(after_completed - before_completed, 0)
        submitted_audio_asset_count += max(after_submitted - before_submitted, 0)
        targeted_audio_asset_count += len(targeted_asset_ids)
        repair_audio_asset_count += repair_count

    return {
        "episodes_scanned": scanned_count,
        "episodes_planned": planned_episode_count,
        "episodes_generated": generated_episode_count,
        "completed_audio_assets": completed_audio_asset_count,
        "submitted_audio_assets": submitted_audio_asset_count,
        "targeted_audio_assets": targeted_audio_asset_count,
        "repair_audio_assets": repair_audio_asset_count,
        "enabled_voicebox_endpoints": sum(1 for endpoint in endpoints if endpoint.enabled),
        "enabled_voice_profiles": sum(1 for profile in profiles if profile.enabled),
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


async def run_voicebox_adapter_once(
    repository: VoiceboxSyncRepository,
    voicebox_service: VoiceboxService,
    batch_limit: int,
    user_id: str = "voicebox-adapter-worker",
) -> dict:
    endpoints = repository.list_voicebox_endpoints()
    profiles = repository.list_voice_profiles()
    episodes = list(repository.list())
    scanned_count = 0
    synced_episode_count = 0
    pending_asset_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        pending_languages = sorted(
            {
                asset.language
                for asset in episode.assets
                if asset.asset_type == AssetType.audio
                and asset.status in {"submitted", "running"}
                and asset.language
            }
        )
        if not pending_languages:
            continue

        episode_changed = False
        for language in pending_languages:
            pending_for_language = [
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.audio
                and asset.status in {"submitted", "running"}
                and asset.language == language
            ]
            pending_asset_count += len(pending_for_language)
            try:
                episode = await voicebox_service.sync_audio_results(
                    episode,
                    AudioResultSyncRequest(language=language, user_id=user_id),
                    voicebox_endpoints=endpoints,
                    voice_profiles=profiles,
                )
            except ValueError as exc:
                error_count += 1
                errors.append(
                    {
                        "episode_id": str(episode.id),
                        "language": language,
                        "error": str(exc),
                    }
                )
                continue
            episode_changed = True

        if episode_changed:
            repository.save(episode)
            synced_episode_count += 1

    return {
        "episodes_scanned": scanned_count,
        "episodes_synced": synced_episode_count,
        "pending_audio_assets": pending_asset_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


def run_subtitle_worker_once(
    repository: SubtitleWorkerRepository,
    subtitle_service: SubtitleService,
    batch_limit: int,
    user_id: str = "subtitle-worker",
) -> dict:
    episodes = list(repository.list())
    scanned_count = 0
    generated_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    prerequisite_blocked_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        transcript = _target_transcript_needing_subtitles(episode)
        if transcript is None:
            skipped_count += 1
            continue
        if not _subtitle_prerequisites_met(episode, transcript):
            prerequisite_blocked_count += 1
            continue
        try:
            updated = subtitle_service.generate_subtitles(
                episode,
                SubtitleGenerationRequest(
                    transcript_version_id=transcript.id,
                    user_id=user_id,
                ),
            )
        except ValueError as exc:
            error_count += 1
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "error": str(exc),
                }
            )
            continue
        repository.save(updated)
        generated_count += 1

    return {
        "episodes_scanned": scanned_count,
        "subtitles_generated": generated_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "prerequisite_blocked": prerequisite_blocked_count,
        "error_count": error_count,
        "errors": errors,
    }


async def run_visual_production_worker_once(
    repository: VisualProductionRepository,
    comfyui_service: ComfyUiService,
    batch_limit: int,
    user_id: str = "visual-production-worker",
) -> dict:
    endpoints = repository.list_comfyui_endpoints()
    workflows = repository.list_comfyui_workflows()
    episodes = list(repository.list())
    scanned_count = 0
    planned_episode_count = 0
    generated_episode_count = 0
    completed_visual_asset_count = 0
    submitted_visual_asset_count = 0
    targeted_visual_asset_count = 0
    repair_visual_asset_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    prerequisite_blocked_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        transcript = _target_transcript_needing_visuals(episode)
        if transcript is None:
            skipped_count += 1
            continue
        if not _visual_prerequisites_met(episode, transcript):
            prerequisite_blocked_count += 1
            continue

        try:
            if _transcript_has_missing_primary_visual_assets(episode, transcript):
                episode = comfyui_service.plan_visual_assets(
                    episode,
                    VisualAssetPlanRequest(
                        transcript_version_id=transcript.id,
                        user_id=user_id,
                    ),
                    visual_profiles=repository.list_visual_profiles(),
                    workflows=workflows,
                )
                planned_episode_count += 1

            targeted_asset_ids = _visual_generation_target_asset_ids(episode, transcript)
            repair_count = _visual_asset_count(episode, transcript, {"failed", "cancelled"})
            before_completed = _visual_asset_count(episode, transcript, {"completed"})
            before_submitted = _visual_asset_count(episode, transcript, {"submitted", "running"})
            episode = await comfyui_service.generate_visual_assets(
                episode,
                VisualGenerationRequest(
                    transcript_version_id=transcript.id,
                    asset_ids=targeted_asset_ids or None,
                    user_id=user_id,
                    fallback_on_failure=False,
                ),
                endpoints=endpoints,
                workflows=workflows,
                visual_profiles=repository.list_visual_profiles(),
            )
            episode = comfyui_service.run_visual_quality(
                episode,
                VisualQualityRequest(
                    transcript_version_id=transcript.id,
                    user_id=user_id,
                ),
                endpoints=endpoints,
                workflows=workflows,
            )
            after_completed = _visual_asset_count(episode, transcript, {"completed"})
            after_submitted = _visual_asset_count(episode, transcript, {"submitted", "running"})
        except ValueError as exc:
            error_count += 1
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "error": str(exc),
                }
            )
            continue

        repository.save(episode)
        generated_episode_count += 1
        completed_visual_asset_count += max(after_completed - before_completed, 0)
        submitted_visual_asset_count += max(after_submitted - before_submitted, 0)
        targeted_visual_asset_count += len(targeted_asset_ids)
        repair_visual_asset_count += repair_count

    return {
        "episodes_scanned": scanned_count,
        "episodes_planned": planned_episode_count,
        "episodes_generated": generated_episode_count,
        "completed_visual_assets": completed_visual_asset_count,
        "submitted_visual_assets": submitted_visual_asset_count,
        "targeted_visual_assets": targeted_visual_asset_count,
        "repair_visual_assets": repair_visual_asset_count,
        "enabled_comfyui_endpoints": sum(1 for endpoint in endpoints if endpoint.enabled),
        "enabled_comfyui_workflows": sum(1 for workflow in workflows if workflow.enabled),
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "prerequisite_blocked": prerequisite_blocked_count,
        "error_count": error_count,
        "errors": errors,
    }


async def run_comfyui_adapter_once(
    repository: ComfyUiPlanRepository,
    comfyui_service: ComfyUiService,
    batch_limit: int,
    user_id: str = "comfyui-adapter-worker",
) -> dict:
    endpoints = repository.list_comfyui_endpoints()
    workflows = repository.list_comfyui_workflows()
    episodes = list(repository.list())
    scanned_count = 0
    synced_episode_count = 0
    pending_asset_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        pending_languages = sorted(
            {
                asset.language
                for asset in episode.assets
                if asset.asset_type
                in {
                    AssetType.video,
                    AssetType.broll,
                    AssetType.reaction_loop,
                    AssetType.studio_scene,
                }
                and asset.status in {"submitted", "running"}
                and asset.language
            }
        )
        if not pending_languages:
            continue

        episode_changed = False
        for language in pending_languages:
            pending_for_language = [
                asset
                for asset in episode.assets
                if asset.asset_type
                in {
                    AssetType.video,
                    AssetType.broll,
                    AssetType.reaction_loop,
                    AssetType.studio_scene,
                }
                and asset.status in {"submitted", "running"}
                and asset.language == language
            ]
            pending_asset_count += len(pending_for_language)
            try:
                episode = await comfyui_service.sync_visual_results(
                    episode,
                    VisualResultSyncRequest(language=language, user_id=user_id),
                    endpoints=endpoints,
                    workflows=workflows,
                )
            except ValueError as exc:
                error_count += 1
                errors.append(
                    {
                        "episode_id": str(episode.id),
                        "language": language,
                        "error": str(exc),
                    }
                )
                continue
            episode_changed = True

        if episode_changed:
            repository.save(episode)
            synced_episode_count += 1

    return {
        "episodes_scanned": scanned_count,
        "episodes_synced": synced_episode_count,
        "pending_visual_assets": pending_asset_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
        "enabled_comfyui_endpoints": sum(1 for endpoint in endpoints if endpoint.enabled),
        "enabled_comfyui_workflows": sum(1 for workflow in workflows if workflow.enabled),
    }


def run_timeline_worker_once(
    repository: TimelineWorkerRepository,
    timeline_service: TimelineService,
    batch_limit: int,
    user_id: str = "timeline-worker",
) -> dict:
    episodes = list(repository.list())
    scanned_count = 0
    built_count = 0
    workflow_blocked_count = 0
    skipped_prerequisite_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        transcript = _target_transcript_without_timeline(episode)
        if transcript is None:
            skipped_count += 1
            continue
        if not _timeline_prerequisites_met(episode, transcript):
            skipped_prerequisite_count += 1
            skipped_count += 1
            continue
        try:
            updated = timeline_service.build_timeline(
                episode,
                TimelineBuildRequest(
                    transcript_version_id=transcript.id,
                    user_id=user_id,
                ),
            )
        except ValueError as exc:
            error_count += 1
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "transcript_version_id": str(transcript.id),
                    "error": str(exc),
                }
            )
            continue
        repository.save(updated)
        built_count += 1

    return {
        "episodes_scanned": scanned_count,
        "timelines_built": built_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "skipped_prerequisites": skipped_prerequisite_count,
        "error_count": error_count,
        "errors": errors,
    }


def queue_render_worker_once(
    repository: RenderWorkerRepository,
    render_service: RenderService,
    batch_limit: int,
    user_id: str = "render-worker",
    *,
    auto_queue_renders: bool = False,
) -> dict:
    """Report render requests and queue them only for an explicit workflow action.

    Passive coordinators must never infer a preview or final render solely from a
    completed timeline, otherwise a resumed production run can consume resources
    and retry an unwanted render forever. An explicitly invoked workflow advance
    may queue its next render, but composition remains the render worker's job.
    """
    episodes = list(repository.list())
    scanned_count = 0
    preview_render_request_count = 0
    final_render_request_count = 0
    queued_preview_render_count = 0
    queued_final_render_count = 0
    workflow_blocked_count = 0
    skipped_count = 0

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        queued_asset = _next_queued_render_asset(episode)
        if queued_asset is not None:
            render_type = str(
                (queued_asset.generation_metadata or {}).get("render_type") or ""
            )
            if render_type == "preview":
                queued_preview_render_count += 1
            elif render_type == "final":
                queued_final_render_count += 1
            else:
                skipped_count += 1
            continue
        if not auto_queue_renders:
            skipped_count += 1
            continue
        timeline_asset = _latest_completed_timeline_asset(episode)
        if timeline_asset is None:
            skipped_count += 1
            continue
        active_render = _active_render_asset(episode, timeline_asset)
        if active_render is not None:
            skipped_count += 1
            continue
        preview_render = _latest_render_asset(episode, timeline_asset, render_type="preview")
        final_render = _latest_render_asset(episode, timeline_asset, render_type="final")
        if preview_render is None:
            request = RenderRequest(
                timeline_asset_id=timeline_asset.id,
                render_type="preview",
                preset_id="preview-low-bitrate",
                user_id=user_id,
            )
            counter = "preview"
        elif final_render is None:
            if not _preview_render_approved(episode, preview_render):
                skipped_count += 1
                continue
            request = RenderRequest(
                timeline_asset_id=timeline_asset.id,
                render_type="final",
                preset_id="youtube-1080p",
                user_id=user_id,
            )
            counter = "final"
        else:
            skipped_count += 1
            continue
        try:
            updated = render_service.enqueue_render(
                episode,
                request,
                presets=default_render_presets(),
            )
        except ValueError:
            skipped_count += 1
            continue
        repository.save(updated)
        if counter == "preview":
            preview_render_request_count += 1
            queued_preview_render_count += 1
        else:
            final_render_request_count += 1
            queued_final_render_count += 1

    return {
        "episodes_scanned": scanned_count,
        "preview_render_requests_submitted": preview_render_request_count,
        "final_render_requests_submitted": final_render_request_count,
        "preview_renders_created": 0,
        "final_renders_created": 0,
        "queued_preview_renders": queued_preview_render_count,
        "queued_final_renders": queued_final_render_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": 0,
        "errors": [],
    }


def run_render_worker_once(
    repository: RenderWorkerRepository,
    render_service: RenderService,
    batch_limit: int,
    user_id: str = "render-worker",
) -> dict:
    queued = queue_render_worker_once(
        repository=repository,
        render_service=render_service,
        batch_limit=batch_limit,
        user_id=user_id,
    )
    scanned_count = 0
    preview_render_count = 0
    final_render_count = 0
    skipped_count = 0
    error_count = int(queued["error_count"])
    errors = list(queued["errors"])
    presets = default_render_presets()

    for episode in repository.list():
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        queued_asset = _next_queued_render_asset(episode)
        if queued_asset is None:
            skipped_count += 1
            continue
        request = _render_request_from_queued_asset(queued_asset, user_id=user_id)
        if request is None:
            error_count += 1
            error = "queued render is missing a valid render request"
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "render_asset_id": str(queued_asset.id),
                    "error": error,
                }
            )
            repository.save(
                render_service.fail_queued_render(
                    episode,
                    queued_asset.id,
                    actor=user_id,
                    error=error,
                )
            )
            continue
        workflow_control = episode.workflow_control or {}
        if _workflow_control_blocks_stage_work(episode) and not (
            workflow_control.get("paused") is True
            and workflow_control.get("cancelled") is not True
            and episode.status != EpisodeStatus.cancelled
            and request.render_type == "preview"
            and request.allow_paused_episode
        ):
            skipped_count += 1
            continue
        counter = request.render_type
        try:
            started = render_service.start_queued_render(
                episode,
                queued_asset.id,
                actor=user_id,
            )
            repository.save(started)
            updated = render_service.render_episode(
                started,
                request,
                presets=presets,
                queued_render_asset_id=queued_asset.id,
            )
        except ValueError as exc:
            error_count += 1
            error = str(exc)
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "render_asset_id": str(queued_asset.id),
                    "render_type": counter,
                    "error": error,
                }
            )
            repository.save(
                render_service.fail_queued_render(
                    episode,
                    queued_asset.id,
                    actor=user_id,
                    error=error,
                )
            )
            continue
        repository.save(updated)
        if counter == "preview":
            preview_render_count += 1
        else:
            final_render_count += 1

    return {
        "episodes_scanned": scanned_count,
        "preview_render_requests_submitted": queued["preview_render_requests_submitted"],
        "final_render_requests_submitted": queued["final_render_requests_submitted"],
        "preview_renders_created": queued["preview_renders_created"] + preview_render_count,
        "final_renders_created": queued["final_renders_created"] + final_render_count,
        "workflow_blocked": queued["workflow_blocked"],
        "skipped": max(queued["skipped"], skipped_count),
        "error_count": error_count,
        "errors": errors,
    }


def run_publishing_worker_once(
    repository: PublishingWorkerRepository,
    render_service: RenderService,
    publisher_service: PublisherService,
    batch_limit: int,
    user_id: str = "publishing-worker",
    automated_live_enabled: bool = False,
) -> dict:
    episodes = list(repository.list())
    targets = [target for target in repository.list_publisher_targets() if target.enabled]
    scanned_count = 0
    thumbnail_count = 0
    package_count = 0
    production_manifest_count = 0
    production_manifest_refresh_count = 0
    dry_run_publish_count = 0
    live_publish_count = 0
    package_qc_blocked_handoff_count = 0
    production_manifest_blocked_handoff_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    pending_final_render_approvals = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        final_render = _latest_final_render_asset(episode)
        if final_render is None:
            skipped_count += 1
            continue
        if not _final_render_approved(episode, final_render):
            pending_final_render_approvals += 1
            skipped_count += 1
            continue
        changed = False
        try:
            thumbnail = _latest_thumbnail_asset(episode, final_render)
            if thumbnail is None:
                episode = render_service.generate_thumbnail(
                    episode,
                    ThumbnailRequest(render_asset_id=final_render.id, user_id=user_id),
                )
                thumbnail = _latest_thumbnail_asset(episode, final_render)
                thumbnail_count += 1
                changed = True

            package = _latest_export_package_asset(episode, final_render)
            if package is None:
                episode = render_service.export_youtube_package(
                    episode,
                    YouTubeExportRequest(
                        render_asset_id=final_render.id,
                        thumbnail_asset_id=thumbnail.id if thumbnail else None,
                        user_id=user_id,
                    ),
                )
                package = _latest_export_package_asset(episode, final_render)
                package_count += 1
                changed = True

            production_manifest = (
                _latest_production_manifest_asset(episode, package)
                if package is not None
                else None
            )
            if package is not None and production_manifest is None:
                episode = render_service.generate_production_manifest(
                    episode,
                    ProductionManifestRequest(
                        package_asset_id=package.id,
                        render_asset_id=final_render.id,
                        user_id=user_id,
                    ),
                )
                production_manifest = _latest_production_manifest_asset(
                    episode,
                    package,
                )
                production_manifest_count += 1
                changed = True

            target = _publisher_target_for_automation(targets, automated_live_enabled)
            if (
                target is not None
                and package is not None
                and production_manifest is not None
                and _latest_publish_job_exists(episode, target.id, package.id) is False
            ):
                dry_run = not _automated_live_publish_allowed(
                    target,
                    automated_live_enabled,
                )
                episode = publisher_service.publish_package(
                    episode,
                    PublishRequest(
                        publisher_target_id=target.id,
                        package_asset_id=package.id,
                        user_id=user_id,
                        dry_run=dry_run,
                    ),
                    targets=targets,
                )
                if dry_run:
                    dry_run_publish_count += 1
                else:
                    live_publish_count += 1
                publish_job = _latest_publish_job(episode, package)
                publish_qc = _latest_publish_qc(episode, publish_job) if publish_job else None
                if publish_job is not None and publish_qc is not None:
                    episode = render_service.generate_production_manifest(
                        episode,
                        ProductionManifestRequest(
                            package_asset_id=package.id,
                            render_asset_id=final_render.id,
                            user_id=user_id,
                            regenerate=True,
                        ),
                    )
                    production_manifest = _latest_production_manifest_asset(
                        episode,
                        package,
                    )
                    production_manifest_refresh_count += 1
                changed = True
        except ValueError as exc:
            error_kind = _publishing_error_kind(exc)
            if error_kind == "package_qc_blocked":
                package_qc_blocked_handoff_count += 1
            elif error_kind == "production_manifest_blocked":
                production_manifest_blocked_handoff_count += 1
            error_count += 1
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "render_asset_id": str(final_render.id),
                    "error_kind": error_kind,
                    "error": str(exc),
                }
            )
            continue
        if changed:
            repository.save(episode)
        else:
            skipped_count += 1

    return {
        "episodes_scanned": scanned_count,
        "thumbnails_created": thumbnail_count,
        "youtube_packages_created": package_count,
        "production_manifests_created": production_manifest_count,
        "production_manifests_refreshed": production_manifest_refresh_count,
        "dry_run_publish_jobs_created": dry_run_publish_count,
        "live_publish_jobs_created": live_publish_count,
        "package_qc_blocked_handoffs": package_qc_blocked_handoff_count,
        "production_manifest_blocked_handoffs": production_manifest_blocked_handoff_count,
        "automated_live_enabled": automated_live_enabled,
        "automated_live_capable_targets": sum(
            1 for target in targets if _target_allows_automated_live_publish(target)
        ),
        "enabled_publisher_targets": len(targets),
        "pending_final_render_approvals": pending_final_render_approvals,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


def run_completion_worker_once(
    repository: WorkflowWorkerRepository,
    production_control: ProductionControlService,
    batch_limit: int,
    user_id: str = "completion-worker",
) -> dict:
    episodes = list(repository.list())
    scanned_count = 0
    completed_count = 0
    workflow_blocked_count = 0
    readiness_blocked_count = 0
    skipped_count = 0
    error_count = 0
    completed_episode_ids: list[str] = []
    readiness_blockers: list[dict] = []
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        if episode.status in {
            EpisodeStatus.completed,
            EpisodeStatus.cancelled,
            EpisodeStatus.failed,
        }:
            skipped_count += 1
            continue
        run = (episode.workflow_control or {}).get("run")
        if not isinstance(run, dict) or run.get("state") != "running":
            skipped_count += 1
            continue
        readiness = production_control.completion_readiness(episode)
        if readiness.get("status") != "pass":
            readiness_blocked_count += 1
            skipped_count += 1
            readiness_blockers.append(
                {
                    "episode_id": str(episode.id),
                    "failed_checks": list(readiness.get("failed_checks") or [])[:8],
                }
            )
            continue
        try:
            updated = production_control.complete(
                episode,
                WorkflowActionRequest(
                    action="complete",
                    user_id=user_id,
                    comment="Automated workflow completion after production gates passed.",
                ),
            )
        except ValueError as exc:
            error_count += 1
            errors.append({"episode_id": str(episode.id), "error": str(exc)})
            continue
        repository.save(updated)
        completed_count += 1
        completed_episode_ids.append(str(updated.id))

    return {
        "episodes_scanned": scanned_count,
        "episodes_completed": completed_count,
        "completed_episode_ids": completed_episode_ids,
        "workflow_blocked": workflow_blocked_count,
        "readiness_blocked": readiness_blocked_count,
        "readiness_blockers": readiness_blockers,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


def _publishing_error_kind(exc: ValueError) -> str:
    message = str(exc)
    if (
        "YouTube package QC is required before production manifest generation" in message
        or "failing YouTube package QC blocks production manifest generation" in message
        or "YouTube package QC is required before live publishing" in message
        or "failing YouTube package QC blocks publishing" in message
    ):
        return "package_qc_blocked"
    if (
        "production manifest is required before live publishing" in message
        or "valid production_manifest.v1 asset is required before live publishing" in message
    ):
        return "production_manifest_blocked"
    return "other"


def _publisher_target_for_automation(
    targets: list[PublisherTarget],
    automated_live_enabled: bool,
) -> PublisherTarget | None:
    if not targets:
        return None
    if automated_live_enabled:
        for target in targets:
            if _target_allows_automated_live_publish(target):
                return target
    return targets[0]


def _automated_live_publish_allowed(
    target: PublisherTarget,
    automated_live_enabled: bool,
) -> bool:
    return automated_live_enabled and _target_allows_automated_live_publish(target)


def _target_allows_automated_live_publish(target: PublisherTarget) -> bool:
    return target.capabilities.get("automated_live_publish") is True


def run_research_worker_once(
    repository: ResearchWorkerRepository,
    research_service: ResearchService,
    batch_limit: int,
    user_id: str = "research-worker",
) -> dict:
    episodes = list(repository.list())
    scanned_count = 0
    evidence_pack_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        if not episode.definition.research.enabled:
            skipped_count += 1
            continue
        if _latest_evidence_pack_asset(episode) is not None:
            skipped_count += 1
            continue
        if _has_pending_approval(episode, "research_review"):
            skipped_count += 1
            continue
        try:
            updated = research_service.build_evidence_pack(
                episode,
                ResearchBuildRequest(user_id=user_id),
            )
        except ValueError as exc:
            error_count += 1
            errors.append({"episode_id": str(episode.id), "error": str(exc)})
            continue
        repository.save(updated)
        evidence_pack_count += 1

    return {
        "episodes_scanned": scanned_count,
        "evidence_packs_built": evidence_pack_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


async def run_discussion_worker_once(
    repository: DiscussionWorkerRepository,
    discussion_engine: DiscussionEngine,
    production_control: ProductionControlService,
    batch_limit: int,
) -> dict:
    episodes = list(repository.list())
    scanned_count = 0
    discussions_completed = 0
    model_configuration_blocked_count = 0
    auto_start_disabled_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        if not _has_running_workflow_run(episode) and (
            not production_control.settings.worker_auto_start_production_runs_enabled
        ):
            auto_start_disabled_count += 1
            skipped_count += 1
            continue
        if not _discussion_worker_can_start(episode):
            skipped_count += 1
            continue
        model_configuration = _discussion_model_configuration(episode)
        if model_configuration["ready"] is False:
            model_configuration_blocked_count += 1
            skipped_count += 1
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "error_kind": "discussion_model_configuration_blocked",
                    "model_configuration": model_configuration,
                    "error": "episode participant model configuration is incomplete",
                }
            )
            continue
        try:
            episode = _ensure_workflow_run_started(
                episode,
                production_control,
                user_id="discussion-worker",
            )
            updated = await discussion_engine.run(episode)
        except ValueError as exc:
            error_count += 1
            errors.append({"episode_id": str(episode.id), "error": str(exc)})
            continue
        repository.save(updated)
        discussions_completed += 1

    return {
        "episodes_scanned": scanned_count,
        "discussions_completed": discussions_completed,
        "model_configuration_blocked": model_configuration_blocked_count,
        "auto_start_disabled": auto_start_disabled_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


def run_localization_worker_once(
    repository: LocalizationWorkerRepository,
    localization_service: LocalizationService,
    batch_limit: int,
    user_id: str = "localization-worker",
) -> dict:
    episodes = list(repository.list())
    scanned_count = 0
    localized_episode_count = 0
    localized_language_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        languages = _missing_localized_languages(episode)
        if not languages:
            skipped_count += 1
            continue
        try:
            updated = localization_service.create_language_variants(
                episode,
                LocalizationRequest(languages=languages, user_id=user_id),
            )
        except ValueError as exc:
            error_count += 1
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "languages": languages,
                    "error": str(exc),
                }
            )
            continue
        repository.save(updated)
        localized_episode_count += 1
        localized_language_count += len(languages)

    return {
        "episodes_scanned": scanned_count,
        "episodes_localized": localized_episode_count,
        "localized_languages_created": localized_language_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


def run_qc_worker_once(
    repository: QcWorkerRepository,
    research_service: ResearchService,
    batch_limit: int,
    user_id: str = "qc-worker",
) -> dict:
    episodes = list(repository.list())
    scanned_count = 0
    claim_qc_count = 0
    workflow_blocked_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            workflow_blocked_count += 1
            skipped_count += 1
            continue
        evidence_asset = _latest_evidence_pack_asset(episode)
        transcript = _canonical_or_latest_broadcast_transcript(episode)
        if (
            evidence_asset is None
            or transcript is None
            or transcript.status not in {"pending_review", "approved"}
        ):
            skipped_count += 1
            continue
        if _claim_qc_exists(episode, evidence_asset, transcript):
            skipped_count += 1
            continue
        try:
            updated = research_service.run_claim_qc(
                episode,
                ResearchClaimQcRequest(
                    evidence_pack_asset_id=evidence_asset.id,
                    transcript_version_id=transcript.id,
                    user_id=user_id,
                ),
            )
        except ValueError as exc:
            error_count += 1
            errors.append(
                {
                    "episode_id": str(episode.id),
                    "evidence_pack_asset_id": str(evidence_asset.id),
                    "transcript_version_id": str(transcript.id),
                    "error": str(exc),
                }
            )
            continue
        repository.save(updated)
        claim_qc_count += 1

    return {
        "episodes_scanned": scanned_count,
        "claim_qc_completed": claim_qc_count,
        "workflow_blocked": workflow_blocked_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


async def run_workflow_worker_once(
    repository: WorkflowWorkerRepository,
    settings: Settings,
    batch_limit: int,
    research_service: ResearchService | None = None,
    discussion_engine: DiscussionEngine | None = None,
    production_control: ProductionControlService | None = None,
    localization_service: LocalizationService | None = None,
    voicebox_service: VoiceboxService | None = None,
    subtitle_service: SubtitleService | None = None,
    comfyui_service: ComfyUiService | None = None,
    timeline_service: TimelineService | None = None,
    render_service: RenderService | None = None,
    publisher_service: PublisherService | None = None,
    auto_queue_renders: bool = False,
) -> dict:
    research_service = research_service or ResearchService(settings)
    discussion_engine = discussion_engine or DiscussionEngine(ModelGateway(), settings)
    production_control = production_control or ProductionControlService(settings)
    localization_service = localization_service or LocalizationService()
    voicebox_service = voicebox_service or VoiceboxService(settings)
    subtitle_service = subtitle_service or SubtitleService(settings)
    comfyui_service = comfyui_service or ComfyUiService(settings)
    timeline_service = timeline_service or TimelineService(settings)
    render_service = render_service or RenderService(settings)
    publisher_service = publisher_service or PublisherService()
    if isinstance(repository, _CachedWorkflowWorkerRepository):
        # A cached repository is long-lived in the worker loop. Refresh here
        # so all callers, including direct one-shot invocations, start from
        # current operator/API state.
        repository.refresh()
        worker_repository = repository
    else:
        worker_repository = _CachedWorkflowWorkerRepository(repository)

    workflow_start_summary = _ensure_workflow_runs_started(
        repository=worker_repository,
        production_control=production_control,
        batch_limit=batch_limit,
        user_id="workflow-worker",
    )
    automatic_retry_records = _apply_due_workflow_stage_retries(
        repository=worker_repository,
        production_control=production_control,
        batch_limit=batch_limit,
        user_id="workflow-worker",
    )
    stage_repository = _ActiveWorkflowRunRepository(worker_repository)
    workflow_admission = _workflow_stage_admission_summary(
        repository=worker_repository,
        batch_limit=batch_limit,
    )
    stage_summaries = {
        "research": run_research_worker_once(
            repository=stage_repository,
            research_service=research_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "discussion": await run_discussion_worker_once(
            repository=stage_repository,
            discussion_engine=discussion_engine,
            production_control=production_control,
            batch_limit=batch_limit,
        ),
        "localization": run_localization_worker_once(
            repository=stage_repository,
            localization_service=localization_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "qc": run_qc_worker_once(
            repository=stage_repository,
            research_service=research_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "audio": await run_audio_production_worker_once(
            repository=stage_repository,
            voicebox_service=voicebox_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "voicebox": await run_voicebox_adapter_once(
            repository=stage_repository,
            voicebox_service=voicebox_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "subtitles": run_subtitle_worker_once(
            repository=stage_repository,
            subtitle_service=subtitle_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "visuals": await run_visual_production_worker_once(
            repository=stage_repository,
            comfyui_service=comfyui_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "comfyui": await run_comfyui_adapter_once(
            repository=stage_repository,
            comfyui_service=comfyui_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "timeline": run_timeline_worker_once(
            repository=stage_repository,
            timeline_service=timeline_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
        "render": queue_render_worker_once(
            repository=stage_repository,
            render_service=render_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
            auto_queue_renders=auto_queue_renders,
        ),
        "publishing": run_publishing_worker_once(
            repository=stage_repository,
            render_service=render_service,
            publisher_service=publisher_service,
            batch_limit=batch_limit,
            user_id="workflow-worker",
            automated_live_enabled=settings.publisher_automated_live_enabled,
        ),
        "completion": run_completion_worker_once(
            repository=stage_repository,
            production_control=production_control,
            batch_limit=batch_limit,
            user_id="workflow-worker",
        ),
    }
    progressed_stage_count = sum(
        _workflow_stage_progress_count(stage, summary)
        for stage, summary in stage_summaries.items()
    )
    error_count = sum(int(summary.get("error_count") or 0) for summary in stage_summaries.values())
    summary = {
        "schema_version": "workflow_worker_orchestration_summary.v1",
        "orchestration_attempt_id": str(uuid4()),
        "policy": "local_stage_worker_orchestrator_v1",
        "batch_limit": batch_limit,
        "stage_order": list(stage_summaries),
        "stages": stage_summaries,
        "workflow_run_starts": workflow_start_summary,
        "workflow_admission": workflow_admission,
        "progressed_stage_count": progressed_stage_count,
        "error_count": error_count,
        "automatic_stage_retry_count": len(automatic_retry_records),
        "automatic_stage_retries": automatic_retry_records,
        "production_handoffs": _workflow_production_handoffs(stage_repository),
    }
    summary["orchestration_records"] = _record_workflow_worker_orchestration(
        worker_repository,
        production_control,
        summary,
    )
    return summary


def _ensure_workflow_runs_started(
    repository: WorkflowWorkerRepository,
    production_control: ProductionControlService,
    batch_limit: int,
    user_id: str,
) -> dict:
    episodes = list(repository.list())
    scanned_count = 0
    started_count = 0
    auto_start_disabled_count = 0
    skipped_count = 0
    error_count = 0
    errors: list[dict] = []

    if not production_control.settings.worker_auto_start_production_runs_enabled:
        return {
            "schema_version": "workflow_run_start_summary.v1",
            "episodes_scanned": 0,
            "workflow_runs_started": 0,
            "auto_start_disabled": True,
            "auto_start_disabled_count": 0,
            "skipped": 0,
            "error_count": 0,
            "errors": [],
        }

    for episode in episodes:
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        if _workflow_control_blocks_stage_work(episode):
            skipped_count += 1
            continue
        if not _workflow_worker_can_start_run(episode):
            skipped_count += 1
            continue
        try:
            updated = _ensure_workflow_run_started(
                episode,
                production_control,
                user_id=user_id,
            )
        except ValueError as exc:
            error_count += 1
            errors.append({"episode_id": str(episode.id), "error": str(exc)})
            continue
        repository.save(updated)
        started_count += 1

    return {
        "schema_version": "workflow_run_start_summary.v1",
        "episodes_scanned": scanned_count,
        "workflow_runs_started": started_count,
        "auto_start_disabled": False,
        "auto_start_disabled_count": auto_start_disabled_count,
        "skipped": skipped_count,
        "error_count": error_count,
        "errors": errors,
    }


def _workflow_stage_admission_summary(
    repository: WorkflowWorkerRepository,
    batch_limit: int,
) -> dict:
    episodes = list(repository.list())[:batch_limit]
    active_episode_ids = [
        str(episode.id)
        for episode in episodes
        if _has_running_workflow_run(episode)
        and not _workflow_control_blocks_stage_work(episode)
    ]
    missing_run_episode_ids = [
        str(episode.id)
        for episode in episodes
        if not _has_running_workflow_run(episode)
        and not _workflow_control_blocks_stage_work(episode)
        and episode.status
        not in {
            EpisodeStatus.cancelled,
            EpisodeStatus.completed,
            EpisodeStatus.failed,
        }
    ]
    blocked_episode_ids = [
        str(episode.id)
        for episode in episodes
        if _workflow_control_blocks_stage_work(episode)
    ]
    return {
        "schema_version": "workflow_stage_admission_summary.v1",
        "episodes_scanned": len(episodes),
        "active_run_episode_count": len(active_episode_ids),
        "missing_run_episode_count": len(missing_run_episode_ids),
        "blocked_episode_count": len(blocked_episode_ids),
        "active_run_episode_ids": active_episode_ids[:8],
        "missing_run_episode_ids": missing_run_episode_ids[:8],
        "blocked_episode_ids": blocked_episode_ids[:8],
        "stage_execution_requires_running_workflow_run": True,
    }


def _ensure_workflow_run_started(
    episode: Episode,
    production_control: ProductionControlService,
    user_id: str,
) -> Episode:
    run = (episode.workflow_control or {}).get("run")
    if isinstance(run, dict) and run.get("state") == "running":
        return episode
    return production_control.begin_run(episode, user_id=user_id)


def _workflow_worker_can_start_run(episode: Episode) -> bool:
    if episode.status in {
        EpisodeStatus.cancelled,
        EpisodeStatus.completed,
        EpisodeStatus.failed,
    }:
        return False
    run = (episode.workflow_control or {}).get("run")
    if isinstance(run, dict) and run.get("state") == "running":
        return False
    if episode.status in {
        EpisodeStatus.draft,
        EpisodeStatus.ready,
        EpisodeStatus.researching,
        EpisodeStatus.research_review,
        EpisodeStatus.preparing_discussion,
        EpisodeStatus.discussing,
        EpisodeStatus.transcript_qc,
        EpisodeStatus.transcript_review,
        EpisodeStatus.localizing,
        EpisodeStatus.generating_audio,
        EpisodeStatus.generating_visuals,
        EpisodeStatus.building_timeline,
        EpisodeStatus.rendering_preview,
        EpisodeStatus.preview_qc,
        EpisodeStatus.preview_review,
        EpisodeStatus.rendering_final,
        EpisodeStatus.final_qc,
        EpisodeStatus.exporting,
    }:
        return True
    return False


def _has_running_workflow_run(episode: Episode) -> bool:
    run = (episode.workflow_control or {}).get("run")
    return isinstance(run, dict) and run.get("state") == "running"


def _canonical_transcript_requires_review(episode: Episode) -> bool:
    canonical_id = episode.canonical_transcript_version_id
    if canonical_id is None:
        return False
    canonical = next(
        (transcript for transcript in episode.transcripts if transcript.id == canonical_id),
        None,
    )
    return canonical is not None and canonical.status != "approved"


async def run_temporal_worker_once(
    repository: WorkflowWorkerRepository,
    settings: Settings,
    batch_limit: int,
    research_service: ResearchService | None = None,
    discussion_engine: DiscussionEngine | None = None,
    production_control: ProductionControlService | None = None,
    localization_service: LocalizationService | None = None,
    voicebox_service: VoiceboxService | None = None,
    subtitle_service: SubtitleService | None = None,
    comfyui_service: ComfyUiService | None = None,
    timeline_service: TimelineService | None = None,
    render_service: RenderService | None = None,
    publisher_service: PublisherService | None = None,
) -> dict:
    activity_order = [
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
    if settings.temporal_backend_mode.strip().lower() != "external":
        return _temporal_worker_inactive_summary(
            status="disabled",
            reason="DIALECTICORE_TEMPORAL_BACKEND_MODE is not external",
            settings=settings,
            batch_limit=batch_limit,
            activity_order=activity_order,
        )
    missing = _temporal_worker_missing_settings(settings)
    if missing:
        summary = _temporal_worker_inactive_summary(
            status="blocked",
            reason="external Temporal activity worker is blocked by missing runtime settings",
            settings=settings,
            batch_limit=batch_limit,
            activity_order=activity_order,
        )
        summary["missing"] = missing
        return summary

    research_service = research_service or ResearchService(settings)
    discussion_engine = discussion_engine or DiscussionEngine(ModelGateway(), settings)
    production_control = production_control or ProductionControlService(settings)
    localization_service = localization_service or LocalizationService()
    voicebox_service = voicebox_service or VoiceboxService(settings)
    subtitle_service = subtitle_service or SubtitleService(settings)
    comfyui_service = comfyui_service or ComfyUiService(settings)
    timeline_service = timeline_service or TimelineService(settings)
    render_service = render_service or RenderService(settings)
    publisher_service = publisher_service or PublisherService()

    stage_summaries = {
        "research": run_research_worker_once(
            repository=repository,
            research_service=research_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "discussion": await run_discussion_worker_once(
            repository=repository,
            discussion_engine=discussion_engine,
            production_control=production_control,
            batch_limit=batch_limit,
        ),
        "localization": run_localization_worker_once(
            repository=repository,
            localization_service=localization_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "qc": run_qc_worker_once(
            repository=repository,
            research_service=research_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "audio": await run_audio_production_worker_once(
            repository=repository,
            voicebox_service=voicebox_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "voicebox": await run_voicebox_adapter_once(
            repository=repository,
            voicebox_service=voicebox_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "subtitles": run_subtitle_worker_once(
            repository=repository,
            subtitle_service=subtitle_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "visuals": await run_visual_production_worker_once(
            repository=repository,
            comfyui_service=comfyui_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "comfyui": await run_comfyui_adapter_once(
            repository=repository,
            comfyui_service=comfyui_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "timeline": run_timeline_worker_once(
            repository=repository,
            timeline_service=timeline_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "render": queue_render_worker_once(
            repository=repository,
            render_service=render_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
        "publishing": run_publishing_worker_once(
            repository=repository,
            render_service=render_service,
            publisher_service=publisher_service,
            batch_limit=batch_limit,
            user_id="temporal-worker",
            automated_live_enabled=settings.publisher_automated_live_enabled,
        ),
        "completion": run_completion_worker_once(
            repository=repository,
            production_control=production_control,
            batch_limit=batch_limit,
            user_id="temporal-worker",
        ),
    }
    progressed_stage_count = sum(
        _workflow_stage_progress_count(stage, summary)
        for stage, summary in stage_summaries.items()
    )
    error_count = sum(int(summary.get("error_count") or 0) for summary in stage_summaries.values())
    summary = {
        "schema_version": "temporal_worker_execution_summary.v1",
        "orchestration_attempt_id": str(uuid4()),
        "policy": "external_temporal_stage_activity_worker_v1",
        "status": "running",
        "reason": "external Temporal stage activities executed by Docker temporal-worker",
        "namespace": settings.temporal_namespace,
        "task_queue": settings.temporal_task_queue,
        "backend_address": settings.temporal_backend_address,
        "batch_limit": batch_limit,
        "activity_order": list(stage_summaries),
        "stage_order": list(stage_summaries),
        "activities": {
            stage: _temporal_activity_execution_summary(
                stage,
                stage_summary,
                production_control,
            )
            for stage, stage_summary in stage_summaries.items()
        },
        "stages": stage_summaries,
        "progressed_stage_count": progressed_stage_count,
        "error_count": error_count,
    }
    summary["orchestration_records"] = _record_workflow_worker_orchestration(
        repository,
        production_control,
        summary,
        worker_id="temporal-worker",
    )
    return summary


def _temporal_worker_inactive_summary(
    status: str,
    reason: str,
    settings: Settings,
    batch_limit: int,
    activity_order: list[str],
) -> dict:
    return {
        "schema_version": "temporal_worker_execution_summary.v1",
        "policy": "external_temporal_stage_activity_worker_v1",
        "status": status,
        "reason": reason,
        "namespace": settings.temporal_namespace,
        "task_queue": settings.temporal_task_queue,
        "backend_address": settings.temporal_backend_address,
        "batch_limit": batch_limit,
        "activity_order": activity_order,
        "stage_order": activity_order,
        "activities": {},
        "stages": {},
        "progressed_stage_count": 0,
        "error_count": 0,
        "orchestration_records": [],
    }


def _temporal_worker_missing_settings(settings: Settings) -> list[str]:
    missing = []
    if not settings.temporal_backend_address:
        missing.append("DIALECTICORE_TEMPORAL_BACKEND_ADDRESS")
    if not settings.temporal_task_queue:
        missing.append("DIALECTICORE_TEMPORAL_TASK_QUEUE")
    if not settings.temporal_backend_worker_enabled:
        missing.append("DIALECTICORE_TEMPORAL_BACKEND_WORKER_ENABLED")
    return missing


def _temporal_activity_execution_summary(
    stage: str,
    stage_summary: dict,
    production_control: ProductionControlService,
) -> dict:
    progress_count = _workflow_stage_progress_count(stage, stage_summary)
    error_count = int(stage_summary.get("error_count") or 0)
    return {
        "schema_version": "temporal_stage_activity_execution.v1",
        "activity_name": f"dialecticore.production.{stage}",
        "status": "failed" if error_count else "progressed" if progress_count else "idle",
        "progress_count": progress_count,
        "error_count": error_count,
        "episodes_scanned": int(stage_summary.get("episodes_scanned") or 0),
        "skipped": int(stage_summary.get("skipped") or 0),
        "summary_checksum": production_control._stable_checksum(stage_summary),
    }


def _record_workflow_worker_orchestration(
    repository: WorkflowWorkerRepository,
    production_control: ProductionControlService,
    summary: dict,
    worker_id: str = "workflow-worker",
) -> list[dict]:
    records = []
    completed_episode_ids = _workflow_completion_episode_ids(summary)
    for episode in repository.list():
        run = (episode.workflow_control or {}).get("run")
        if not isinstance(run, dict):
            continue
        if run.get("state") != "running" and str(episode.id) not in completed_episode_ids:
            continue
        before_count = len((episode.workflow_control or {}).get("worker_orchestration_log", []))
        updated = production_control.record_worker_orchestration(
            episode,
            summary,
            worker_id=worker_id,
        )
        after_count = len((updated.workflow_control or {}).get("worker_orchestration_log", []))
        if after_count == before_count:
            continue
        repository.save(updated)
        records.append(
            {
                "episode_id": str(updated.id),
                "run_id": run.get("run_id"),
                "attempt_sequence": after_count,
                "retry_queue_count": len(
                    (updated.workflow_control or {}).get("stage_retry_queue", [])
                ),
            }
        )
    return records


def _workflow_completion_episode_ids(summary: dict) -> set[str]:
    stages = summary.get("stages")
    if not isinstance(stages, dict):
        return set()
    completion = stages.get("completion")
    if not isinstance(completion, dict):
        return set()
    return {
        str(episode_id)
        for episode_id in completion.get("completed_episode_ids", [])
        if episode_id
    }


def _apply_due_workflow_stage_retries(
    repository: WorkflowWorkerRepository,
    production_control: ProductionControlService,
    batch_limit: int,
    user_id: str,
) -> list[dict]:
    records = []
    scanned_count = 0
    for episode in repository.list():
        if scanned_count >= batch_limit:
            break
        scanned_count += 1
        record = production_control.retry_due_stage(episode, user_id=user_id)
        if record is None:
            continue
        repository.save(episode)
        records.append(record)
    return records


def _workflow_stage_progress_count(stage: str, summary: dict) -> int:
    return worker_stage_progress_count(stage, summary)


def _workflow_production_handoffs(repository: WorkflowWorkerRepository) -> list[dict]:
    return [
        _talkshow_production_handoff(episode)
        for episode in repository.list()
        if (episode.workflow_control or {}).get("run")
    ]


def _talkshow_production_handoff(episode: Episode) -> dict:
    transcript = _canonical_or_latest_broadcast_transcript(episode)
    final_render = _latest_final_render_asset(episode)
    package = _latest_export_package_asset(episode, final_render) if final_render else None
    manifest = (
        _latest_production_manifest_asset(episode, package)
        if package is not None
        else None
    )
    if transcript is None:
        return {
            "schema_version": "talkshow_production_handoff.v1",
            "episode_id": str(episode.id),
            "status": "blocked",
            "transcript_version_id": None,
            "blocking_reasons": ["approved_transcript_missing"],
        }

    playable_turn_ids = [
        str(turn.id) for turn in transcript.turns if turn.status != "excluded"
    ]
    playable_turn_id_set = set(playable_turn_ids)
    character_config_handoff = _character_config_handoff(episode, transcript)
    audio_turn_ids = _completed_audio_turn_ids(episode, transcript, playable_turn_id_set)
    primary_visual_turn_ids = _completed_primary_visual_turn_ids(
        episode,
        transcript,
        playable_turn_id_set,
    )
    subtitle_asset = _latest_completed_subtitle_asset(episode, transcript)
    timeline_asset = _latest_timeline_asset_for_transcript(episode, transcript)
    timeline = _timeline_payload(timeline_asset)
    segment_handoff = _timeline_segment_handoff(timeline, playable_turn_id_set)
    reusable_visual_handoff = _shot_planned_reusable_visual_handoff(
        episode,
        transcript,
        timeline,
        playable_turn_id_set,
    )
    localized_output_handoff = _localized_output_handoff(episode)
    preview_render = (
        _latest_render_asset(episode, timeline_asset, render_type="preview")
        if timeline_asset is not None
        else None
    )
    final_render = (
        _latest_render_asset(episode, timeline_asset, render_type="final")
        if timeline_asset is not None
        else final_render
    )
    thumbnail_asset = (
        _latest_thumbnail_asset(episode, final_render) if final_render is not None else None
    )
    final_package = _latest_export_package_asset(episode, final_render) if final_render else None
    production_manifest = (
        _latest_production_manifest_asset(episode, final_package)
        if final_package is not None
        else manifest
    )
    export_package_thumbnail_included = (
        _export_package_includes_thumbnail(final_package, thumbnail_asset)
        if final_package is not None and thumbnail_asset is not None
        else None
    )
    export_package_subtitles_included = (
        _export_package_includes_subtitles(final_package)
        if final_package is not None and subtitle_asset is not None
        else None
    )
    claim_qc = _latest_claim_qc_for_transcript(episode, transcript)
    preview_render_approved = (
        _preview_render_approved(episode, preview_render) if preview_render is not None else False
    )
    final_render_approved = (
        _final_render_approved(episode, final_render) if final_render is not None else False
    )
    preview_render_qc = (
        _latest_render_qc(episode, preview_render, "render_preview_integrity")
        if preview_render is not None
        else None
    )
    final_render_qc = (
        _latest_render_qc(episode, final_render, "render_final_integrity")
        if final_render is not None
        else None
    )
    package_qc = (
        _latest_package_qc(episode, final_package)
        if final_package is not None
        else None
    )
    production_manifest_validity = (
        _production_manifest_validity(
            production_manifest,
            final_package,
            thumbnail_asset,
            subtitle_asset,
        )
        if production_manifest is not None and final_package is not None
        else {"valid": False, "reason": "production manifest is missing"}
    )
    publish_job = (
        _latest_publish_job(episode, final_package)
        if final_package is not None
        else None
    )
    publish_qc = (
        _latest_publish_qc(episode, publish_job)
        if publish_job is not None
        else None
    )
    production_manifest_publish_evidence = _production_manifest_publish_evidence(
        production_manifest,
        publish_job,
        publish_qc,
    )

    missing_audio_turn_ids = sorted(playable_turn_id_set - audio_turn_ids)
    missing_primary_visual_turn_ids = sorted(playable_turn_id_set - primary_visual_turn_ids)
    stale_voice_asset_turn_ids = _voice_profile_mismatch_turn_ids(
        episode,
        transcript,
        playable_turn_id_set,
    )
    stale_visual_asset_turn_ids = _visual_profile_mismatch_turn_ids(
        episode,
        transcript,
        playable_turn_id_set,
    )
    stale_model_turn_ids = _model_assignment_mismatch_turn_ids(
        episode,
        transcript,
        playable_turn_id_set,
    )
    blocking_reasons = []
    if transcript.status != "approved":
        blocking_reasons.append("transcript_not_approved")
    if not playable_turn_ids:
        blocking_reasons.append("playable_turns_missing")
    if character_config_handoff["unknown_speaker_participant_ids"]:
        blocking_reasons.append("character_profile_missing")
    if character_config_handoff["missing_model_participant_ids"]:
        blocking_reasons.append("character_model_missing")
    if stale_model_turn_ids:
        blocking_reasons.append("character_model_turn_stale")
    if character_config_handoff["missing_voice_participant_ids"]:
        blocking_reasons.append("character_voice_missing")
    if character_config_handoff["missing_visual_participant_ids"]:
        blocking_reasons.append("character_visual_missing")
    if missing_audio_turn_ids:
        blocking_reasons.append("completed_audio_missing")
    if stale_voice_asset_turn_ids:
        blocking_reasons.append("character_voice_asset_stale")
    if missing_primary_visual_turn_ids:
        blocking_reasons.append("completed_character_visual_missing")
    if stale_visual_asset_turn_ids:
        blocking_reasons.append("character_visual_asset_stale")
    if subtitle_asset is None:
        blocking_reasons.append("subtitle_asset_missing")
    if timeline_asset is None:
        blocking_reasons.append("timeline_asset_missing")
    elif segment_handoff["missing_segment_turn_ids"]:
        blocking_reasons.append("timeline_segments_missing")
    if reusable_visual_handoff["missing_reaction_loop_turn_ids"]:
        blocking_reasons.append("shot_planned_reaction_loop_missing")
    if reusable_visual_handoff["missing_studio_scene_turn_ids"]:
        blocking_reasons.append("shot_planned_studio_scene_missing")
    if localized_output_handoff["missing_languages"]:
        blocking_reasons.append("localized_output_missing")
    if localized_output_handoff["not_approved_languages"]:
        blocking_reasons.append("localized_output_not_approved")
    if localized_output_handoff["qc_missing_languages"]:
        blocking_reasons.append("localized_output_qc_missing")
    if localized_output_handoff["qc_failing_languages"]:
        blocking_reasons.append("localized_output_qc_failing")
    if preview_render is None and final_render is None:
        blocking_reasons.append("render_asset_missing")
    if _quality_result_failing(preview_render_qc):
        blocking_reasons.append("preview_render_qc_failed")
    if _quality_result_failing(final_render_qc):
        blocking_reasons.append("final_render_qc_failed")
    if final_render_approved and final_package is not None and package_qc is None:
        blocking_reasons.append("export_package_qc_missing")
    if final_render_approved and _quality_result_failing(package_qc):
        blocking_reasons.append("export_package_qc_failed")
    if final_render_approved and final_render is not None and thumbnail_asset is None:
        blocking_reasons.append("thumbnail_missing")
    if (
        final_render_approved
        and final_package is not None
        and thumbnail_asset is not None
        and export_package_thumbnail_included is False
    ):
        blocking_reasons.append("export_package_thumbnail_missing")
    if (
        final_render_approved
        and final_package is not None
        and subtitle_asset is not None
        and export_package_subtitles_included is False
    ):
        blocking_reasons.append("export_package_subtitles_missing")
    if (
        final_render_approved
        and production_manifest is not None
        and production_manifest_validity["valid"] is False
    ):
        blocking_reasons.append("production_manifest_invalid")
    if publish_job is not None and publish_job.status == "completed" and publish_qc is None:
        blocking_reasons.append("publish_delivery_qc_missing")
    if publish_qc is not None and _publish_qc_blocks_handoff(publish_qc):
        blocking_reasons.append("publish_delivery_qc_failed")
    if (
        publish_job is not None
        and publish_job.status == "completed"
        and publish_qc is not None
        and not _publish_qc_blocks_handoff(publish_qc)
        and production_manifest_publish_evidence["valid"] is False
    ):
        blocking_reasons.append("production_manifest_publish_evidence_missing")
    if _claim_qc_blocks_handoff(episode, transcript, claim_qc):
        blocking_reasons.append("claim_qc_failed")

    if (
        final_render is not None
        and final_render_approved
        and final_package is not None
        and production_manifest is not None
        and package_qc is not None
        and not _quality_result_failing(package_qc)
        and production_manifest_validity["valid"] is True
        and publish_job is not None
        and publish_job.status == "completed"
        and publish_qc is not None
        and not _publish_qc_blocks_handoff(publish_qc)
        and production_manifest_publish_evidence["valid"] is True
        and not blocking_reasons
    ):
        status = "delivery_ready"
    elif not blocking_reasons and (preview_render is not None or final_render is not None):
        status = "review_ready"
    elif not blocking_reasons:
        status = "media_ready"
    else:
        status = "blocked"

    return {
        "schema_version": "talkshow_production_handoff.v1",
        "episode_id": str(episode.id),
        "status": status,
        "blocking_reasons": blocking_reasons,
        "transcript_version_id": str(transcript.id),
        "transcript_status": transcript.status,
        "language": transcript.language,
        "playable_turn_count": len(playable_turn_ids),
        "character_configuration": character_config_handoff,
        "localized_outputs": localized_output_handoff,
        "turn_handoffs": {
            "completed_audio_turn_count": len(audio_turn_ids),
            "completed_primary_visual_turn_count": len(primary_visual_turn_ids),
            "missing_audio_turn_ids": missing_audio_turn_ids,
            "missing_primary_visual_turn_ids": missing_primary_visual_turn_ids,
            "stale_model_turn_ids": stale_model_turn_ids,
            "stale_voice_asset_turn_ids": stale_voice_asset_turn_ids,
            "stale_visual_asset_turn_ids": stale_visual_asset_turn_ids,
        },
        "speech": {
            "ready": (
                not missing_audio_turn_ids
                and not stale_voice_asset_turn_ids
                and bool(playable_turn_ids)
            ),
            "completed_audio_asset_count": len(audio_turn_ids),
            "stale_voice_asset_turn_count": len(stale_voice_asset_turn_ids),
        },
        "character_animation": {
            "ready": (
                not missing_primary_visual_turn_ids
                and not stale_visual_asset_turn_ids
                and not reusable_visual_handoff["missing_reaction_loop_turn_ids"]
                and bool(playable_turn_ids)
            ),
            "completed_primary_visual_asset_count": len(primary_visual_turn_ids),
            "stale_visual_asset_turn_count": len(stale_visual_asset_turn_ids),
            "expected_reaction_loop_segment_count": (
                reusable_visual_handoff["expected_reaction_loop_segment_count"]
            ),
            "linked_reaction_loop_segment_count": (
                reusable_visual_handoff["linked_reaction_loop_segment_count"]
            ),
            "missing_reaction_loop_turn_ids": (
                reusable_visual_handoff["missing_reaction_loop_turn_ids"]
            ),
            "policy": "one_completed_primary_visual_asset_per_playable_turn",
        },
        "studio_scene": {
            "ready": not reusable_visual_handoff["missing_studio_scene_turn_ids"],
            "expected_studio_scene_segment_count": (
                reusable_visual_handoff["expected_studio_scene_segment_count"]
            ),
            "linked_studio_scene_segment_count": (
                reusable_visual_handoff["linked_studio_scene_segment_count"]
            ),
            "missing_studio_scene_turn_ids": (
                reusable_visual_handoff["missing_studio_scene_turn_ids"]
            ),
        },
        "subtitles": {
            "ready": subtitle_asset is not None,
            "subtitle_asset_id": str(subtitle_asset.id) if subtitle_asset else None,
        },
        "timeline": {
            "ready": timeline_asset is not None
            and not segment_handoff["missing_segment_turn_ids"]
            and not reusable_visual_handoff["missing_reaction_loop_turn_ids"]
            and not reusable_visual_handoff["missing_studio_scene_turn_ids"],
            "timeline_asset_id": str(timeline_asset.id) if timeline_asset else None,
            **segment_handoff,
            **reusable_visual_handoff,
        },
        "render": {
            "preview_render_asset_id": str(preview_render.id) if preview_render else None,
            "preview_render_approved": preview_render_approved,
            "preview_render_qc_status": preview_render_qc.status if preview_render_qc else None,
            "preview_render_qc_id": str(preview_render_qc.id) if preview_render_qc else None,
            "final_render_asset_id": str(final_render.id) if final_render else None,
            "final_render_approved": final_render_approved,
            "final_render_qc_status": final_render_qc.status if final_render_qc else None,
            "final_render_qc_id": str(final_render_qc.id) if final_render_qc else None,
            "thumbnail_asset_id": str(thumbnail_asset.id) if thumbnail_asset else None,
            "delivery_package_asset_id": str(final_package.id) if final_package else None,
            "delivery_package_qc_status": package_qc.status if package_qc else None,
            "delivery_package_qc_id": str(package_qc.id) if package_qc else None,
            "delivery_package_thumbnail_included": export_package_thumbnail_included,
            "delivery_package_subtitles_included": export_package_subtitles_included,
            "production_manifest_asset_id": (
                str(production_manifest.id) if production_manifest else None
            ),
            "production_manifest_valid": production_manifest_validity["valid"],
            "production_manifest_invalid_reason": production_manifest_validity["reason"],
            "production_manifest_publish_evidence_valid": (
                production_manifest_publish_evidence["valid"]
            ),
            "production_manifest_publish_evidence_reason": (
                production_manifest_publish_evidence["reason"]
            ),
        },
        "publish": {
            "ready": publish_job is not None
            and publish_job.status == "completed"
            and publish_qc is not None
            and not _publish_qc_blocks_handoff(publish_qc),
            "publish_job_id": str(publish_job.id) if publish_job else None,
            "publish_job_status": publish_job.status if publish_job else None,
            "publish_job_dry_run": publish_job.dry_run if publish_job else None,
            "publisher_target_id": publish_job.publisher_target_id if publish_job else None,
            "publish_url": publish_job.publish_url if publish_job else None,
            "publish_delivery_qc_id": str(publish_qc.id) if publish_qc else None,
            "publish_delivery_qc_status": publish_qc.status if publish_qc else None,
        },
        "claim_qc": {
            "status": claim_qc.status if claim_qc else None,
            "quality_result_id": str(claim_qc.id) if claim_qc else None,
            "editorially_accepted": _claim_qc_is_editorially_accepted(
                episode,
                transcript,
                claim_qc,
            ),
        },
    }


def _latest_render_qc(
    episode: Episode,
    render_asset: Asset,
    check_type: str,
):
    return next(
        (
            result
            for result in reversed(episode.quality_results)
            if result.target_type == "render_asset"
            and result.target_id == str(render_asset.id)
            and result.check_type == check_type
        ),
        None,
    )


def _localized_output_handoff(episode: Episode) -> dict:
    required_outputs = [
        output
        for output in episode.definition.languages.outputs
        if output.language != episode.source_language or output.mode != "canonical"
    ]
    outputs = []
    missing_languages = []
    not_approved_languages = []
    qc_missing_languages = []
    qc_failing_languages = []
    for output in required_outputs:
        transcript = _latest_localized_transcript(episode, output.language)
        qc = (
            _latest_transcript_qc(
                episode,
                transcript,
                "localized_transcript_semantic_fidelity",
            )
            if transcript is not None
            else None
        )
        transcript_status = transcript.status if transcript is not None else None
        qc_status = qc.status if qc is not None else None
        if transcript is None:
            missing_languages.append(output.language)
        elif transcript.status != "approved":
            not_approved_languages.append(output.language)
        if transcript is not None and qc is None:
            qc_missing_languages.append(output.language)
        elif _quality_result_failing(qc):
            qc_failing_languages.append(output.language)
        outputs.append(
            {
                "language": output.language,
                "mode": output.mode,
                "transcript_version_id": str(transcript.id) if transcript else None,
                "transcript_status": transcript_status,
                "qc_id": str(qc.id) if qc else None,
                "qc_status": qc_status,
            }
        )
    return {
        "schema_version": "localized_output_handoff.v1",
        "ready": not (
            missing_languages
            or not_approved_languages
            or qc_missing_languages
            or qc_failing_languages
        ),
        "required": bool(required_outputs),
        "required_language_count": len(required_outputs),
        "approved_language_count": sum(
            1 for item in outputs if item["transcript_status"] == "approved"
        ),
        "missing_languages": missing_languages,
        "not_approved_languages": not_approved_languages,
        "qc_missing_languages": qc_missing_languages,
        "qc_failing_languages": qc_failing_languages,
        "outputs": outputs,
    }


def _latest_localized_transcript(
    episode: Episode,
    language: str,
) -> TranscriptVersion | None:
    return next(
        (
            transcript
            for transcript in reversed(episode.transcripts)
            if transcript.type == TranscriptType.localized and transcript.language == language
        ),
        None,
    )


def _latest_transcript_qc(
    episode: Episode,
    transcript: TranscriptVersion,
    check_type: str,
):
    return next(
        (
            result
            for result in reversed(episode.quality_results)
            if result.target_type == "transcript_version"
            and result.target_id == str(transcript.id)
            and result.check_type == check_type
        ),
        None,
    )


def _latest_package_qc(episode: Episode, package_asset: Asset):
    return next(
        (
            result
            for result in reversed(episode.quality_results)
            if result.check_type == "youtube_package_integrity"
            and result.target_id == str(package_asset.id)
        ),
        None,
    )


def _production_manifest_validity(
    manifest_asset: Asset,
    package_asset: Asset,
    thumbnail_asset: Asset | None = None,
    subtitle_asset: Asset | None = None,
) -> dict:
    manifest = manifest_asset.generation_metadata.get("production_manifest")
    if not isinstance(manifest, dict):
        return {"valid": False, "reason": "embedded production_manifest is missing"}
    if manifest.get("schema_version") != "production_manifest.v1":
        return {
            "valid": False,
            "reason": "embedded production_manifest schema_version is invalid",
        }
    delivery_package = manifest.get("delivery_package")
    if not isinstance(delivery_package, dict) or not delivery_package.get("asset_id"):
        return {
            "valid": False,
            "reason": "embedded delivery package asset_id is missing",
        }
    if str(delivery_package.get("asset_id")) != str(package_asset.id):
        return {
            "valid": False,
            "reason": "embedded delivery package asset_id does not match package asset",
        }
    embedded_checksum = delivery_package.get("checksum")
    if (
        embedded_checksum
        and package_asset.checksum
        and str(embedded_checksum) != str(package_asset.checksum)
    ):
        return {
            "valid": False,
            "reason": "embedded delivery package checksum does not match package asset",
        }
    embedded_storage_uri = delivery_package.get("storage_uri")
    if (
        embedded_storage_uri
        and package_asset.storage_uri
        and str(embedded_storage_uri) != str(package_asset.storage_uri)
    ):
        return {
            "valid": False,
            "reason": "embedded delivery package storage_uri does not match package asset",
        }
    embedded_delivery_package_id = delivery_package.get("package_id")
    current_package_id = package_asset.generation_metadata.get("package_id")
    if (
        embedded_delivery_package_id
        and current_package_id
        and str(embedded_delivery_package_id) != str(current_package_id)
    ):
        return {
            "valid": False,
            "reason": "embedded delivery package package_id does not match package asset",
        }
    chapter_validity = _production_manifest_chapters_valid(manifest, delivery_package)
    if chapter_validity["valid"] is False:
        return chapter_validity
    if thumbnail_asset is not None:
        embedded_package_manifest = delivery_package.get("manifest")
        if not isinstance(embedded_package_manifest, dict):
            return {
                "valid": False,
                "reason": "embedded delivery package manifest is missing",
            }
        if str(embedded_package_manifest.get("thumbnail_asset_id") or "") != str(
            thumbnail_asset.id
        ):
            return {
                "valid": False,
                "reason": "embedded delivery package thumbnail does not match thumbnail asset",
            }
        embedded_files = delivery_package.get("included_files")
        if isinstance(embedded_files, list) and "thumbnail/thumbnail.jpg" not in embedded_files:
            return {
                "valid": False,
                "reason": "embedded delivery package thumbnail file is missing",
            }
    if subtitle_asset is not None:
        embedded_files = delivery_package.get("included_files")
        if isinstance(embedded_files, list) and not any(
            isinstance(name, str) and name.startswith("subtitles/")
            for name in embedded_files
        ):
            return {
                "valid": False,
                "reason": "embedded delivery package subtitle file is missing",
            }
        embedded_package_manifest = delivery_package.get("manifest")
        if not isinstance(embedded_package_manifest, dict):
            return {
                "valid": False,
                "reason": "embedded delivery package manifest is missing",
            }
        embedded_subtitles = embedded_package_manifest.get("subtitles")
        if not isinstance(embedded_subtitles, list) or not embedded_subtitles:
            return {
                "valid": False,
                "reason": "embedded delivery package subtitle manifest is missing",
            }
    return {"valid": True, "reason": None}


def _production_manifest_chapters_valid(manifest: dict, delivery_package: dict) -> dict:
    timeline = manifest.get("timeline")
    if not isinstance(timeline, dict):
        return {"valid": True, "reason": None}
    expected_chapters = timeline.get("chapters")
    expected_count = int(timeline.get("chapter_count") or 0)
    if expected_count <= 0 and not expected_chapters:
        return {"valid": True, "reason": None}
    if not isinstance(expected_chapters, list) or len(expected_chapters) < expected_count:
        return {
            "valid": False,
            "reason": "embedded production manifest timeline chapters are missing",
        }
    package_manifest = delivery_package.get("manifest")
    if not isinstance(package_manifest, dict):
        return {
            "valid": False,
            "reason": "embedded delivery package manifest is missing",
        }
    package_chapters = package_manifest.get("chapters")
    if not _chapter_entries_match(expected_chapters, package_chapters):
        return {
            "valid": False,
            "reason": "embedded delivery package chapters do not match timeline chapters",
        }
    return {"valid": True, "reason": None}


def _chapter_entries_match(expected: list, actual: object) -> bool:
    if not isinstance(actual, list) or len(actual) < len(expected):
        return False
    actual_by_start = {
        int(chapter.get("start_ms") or 0): chapter
        for chapter in actual
        if isinstance(chapter, dict)
    }
    for chapter in expected:
        if not isinstance(chapter, dict):
            continue
        start_ms = int(chapter.get("start_ms") or 0)
        actual_chapter = actual_by_start.get(start_ms)
        if actual_chapter is None:
            return False
        if str(actual_chapter.get("title") or "") != str(chapter.get("title") or ""):
            return False
    return True


def _export_package_includes_thumbnail(
    package_asset: Asset | None,
    thumbnail_asset: Asset | None,
) -> bool:
    if package_asset is None or thumbnail_asset is None:
        return False
    metadata = package_asset.generation_metadata
    package_thumbnail_id = metadata.get("thumbnail_asset_id")
    if package_thumbnail_id is not None and str(package_thumbnail_id) != str(thumbnail_asset.id):
        return False
    manifest = metadata.get("youtube_package_manifest")
    if isinstance(manifest, dict):
        manifest_thumbnail_id = manifest.get("thumbnail_asset_id")
        if manifest_thumbnail_id is not None and str(manifest_thumbnail_id) != str(
            thumbnail_asset.id
        ):
            return False
    included_files = metadata.get("included_files")
    if isinstance(included_files, list):
        return "thumbnail/thumbnail.jpg" in included_files
    return package_thumbnail_id is not None or (
        isinstance(manifest, dict) and manifest.get("thumbnail_asset_id") is not None
    )


def _export_package_includes_subtitles(package_asset: Asset | None) -> bool:
    if package_asset is None:
        return False
    metadata = package_asset.generation_metadata
    included_files = metadata.get("included_files")
    if isinstance(included_files, list):
        return any(
            isinstance(name, str) and name.startswith("subtitles/")
            for name in included_files
        )
    manifest = metadata.get("youtube_package_manifest")
    if isinstance(manifest, dict):
        subtitles = manifest.get("subtitles")
        return isinstance(subtitles, list) and bool(subtitles)
    return False


def _production_manifest_publish_evidence(
    manifest_asset: Asset | None,
    publish_job,
    publish_qc,
) -> dict:
    if publish_job is None:
        return {"valid": True, "reason": None}
    if manifest_asset is None:
        return {"valid": False, "reason": "production manifest is missing"}
    manifest = manifest_asset.generation_metadata.get("production_manifest")
    if not isinstance(manifest, dict):
        return {"valid": False, "reason": "embedded production_manifest is missing"}
    publish_jobs = manifest.get("publish_jobs")
    if not isinstance(publish_jobs, list):
        return {"valid": False, "reason": "embedded publish_jobs is missing"}
    embedded_job = next(
        (job for job in publish_jobs if str(job.get("id")) == str(publish_job.id)),
        None,
    )
    if embedded_job is None:
        return {
            "valid": False,
            "reason": "embedded publish_jobs does not include latest publish job",
        }
    if embedded_job.get("status") != publish_job.status:
        return {
            "valid": False,
            "reason": "embedded publish job status does not match latest publish job",
        }
    if str(embedded_job.get("package_asset_id")) != str(publish_job.package_asset_id):
        return {
            "valid": False,
            "reason": "embedded publish job package does not match latest publish job",
        }
    if publish_qc is None:
        return {"valid": True, "reason": None}
    quality_results = manifest.get("quality_results")
    if not isinstance(quality_results, list):
        return {"valid": False, "reason": "embedded quality_results is missing"}
    embedded_qc = next(
        (result for result in quality_results if str(result.get("id")) == str(publish_qc.id)),
        None,
    )
    if embedded_qc is None:
        return {
            "valid": False,
            "reason": "embedded quality_results does not include latest publish QC",
        }
    if embedded_qc.get("check_type") != "publish_delivery_integrity":
        return {"valid": False, "reason": "embedded publish QC check_type is invalid"}
    if embedded_qc.get("target_type") != "publish_job":
        return {"valid": False, "reason": "embedded publish QC target_type is invalid"}
    if str(embedded_qc.get("target_id")) != str(publish_job.id):
        return {
            "valid": False,
            "reason": "embedded publish QC target does not match latest publish job",
        }
    if embedded_qc.get("status") != publish_qc.status:
        return {
            "valid": False,
            "reason": "embedded publish QC status does not match latest publish QC",
        }
    return {"valid": True, "reason": None}


def _quality_result_failing(result) -> bool:
    return result is not None and (
        result.status == "fail" or result.severity == QualitySeverity.fail
    )


def _completed_audio_turn_ids(
    episode: Episode,
    transcript: TranscriptVersion,
    playable_turn_ids: set[str],
) -> set[str]:
    return {
        asset.source_entity_id
        for asset in episode.assets
        if asset.asset_type == AssetType.audio
        and asset.status == "completed"
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.source_entity_id in playable_turn_ids
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
    }


def _completed_primary_visual_turn_ids(
    episode: Episode,
    transcript: TranscriptVersion,
    playable_turn_ids: set[str],
) -> set[str]:
    return {
        asset.source_entity_id
        for asset in episode.assets
        if asset.asset_type == AssetType.video
        and asset.status == "completed"
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.source_entity_id in playable_turn_ids
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and asset.generation_metadata.get("visual_role") == "video_primary"
    }


def _voice_profile_mismatch_turn_ids(
    episode: Episode,
    transcript: TranscriptVersion,
    playable_turn_ids: set[str],
) -> list[str]:
    participant_by_id = {participant.id: participant for participant in episode.participants}
    speaker_by_turn_id = {
        str(turn.id): turn.speaker_participant_id
        for turn in transcript.turns
        if str(turn.id) in playable_turn_ids
    }
    latest_audio_by_turn_id = _latest_completed_turn_assets(
        episode,
        transcript,
        playable_turn_ids,
        AssetType.audio,
    )
    stale_turn_ids = []
    for turn_id, asset in latest_audio_by_turn_id.items():
        participant = participant_by_id.get(speaker_by_turn_id.get(turn_id) or "")
        expected_profile_id = participant.voice_profile_id if participant else None
        actual_profile_id = asset.generation_metadata.get("voice_profile_id")
        if (
            isinstance(expected_profile_id, str)
            and expected_profile_id
            and isinstance(actual_profile_id, str)
            and actual_profile_id
            and actual_profile_id != expected_profile_id
        ):
            stale_turn_ids.append(turn_id)
    return sorted(stale_turn_ids)


def _visual_profile_mismatch_turn_ids(
    episode: Episode,
    transcript: TranscriptVersion,
    playable_turn_ids: set[str],
) -> list[str]:
    participant_by_id = {participant.id: participant for participant in episode.participants}
    speaker_by_turn_id = {
        str(turn.id): turn.speaker_participant_id
        for turn in transcript.turns
        if str(turn.id) in playable_turn_ids
    }
    latest_visual_by_turn_id = _latest_completed_turn_assets(
        episode,
        transcript,
        playable_turn_ids,
        AssetType.video,
        visual_role="video_primary",
    )
    stale_turn_ids = []
    for turn_id, asset in latest_visual_by_turn_id.items():
        participant = participant_by_id.get(speaker_by_turn_id.get(turn_id) or "")
        expected_profile_id = participant.visual_profile_id if participant else None
        actual_profile_id = asset.generation_metadata.get("visual_profile_id")
        if (
            isinstance(expected_profile_id, str)
            and expected_profile_id
            and isinstance(actual_profile_id, str)
            and actual_profile_id
            and actual_profile_id != expected_profile_id
        ):
            stale_turn_ids.append(turn_id)
    return sorted(stale_turn_ids)


def _model_assignment_mismatch_turn_ids(
    episode: Episode,
    transcript: TranscriptVersion,
    playable_turn_ids: set[str],
) -> list[str]:
    if episode.discussion_session is None:
        return []
    participant_by_id = {participant.id: participant for participant in episode.participants}
    discussion_turn_by_id = {
        str(turn.id): turn for turn in episode.discussion_session.turns
    }
    stale_turn_ids = []
    for transcript_turn in transcript.turns:
        turn_id = str(transcript_turn.id)
        if turn_id not in playable_turn_ids:
            continue
        participant = participant_by_id.get(transcript_turn.speaker_participant_id)
        if participant is None:
            continue
        source_turn = next(
            (
                discussion_turn_by_id.get(str(source_id))
                for source_id in transcript_turn.source_discussion_turn_ids
                if discussion_turn_by_id.get(str(source_id)) is not None
            ),
            None,
        )
        if source_turn is None:
            continue
        metadata = source_turn.generation_metadata
        actual_endpoint_id = metadata.get("model_endpoint_id")
        actual_model_id = metadata.get("model_id")
        if (
            isinstance(actual_endpoint_id, str)
            and actual_endpoint_id
            and actual_endpoint_id != participant.model_endpoint_id
        ):
            stale_turn_ids.append(turn_id)
            continue
        if (
            isinstance(actual_model_id, str)
            and actual_model_id
            and actual_model_id != participant.model_id
        ):
            stale_turn_ids.append(turn_id)
    return sorted(stale_turn_ids)


def _latest_completed_turn_assets(
    episode: Episode,
    transcript: TranscriptVersion,
    playable_turn_ids: set[str],
    asset_type: AssetType,
    visual_role: str | None = None,
) -> dict[str, Asset]:
    assets_by_turn_id: dict[str, Asset] = {}
    for asset in reversed(episode.assets):
        if asset.source_entity_id in assets_by_turn_id:
            continue
        if asset.asset_type != asset_type:
            continue
        if asset.status != "completed":
            continue
        if asset.language != transcript.language:
            continue
        if asset.source_entity_type != "transcript_turn":
            continue
        if asset.source_entity_id not in playable_turn_ids:
            continue
        if asset.generation_metadata.get("transcript_version_id") != str(transcript.id):
            continue
        if visual_role is not None and asset.generation_metadata.get("visual_role") != visual_role:
            continue
        assets_by_turn_id[asset.source_entity_id] = asset
    return assets_by_turn_id


def _latest_completed_subtitle_asset(
    episode: Episode,
    transcript: TranscriptVersion,
) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.subtitle
            and asset.status == "completed"
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_version"
            and asset.source_entity_id == str(transcript.id)
        ),
        None,
    )


def _timeline_payload(timeline_asset: Asset | None) -> dict | None:
    if timeline_asset is None:
        return None
    timeline = timeline_asset.generation_metadata.get("timeline_json")
    return timeline if isinstance(timeline, dict) else None


def _timeline_segment_handoff(
    timeline: dict | None,
    playable_turn_ids: set[str],
) -> dict:
    if timeline is None:
        return {
            "segment_count": 0,
            "linked_turn_count": 0,
            "audio_linked_segment_count": 0,
            "visual_linked_segment_count": 0,
            "subtitle_linked_segment_count": 0,
            "missing_segment_turn_ids": sorted(playable_turn_ids),
        }
    segments = [
        segment for segment in timeline.get("segments", []) if isinstance(segment, dict)
    ]
    linked_turn_ids = {
        segment["source_turn_id"]
        for segment in segments
        if isinstance(segment.get("source_turn_id"), str)
    }
    return {
        "segment_count": len(segments),
        "linked_turn_count": len(linked_turn_ids & playable_turn_ids),
        "audio_linked_segment_count": sum(
            1 for segment in segments if isinstance(segment.get("audio_asset_id"), str)
        ),
        "visual_linked_segment_count": sum(
            1
            for segment in segments
            if isinstance(segment.get("video_asset_id"), str)
            or isinstance(segment.get("fallback_video_asset_id"), str)
            or bool(segment.get("visual_layers"))
        ),
        "subtitle_linked_segment_count": sum(
            1 for segment in segments if isinstance(segment.get("subtitle_asset_id"), str)
        ),
        "missing_segment_turn_ids": sorted(playable_turn_ids - linked_turn_ids),
    }


def _shot_planned_reusable_visual_handoff(
    episode: Episode,
    transcript: TranscriptVersion,
    timeline: dict | None,
    playable_turn_ids: set[str],
) -> dict:
    if timeline is None:
        return {
            "expected_reaction_loop_segment_count": 0,
            "linked_reaction_loop_segment_count": 0,
            "missing_reaction_loop_turn_ids": [],
            "expected_studio_scene_segment_count": 0,
            "linked_studio_scene_segment_count": 0,
            "missing_studio_scene_turn_ids": [],
        }

    segments_by_turn_id = {
        segment.get("source_turn_id"): segment
        for segment in timeline.get("segments", [])
        if isinstance(segment, dict) and isinstance(segment.get("source_turn_id"), str)
    }
    assets_by_id = {
        str(asset.id): asset for asset in episode.assets if asset.status != "replaced"
    }
    primary_assets_by_turn_id = {
        asset.source_entity_id: asset
        for asset in episode.assets
        if asset.asset_type == AssetType.video
        and asset.status == "completed"
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.source_entity_id in playable_turn_ids
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and asset.generation_metadata.get("visual_role") == "video_primary"
    }
    expected_reaction_turn_ids = set()
    linked_reaction_turn_ids = set()
    expected_studio_turn_ids = set()
    linked_studio_turn_ids = set()

    for turn_id, primary_asset in primary_assets_by_turn_id.items():
        shot_plan = primary_asset.generation_metadata.get("shot_plan")
        if not isinstance(shot_plan, dict):
            continue
        segment = segments_by_turn_id.get(turn_id)
        expected_reaction_id = _non_empty_string(
            shot_plan.get("reusable_reaction_asset_id")
        )
        if expected_reaction_id is not None:
            expected_reaction_turn_ids.add(turn_id)
            expected_reaction = assets_by_id.get(expected_reaction_id)
            if (
                segment is not None
                and segment.get("reaction_visual_asset_id") == expected_reaction_id
                and _asset_completed_render_ready(
                    expected_reaction,
                    AssetType.reaction_loop,
                )
            ):
                linked_reaction_turn_ids.add(turn_id)
        expected_studio_id = _non_empty_string(shot_plan.get("studio_scene_asset_id"))
        if expected_studio_id is not None:
            expected_studio_turn_ids.add(turn_id)
            expected_studio = assets_by_id.get(expected_studio_id)
            if (
                segment is not None
                and segment.get("studio_scene_asset_id") == expected_studio_id
                and _asset_completed_render_ready(
                    expected_studio,
                    AssetType.studio_scene,
                )
            ):
                linked_studio_turn_ids.add(turn_id)

    return {
        "expected_reaction_loop_segment_count": len(expected_reaction_turn_ids),
        "linked_reaction_loop_segment_count": len(linked_reaction_turn_ids),
        "missing_reaction_loop_turn_ids": sorted(
            expected_reaction_turn_ids - linked_reaction_turn_ids
        ),
        "expected_studio_scene_segment_count": len(expected_studio_turn_ids),
        "linked_studio_scene_segment_count": len(linked_studio_turn_ids),
        "missing_studio_scene_turn_ids": sorted(
            expected_studio_turn_ids - linked_studio_turn_ids
        ),
    }


def _character_config_handoff(
    episode: Episode,
    transcript: TranscriptVersion,
) -> dict:
    participant_by_id = {participant.id: participant for participant in episode.participants}
    active_speaker_ids = sorted(
        {
            turn.speaker_participant_id
            for turn in transcript.turns
            if turn.status != "excluded" and turn.speaker_participant_id
        }
    )
    unknown_speaker_ids = [
        participant_id
        for participant_id in active_speaker_ids
        if participant_id not in participant_by_id
    ]
    participant_entries = []
    missing_model_ids = []
    missing_voice_ids = []
    missing_visual_ids = []

    for participant_id in active_speaker_ids:
        participant = participant_by_id.get(participant_id)
        if participant is None:
            participant_entries.append(
                {
                    "participant_id": participant_id,
                    "display_name": None,
                    "participant_type": None,
                    "model_endpoint_id": None,
                    "model_id": None,
                    "voice_profile_id": None,
                    "visual_profile_id": None,
                    "model_ready": False,
                    "voice_ready": False,
                    "visual_ready": False,
                }
            )
            continue

        model_ready = bool(participant.model_endpoint_id and participant.model_id)
        voice_ready = bool(participant.voice_profile_id)
        visual_ready = bool(participant.visual_profile_id)
        if not model_ready:
            missing_model_ids.append(participant_id)
        if not voice_ready:
            missing_voice_ids.append(participant_id)
        if not visual_ready:
            missing_visual_ids.append(participant_id)
        participant_entries.append(
            {
                "participant_id": participant_id,
                "display_name": participant.display_name,
                "participant_type": participant.participant_type.value
                if hasattr(participant.participant_type, "value")
                else str(participant.participant_type),
                "model_endpoint_id": participant.model_endpoint_id,
                "model_id": participant.model_id,
                "voice_profile_id": participant.voice_profile_id,
                "visual_profile_id": participant.visual_profile_id,
                "model_ready": model_ready,
                "voice_ready": voice_ready,
                "visual_ready": visual_ready,
            }
        )

    ready = (
        bool(active_speaker_ids)
        and not unknown_speaker_ids
        and not missing_model_ids
        and not missing_voice_ids
        and not missing_visual_ids
    )
    return {
        "schema_version": "character_configuration_handoff.v1",
        "ready": ready,
        "policy": "each_playable_speaker_requires_model_voice_and_visual_profile",
        "active_speaker_count": len(active_speaker_ids),
        "configured_model_speaker_count": len(active_speaker_ids) - len(missing_model_ids),
        "configured_voice_speaker_count": len(active_speaker_ids) - len(missing_voice_ids),
        "configured_visual_speaker_count": len(active_speaker_ids) - len(missing_visual_ids),
        "unknown_speaker_participant_ids": unknown_speaker_ids,
        "missing_model_participant_ids": missing_model_ids,
        "missing_voice_participant_ids": missing_voice_ids,
        "missing_visual_participant_ids": missing_visual_ids,
        "participants": participant_entries,
    }


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _asset_completed_render_ready(asset: Asset | None, asset_type: AssetType) -> bool:
    return (
        asset is not None
        and asset.asset_type == asset_type
        and asset.status == "completed"
        and asset.generation_metadata.get("render_ready") is not False
    )


def _latest_claim_qc_for_transcript(episode: Episode, transcript: TranscriptVersion):
    return next(
        (
            result
            for result in reversed(episode.quality_results)
            if result.check_type == "claim_citation_integrity"
            and result.target_type == "transcript_version"
            and result.target_id == str(transcript.id)
        ),
        None,
    )


def _claim_qc_is_editorially_accepted(
    episode: Episode,
    transcript: TranscriptVersion,
    claim_qc,
) -> bool:
    if claim_qc is None or transcript.status != "approved":
        return False
    if claim_qc.target_id != str(transcript.id):
        return False
    return any(
        approval.stage == "transcript_review"
        and approval.decision == "approved"
        and (
            approval.target_id == str(transcript.id)
            or approval.target_id is None
        )
        for approval in episode.approvals
    )


def _claim_qc_blocks_handoff(
    episode: Episode,
    transcript: TranscriptVersion,
    claim_qc,
) -> bool:
    return bool(
        claim_qc is not None
        and (claim_qc.status == "fail" or claim_qc.severity == QualitySeverity.fail)
        and episode.definition.quality.block_on_unsupported_high_impact_claims
        and not _claim_qc_is_editorially_accepted(episode, transcript, claim_qc)
    )


def _has_pending_approval(episode: Episode, stage: str) -> bool:
    return any(
        approval.stage == stage and approval.decision == "pending"
        for approval in episode.approvals
    )


def _latest_approval_decision(episode: Episode, stage: str) -> str | None:
    approval = next(
        (
            item
            for item in sorted(
                episode.approvals,
                key=lambda approval: approval.created_at,
                reverse=True,
            )
            if item.stage == stage
        ),
        None,
    )
    return approval.decision if approval else None


def _final_render_approved(episode: Episode, render_asset: Asset) -> bool:
    if render_asset.generation_metadata.get("approval_status") == "approved":
        return True
    return any(
        approval.stage == "final_render_review"
        and approval.target_type == "render_asset"
        and approval.target_id == str(render_asset.id)
        and approval.decision == "approved"
        for approval in episode.approvals
    )


def _preview_render_approved(episode: Episode, render_asset: Asset) -> bool:
    if render_asset.generation_metadata.get("approval_status") == "approved":
        return True
    return any(
        approval.stage == "preview_render_review"
        and approval.target_type == "render_asset"
        and approval.target_id == str(render_asset.id)
        and approval.decision == "approved"
        for approval in episode.approvals
    )


def _discussion_model_configuration(episode: Episode) -> dict:
    active_participants = [
        participant for participant in episode.participants if participant.enabled
    ]
    endpoint_by_id = {endpoint.id: endpoint for endpoint in episode.model_endpoints}
    missing_model_ids = [
        participant.id for participant in active_participants if not participant.model_id.strip()
    ]
    unknown_endpoint_participant_ids = [
        participant.id
        for participant in active_participants
        if participant.model_endpoint_id not in endpoint_by_id
    ]
    disabled_endpoint_participant_ids = [
        participant.id
        for participant in active_participants
        if participant.model_endpoint_id in endpoint_by_id
        and endpoint_by_id[participant.model_endpoint_id].enabled is False
    ]
    return {
        "schema_version": "discussion_model_configuration.v1",
        "ready": not (
            missing_model_ids
            or unknown_endpoint_participant_ids
            or disabled_endpoint_participant_ids
        ),
        "active_participant_count": len(active_participants),
        "configured_model_participant_count": sum(
            1
            for participant in active_participants
            if participant.model_id.strip()
            and participant.model_endpoint_id in endpoint_by_id
            and endpoint_by_id[participant.model_endpoint_id].enabled is True
        ),
        "missing_model_participant_ids": missing_model_ids,
        "unknown_model_endpoint_participant_ids": unknown_endpoint_participant_ids,
        "disabled_model_endpoint_participant_ids": disabled_endpoint_participant_ids,
    }


def _discussion_worker_can_start(episode: Episode) -> bool:
    if episode.canonical_transcript_version_id is not None:
        return False
    if _has_pending_approval(episode, "research_review"):
        return False
    if _latest_approval_decision(episode, "research_review") == "rejected":
        return False
    if episode.definition.research.enabled and _latest_evidence_pack_asset(episode) is None:
        return False
    if episode.status in {EpisodeStatus.draft, EpisodeStatus.ready}:
        return episode.discussion_session is None
    if episode.status == EpisodeStatus.discussing:
        if episode.discussion_session is None:
            return True
        return (
            not episode.discussion_session.turns
            and episode.discussion_session.status != "completed"
        )
    return False


def _workflow_control_blocks_stage_work(episode: Episode) -> bool:
    workflow_control = episode.workflow_control or {}
    if workflow_control.get("paused") is True or workflow_control.get("cancelled") is True:
        return True
    return episode.status == EpisodeStatus.cancelled


def _get_repository_episode(
    repository: WorkflowWorkerRepository,
    episode_id: UUID,
) -> Episode | None:
    get_episode = getattr(repository, "get", None)
    if not callable(get_episode):
        return None
    with suppress(KeyError):
        return get_episode(episode_id)
    return None


def _latest_evidence_pack_asset(episode: Episode) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.evidence_pack and asset.status == "completed"
        ),
        None,
    )


def _canonical_or_latest_broadcast_transcript(episode: Episode) -> TranscriptVersion | None:
    if episode.canonical_transcript_version_id is not None:
        transcript = next(
            (
                item
                for item in episode.transcripts
                if item.id == episode.canonical_transcript_version_id
            ),
            None,
        )
        if transcript is not None:
            return transcript
    return next(
        (
            transcript
            for transcript in reversed(episode.transcripts)
            if transcript.type == TranscriptType.broadcast
        ),
        None,
    )


def _approved_canonical_or_latest_broadcast_transcript(
    episode: Episode,
) -> TranscriptVersion | None:
    transcript = _canonical_or_latest_broadcast_transcript(episode)
    if transcript is None or transcript.status != "approved":
        return None
    return transcript


def _claim_qc_exists(
    episode: Episode,
    evidence_asset: Asset,
    transcript: TranscriptVersion,
) -> bool:
    return any(
        result.check_type == "claim_citation_integrity"
        and result.target_type == "transcript_version"
        and result.target_id == str(transcript.id)
        and result.details.get("evidence_pack_asset_id") == str(evidence_asset.id)
        for result in episode.quality_results
    )


def _missing_localized_languages(episode: Episode) -> list[str]:
    canonical = _canonical_or_latest_broadcast_transcript(episode)
    if canonical is None or canonical.status != "approved":
        return []
    configured = [
        output.language
        for output in episode.definition.languages.outputs
        if output.language != episode.source_language or output.mode != "canonical"
    ]
    existing = {
        transcript.language
        for transcript in episode.transcripts
        if transcript.type == TranscriptType.localized
    }
    return sorted(language for language in configured if language not in existing)


def _production_transcript_candidates(episode: Episode) -> list[TranscriptVersion]:
    """Return the canonical broadcast and its approved current localizations.

    Superseded approved broadcasts remain audit history, not active production
    inputs. Without this boundary, a worker can revive an old transcript solely
    because it has unfinished media assets.
    """
    canonical = _approved_canonical_or_latest_broadcast_transcript(episode)
    if canonical is None:
        candidates = [
            transcript
            for transcript in episode.transcripts
            if transcript.type in {TranscriptType.broadcast, TranscriptType.localized}
            and transcript.status == "approved"
        ]
        return sorted(candidates, key=lambda transcript: transcript.created_at, reverse=True)

    candidates = [canonical]
    localized_by_language: dict[str, list[TranscriptVersion]] = {}
    for transcript in episode.transcripts:
        if (
            transcript.type == TranscriptType.localized
            and transcript.status == "approved"
            and transcript.parent_version_id == canonical.id
        ):
            localized_by_language.setdefault(transcript.language, []).append(transcript)
    for language in sorted(localized_by_language):
        candidates.append(
            max(localized_by_language[language], key=lambda transcript: transcript.created_at)
        )
    return candidates


def _target_transcript_without_timeline(episode: Episode) -> TranscriptVersion | None:
    for transcript in _production_transcript_candidates(episode):
        if _latest_timeline_asset_for_transcript(episode, transcript) is None:
            return transcript
    return None


def _target_transcript_needing_audio(episode: Episode) -> TranscriptVersion | None:
    for transcript in _production_transcript_candidates(episode):
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        if not playable_turns:
            continue
        if _transcript_has_missing_audio_assets(episode, transcript):
            return transcript
        if _audio_worker_target_asset_count(episode, transcript) > 0:
            return transcript
    return None


def _transcript_has_missing_audio_assets(
    episode: Episode,
    transcript: TranscriptVersion,
) -> bool:
    playable_turn_ids = {str(turn.id) for turn in transcript.turns if turn.status != "excluded"}
    if not playable_turn_ids:
        return False
    existing_turn_ids = {
        asset.source_entity_id
        for asset in episode.assets
        if asset.asset_type == AssetType.audio
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and asset.status in {"planned", "submitted", "running", "failed", "cancelled", "completed"}
    }
    return bool(playable_turn_ids - existing_turn_ids)


def _audio_asset_count(
    episode: Episode,
    transcript: TranscriptVersion,
    statuses: set[str],
) -> int:
    return sum(
        1
        for asset in episode.assets
        if asset.asset_type == AssetType.audio
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and asset.status in statuses
    )


def _audio_worker_target_asset_count(
    episode: Episode,
    transcript: TranscriptVersion,
) -> int:
    return len(_audio_generation_target_asset_ids(episode, transcript))


def _audio_repair_target_count(
    episode: Episode,
    transcript: TranscriptVersion,
) -> int:
    return sum(
        1
        for asset in _audio_generation_target_assets(episode, transcript)
        if asset.status in {"failed", "cancelled"}
    )


def _audio_generation_target_asset_ids(
    episode: Episode,
    transcript: TranscriptVersion,
) -> list[UUID]:
    return [asset.id for asset in _audio_generation_target_assets(episode, transcript)]


def _audio_generation_target_assets(
    episode: Episode,
    transcript: TranscriptVersion,
) -> list[Asset]:
    return [
        asset
        for asset in episode.assets
        if asset.asset_type == AssetType.audio
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and (
            asset.status == "planned"
            or (
                asset.status in {"failed", "cancelled"}
                and asset.generation_metadata.get("ready_for_retry") is True
            )
        )
    ]


def _target_transcript_needing_visuals(episode: Episode) -> TranscriptVersion | None:
    for transcript in _production_transcript_candidates(episode):
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        if not playable_turns:
            continue
        if _transcript_has_missing_primary_visual_assets(episode, transcript):
            return transcript
        if _visual_generation_target_asset_ids(episode, transcript):
            return transcript
    return None


def _visual_prerequisites_met(episode: Episode, transcript: TranscriptVersion) -> bool:
    playable_turn_ids = {str(turn.id) for turn in transcript.turns if turn.status != "excluded"}
    if not playable_turn_ids:
        return False
    completed_audio_turn_ids = {
        asset.source_entity_id
        for asset in episode.assets
        if asset.asset_type == AssetType.audio
        and asset.status == "completed"
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.source_entity_id in playable_turn_ids
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
    }
    return completed_audio_turn_ids == playable_turn_ids


def _transcript_has_missing_primary_visual_assets(
    episode: Episode,
    transcript: TranscriptVersion,
) -> bool:
    playable_turn_ids = {str(turn.id) for turn in transcript.turns if turn.status != "excluded"}
    if not playable_turn_ids:
        return False
    primary_visual_turn_ids = {
        asset.source_entity_id
        for asset in episode.assets
        if asset.asset_type == AssetType.video
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.source_entity_id in playable_turn_ids
        and asset.status != "replaced"
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and asset.generation_metadata.get("visual_role") == "video_primary"
    }
    return bool(playable_turn_ids - primary_visual_turn_ids)


def _visual_asset_count(
    episode: Episode,
    transcript: TranscriptVersion,
    statuses: set[str],
) -> int:
    return sum(
        1
        for asset in episode.assets
        if asset.asset_type
        in {
            AssetType.video,
            AssetType.broll,
            AssetType.reaction_loop,
            AssetType.studio_scene,
            AssetType.citation_card,
            AssetType.image,
        }
        and asset.language == transcript.language
        and asset.status in statuses
        and (
            asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
            or asset.source_entity_type in {"participant_profile", "episode"}
        )
    )


def _visual_generation_target_asset_ids(
    episode: Episode,
    transcript: TranscriptVersion,
) -> list[UUID]:
    return [
        asset.id
        for asset in _visual_generation_target_assets(episode, transcript)
    ]


def _visual_generation_target_assets(
    episode: Episode,
    transcript: TranscriptVersion,
) -> list[Asset]:
    return [
        asset
        for asset in episode.assets
        if asset.asset_type
        in {
            AssetType.video,
            AssetType.broll,
            AssetType.reaction_loop,
            AssetType.studio_scene,
            AssetType.citation_card,
            AssetType.image,
        }
        and asset.language == transcript.language
        and (
            asset.status == "planned"
            or (
                asset.status in {"failed", "cancelled"}
                and asset.generation_metadata.get("ready_for_retry") is not False
            )
        )
        and (
            asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
            or asset.source_entity_type in {"participant_profile", "episode"}
        )
    ]


def _target_transcript_needing_subtitles(episode: Episode) -> TranscriptVersion | None:
    for transcript in _production_transcript_candidates(episode):
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        if not playable_turns:
            continue
        existing = next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.subtitle
                and asset.language == transcript.language
                and asset.source_entity_type == "transcript_version"
                and asset.source_entity_id == str(transcript.id)
                and asset.generation_metadata.get("format") == "vtt"
                and asset.status != "replaced"
            ),
            None,
        )
        if existing is None:
            return transcript
    return None


def _subtitle_prerequisites_met(episode: Episode, transcript: TranscriptVersion) -> bool:
    return _visual_prerequisites_met(episode, transcript)


def _timeline_prerequisites_met(episode: Episode, transcript: TranscriptVersion) -> bool:
    playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
    if not playable_turns:
        return False
    audio_turn_ids = {
        asset.source_entity_id
        for asset in episode.assets
        if asset.asset_type == AssetType.audio
        and asset.status == "completed"
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
    }
    if any(str(turn.id) not in audio_turn_ids for turn in playable_turns):
        return False

    studio_scene = any(
        asset.asset_type == AssetType.studio_scene
        and asset.status == "completed"
        and asset.language == transcript.language
        and asset.source_entity_type == "episode"
        and asset.source_entity_id == str(episode.id)
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and asset.generation_metadata.get("render_ready") is not False
        for asset in episode.assets
    )
    if studio_scene:
        return True

    visual_turn_ids = {
        asset.source_entity_id
        for asset in episode.assets
        if asset.asset_type in {AssetType.image, AssetType.video, AssetType.broll}
        and asset.status == "completed"
        and asset.language == transcript.language
        and asset.source_entity_type == "transcript_turn"
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and asset.generation_metadata.get("render_ready") is not False
    }
    reaction_speakers = {
        asset.source_entity_id
        for asset in episode.assets
        if asset.asset_type == AssetType.reaction_loop
        and asset.status == "completed"
        and asset.language == transcript.language
        and asset.source_entity_type == "participant_profile"
        and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
        and asset.generation_metadata.get("render_ready") is not False
    }
    return all(
        str(turn.id) in visual_turn_ids or turn.speaker_participant_id in reaction_speakers
        for turn in playable_turns
    )


def _latest_timeline_asset_for_transcript(
    episode: Episode,
    transcript: TranscriptVersion,
) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.timeline
            and asset.status == "completed"
            and asset.source_entity_type == "transcript_version"
            and asset.source_entity_id == str(transcript.id)
        ),
        None,
    )


def _latest_completed_timeline_asset(episode: Episode) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.timeline and asset.status == "completed"
        ),
        None,
    )


def _latest_render_asset(
    episode: Episode,
    timeline_asset: Asset,
    render_type: str,
) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.render
            and asset.status == "completed"
            and asset.source_entity_type == "timeline_asset"
            and asset.source_entity_id == str(timeline_asset.id)
            and asset.generation_metadata.get("render_type") == render_type
        ),
        None,
    )


def _active_render_asset(episode: Episode, timeline_asset: Asset) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.render
            and asset.source_entity_type == "timeline_asset"
            and asset.source_entity_id == str(timeline_asset.id)
            and asset.status in {"submitted", "running"}
        ),
        None,
    )


def _next_queued_render_asset(episode: Episode) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.render and asset.status in {"submitted", "running"}
        ),
        None,
    )


def _render_request_from_queued_asset(
    asset: Asset,
    *,
    user_id: str,
) -> RenderRequest | None:
    metadata = asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
    payload = metadata.get("render_request")
    if not isinstance(payload, dict):
        return None
    try:
        request = RenderRequest.model_validate(payload)
    except ValueError:
        return None
    return request.model_copy(update={"user_id": request.user_id or user_id})


def _latest_final_render_asset(episode: Episode) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.render
            and asset.status == "completed"
            and asset.generation_metadata.get("render_type") == "final"
        ),
        None,
    )


def _latest_thumbnail_asset(episode: Episode, render_asset: Asset) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.thumbnail
            and asset.status == "completed"
            and asset.source_entity_type == "render_asset"
            and asset.source_entity_id == str(render_asset.id)
        ),
        None,
    )


def _latest_export_package_asset(episode: Episode, render_asset: Asset) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.export_package
            and asset.status == "completed"
            and asset.source_entity_type == "render_asset"
            and asset.source_entity_id == str(render_asset.id)
        ),
        None,
    )


def _latest_production_manifest_asset(episode: Episode, package_asset: Asset) -> Asset | None:
    return next(
        (
            asset
            for asset in reversed(episode.assets)
            if asset.asset_type == AssetType.production_manifest
            and asset.status == "completed"
            and asset.source_entity_type == "export_package"
            and asset.source_entity_id == str(package_asset.id)
        ),
        None,
    )


def _latest_publish_job(episode: Episode, package_asset: Asset):
    return next(
        (
            job
            for job in reversed(episode.publish_jobs)
            if job.package_asset_id == package_asset.id and job.status != "replaced"
        ),
        None,
    )


def _latest_publish_qc(episode: Episode, publish_job):
    return next(
        (
            result
            for result in reversed(episode.quality_results)
            if result.check_type == "publish_delivery_integrity"
            and result.target_type == "publish_job"
            and result.target_id == str(publish_job.id)
        ),
        None,
    )


def _publish_qc_blocks_handoff(result) -> bool:
    if result.status != "fail" and result.severity != QualitySeverity.fail:
        return False
    return result.details.get("dry_run") is not True


def _latest_publish_job_exists(episode: Episode, target_id: str, package_asset_id: object) -> bool:
    return any(
        job.publisher_target_id == target_id
        and job.package_asset_id == package_asset_id
        and job.status != "replaced"
        for job in episode.publish_jobs
    )


def _worker_signal_state(role: str, settings: Settings) -> dict:
    signal = RedisBusService(settings).latest_worker_signal(role)
    if signal is None:
        return {}
    signal_type = str(signal.get("signal_type") or "")
    return {
        "worker_signal": {
            "schema_version": signal.get("schema_version"),
            "signal_id": signal.get("signal_id"),
            "target_role": signal.get("target_role"),
            "signal_type": signal_type,
            "reason": signal.get("reason"),
            "created_by": signal.get("created_by"),
            "created_at": signal.get("created_at"),
            "redis_stream_id": signal.get("redis_stream_id"),
        },
        "signal_skipped": signal_type in {"drain", "stop_after_current"},
    }


async def run_voicebox_adapter_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    voicebox_service = VoiceboxService(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="voicebox-adapter",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("voicebox-adapter worker polling remote Voicebox jobs.", flush=True)
    while True:
        signal_state = _worker_signal_state("voicebox-adapter", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "episodes_synced": 0,
                "pending_audio_assets": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("voicebox-adapter", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "episodes_synced": 0,
                    "pending_audio_assets": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                }
            else:
                summary = await run_voicebox_adapter_once(
                    repository=repository,
                    voicebox_service=voicebox_service,
                    batch_limit=settings.worker_sync_batch_limit,
                )
                lease = worker_lease.renew("voicebox-adapter", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="voicebox-adapter",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if summary["pending_audio_assets"] or summary["error_count"]:
            print(f"voicebox-adapter sync summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_comfyui_adapter_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    comfyui_service = ComfyUiService(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="comfyui-adapter",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("comfyui-adapter worker polling remote ComfyUI jobs.", flush=True)
    while True:
        signal_state = _worker_signal_state("comfyui-adapter", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "episodes_synced": 0,
                "pending_visual_assets": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                "enabled_comfyui_endpoints": 0,
                "enabled_comfyui_workflows": 0,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("comfyui-adapter", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "episodes_synced": 0,
                    "pending_visual_assets": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                    "enabled_comfyui_endpoints": 0,
                    "enabled_comfyui_workflows": 0,
                }
            else:
                summary = await run_comfyui_adapter_once(
                    repository=repository,
                    comfyui_service=comfyui_service,
                    batch_limit=settings.worker_sync_batch_limit,
                )
                lease = worker_lease.renew("comfyui-adapter", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="comfyui-adapter",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if summary["pending_visual_assets"] or summary["error_count"]:
            print(f"comfyui-adapter sync summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_timeline_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    timeline_service = TimelineService(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="timeline-worker",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("timeline-worker building ready episode timelines.", flush=True)
    while True:
        signal_state = _worker_signal_state("timeline-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "timelines_built": 0,
                "skipped_prerequisites": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("timeline-worker", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "timelines_built": 0,
                    "skipped_prerequisites": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                }
            else:
                summary = run_timeline_worker_once(
                    repository=repository,
                    timeline_service=timeline_service,
                    batch_limit=settings.worker_sync_batch_limit,
                )
                lease = worker_lease.renew("timeline-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="timeline-worker",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if summary["timelines_built"] or summary["error_count"]:
            print(f"timeline-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_render_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    render_service = RenderService(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="render-worker",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("render-worker creating preview and final renders from completed timelines.", flush=True)
    while True:
        signal_state = _worker_signal_state("render-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "preview_renders_created": 0,
                "final_renders_created": 0,
                "skipped": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("render-worker", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "preview_renders_created": 0,
                    "final_renders_created": 0,
                    "skipped": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                }
            else:
                summary = run_render_worker_once(
                    repository=repository,
                    render_service=render_service,
                    batch_limit=settings.worker_sync_batch_limit,
                )
                lease = worker_lease.renew("render-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="render-worker",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if (
            summary["preview_renders_created"]
            or summary["final_renders_created"]
            or summary["error_count"]
        ):
            print(f"render-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_publishing_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    render_service = RenderService(settings)
    publisher_service = PublisherService()
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="publishing-worker",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("publishing-worker preparing thumbnails, packages, and dry-run publishes.", flush=True)
    while True:
        signal_state = _worker_signal_state("publishing-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "thumbnails_created": 0,
                "youtube_packages_created": 0,
                "production_manifests_created": 0,
                "dry_run_publish_jobs_created": 0,
                "live_publish_jobs_created": 0,
                "package_qc_blocked_handoffs": 0,
                "production_manifest_blocked_handoffs": 0,
                "automated_live_enabled": settings.publisher_automated_live_enabled,
                "automated_live_capable_targets": 0,
                "enabled_publisher_targets": 0,
                "pending_final_render_approvals": 0,
                "skipped": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("publishing-worker", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "thumbnails_created": 0,
                    "youtube_packages_created": 0,
                    "production_manifests_created": 0,
                    "dry_run_publish_jobs_created": 0,
                    "live_publish_jobs_created": 0,
                    "package_qc_blocked_handoffs": 0,
                    "production_manifest_blocked_handoffs": 0,
                    "automated_live_enabled": settings.publisher_automated_live_enabled,
                    "automated_live_capable_targets": 0,
                    "enabled_publisher_targets": 0,
                    "pending_final_render_approvals": 0,
                    "skipped": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                }
            else:
                summary = run_publishing_worker_once(
                    repository=repository,
                    render_service=render_service,
                    publisher_service=publisher_service,
                    batch_limit=settings.worker_sync_batch_limit,
                    automated_live_enabled=settings.publisher_automated_live_enabled,
                )
                lease = worker_lease.renew("publishing-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="publishing-worker",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if (
            summary["thumbnails_created"]
            or summary["youtube_packages_created"]
            or summary["production_manifests_created"]
            or summary["dry_run_publish_jobs_created"]
            or summary["live_publish_jobs_created"]
            or summary["pending_final_render_approvals"]
            or summary["error_count"]
        ):
            print(f"publishing-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_research_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    research_service = ResearchService(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="research-worker",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("research-worker building configured evidence packs.", flush=True)
    while True:
        signal_state = _worker_signal_state("research-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "evidence_packs_built": 0,
                "skipped": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("research-worker", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "evidence_packs_built": 0,
                    "skipped": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                }
            else:
                summary = run_research_worker_once(
                    repository=repository,
                    research_service=research_service,
                    batch_limit=settings.worker_sync_batch_limit,
                )
                lease = worker_lease.renew("research-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="research-worker",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if summary["evidence_packs_built"] or summary["error_count"]:
            print(f"research-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_discussion_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    discussion_engine = DiscussionEngine(ModelGateway(), settings)
    production_control = ProductionControlService(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="discussion-worker",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("discussion-worker running ready turn-by-turn discussions.", flush=True)
    while True:
        signal_state = _worker_signal_state("discussion-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "discussions_completed": 0,
                "skipped": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("discussion-worker", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "discussions_completed": 0,
                    "skipped": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                }
            else:
                summary = await _await_with_busy_heartbeat(
                    run_discussion_worker_once(
                        repository=repository,
                        discussion_engine=discussion_engine,
                        production_control=production_control,
                        batch_limit=settings.worker_sync_batch_limit,
                    ),
                    worker_status,
                    role="discussion-worker",
                    worker_id=worker_id,
                    settings=settings,
                )
                lease = worker_lease.renew("discussion-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="discussion-worker",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if summary["discussions_completed"] or summary["error_count"]:
            print(f"discussion-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_localization_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    localization_service = LocalizationService()
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="localization-worker",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("localization-worker creating missing approved-transcript language variants.", flush=True)
    while True:
        signal_state = _worker_signal_state("localization-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "episodes_localized": 0,
                "localized_languages_created": 0,
                "skipped": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("localization-worker", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "episodes_localized": 0,
                    "localized_languages_created": 0,
                    "skipped": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                }
            else:
                summary = run_localization_worker_once(
                    repository=repository,
                    localization_service=localization_service,
                    batch_limit=settings.worker_sync_batch_limit,
                )
                lease = worker_lease.renew("localization-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="localization-worker",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if summary["localized_languages_created"] or summary["error_count"]:
            print(f"localization-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_qc_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    research_service = ResearchService(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="qc-worker",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("qc-worker running missing source-bound claim QC.", flush=True)
    while True:
        signal_state = _worker_signal_state("qc-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "episodes_scanned": 0,
                "claim_qc_completed": 0,
                "skipped": 0,
                "error_count": 0,
                "errors": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("qc-worker", worker_id)
            if lease is None:
                summary = {
                    "episodes_scanned": 0,
                    "claim_qc_completed": 0,
                    "skipped": 0,
                    "error_count": 0,
                    "errors": [],
                    "lease_skipped": True,
                }
            else:
                summary = run_qc_worker_once(
                    repository=repository,
                    research_service=research_service,
                    batch_limit=settings.worker_sync_batch_limit,
                )
                lease = worker_lease.renew("qc-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="qc-worker",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if summary["claim_qc_completed"] or summary["error_count"]:
            print(f"qc-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_workflow_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="workflow-worker",
            worker_id=worker_id,
            status="starting",
            details={"poll_interval_seconds": settings.worker_poll_interval_seconds},
        )
    )
    print("workflow-worker coordinating eligible production stage workers.", flush=True)
    worker_repository = _CachedWorkflowWorkerRepository(repository)
    while True:
        signal_state = _worker_signal_state("workflow-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "schema_version": "workflow_worker_orchestration_summary.v1",
                "policy": "local_stage_worker_orchestrator_v1",
                "batch_limit": settings.worker_sync_batch_limit,
                "stage_order": [],
                "stages": {},
                "progressed_stage_count": 0,
                "error_count": 0,
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("workflow-worker", worker_id)
            if lease is None:
                summary = {
                    "schema_version": "workflow_worker_orchestration_summary.v1",
                    "policy": "local_stage_worker_orchestrator_v1",
                    "batch_limit": settings.worker_sync_batch_limit,
                    "stage_order": [],
                    "stages": {},
                    "progressed_stage_count": 0,
                    "error_count": 0,
                    "lease_skipped": True,
                }
            else:
                summary = await _await_with_busy_heartbeat(
                    run_workflow_worker_once(
                        repository=worker_repository,
                        settings=settings,
                        batch_limit=settings.worker_sync_batch_limit,
                    ),
                    worker_status,
                    role="workflow-worker",
                    worker_id=worker_id,
                    settings=settings,
                )
                lease = worker_lease.renew("workflow-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="workflow-worker",
                worker_id=worker_id,
                status="degraded" if summary["error_count"] else "running",
                details=summary,
            )
        )
        if summary["progressed_stage_count"] or summary["error_count"]:
            print(f"workflow-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


async def run_temporal_worker(settings: Settings) -> None:
    repository = build_repository(settings)
    worker_status = WorkerStatusService(settings)
    worker_lease = WorkerLeaseService(settings)
    worker_id = current_worker_id()
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role="temporal-worker",
            worker_id=worker_id,
            status="starting",
            details={
                "poll_interval_seconds": settings.worker_poll_interval_seconds,
                "backend_mode": settings.temporal_backend_mode,
                "namespace": settings.temporal_namespace,
                "task_queue": settings.temporal_task_queue,
            },
        )
    )
    print("temporal-worker executing external Temporal production activities.", flush=True)
    while True:
        signal_state = _worker_signal_state("temporal-worker", settings)
        if signal_state.get("signal_skipped"):
            summary = {
                "schema_version": "temporal_worker_execution_summary.v1",
                "policy": "external_temporal_stage_activity_worker_v1",
                "status": "signal_skipped",
                "reason": "worker signal prevents starting a new Temporal activity pass",
                "batch_limit": settings.worker_sync_batch_limit,
                "activity_order": [],
                "stage_order": [],
                "activities": {},
                "stages": {},
                "progressed_stage_count": 0,
                "error_count": 0,
                "orchestration_records": [],
                "lease_skipped": False,
                **signal_state,
            }
        else:
            lease = worker_lease.acquire("temporal-worker", worker_id)
            if lease is None:
                summary = {
                    "schema_version": "temporal_worker_execution_summary.v1",
                    "policy": "external_temporal_stage_activity_worker_v1",
                    "status": "lease_skipped",
                    "reason": "another temporal-worker lease is active",
                    "batch_limit": settings.worker_sync_batch_limit,
                    "activity_order": [],
                    "stage_order": [],
                    "activities": {},
                    "stages": {},
                    "progressed_stage_count": 0,
                    "error_count": 0,
                    "orchestration_records": [],
                    "lease_skipped": True,
                }
            else:
                summary = await _await_with_busy_heartbeat(
                    run_temporal_worker_once(
                        repository=repository,
                        settings=settings,
                        batch_limit=settings.worker_sync_batch_limit,
                    ),
                    worker_status,
                    role="temporal-worker",
                    worker_id=worker_id,
                    settings=settings,
                )
                lease = worker_lease.renew("temporal-worker", worker_id)
                summary["lease_skipped"] = False
                summary["lease_expires_at"] = lease.expires_at.isoformat() if lease else None
            summary.update(signal_state)
        status = "running"
        if summary["error_count"]:
            status = "degraded"
        if summary.get("status") in {"blocked", "disabled"}:
            status = "degraded"
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role="temporal-worker",
                worker_id=worker_id,
                status=status,
                details=summary,
            )
        )
        if summary["progressed_stage_count"] or summary["error_count"]:
            print(f"temporal-worker summary: {summary}", flush=True)
        await asyncio.sleep(settings.worker_poll_interval_seconds)


def run_unsupported_worker(role: str, settings: Settings) -> dict:
    worker_status = WorkerStatusService(settings)
    worker_id = current_worker_id()
    supported_roles = supported_worker_roles()
    summary = {
        "schema_version": "unsupported_worker_role.v1",
        "status": "failed",
        "reason": "DIALECTICORE_WORKER_ROLE is not a supported worker role",
        "configured_role": role,
        "supported_roles": supported_roles,
    }
    worker_status.record_heartbeat(
        WorkerHeartbeatRequest(
            role=role,
            worker_id=worker_id,
            status="failed",
            details=summary,
        )
    )
    print(f"{role} is not a supported DialectiCore worker role.", flush=True)
    return summary


def supported_worker_roles() -> list[str]:
    return [
        "workflow-worker",
        "temporal-worker",
        "voicebox-adapter",
        "comfyui-adapter",
        "research-worker",
        "discussion-worker",
        "localization-worker",
        "timeline-worker",
        "render-worker",
        "qc-worker",
        "publishing-worker",
    ]


def current_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _maintain_busy_heartbeat(
    worker_status: WorkerStatusService,
    *,
    role: str,
    worker_id: str,
    settings: Settings,
) -> None:
    interval = max(
        1.0,
        min(
            settings.worker_poll_interval_seconds,
            settings.worker_heartbeat_ttl_seconds / 3,
        ),
    )
    while True:
        await asyncio.sleep(interval)
        worker_status.record_heartbeat(
            WorkerHeartbeatRequest(
                role=role,
                worker_id=worker_id,
                status="running",
                details={
                    "schema_version": "worker_busy_heartbeat.v1",
                    "poll_interval_seconds": settings.worker_poll_interval_seconds,
                    "heartbeat_interval_seconds": interval,
                    "reason": "worker is still executing the current production pass",
                },
            )
        )


async def _await_with_busy_heartbeat(
    awaitable,
    worker_status: WorkerStatusService,
    *,
    role: str,
    worker_id: str,
    settings: Settings,
):
    heartbeat_task = asyncio.create_task(
        _maintain_busy_heartbeat(
            worker_status,
            role=role,
            worker_id=worker_id,
            settings=settings,
        )
    )
    try:
        return await awaitable
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


def main() -> None:
    role = os.getenv("DIALECTICORE_WORKER_ROLE", "workflow-worker")
    settings = get_settings()
    try:
        if role == "workflow-worker":
            asyncio.run(run_workflow_worker(settings))
            return
        if role == "temporal-worker":
            asyncio.run(run_temporal_worker(settings))
            return
        if role == "voicebox-adapter":
            asyncio.run(run_voicebox_adapter_worker(settings))
            return
        if role == "comfyui-adapter":
            asyncio.run(run_comfyui_adapter_worker(settings))
            return
        if role == "research-worker":
            asyncio.run(run_research_worker(settings))
            return
        if role == "discussion-worker":
            asyncio.run(run_discussion_worker(settings))
            return
        if role == "localization-worker":
            asyncio.run(run_localization_worker(settings))
            return
        if role == "timeline-worker":
            asyncio.run(run_timeline_worker(settings))
            return
        if role == "render-worker":
            asyncio.run(run_render_worker(settings))
            return
        if role == "qc-worker":
            asyncio.run(run_qc_worker(settings))
            return
        if role == "publishing-worker":
            asyncio.run(run_publishing_worker(settings))
            return
        run_unsupported_worker(role, settings)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print(f"{role} worker stopping.", flush=True)


if __name__ == "__main__":
    main()
