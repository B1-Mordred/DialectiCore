from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

import httpx
from app.core.config import Settings
from app.domain.defaults import default_render_presets
from app.domain.enums import AssetType, EpisodeStatus, TranscriptType
from app.domain.schemas import (
    Approval,
    ApprovalDecisionRequest,
    Asset,
    AssetReplacementRequest,
    AudioAssetPlanRequest,
    AudioCancellationRequest,
    AudioGenerationRequest,
    AudioQualityRequest,
    AudioResultSyncRequest,
    AuditEvent,
    B1ManagedMediaSmokeRequest,
    B1VoiceInventorySyncResponse,
    B1VoicePresetProvisionRequest,
    B1VoicePresetProvisionResponse,
    BackupCreateRequest,
    BackupRestoreRequest,
    ComfyUiEndpoint,
    ComfyUiEndpointCreateRequest,
    ComfyUiWorkflow,
    ComfyUiWorkflowCreateRequest,
    DiscussionPromptTemplate,
    DiscussionPromptTemplateCreateRequest,
    Episode,
    EpisodeCreateRequest,
    EpisodeDefinitionUpdateRequest,
    EpisodeMediaAsset,
    EpisodeProductionSettingsUpdateRequest,
    EpisodeSummary,
    EvidenceClaim,
    LanguageProfile,
    LanguageProfileCreateRequest,
    LiveProviderCastPreflightRequest,
    LocalizationRequest,
    ModelEndpoint,
    ModelEndpointCreateRequest,
    OpeningMediaUploadRequest,
    OpenRouterPresetProvisionRequest,
    OpenRouterPresetProvisionResponse,
    ParticipantProfile,
    ParticipantProfileCreateRequest,
    PrimerMediaAcquireRequest,
    PrimerMediaCandidate,
    PrimerMediaDiscoveryRequest,
    PrimerMediaImportRequest,
    PrimerNarrationTimingRequest,
    PrimerNarratorProfile,
    PrimerNarratorProfileCreateRequest,
    PrimerProductionRequest,
    PrimerProductionStatus,
    PrimerSpokenScriptApprovalRequest,
    PrimerSpokenScriptPrepareRequest,
    PrimerSpokenScriptStatus,
    PrimerSpokenScriptUpdateRequest,
    PrimerVisualPlanApprovalRequest,
    PrimerVisualPlanBeatCreateRequest,
    PrimerVisualPlanBeatUpdateRequest,
    PrimerVisualPlanPrepareRequest,
    PrimerVisualPlanRevisionList,
    PrimerVisualPlanRevisionRestoreRequest,
    PrimerVisualPlanStatus,
    PrimerVisualPlanVerificationRequest,
    ProductionManifestRequest,
    ProductionStatus,
    Project,
    ProjectCreateRequest,
    ProviderSessionRevocationRequest,
    PublisherTarget,
    PublisherTargetCreateRequest,
    PublishJob,
    PublishRequest,
    RenderPreset,
    RenderRequest,
    ResearchBuildRequest,
    ResearchClaimQcRequest,
    ResearchSource,
    ResearchSourceReviewRequest,
    SceneReferenceImageUploadRequest,
    SceneReferenceImageUploadResponse,
    SeatedCharacterReviewRequest,
    StudioCameraPlateGenerateRequest,
    StudioCameraPlateReviewRequest,
    StudioCameraPlateUploadMetadata,
    StudioPanelReviewRequest,
    SubtitleGenerationRequest,
    ThumbnailRequest,
    TimelineBuildRequest,
    TimelineUpdateRequest,
    TurnManualEditRequest,
    TurnReviewActionRequest,
    VisualAssetPlanRequest,
    VisualCancellationRequest,
    VisualGenerationRequest,
    VisualProfile,
    VisualProfileCreateRequest,
    VisualProfileReferenceImageUploadRequest,
    VisualQualityRequest,
    VisualReferenceImage,
    VisualReferenceImageType,
    VisualResultSyncRequest,
    VoiceboxEndpoint,
    VoiceboxEndpointCreateRequest,
    VoicePreviewRequest,
    VoiceProfile,
    VoiceProfileCreateRequest,
    WorkerHeartbeatRequest,
    WorkerSignalRequest,
    WorkerStatusRecord,
    WorkerStatusSummary,
    WorkflowActionRequest,
    WorkflowAdvanceResponse,
    WorkflowRetryResolutionRequest,
    WorkflowRunUntilBlockedRequest,
    WorkflowRunUntilBlockedResponse,
    WorkflowStartRequest,
    YouTubeExportRequest,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.asset_replacement_service import AssetReplacementService
from app.services.auth_service import AuthService
from app.services.b1_managed_media_smoke_service import B1ManagedMediaSmokeService
from app.services.backup_service import BackupService
from app.services.branding_service import MAX_LOGO_BYTES, BrandingService
from app.services.comfyui_service import ComfyUiService
from app.services.discussion_engine import DiscussionEngine
from app.services.live_provider_preflight_service import LiveProviderPreflightService
from app.services.localization_service import LocalizationService
from app.services.managed_media_smoke_evidence import (
    managed_media_smoke_evidence as _managed_media_smoke_evidence,
)
from app.services.model_endpoint_service import ModelEndpointService
from app.services.object_storage import create_object_store
from app.services.primer_media_service import PrimerMediaService
from app.services.primer_production_service import PrimerProductionService
from app.services.production_control_service import ProductionControlService
from app.services.publisher_service import PublisherService
from app.services.redis_bus_service import RedisBusService
from app.services.render_service import RenderService
from app.services.research_service import ResearchService
from app.services.studio_camera_plate_service import (
    MAX_CAMERA_PLATE_BYTES,
    StudioCameraPlateService,
)
from app.services.subtitle_service import SubtitleService
from app.services.system_health_service import SystemHealthService
from app.services.system_metrics_service import SystemMetricsService
from app.services.timeline_service import TimelineService
from app.services.voicebox_service import VoiceboxService
from app.services.worker_lease_service import WorkerLeaseService
from app.services.worker_status_service import WorkerStatusService
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

router = APIRouter(prefix="/api/v1")


def get_repository() -> EpisodeRepository:
    from app.main import repository

    return repository


def get_discussion_engine() -> DiscussionEngine:
    from app.main import discussion_engine

    return discussion_engine


def get_research_service() -> ResearchService:
    from app.main import research_service

    return research_service


def get_primer_media_service() -> PrimerMediaService:
    from app.main import primer_media_service

    return primer_media_service


def get_primer_production_service() -> PrimerProductionService:
    from app.main import primer_production_service

    return primer_production_service


def get_localization_service() -> LocalizationService:
    from app.main import localization_service

    return localization_service


def get_model_endpoint_service() -> ModelEndpointService:
    from app.main import model_endpoint_service

    return model_endpoint_service


def get_voicebox_service() -> VoiceboxService:
    from app.main import voicebox_service

    return voicebox_service


def get_comfyui_service() -> ComfyUiService:
    from app.main import comfyui_service

    return comfyui_service


def get_subtitle_service() -> SubtitleService:
    from app.main import subtitle_service

    return subtitle_service


def get_timeline_service() -> TimelineService:
    from app.main import timeline_service

    return timeline_service


def get_render_service() -> RenderService:
    from app.main import render_service

    return render_service


def get_asset_replacement_service() -> AssetReplacementService:
    from app.main import asset_replacement_service

    return asset_replacement_service


def get_production_control_service() -> ProductionControlService:
    from app.main import production_control_service

    return production_control_service


def get_publisher_service() -> PublisherService:
    from app.main import publisher_service

    return publisher_service


def get_system_health_service() -> SystemHealthService:
    from app.main import system_health_service

    return system_health_service


def get_worker_status_service() -> WorkerStatusService:
    from app.main import worker_status_service

    return worker_status_service


def get_worker_lease_service() -> WorkerLeaseService:
    from app.main import worker_lease_service

    return worker_lease_service


def get_redis_bus_service() -> RedisBusService:
    from app.main import redis_bus_service

    return redis_bus_service


def get_system_metrics_service() -> SystemMetricsService:
    from app.main import system_metrics_service

    return system_metrics_service


def get_backup_service() -> BackupService:
    from app.main import backup_service

    return backup_service


def get_branding_service() -> BrandingService:
    from app.main import branding_service

    return branding_service


def get_studio_camera_plate_service() -> StudioCameraPlateService:
    from app.main import studio_camera_plate_service

    return studio_camera_plate_service


def get_auth_service() -> AuthService:
    from app.main import auth_service

    return auth_service


def get_live_provider_preflight_service() -> LiveProviderPreflightService:
    from app.main import live_provider_preflight_service

    return live_provider_preflight_service


def get_b1_managed_media_smoke_service() -> B1ManagedMediaSmokeService:
    from app.main import b1_managed_media_smoke_service

    return b1_managed_media_smoke_service


def get_settings() -> Settings:
    from app.main import settings

    return settings


RepositoryDep = Annotated[EpisodeRepository, Depends(get_repository)]
DiscussionEngineDep = Annotated[DiscussionEngine, Depends(get_discussion_engine)]
ResearchServiceDep = Annotated[ResearchService, Depends(get_research_service)]
PrimerMediaServiceDep = Annotated[PrimerMediaService, Depends(get_primer_media_service)]
PrimerProductionServiceDep = Annotated[
    PrimerProductionService, Depends(get_primer_production_service)
]
LocalizationServiceDep = Annotated[LocalizationService, Depends(get_localization_service)]
ModelEndpointServiceDep = Annotated[ModelEndpointService, Depends(get_model_endpoint_service)]
VoiceboxServiceDep = Annotated[VoiceboxService, Depends(get_voicebox_service)]
ComfyUiServiceDep = Annotated[ComfyUiService, Depends(get_comfyui_service)]
SubtitleServiceDep = Annotated[SubtitleService, Depends(get_subtitle_service)]
TimelineServiceDep = Annotated[TimelineService, Depends(get_timeline_service)]
RenderServiceDep = Annotated[RenderService, Depends(get_render_service)]
AssetReplacementServiceDep = Annotated[
    AssetReplacementService,
    Depends(get_asset_replacement_service),
]
ProductionControlServiceDep = Annotated[
    ProductionControlService,
    Depends(get_production_control_service),
]
PublisherServiceDep = Annotated[PublisherService, Depends(get_publisher_service)]
SystemHealthServiceDep = Annotated[SystemHealthService, Depends(get_system_health_service)]
WorkerStatusServiceDep = Annotated[WorkerStatusService, Depends(get_worker_status_service)]
WorkerLeaseServiceDep = Annotated[WorkerLeaseService, Depends(get_worker_lease_service)]
RedisBusServiceDep = Annotated[RedisBusService, Depends(get_redis_bus_service)]
SystemMetricsServiceDep = Annotated[SystemMetricsService, Depends(get_system_metrics_service)]
BackupServiceDep = Annotated[BackupService, Depends(get_backup_service)]
BrandingServiceDep = Annotated[BrandingService, Depends(get_branding_service)]
StudioCameraPlateServiceDep = Annotated[
    StudioCameraPlateService, Depends(get_studio_camera_plate_service)
]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
LiveProviderPreflightServiceDep = Annotated[
    LiveProviderPreflightService,
    Depends(get_live_provider_preflight_service),
]
B1ManagedMediaSmokeServiceDep = Annotated[
    B1ManagedMediaSmokeService,
    Depends(get_b1_managed_media_smoke_service),
]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class EpisodeScopedWorkflowRepository:
    def __init__(self, repository: EpisodeRepository, episode_id: UUID) -> None:
        self._repository = repository
        self._episode_id = episode_id

    def list(self) -> list[Episode]:
        try:
            return [self._repository.get(self._episode_id)]
        except KeyError:
            return []

    def save(self, episode: Episode) -> Episode:
        return self._repository.save(episode)

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


def _sse_event(event_name: str, payload: dict, event_id: str | None = None) -> str:
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    event_lines = []
    if event_id:
        safe_event_id = str(event_id).replace("\r", " ").replace("\n", " ")
        event_lines.append(f"id: {safe_event_id}")
    event_lines.extend([f"event: {event_name}", f"data: {data}", ""])
    return "\n".join(event_lines) + "\n"


def _worker_status_summary(
    worker_status: WorkerStatusService,
    worker_leases: WorkerLeaseService,
) -> WorkerStatusSummary:
    leases = worker_leases.list_leases()
    return worker_status.summary(leases, worker_leases.last_cleanup_counts)


@router.get("/system/health")
async def health(
    repo: RepositoryDep,
    system_health: SystemHealthServiceDep,
    worker_status: WorkerStatusServiceDep,
    worker_leases: WorkerLeaseServiceDep,
    redis_bus: RedisBusServiceDep,
) -> dict:
    return system_health.summary(
        repo,
        _worker_status_summary(worker_status, worker_leases),
        redis_bus.worker_signal_summary(),
    )


@router.get("/system/workflow-retries")
async def workflow_retries(
    repo: RepositoryDep,
    system_health: SystemHealthServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return system_health.workflow_retry_backlog(repo, limit=limit)


@router.get("/system/workflow-orchestration")
async def workflow_orchestration(
    repo: RepositoryDep,
    system_health: SystemHealthServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return system_health.workflow_orchestration_evidence(repo, limit=limit)


@router.get("/system/live-provider-readiness")
async def live_provider_readiness(
    repo: RepositoryDep,
    system_health: SystemHealthServiceDep,
    worker_status: WorkerStatusServiceDep,
    worker_leases: WorkerLeaseServiceDep,
    redis_bus: RedisBusServiceDep,
) -> dict:
    worker_summary = _worker_status_summary(worker_status, worker_leases)
    signal_summary = redis_bus.worker_signal_summary()
    return await asyncio.to_thread(
        system_health.live_provider_readiness,
        repo,
        worker_summary,
        signal_summary,
    )


@router.post("/system/live-provider-preflight")
async def live_provider_preflight(
    repo: RepositoryDep,
    live_provider_preflight: LiveProviderPreflightServiceDep,
    request: Annotated[LiveProviderCastPreflightRequest | None, Body()] = None,
) -> dict:
    request = request or LiveProviderCastPreflightRequest()
    return await _run_live_provider_preflight_request(
        request=request,
        repo=repo,
        live_provider_preflight=live_provider_preflight,
    )


@router.get("/system/live-provider-preflight")
async def live_provider_preflight_defaults(
    repo: RepositoryDep,
    live_provider_preflight: LiveProviderPreflightServiceDep,
) -> dict:
    return await _run_live_provider_preflight_request(
        request=LiveProviderCastPreflightRequest(),
        repo=repo,
        live_provider_preflight=live_provider_preflight,
    )


async def _run_live_provider_preflight_request(
    *,
    request: LiveProviderCastPreflightRequest,
    repo: EpisodeRepository,
    live_provider_preflight: LiveProviderPreflightService,
) -> dict:
    try:
        result = await live_provider_preflight.run_cast_preflight(
            repo,
            participant_ids=request.participant_ids,
            frontier_cast=request.frontier_cast,
            include_models=request.include_models,
            include_voices=request.include_voices,
            text=request.text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    repo.record_global_audit_event(
        AuditEvent(
            event_type="live_provider.cast_preflight_checked",
            actor=request.user_id or "web-ui",
            details={
                "schema_version": "live_provider_cast_preflight_audit.v1",
                "status": result.get("status"),
                "participant_ids": result.get("participant_scope", {}).get(
                    "participant_ids",
                    [],
                ),
                "blocking_sections": result.get("blocking_sections", []),
                "model_summary": result.get("model_summary", {}),
                "voicebox_summary": result.get("voicebox_summary", {}),
            },
        )
    )
    return result


@router.post("/system/b1-managed-media-smoke")
async def b1_managed_media_smoke(
    request: B1ManagedMediaSmokeRequest,
    repo: RepositoryDep,
    b1_smoke: B1ManagedMediaSmokeServiceDep,
) -> dict:
    result = await b1_smoke.run_smoke(
        api_base=request.api_base,
        model=request.model,
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        width=request.width,
        height=request.height,
        steps=request.steps,
        cfg=request.cfg,
        seed=request.seed,
        poll_attempts=request.poll_attempts,
        poll_interval_seconds=request.poll_interval_seconds,
        evidence_output=request.evidence_output,
        requirements_output=request.requirements_output,
        allow_runner_failure=request.allow_runner_failure,
    )
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    evidence = (
        payload.get("evidence_file") if isinstance(payload.get("evidence_file"), dict) else {}
    )
    requirements = (
        payload.get("requirements_update")
        if isinstance(payload.get("requirements_update"), dict)
        else {}
    )
    repo.record_global_audit_event(
        AuditEvent(
            event_type="b1_managed_media.smoke_checked",
            actor=request.user_id or "web-ui",
            details={
                "schema_version": "b1_managed_media_smoke_audit.v1",
                "status": payload.get("status"),
                "exit_code": result.get("exit_code"),
                "api_base": payload.get("api_base"),
                "model": payload.get("model"),
                "modality": payload.get("modality"),
                "operation": payload.get("operation"),
                "job_id": payload.get("job_id"),
                "evidence_path": evidence.get("path"),
                "requirements_path": requirements.get("path"),
                "requirements_appended": requirements.get("appended") is True,
            },
        )
    )
    return result


@router.get("/system/credential-provisioning")
async def credential_provisioning(
    repo: RepositoryDep,
    system_health: SystemHealthServiceDep,
    include_disabled: bool = Query(default=True),
) -> dict:
    return system_health.credential_provisioning_plan(
        repo,
        include_disabled=include_disabled,
    )


@router.get("/system/auth-policy")
async def auth_policy(auth_service: AuthServiceDep) -> dict:
    return auth_service.policy()


@router.get("/system/auth/provider-session/revocations")
async def list_provider_session_revocations(auth_service: AuthServiceDep) -> dict:
    return {"revocations": auth_service.list_provider_session_revocations()}


@router.get("/system/auth/provider-session/decisions")
async def list_provider_session_decisions(
    auth_service: AuthServiceDep,
    limit: int = Query(default=25, ge=1, le=200),
) -> dict:
    return {"decisions": auth_service.list_provider_session_decisions(limit=limit)}


@router.post("/system/auth/provider-session/revocations")
async def record_provider_session_revocation(
    request: ProviderSessionRevocationRequest,
    auth_service: AuthServiceDep,
) -> dict:
    try:
        return auth_service.record_provider_session_revocation(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/projects", response_model=list[Project])
async def list_projects(repo: RepositoryDep) -> list[Project]:
    return repo.list_projects()


@router.post("/projects", response_model=Project)
async def create_project(
    request: ProjectCreateRequest,
    repo: RepositoryDep,
) -> Project:
    return repo.upsert_project(request.to_project())


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: UUID, repo: RepositoryDep) -> Project:
    try:
        return repo.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc


@router.put("/projects/{project_id}", response_model=Project)
async def update_project(
    project_id: UUID,
    request: ProjectCreateRequest,
    repo: RepositoryDep,
) -> Project:
    try:
        existing = repo.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    updated = request.to_project(project_id=project_id)
    updated.created_at = existing.created_at
    if request.branding is None:
        updated.branding = existing.branding
    return repo.upsert_project(updated)


@router.post("/projects/{project_id}/branding/logo", response_model=Project)
async def upload_project_branding_logo(
    project_id: UUID,
    branding_service: BrandingServiceDep,
    repo: RepositoryDep,
    logo: Annotated[UploadFile, File()],
) -> Project:
    try:
        project = repo.get_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    payload = await logo.read(MAX_LOGO_BYTES + 1)
    try:
        project.branding.logo = branding_service.store_logo(
            payload,
            source="project_upload",
            owner_id=str(project.id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return repo.upsert_project(project)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: UUID, repo: RepositoryDep) -> None:
    try:
        repo.delete_project(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/language-profiles", response_model=list[LanguageProfile])
async def list_language_profiles(repo: RepositoryDep) -> list[LanguageProfile]:
    return repo.list_language_profiles()


@router.post("/language-profiles", response_model=LanguageProfile)
async def create_language_profile(
    request: LanguageProfileCreateRequest,
    repo: RepositoryDep,
) -> LanguageProfile:
    return repo.upsert_language_profile(request.to_profile())


@router.get("/language-profiles/{profile_id}", response_model=LanguageProfile)
async def get_language_profile(profile_id: str, repo: RepositoryDep) -> LanguageProfile:
    try:
        return repo.get_language_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="language profile not found") from exc


@router.put("/language-profiles/{profile_id}", response_model=LanguageProfile)
async def update_language_profile(
    profile_id: str,
    request: LanguageProfileCreateRequest,
    repo: RepositoryDep,
) -> LanguageProfile:
    try:
        repo.get_language_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="language profile not found") from exc
    updated = request.to_profile()
    updated.id = profile_id
    return repo.upsert_language_profile(updated)


@router.delete("/language-profiles/{profile_id}", status_code=204)
async def delete_language_profile(profile_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_language_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="language profile not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/system/workers", response_model=WorkerStatusSummary)
async def workers(
    worker_status: WorkerStatusServiceDep,
    worker_leases: WorkerLeaseServiceDep,
) -> WorkerStatusSummary:
    return _worker_status_summary(worker_status, worker_leases)


@router.post("/system/workers/heartbeat", response_model=WorkerStatusRecord)
async def record_worker_heartbeat(
    request: WorkerHeartbeatRequest,
    worker_status: WorkerStatusServiceDep,
) -> WorkerStatusRecord:
    return worker_status.record_heartbeat(request)


@router.get("/system/workers/signals")
async def list_worker_signals(
    redis_bus: RedisBusServiceDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return {"signals": redis_bus.list_worker_signals(limit=limit)}


@router.post("/system/workers/signals")
async def record_worker_signal(
    request: WorkerSignalRequest,
    redis_bus: RedisBusServiceDep,
    repo: RepositoryDep,
) -> dict:
    record = redis_bus.record_worker_signal(request)
    repo.record_global_audit_event(
        AuditEvent(
            event_type="worker.signal.recorded",
            actor=request.user_id or "system",
            details={
                "schema_version": "worker_signal_audit.v1",
                "signal_id": record.get("signal_id"),
                "target_role": record.get("target_role"),
                "signal_type": record.get("signal_type"),
                "status": record.get("status"),
                "redis_enabled": record.get("redis_enabled"),
                "redis_stream": record.get("redis_stream"),
                "redis_stream_maxlen": record.get("redis_stream_maxlen"),
                "redis_stream_id": record.get("redis_stream_id"),
                "delivery_sources": record.get("delivery_sources", []),
                "payload_keys": sorted(request.payload.keys()),
            },
        )
    )
    return record


@router.get("/system/metrics", response_class=PlainTextResponse)
async def system_metrics(
    repo: RepositoryDep,
    system_health: SystemHealthServiceDep,
    worker_status: WorkerStatusServiceDep,
    worker_leases: WorkerLeaseServiceDep,
    system_metrics_service: SystemMetricsServiceDep,
    redis_bus: RedisBusServiceDep,
) -> str:
    workers_summary = _worker_status_summary(worker_status, worker_leases)
    signal_summary = redis_bus.worker_signal_summary()
    health_summary = system_health.summary(repo, workers_summary, signal_summary)
    return system_metrics_service.render(health_summary, workers_summary)


@router.get("/system/events")
async def system_events(
    repo: RepositoryDep,
    system_health: SystemHealthServiceDep,
    worker_status: WorkerStatusServiceDep,
    worker_leases: WorkerLeaseServiceDep,
    redis_bus: RedisBusServiceDep,
    interval_seconds: float = Query(default=5.0, ge=1.0, le=60.0),
    audit_limit: int = Query(default=8, ge=0, le=50),
    once: bool = Query(default=False),
) -> StreamingResponse:
    async def stream():
        while True:
            workers_summary = _worker_status_summary(worker_status, worker_leases)
            signal_summary = redis_bus.worker_signal_summary()
            health_summary = system_health.summary(repo, workers_summary, signal_summary)
            payload = {
                "schema_version": "system_status_event.v1",
                "event_type": "system.snapshot",
                "health": {
                    "status": health_summary["status"],
                    "checked_at": health_summary["checked_at"],
                    "counts": health_summary["counts"],
                    "queues": health_summary["queues"],
                    "worker_signals": health_summary["worker_signals"],
                },
                "workers": {
                    "status": workers_summary.status,
                    "checked_at": workers_summary.checked_at.isoformat(),
                    "counts": workers_summary.counts,
                    "heartbeat_ttl_seconds": workers_summary.heartbeat_ttl_seconds,
                    "lease_ttl_seconds": workers_summary.lease_ttl_seconds,
                    "runtime_state_retention_seconds": (
                        workers_summary.runtime_state_retention_seconds
                    ),
                    "stale_worker_count": workers_summary.counts.get("stale_workers", 0),
                    "active_lease_count": workers_summary.counts.get("active_leases", 0),
                    "retained_heartbeat_count": workers_summary.counts.get(
                        "retained_heartbeats", 0
                    ),
                    "pruned_stale_heartbeat_count": workers_summary.counts.get(
                        "pruned_stale_heartbeats", 0
                    ),
                },
                "audit_events": [
                    event.model_dump(mode="json")
                    for event in repo.list_audit_events(limit=audit_limit)
                ],
            }
            payload["redis_fanout"] = redis_bus.publish_system_event(payload)
            yield _sse_event(
                "system.snapshot",
                payload,
                event_id=f"system.snapshot:{health_summary['checked_at']}",
            )
            if once:
                break
            await asyncio.sleep(interval_seconds)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/system/backups")
async def list_system_backups(repo: RepositoryDep, backup_service: BackupServiceDep) -> dict:
    audit_events = await asyncio.to_thread(repo.list_audit_events, limit=500)
    backups = await asyncio.to_thread(backup_service.list_backups, audit_events)
    return {"backups": backups}


@router.post("/system/backups")
async def create_system_backup(
    request: BackupCreateRequest,
    repo: RepositoryDep,
    backup_service: BackupServiceDep,
) -> dict:
    try:
        return await asyncio.to_thread(backup_service.create_backup, repo, request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/system/backups/restore")
async def restore_system_backup(
    request: BackupRestoreRequest,
    repo: RepositoryDep,
    backup_service: BackupServiceDep,
) -> dict:
    try:
        return await asyncio.to_thread(backup_service.restore_backup, repo, request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes", response_model=Episode)
async def create_episode(
    request: EpisodeCreateRequest,
    repo: RepositoryDep,
) -> Episode:
    try:
        return repo.create(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/episodes/{episode_id}", response_model=Episode)
async def update_episode_definition(
    episode_id: UUID,
    request: EpisodeDefinitionUpdateRequest,
    repo: RepositoryDep,
) -> Episode:
    try:
        return repo.update_definition(episode_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes", response_model=list[Episode])
async def list_episodes(repo: RepositoryDep) -> list[Episode]:
    return list(await asyncio.to_thread(repo.list))


@router.get("/episodes/summaries", response_model=list[EpisodeSummary])
async def list_episode_summaries(
    repo: RepositoryDep,
    include_archived: bool = Query(default=True),
) -> list[EpisodeSummary]:
    return await asyncio.to_thread(
        repo.list_summaries,
        include_archived=include_archived,
    )


@router.get("/episodes/{episode_id}", response_model=Episode)
async def get_episode(
    episode_id: UUID,
    repo: RepositoryDep,
) -> Episode:
    try:
        episode = await asyncio.to_thread(repo.get, episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return _episode_detail_projection(episode)


@router.post("/episodes/{episode_id}/branding/logo", response_model=Episode)
async def upload_episode_branding_logo(
    episode_id: UUID,
    branding_service: BrandingServiceDep,
    repo: RepositoryDep,
    logo: Annotated[UploadFile, File()],
) -> Episode:
    try:
        episode = await asyncio.to_thread(repo.get, episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    payload = await logo.read(MAX_LOGO_BYTES + 1)
    try:
        metadata = branding_service.store_logo(
            payload,
            source="episode_upload",
            owner_id=str(episode.id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logo_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.image,
        language=episode.source_language,
        source_entity_type="episode_branding_logo",
        source_entity_id=metadata.revision_id,
        storage_uri=metadata.storage_uri,
        mime_type=metadata.mime_type,
        width=metadata.width,
        height=metadata.height,
        checksum=metadata.checksum,
        status="completed",
        generation_metadata={
            "schema_version": "episode_brand_logo.v1",
            "visual_role": "show_logo",
            "logo": metadata.model_dump(mode="json"),
        },
    )
    episode.definition.media.branding.logo_override = metadata
    episode.assets.append(logo_asset)
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="episode.branding.logo_uploaded",
            actor="user",
            details={
                "asset_id": str(logo_asset.id),
                "revision_id": metadata.revision_id,
                "checksum": metadata.checksum,
            },
        )
    )
    await asyncio.to_thread(repo.save, episode)
    return _episode_detail_projection(episode)


@router.post("/episodes/{episode_id}/branding/identity-slate", response_model=Episode)
async def ensure_episode_identity_slate(
    episode_id: UUID,
    branding_service: BrandingServiceDep,
    repo: RepositoryDep,
) -> Episode:
    """Create or reuse the immutable effective-brand slate for this episode."""
    try:
        episode = await asyncio.to_thread(repo.get, episode_id)
        project = (
            repo.get_project(episode.project_id) if episode.project_id is not None else None
        )
        branding_service.ensure_identity_slate(episode, project)
        await asyncio.to_thread(repo.save, episode)
        return _episode_detail_projection(episode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode or project not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/studio-camera-plates/upload", response_model=Episode)
async def upload_studio_camera_plate(
    episode_id: UUID,
    camera_plates: StudioCameraPlateServiceDep,
    repo: RepositoryDep,
    image: Annotated[UploadFile, File()],
    metadata: Annotated[str, Form()],
) -> Episode:
    try:
        episode = await asyncio.to_thread(repo.get, episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    try:
        parsed_metadata = StudioCameraPlateUploadMetadata.model_validate_json(metadata)
        payload = await image.read(MAX_CAMERA_PLATE_BYTES + 1)
        camera_plates.upload_plate(episode, payload, parsed_metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(repo.save, episode)
    return _episode_detail_projection(episode)


@router.post("/episodes/{episode_id}/studio-camera-plates/generate", response_model=Episode)
async def generate_studio_camera_plate(
    episode_id: UUID,
    request: StudioCameraPlateGenerateRequest,
    camera_plates: StudioCameraPlateServiceDep,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    try:
        episode = await asyncio.to_thread(repo.get, episode_id)
        asset = camera_plates.plan_managed_generation(episode, request)
        updated = await comfyui.generate_visual_assets(
            episode,
            VisualGenerationRequest(
                transcript_version_id=episode.canonical_transcript_version_id,
                asset_ids=[asset.id],
                user_id=request.user_id,
                fallback_on_failure=False,
                local_fallback_only=False,
            ),
            endpoints=repo.list_comfyui_endpoints(),
            workflows=repo.list_comfyui_workflows(),
            visual_profiles=repo.list_visual_profiles(),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail="managed B1 camera-plate generation failed"
        ) from exc
    await asyncio.to_thread(repo.save, updated)
    return _episode_detail_projection(updated)


@router.post(
    "/episodes/{episode_id}/studio-camera-plates/{asset_id}/review",
    response_model=Episode,
)
async def review_studio_camera_plate(
    episode_id: UUID,
    asset_id: UUID,
    request: StudioCameraPlateReviewRequest,
    camera_plates: StudioCameraPlateServiceDep,
    repo: RepositoryDep,
) -> Episode:
    try:
        episode = await asyncio.to_thread(repo.get, episode_id)
        camera_plates.review_plate(episode, asset_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        status_code = 404 if str(exc) == "studio camera plate not found" else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    await asyncio.to_thread(repo.save, episode)
    return _episode_detail_projection(episode)


_EPISODE_DETAIL_TIMING_TRACK_KEYS = frozenset(
    {
        "character_timestamps",
        "normalized_phoneme_timestamps",
        "phoneme_timestamps",
        "viseme_timestamps",
        "word_timestamps",
    }
)


def _episode_detail_projection(episode: Episode) -> Episode:
    """Keep large alignment tracks out of the frequently-polled UI response.

    The full tracks remain on the stored audio asset for subtitle and lipsync
    work. The episode detail surface only needs their compact timing summaries.
    """
    assets = [
        asset.model_copy(
            update={
                "generation_metadata": {
                    key: value
                    for key, value in asset.generation_metadata.items()
                    if key not in _EPISODE_DETAIL_TIMING_TRACK_KEYS
                }
            }
        )
        for asset in episode.assets
    ]
    return episode.model_copy(update={"assets": assets})


def _is_actionable_render_approval(episode: Episode, approval: Approval) -> bool:
    """Keep primer renders out of talk-show delivery approval gates.

    Primer rendering has its own review flow. Older releases created generic
    render approvals for every timeline, so retain those records for audit but
    do not let a primer or superseded transcript render block this episode.
    """
    if approval.stage not in {"preview_render_review", "final_render_review"}:
        return True
    if approval.target_type != "render_asset" or not approval.target_id:
        return True
    render_asset = next(
        (
            asset
            for asset in episode.assets
            if str(asset.id) == approval.target_id and asset.asset_type == AssetType.render
        ),
        None,
    )
    if render_asset is None or render_asset.source_entity_type != "timeline_asset":
        return True
    timeline_asset = next(
        (
            asset
            for asset in episode.assets
            if str(asset.id) == render_asset.source_entity_id
            and asset.asset_type == AssetType.timeline
        ),
        None,
    )
    if timeline_asset is None:
        return True
    if timeline_asset.source_entity_type == "primer_production":
        return False
    if timeline_asset.status == "replaced":
        return False
    if (
        timeline_asset.source_entity_type == "transcript_version"
        and episode.canonical_transcript_version_id is not None
    ):
        if timeline_asset.source_entity_id != str(episode.canonical_transcript_version_id):
            return False
        expected_render_type = "preview" if approval.stage == "preview_render_review" else "final"
        latest_render_asset = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.render
                and asset.status == "completed"
                and asset.source_entity_type == "timeline_asset"
                and asset.source_entity_id == str(timeline_asset.id)
                and asset.generation_metadata.get("render_type") == expected_render_type
            ),
            None,
        )
        return latest_render_asset is not None and latest_render_asset.id == render_asset.id
    return True


def _episode_summary(episode: Episode) -> EpisodeSummary:
    discussion = episode.discussion_session
    pending_approvals = [
        approval
        for approval in episode.approvals
        if approval.decision == "pending" and _is_actionable_render_approval(episode, approval)
    ]
    output_languages = [
        output.language for output in episode.definition.languages.outputs if output.language
    ]
    return EpisodeSummary(
        id=episode.id,
        project_id=episode.project_id,
        title=episode.title,
        slug=episode.slug,
        status=episode.status,
        source_language=episode.source_language,
        target_duration_seconds=episode.target_duration_seconds,
        minimum_duration_seconds=episode.minimum_duration_seconds,
        maximum_duration_seconds=episode.maximum_duration_seconds,
        current_workflow_id=episode.current_workflow_id,
        canonical_transcript_version_id=episode.canonical_transcript_version_id,
        output_languages=output_languages,
        discussion_phase=discussion.phase if discussion else None,
        discussion_status=discussion.status if discussion else None,
        discussion_turn_count=len(discussion.turns) if discussion else 0,
        estimated_duration_seconds=discussion.estimated_duration_seconds if discussion else 0,
        transcript_count=len(episode.transcripts),
        asset_count=len(episode.assets),
        quality_result_count=len(episode.quality_results),
        publish_job_count=len(episode.publish_jobs),
        pending_approval_count=len(pending_approvals),
        pending_approvals=pending_approvals,
        created_at=episode.created_at,
        updated_at=episode.updated_at,
    )


@router.patch("/episodes/{episode_id}/production-settings", response_model=Episode)
async def update_episode_production_settings(
    episode_id: UUID,
    request: EpisodeProductionSettingsUpdateRequest,
    repo: RepositoryDep,
) -> Episode:
    if request.minimum_duration_seconds > request.target_duration_seconds:
        raise HTTPException(
            status_code=422,
            detail="minimum duration must not exceed target duration",
        )
    if request.target_duration_seconds > request.maximum_duration_seconds:
        raise HTTPException(
            status_code=422,
            detail="target duration must not exceed maximum duration",
        )
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    requested_scene_reference_image_uri = (
        request.scene_reference_image_uri.strip()
        if isinstance(request.scene_reference_image_uri, str)
        else None
    )
    previous_scene_reference_image_uri = episode.definition.media.scene_reference_image_uri
    scene_reference_changed = (
        "scene_reference_image_uri" in request.model_fields_set
        and (requested_scene_reference_image_uri or None) != previous_scene_reference_image_uri
    )
    scene_dependent_assets: list[Asset] = []
    if scene_reference_changed:
        media_transcript = next(
            (
                transcript
                for transcript in reversed(episode.transcripts)
                if transcript.type == TranscriptType.localized
            ),
            None,
        )
        if media_transcript is None and episode.canonical_transcript_version_id is not None:
            media_transcript = next(
                (
                    transcript
                    for transcript in episode.transcripts
                    if transcript.id == episode.canonical_transcript_version_id
                ),
                None,
            )
        media_transcript_id = str(media_transcript.id) if media_transcript else None
        for asset in episode.assets:
            metadata = asset.generation_metadata
            prompt_inputs = metadata.get("prompt_inputs")
            belongs_to_media_transcript = (
                media_transcript_id is None
                or metadata.get("transcript_version_id") == media_transcript_id
            )
            is_scene_dependent = metadata.get("visual_role") in {
                "studio_seated_character",
                "studio_panel_keyframe",
            } or (
                metadata.get("visual_role") == "video_primary"
                and isinstance(prompt_inputs, dict)
                and prompt_inputs.get("studio_layout") == "seated_panel"
            )
            if asset.status != "replaced" and belongs_to_media_transcript and is_scene_dependent:
                scene_dependent_assets.append(asset)
        active_asset_ids = [
            str(asset.id)
            for asset in scene_dependent_assets
            if asset.status in {"submitted", "running"}
        ]
        if active_asset_ids:
            raise HTTPException(
                status_code=409,
                detail=(
                    "studio reference cannot be changed while scene-dependent "
                    f"media jobs are active: {', '.join(active_asset_ids)}"
                ),
            )
    previous = {
        "target_duration_seconds": episode.target_duration_seconds,
        "minimum_duration_seconds": episode.minimum_duration_seconds,
        "maximum_duration_seconds": episode.maximum_duration_seconds,
    }
    media_previous: dict[str, object] | None = None
    episode.target_duration_seconds = request.target_duration_seconds
    episode.minimum_duration_seconds = request.minimum_duration_seconds
    episode.maximum_duration_seconds = request.maximum_duration_seconds
    if {"scene_reference_image_uri", "opening", "directing"} & request.model_fields_set:
        media_previous = {}
        if "scene_reference_image_uri" in request.model_fields_set:
            media_previous["scene_reference_image_uri"] = (
                episode.definition.media.scene_reference_image_uri
            )
        if "opening" in request.model_fields_set:
            media_previous["opening"] = episode.definition.media.opening.model_dump(mode="json")
        if "directing" in request.model_fields_set:
            media_previous["directing"] = episode.definition.media.directing.model_dump(mode="json")
    if "scene_reference_image_uri" in request.model_fields_set:
        episode.definition.media.scene_reference_image_uri = (
            requested_scene_reference_image_uri or None
        )
    if "opening" in request.model_fields_set and request.opening is not None:
        episode.definition.media.opening = request.opening
    if "directing" in request.model_fields_set and request.directing is not None:
        episode.definition.media.directing = request.directing
    episode.definition.format.target_duration_minutes = max(
        1,
        round(request.target_duration_seconds / 60),
    )
    updated_at = datetime.now(UTC)
    invalidated_scene_asset_ids: list[str] = []
    if scene_reference_changed:
        for asset in scene_dependent_assets:
            asset.status = "replaced"
            asset.updated_at = updated_at
            asset.generation_metadata = {
                **asset.generation_metadata,
                "render_ready": False,
                "approval_status": "superseded",
                "replacement_reason": "episode studio reference changed",
                "replaced_at": updated_at.isoformat(),
                "replacement_scene_reference_image_uri": (
                    episode.definition.media.scene_reference_image_uri
                ),
            }
            invalidated_scene_asset_ids.append(str(asset.id))
    episode.updated_at = updated_at
    details: dict[str, object] = {
        "previous": previous,
        "current": {
            "target_duration_seconds": episode.target_duration_seconds,
            "minimum_duration_seconds": episode.minimum_duration_seconds,
            "maximum_duration_seconds": episode.maximum_duration_seconds,
        },
    }
    if media_previous is not None:
        details["media_previous"] = media_previous
        details["media_current"] = {
            "scene_reference_image_uri": episode.definition.media.scene_reference_image_uri,
            "opening": episode.definition.media.opening.model_dump(mode="json"),
            "directing": episode.definition.media.directing.model_dump(mode="json"),
        }
    if scene_reference_changed:
        details["scene_reference_change"] = {
            "invalidated_scene_asset_ids": invalidated_scene_asset_ids,
            "invalidated_scene_asset_count": len(invalidated_scene_asset_ids),
            "requires_panel_coverage_rebuild": bool(invalidated_scene_asset_ids),
        }
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="episode.production_settings.updated",
            actor=request.user_id or "system",
            details=details,
        )
    )
    return repo.save(episode)


@router.post("/episodes/{episode_id}/research/build", response_model=Episode)
async def build_episode_research(
    episode_id: UUID,
    request: ResearchBuildRequest,
    repo: RepositoryDep,
    research: ResearchServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = research.build_evidence_pack(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/research")
async def get_episode_research(
    episode_id: UUID,
    repo: RepositoryDep,
    research: ResearchServiceDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
        result = research.latest_evidence_pack(episode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        return {"evidence_pack_asset": None, "evidence_pack": None}
    asset, evidence_pack = result
    return {"evidence_pack_asset": asset, "evidence_pack": evidence_pack}


@router.get("/episodes/{episode_id}/research/sources", response_model=list[ResearchSource])
async def list_episode_research_sources(
    episode_id: UUID,
    repo: RepositoryDep,
) -> list[ResearchSource]:
    try:
        return repo.list_research_sources(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc


@router.get("/episodes/{episode_id}/research/claims", response_model=list[EvidenceClaim])
async def list_episode_evidence_claims(
    episode_id: UUID,
    repo: RepositoryDep,
) -> list[EvidenceClaim]:
    try:
        return repo.list_evidence_claims(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc


@router.post("/episodes/{episode_id}/research/claim-qc", response_model=Episode)
async def run_episode_claim_qc(
    episode_id: UUID,
    request: ResearchClaimQcRequest,
    repo: RepositoryDep,
    research: ResearchServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = research.run_claim_qc(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/research/source-review", response_model=Episode)
async def review_episode_research_source(
    episode_id: UUID,
    request: ResearchSourceReviewRequest,
    repo: RepositoryDep,
    research: ResearchServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = research.review_evidence_source(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/episodes/{episode_id}/primer-media/candidates",
    response_model=list[PrimerMediaCandidate],
)
async def list_primer_media_candidates(
    episode_id: UUID,
    repo: RepositoryDep,
    primer_media: PrimerMediaServiceDep,
) -> list[PrimerMediaCandidate]:
    try:
        return primer_media.candidates(repo.get(episode_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc


@router.post(
    "/episodes/{episode_id}/primer-media/discover",
    response_model=list[PrimerMediaCandidate],
)
async def discover_primer_media(
    episode_id: UUID,
    request: PrimerMediaDiscoveryRequest,
    repo: RepositoryDep,
    primer_media: PrimerMediaServiceDep,
) -> list[PrimerMediaCandidate]:
    try:
        episode = repo.get(episode_id)
        candidates = await primer_media.discover(episode, actor=request.user_id)
        repo.save(episode)
        return candidates
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/primer-media/acquire", response_model=Asset)
async def acquire_primer_media(
    episode_id: UUID,
    request: PrimerMediaAcquireRequest,
    repo: RepositoryDep,
    primer_media: PrimerMediaServiceDep,
) -> Asset:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc

    try:
        asset = primer_media.acquire(episode, request.candidate_id, actor=request.user_id)
    except ValueError as exc:
        # Acquisition records per-candidate failures for the editor to review.
        # Persist that state before returning the expected client error.
        repo.save(episode)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    repo.save(episode)
    return asset


@router.post("/episodes/{episode_id}/primer-media/import", response_model=Asset)
async def import_primer_media(
    episode_id: UUID,
    request: PrimerMediaImportRequest,
    repo: RepositoryDep,
    primer_media: PrimerMediaServiceDep,
) -> Asset:
    try:
        episode = repo.get(episode_id)
        asset = primer_media.import_operator_source(
            episode,
            request.source_url,
            title=request.title,
            actor=request.user_id,
        )
        repo.save(episode)
        return asset
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/primer", response_model=PrimerProductionStatus)
async def primer_production_status(
    episode_id: UUID,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerProductionStatus:
    try:
        episode = repo.get(episode_id)
        narrator_id = episode.definition.media.opening.narrator_profile_id
        narrator = (
            repo.get_primer_narrator_profile(narrator_id) if narrator_id else None
        )
        return primer.status(episode, narrator)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="episode or narrator profile not found",
        ) from exc


@router.post(
    "/episodes/{episode_id}/primer/spoken-script/prepare",
    response_model=PrimerSpokenScriptStatus,
)
async def prepare_primer_spoken_script(
    episode_id: UUID,
    request: PrimerSpokenScriptPrepareRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerSpokenScriptStatus:
    episode: Episode | None = None
    try:
        episode = repo.get(episode_id)
        narrator_id = episode.definition.media.opening.narrator_profile_id
        if not narrator_id:
            raise ValueError("select a narrator profile before preparing spoken narration")
        narrator = repo.get_primer_narrator_profile(narrator_id)
        status = await primer.prepare_spoken_script(
            episode,
            request,
            narrator,
            repo.list_model_endpoints(),
        )
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="episode or narrator profile not found",
        ) from exc
    except ValueError as exc:
        if episode is not None:
            repo.save(episode)
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put(
    "/episodes/{episode_id}/primer/spoken-script",
    response_model=PrimerSpokenScriptStatus,
)
async def update_primer_spoken_script(
    episode_id: UUID,
    request: PrimerSpokenScriptUpdateRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerSpokenScriptStatus:
    try:
        episode = repo.get(episode_id)
        narrator_id = episode.definition.media.opening.narrator_profile_id
        if not narrator_id:
            raise ValueError("select a narrator profile before editing spoken narration")
        narrator = repo.get_primer_narrator_profile(narrator_id)
        status = primer.update_spoken_script(episode, request, narrator)
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="episode or narrator profile not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/primer/spoken-script/approve",
    response_model=PrimerSpokenScriptStatus,
)
async def approve_primer_spoken_script(
    episode_id: UUID,
    request: PrimerSpokenScriptApprovalRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerSpokenScriptStatus:
    try:
        episode = repo.get(episode_id)
        narrator_id = episode.definition.media.opening.narrator_profile_id
        if not narrator_id:
            raise ValueError("select a narrator profile before approving spoken narration")
        narrator = repo.get_primer_narrator_profile(narrator_id)
        status = primer.approve_spoken_script(episode, request, narrator)
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="episode or narrator profile not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/episodes/{episode_id}/primer/visual-plan",
    response_model=PrimerVisualPlanStatus,
)
async def primer_visual_plan_status(
    episode_id: UUID,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanStatus:
    try:
        return primer.visual_plan_status(repo.get(episode_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc


@router.get(
    "/episodes/{episode_id}/primer/visual-plan/revisions",
    response_model=PrimerVisualPlanRevisionList,
)
async def primer_visual_plan_revisions(
    episode_id: UUID,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanRevisionList:
    try:
        return primer.visual_plan_revisions(repo.get(episode_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc


@router.post(
    "/episodes/{episode_id}/primer/visual-plan/revisions/{revision_id}/restore",
    response_model=PrimerVisualPlanStatus,
)
async def restore_primer_visual_plan_revision(
    episode_id: UUID,
    revision_id: str,
    request: PrimerVisualPlanRevisionRestoreRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanStatus:
    try:
        episode = repo.get(episode_id)
        status = primer.restore_visual_plan_revision(episode, revision_id, request.user_id)
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="episode or primer visual-plan revision not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/primer/visual-plan/prepare",
    response_model=PrimerVisualPlanStatus,
)
async def prepare_primer_visual_plan(
    episode_id: UUID,
    request: PrimerVisualPlanPrepareRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanStatus:
    episode: Episode | None = None
    try:
        episode = repo.get(episode_id)
        narrator_id = episode.definition.media.opening.narrator_profile_id
        if not narrator_id:
            raise ValueError("select a narrator profile before preparing the visual plan")
        narrator = repo.get_primer_narrator_profile(narrator_id)
        status = await primer.prepare_visual_plan(
            episode,
            request,
            narrator,
            repo.list_model_endpoints(),
        )
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="episode or narrator profile not found"
        ) from exc
    except ValueError as exc:
        if episode is not None:
            repo.save(episode)
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/primer/narration-timing",
    response_model=PrimerProductionStatus,
)
async def prepare_primer_narration_timing(
    episode_id: UUID,
    request: PrimerNarrationTimingRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerProductionStatus:
    episode: Episode | None = None
    try:
        episode = repo.get(episode_id)
        narrator_id = episode.definition.media.opening.narrator_profile_id
        if not narrator_id:
            raise ValueError("select a narrator profile before generating narration timing")
        narrator = repo.get_primer_narrator_profile(narrator_id)
        status = await primer.prepare_narration_timing(
            episode,
            request,
            narrator,
            repo.list_voice_profiles(),
            repo.list_voicebox_endpoints(),
        )
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="episode or narrator profile not found"
        ) from exc
    except ValueError as exc:
        if episode is not None:
            repo.save(episode)
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put(
    "/episodes/{episode_id}/primer/visual-plan/beats/{beat_id}",
    response_model=PrimerVisualPlanStatus,
)
async def update_primer_visual_plan_beat(
    episode_id: UUID,
    beat_id: str,
    request: PrimerVisualPlanBeatUpdateRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanStatus:
    try:
        episode = repo.get(episode_id)
        status = primer.update_visual_plan_beat(episode, beat_id, request)
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/primer/visual-plan/beats",
    response_model=PrimerVisualPlanStatus,
)
async def add_primer_visual_plan_beat(
    episode_id: UUID,
    request: PrimerVisualPlanBeatCreateRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanStatus:
    try:
        episode = repo.get(episode_id)
        status = primer.add_visual_plan_beat(episode, request)
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete(
    "/episodes/{episode_id}/primer/visual-plan/beats/{beat_id}",
    response_model=PrimerVisualPlanStatus,
)
async def remove_primer_visual_plan_beat(
    episode_id: UUID,
    beat_id: str,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
    user_id: str | None = None,
) -> PrimerVisualPlanStatus:
    try:
        episode = repo.get(episode_id)
        status = primer.remove_visual_plan_beat(episode, beat_id, user_id=user_id)
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/primer/visual-plan/sources/assess",
    response_model=PrimerVisualPlanStatus,
)
async def assess_primer_visual_plan_sources(
    episode_id: UUID,
    request: PrimerVisualPlanVerificationRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanStatus:
    try:
        episode = repo.get(episode_id)
        status = await primer.assess_visual_plan_source_media(
            episode,
            request,
            repo.list_model_endpoints(),
        )
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/primer/visual-plan/verify",
    response_model=PrimerVisualPlanStatus,
)
async def verify_primer_visual_plan_excerpts(
    episode_id: UUID,
    request: PrimerVisualPlanVerificationRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanStatus:
    try:
        episode = repo.get(episode_id)
        status = await primer.verify_visual_plan_excerpts(
            episode,
            request,
            repo.list_model_endpoints(),
        )
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/primer/visual-plan/approve",
    response_model=PrimerVisualPlanStatus,
)
async def approve_primer_visual_plan(
    episode_id: UUID,
    request: PrimerVisualPlanApprovalRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerVisualPlanStatus:
    try:
        episode = repo.get(episode_id)
        status = primer.approve_visual_plan(episode, request)
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/primer/produce", response_model=PrimerProductionStatus)
async def produce_topic_primer(
    episode_id: UUID,
    request: PrimerProductionRequest,
    repo: RepositoryDep,
    primer: PrimerProductionServiceDep,
) -> PrimerProductionStatus:
    episode: Episode | None = None
    try:
        episode = repo.get(episode_id)
        narrator_id = episode.definition.media.opening.narrator_profile_id
        if not narrator_id:
            raise ValueError("select a narrator profile before producing the primer")
        narrator = repo.get_primer_narrator_profile(narrator_id)
        status = await primer.produce(
            episode,
            request,
            narrator,
            repo.list_model_endpoints(),
            repo.list_voice_profiles(),
            repo.list_voicebox_endpoints(),
        )
        repo.save(episode)
        return status
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="episode or narrator profile not found"
        ) from exc
    except ValueError as exc:
        if episode is not None:
            repo.save(episode)
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/produce", response_model=ProductionStatus)
async def produce_episode(
    episode_id: UUID,
    repo: RepositoryDep,
    engine: DiscussionEngineDep,
    production_control: ProductionControlServiceDep,
) -> ProductionStatus:
    try:
        episode = repo.get(episode_id)
        episode = production_control.begin_run(episode)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        episode = await engine.run(episode)
    except ValueError as exc:
        failed_stage = (
            episode.status if isinstance(episode.status, EpisodeStatus) else EpisodeStatus.failed
        )
        repo.save(
            production_control.record_failure(
                episode,
                failed_stage,
                "discussion_engine.run",
                str(exc),
            )
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    repo.save(episode)
    return engine.status(episode)


@router.post("/episodes/{episode_id}/workflow/actions", response_model=Episode)
async def control_episode_workflow(
    episode_id: UUID,
    request: WorkflowActionRequest,
    repo: RepositoryDep,
    production_control: ProductionControlServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = production_control.apply_action(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/workflow/start", response_model=Episode)
async def start_episode_workflow(
    episode_id: UUID,
    request: WorkflowStartRequest,
    repo: RepositoryDep,
    production_control: ProductionControlServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = production_control.begin_run(
            episode,
            user_id=request.user_id or "web-ui",
        )
        if request.comment:
            updated.audit_events.append(
                AuditEvent(
                    episode_id=updated.id,
                    event_type="workflow.run.start_note",
                    actor=request.user_id or "system",
                    details={"comment_present": True},
                )
            )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/workflow/retries/{retry_id}/resolve", response_model=Episode)
async def resolve_episode_workflow_retry(
    episode_id: UUID,
    retry_id: str,
    request: WorkflowRetryResolutionRequest,
    repo: RepositoryDep,
    production_control: ProductionControlServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = production_control.acknowledge_stage_retry(episode, retry_id, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/workflow/advance", response_model=WorkflowAdvanceResponse)
async def advance_episode_workflow(
    episode_id: UUID,
    repo: RepositoryDep,
    settings: SettingsDep,
    research: ResearchServiceDep,
    engine: DiscussionEngineDep,
    production_control: ProductionControlServiceDep,
    localization: LocalizationServiceDep,
    voicebox: VoiceboxServiceDep,
    subtitles: SubtitleServiceDep,
    comfyui: ComfyUiServiceDep,
    timeline: TimelineServiceDep,
    render: RenderServiceDep,
    publisher: PublisherServiceDep,
) -> WorkflowAdvanceResponse:
    try:
        repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc

    summary = await _run_selected_episode_workflow_pass(
        episode_id=episode_id,
        repo=repo,
        settings=settings,
        research=research,
        engine=engine,
        production_control=production_control,
        localization=localization,
        voicebox=voicebox,
        subtitles=subtitles,
        comfyui=comfyui,
        timeline=timeline,
        render=render,
        publisher=publisher,
    )
    return WorkflowAdvanceResponse(episode=repo.get(episode_id), summary=summary)


@router.post(
    "/episodes/{episode_id}/workflow/run-until-blocked",
    response_model=WorkflowRunUntilBlockedResponse,
)
async def run_episode_workflow_until_blocked(
    episode_id: UUID,
    request: WorkflowRunUntilBlockedRequest,
    repo: RepositoryDep,
    settings: SettingsDep,
    research: ResearchServiceDep,
    engine: DiscussionEngineDep,
    production_control: ProductionControlServiceDep,
    localization: LocalizationServiceDep,
    voicebox: VoiceboxServiceDep,
    subtitles: SubtitleServiceDep,
    comfyui: ComfyUiServiceDep,
    timeline: TimelineServiceDep,
    render: RenderServiceDep,
    publisher: PublisherServiceDep,
) -> WorkflowRunUntilBlockedResponse:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc

    if request.start_if_needed and not _episode_has_running_workflow_run(episode):
        try:
            episode = production_control.begin_run(
                episode,
                user_id=request.user_id or "web-ui",
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if request.comment:
            episode.audit_events.append(
                AuditEvent(
                    episode_id=episode.id,
                    event_type="workflow.run_until_blocked.start_note",
                    actor=request.user_id or "system",
                    details={"comment_present": True},
                )
            )
        repo.save(episode)

    summaries: list[dict] = []
    progressed_stage_count = 0
    stop_reason = "max_passes_reached"

    for _ in range(request.max_passes):
        current = repo.get(episode_id)
        if current.status in {EpisodeStatus.completed, EpisodeStatus.cancelled}:
            stop_reason = current.status.value.lower()
            break
        pending = _pending_episode_approvals(current)
        if pending:
            stop_reason = "pending_approval"
            break
        if not _episode_has_running_workflow_run(current):
            stop_reason = "workflow_run_missing"
            break

        summary = await _run_selected_episode_workflow_pass(
            episode_id=episode_id,
            repo=repo,
            settings=settings,
            research=research,
            engine=engine,
            production_control=production_control,
            localization=localization,
            voicebox=voicebox,
            subtitles=subtitles,
            comfyui=comfyui,
            timeline=timeline,
            render=render,
            publisher=publisher,
        )
        summaries.append(summary)
        progressed = int(summary.get("progressed_stage_count") or 0)
        progressed_stage_count += progressed

        current = repo.get(episode_id)
        if current.status == EpisodeStatus.completed:
            stop_reason = "completed"
            break
        if _pending_episode_approvals(current):
            stop_reason = "pending_approval"
            break
        if int(summary.get("error_count") or 0) > 0:
            stop_reason = "stage_errors"
            break
        if progressed == 0:
            stop_reason = "no_progress"
            break

    episode = repo.get(episode_id)
    pending = _pending_episode_approvals(episode)
    completion = production_control.completion_readiness(episode)
    handoff = _latest_run_until_blocked_handoff(
        summaries,
        episode_id,
    ) or _pending_transcript_review_handoff(episode, pending)
    status = _workflow_run_until_blocked_status(
        episode=episode,
        stop_reason=stop_reason,
        pending_approvals=pending,
        completion=completion,
    )
    episode = _record_run_until_blocked_evidence(
        episode=episode,
        status=status,
        stop_reason=stop_reason,
        pass_count=len(summaries),
        progressed_stage_count=progressed_stage_count,
        summaries=summaries,
        pending_approvals=pending,
        completion=completion,
        handoff=handoff,
        actor=request.user_id or "web-ui",
    )
    episode = repo.save(episode)
    return WorkflowRunUntilBlockedResponse(
        episode=episode,
        status=status,
        stop_reason=stop_reason,
        pass_count=len(summaries),
        progressed_stage_count=progressed_stage_count,
        handoff=handoff,
        summaries=summaries,
        pending_approvals=pending,
        completion_readiness=completion,
    )


async def _run_selected_episode_workflow_pass(
    *,
    episode_id: UUID,
    repo: EpisodeRepository,
    settings: Settings,
    research: ResearchService,
    engine: DiscussionEngine,
    production_control: ProductionControlService,
    localization: LocalizationService,
    voicebox: VoiceboxService,
    subtitles: SubtitleService,
    comfyui: ComfyUiService,
    timeline: TimelineService,
    render: RenderService,
    publisher: PublisherService,
) -> dict:
    from app.workflows.worker_placeholder import run_workflow_worker_once

    return await run_workflow_worker_once(
        repository=EpisodeScopedWorkflowRepository(repo, episode_id),
        settings=settings,
        batch_limit=1,
        research_service=research,
        discussion_engine=engine,
        production_control=production_control,
        localization_service=localization,
        voicebox_service=voicebox,
        subtitle_service=subtitles,
        comfyui_service=comfyui,
        timeline_service=timeline,
        render_service=render,
        publisher_service=publisher,
        auto_queue_renders=True,
    )


def _episode_has_running_workflow_run(episode: Episode) -> bool:
    run = (episode.workflow_control or {}).get("run")
    return isinstance(run, dict) and run.get("state") == "running"


def _pending_episode_approvals(episode: Episode) -> list[Approval]:
    status = (
        episode.status.value if isinstance(episode.status, EpisodeStatus) else str(episode.status)
    )
    run = (episode.workflow_control or {}).get("run")
    if isinstance(run, dict) and isinstance(run.get("current_stage"), str):
        status = run["current_stage"]
    return [
        approval
        for approval in sorted(episode.approvals, key=lambda item: item.created_at)
        if approval.decision == "pending"
        and _is_actionable_render_approval(episode, approval)
        and (
            approval.stage != "transcript_review"
            or status == EpisodeStatus.transcript_review.value
            or _has_pending_broadcast_transcript(episode)
        )
    ]


def _has_pending_broadcast_transcript(episode: Episode) -> bool:
    return any(
        transcript.type == TranscriptType.broadcast
        and transcript.status == "pending_review"
        and len(transcript.turns) > 0
        for transcript in episode.transcripts
    )


def _workflow_run_until_blocked_status(
    *,
    episode: Episode,
    stop_reason: str,
    pending_approvals: list[Approval],
    completion: dict,
) -> str:
    if episode.status == EpisodeStatus.completed or stop_reason == "completed":
        return "completed"
    if episode.status == EpisodeStatus.cancelled:
        return "cancelled"
    if stop_reason == "stage_errors" or episode.status == EpisodeStatus.failed:
        return "blocked"
    if pending_approvals:
        return "awaiting_approval"
    if completion.get("status") == "pass":
        return "ready_to_complete"
    return "blocked" if stop_reason in {"no_progress", "workflow_run_missing"} else "running"


def _record_run_until_blocked_evidence(
    *,
    episode: Episode,
    status: str,
    stop_reason: str,
    pass_count: int,
    progressed_stage_count: int,
    summaries: list[dict],
    pending_approvals: list[Approval],
    completion: dict,
    handoff: dict | None,
    actor: str,
) -> Episode:
    now = datetime.now(UTC).isoformat()
    evidence = {
        "schema_version": "workflow_run_until_blocked_evidence.v1",
        "recorded_at": now,
        "status": status,
        "stop_reason": stop_reason,
        "pass_count": pass_count,
        "progressed_stage_count": progressed_stage_count,
        "pending_approval_count": len(pending_approvals),
        "pending_approval_stages": [approval.stage for approval in pending_approvals],
        "completion_status": completion.get("status"),
        "completion_failed_checks": list(completion.get("failed_checks") or [])[:8],
        "handoff": handoff
        if isinstance(handoff, dict)
        else {
            "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
            "status": "missing",
            "blocking_reasons": [],
        },
        "orchestration_attempt_ids": [
            str(summary.get("orchestration_attempt_id"))
            for summary in summaries
            if summary.get("orchestration_attempt_id")
        ][:10],
    }
    control = dict(episode.workflow_control or {})
    control["last_run_until_blocked"] = evidence
    run = control.get("run")
    if isinstance(run, dict):
        run["last_run_until_blocked"] = evidence
    episode.workflow_control = control
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="workflow.run_until_blocked.recorded",
            actor=actor,
            details={
                "schema_version": evidence["schema_version"],
                "status": status,
                "stop_reason": stop_reason,
                "pass_count": pass_count,
                "progressed_stage_count": progressed_stage_count,
                "pending_approval_count": len(pending_approvals),
                "completion_status": completion.get("status"),
            },
        )
    )
    return episode


def _latest_run_until_blocked_handoff(
    summaries: list[dict],
    episode_id: UUID,
) -> dict | None:
    for summary in reversed(summaries):
        handoffs = summary.get("production_handoffs")
        if not isinstance(handoffs, list):
            continue
        for handoff in reversed(handoffs):
            if not isinstance(handoff, dict):
                continue
            if str(handoff.get("episode_id")) != str(episode_id):
                continue
            return _compact_workflow_handoff_summary(handoff)
    return None


def _pending_transcript_review_handoff(
    episode: Episode,
    pending_approvals: list[Approval],
) -> dict | None:
    if not any(approval.stage == "transcript_review" for approval in pending_approvals):
        return None
    transcript = next(
        (
            item
            for item in reversed(episode.transcripts)
            if item.type == TranscriptType.broadcast
            and item.status == "pending_review"
            and len(item.turns) > 0
        ),
        None,
    )
    if transcript is None:
        return None
    blocking_reasons = ["transcript_not_approved"]
    participant_ids = {
        str(turn.speaker_participant_id) for turn in transcript.turns if turn.speaker_participant_id
    }
    participant_profiles = {
        profile.id: profile for profile in episode.participants if profile.id in participant_ids
    }
    configured_model_count = sum(
        1 for profile in participant_profiles.values() if profile.model_endpoint_id
    )
    configured_voice_count = sum(
        1 for profile in participant_profiles.values() if profile.voice_profile_id
    )
    configured_visual_count = sum(
        1 for profile in participant_profiles.values() if profile.visual_profile_id
    )
    active_speaker_count = len(participant_ids)
    return {
        "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
        "source_schema_version": "pending_transcript_review.v1",
        "episode_id": str(episode.id),
        "status": "blocked",
        "blocking_reasons": blocking_reasons,
        "transcript_version_id": str(transcript.id),
        "transcript_status": transcript.status,
        "language": transcript.language,
        "playable_turn_count": len(transcript.turns),
        "character_configuration": {
            "ready": (
                active_speaker_count > 0
                and configured_model_count == active_speaker_count
                and configured_voice_count == active_speaker_count
                and configured_visual_count == active_speaker_count
            ),
            "active_speaker_count": active_speaker_count,
            "configured_model_speaker_count": configured_model_count,
            "configured_voice_speaker_count": configured_voice_count,
            "configured_visual_speaker_count": configured_visual_count,
            "missing_model_participant_ids": [],
            "missing_voice_participant_ids": [],
            "missing_visual_participant_ids": [],
            "unknown_speaker_participant_ids": [],
        },
        "turn_handoffs": _compact_turn_handoff(None),
        "stage_readiness": {
            "speech": None,
            "character_animation": None,
            "studio_scene": None,
            "subtitles": None,
            "timeline": None,
            "publish": None,
            "preview_render_approved": None,
            "final_render_approved": None,
        },
        "asset_ids": {
            "preview_render": None,
            "final_render": None,
            "delivery_package": None,
            "production_manifest": None,
            "publish_job": None,
        },
        "next_handoff_action": _workflow_handoff_next_action(
            status="blocked",
            blocking_reasons=blocking_reasons,
            render={},
            publish={},
        ),
    }


def _compact_workflow_handoff_summary(handoff: dict) -> dict:
    blocking_reasons = [str(reason) for reason in handoff.get("blocking_reasons", []) if reason][
        :12
    ]
    character_configuration = handoff.get("character_configuration")
    turn_handoffs = handoff.get("turn_handoffs")
    speech = handoff.get("speech")
    character_animation = handoff.get("character_animation")
    studio_scene = handoff.get("studio_scene")
    timeline = handoff.get("timeline")
    render = handoff.get("render")
    publish = handoff.get("publish")
    return {
        "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
        "source_schema_version": handoff.get("schema_version"),
        "episode_id": handoff.get("episode_id"),
        "status": handoff.get("status"),
        "blocking_reasons": blocking_reasons,
        "transcript_version_id": handoff.get("transcript_version_id"),
        "transcript_status": handoff.get("transcript_status"),
        "language": handoff.get("language"),
        "playable_turn_count": handoff.get("playable_turn_count"),
        "character_configuration": _compact_character_configuration_handoff(
            character_configuration
        ),
        "turn_handoffs": _compact_turn_handoff(turn_handoffs),
        "stage_readiness": {
            "speech": _ready_flag(speech),
            "character_animation": _ready_flag(character_animation),
            "studio_scene": _ready_flag(studio_scene),
            "subtitles": _ready_flag(handoff.get("subtitles")),
            "timeline": _ready_flag(timeline),
            "publish": _ready_flag(publish),
            "preview_render_approved": (
                render.get("preview_render_approved") if isinstance(render, dict) else None
            ),
            "final_render_approved": (
                render.get("final_render_approved") if isinstance(render, dict) else None
            ),
        },
        "asset_ids": {
            "preview_render": (
                render.get("preview_render_asset_id") if isinstance(render, dict) else None
            ),
            "final_render": (
                render.get("final_render_asset_id") if isinstance(render, dict) else None
            ),
            "delivery_package": (
                render.get("delivery_package_asset_id") if isinstance(render, dict) else None
            ),
            "production_manifest": (
                render.get("production_manifest_asset_id") if isinstance(render, dict) else None
            ),
            "publish_job": (publish.get("publish_job_id") if isinstance(publish, dict) else None),
        },
        "next_handoff_action": _workflow_handoff_next_action(
            status=handoff.get("status"),
            blocking_reasons=blocking_reasons,
            render=render if isinstance(render, dict) else {},
            publish=publish if isinstance(publish, dict) else {},
        ),
    }


def _compact_character_configuration_handoff(value: object) -> dict:
    if not isinstance(value, dict):
        return {
            "ready": False,
            "active_speaker_count": 0,
            "configured_model_speaker_count": 0,
            "configured_voice_speaker_count": 0,
            "configured_visual_speaker_count": 0,
            "missing_model_participant_ids": [],
            "missing_voice_participant_ids": [],
            "missing_visual_participant_ids": [],
            "unknown_speaker_participant_ids": [],
        }
    return {
        "ready": value.get("ready") is True,
        "active_speaker_count": value.get("active_speaker_count"),
        "configured_model_speaker_count": value.get("configured_model_speaker_count"),
        "configured_voice_speaker_count": value.get("configured_voice_speaker_count"),
        "configured_visual_speaker_count": value.get("configured_visual_speaker_count"),
        "missing_model_participant_ids": [
            str(item) for item in value.get("missing_model_participant_ids", []) if item
        ][:12],
        "missing_voice_participant_ids": [
            str(item) for item in value.get("missing_voice_participant_ids", []) if item
        ][:12],
        "missing_visual_participant_ids": [
            str(item) for item in value.get("missing_visual_participant_ids", []) if item
        ][:12],
        "unknown_speaker_participant_ids": [
            str(item) for item in value.get("unknown_speaker_participant_ids", []) if item
        ][:12],
    }


def _compact_turn_handoff(value: object) -> dict:
    if not isinstance(value, dict):
        return {
            "completed_audio_turn_count": 0,
            "completed_primary_visual_turn_count": 0,
            "missing_audio_turn_ids": [],
            "missing_primary_visual_turn_ids": [],
            "stale_model_turn_ids": [],
            "stale_voice_asset_turn_ids": [],
            "stale_visual_asset_turn_ids": [],
        }
    return {
        "completed_audio_turn_count": value.get("completed_audio_turn_count"),
        "completed_primary_visual_turn_count": value.get("completed_primary_visual_turn_count"),
        "missing_audio_turn_ids": [
            str(item) for item in value.get("missing_audio_turn_ids", []) if item
        ][:12],
        "missing_primary_visual_turn_ids": [
            str(item) for item in value.get("missing_primary_visual_turn_ids", []) if item
        ][:12],
        "stale_model_turn_ids": [
            str(item) for item in value.get("stale_model_turn_ids", []) if item
        ][:12],
        "stale_voice_asset_turn_ids": [
            str(item) for item in value.get("stale_voice_asset_turn_ids", []) if item
        ][:12],
        "stale_visual_asset_turn_ids": [
            str(item) for item in value.get("stale_visual_asset_turn_ids", []) if item
        ][:12],
    }


def _ready_flag(value: object) -> bool | None:
    if not isinstance(value, dict):
        return None
    ready = value.get("ready")
    return ready if isinstance(ready, bool) else None


def _workflow_handoff_next_action(
    *,
    status: object,
    blocking_reasons: list[str],
    render: dict,
    publish: dict,
) -> str:
    reason_actions = {
        "approved_transcript_missing": "approve_or_generate_broadcast_transcript",
        "transcript_not_approved": "approve_broadcast_transcript",
        "playable_turns_missing": "regenerate_discussion_turns",
        "character_profile_missing": "map_unknown_speakers_to_participants",
        "character_model_missing": "configure_character_model_sources",
        "character_model_turn_stale": "rerun_discussion_after_model_changes",
        "character_voice_missing": "configure_character_voice_profiles",
        "character_voice_asset_stale": "regenerate_stale_speech_assets",
        "completed_audio_missing": "produce_remaining_speech_assets",
        "character_visual_missing": "configure_character_visual_profiles",
        "character_visual_asset_stale": "regenerate_stale_character_visuals",
        "completed_character_visual_missing": "produce_remaining_character_visuals",
        "shot_planned_reaction_loop_missing": "produce_character_reaction_loop_assets",
        "shot_planned_studio_scene_missing": "produce_studio_scene_assets",
        "subtitle_asset_missing": "generate_subtitles",
        "timeline_asset_missing": "build_episode_timeline",
        "timeline_segments_missing": "repair_episode_timeline_segments",
        "localized_output_missing": "generate_localized_outputs",
        "localized_output_not_approved": "approve_localized_outputs",
        "localized_output_qc_missing": "run_localized_output_qc",
        "localized_output_qc_failing": "repair_localized_outputs",
        "render_asset_missing": "render_preview_or_final_video",
        "preview_render_qc_failed": "repair_preview_render",
        "final_render_qc_failed": "repair_final_render",
        "export_package_qc_missing": "inspect_or_regenerate_youtube_export_package",
        "export_package_qc_failed": "repair_youtube_export_package",
        "thumbnail_missing": "generate_thumbnail",
        "export_package_thumbnail_missing": "regenerate_youtube_export_package",
        "export_package_subtitles_missing": "regenerate_youtube_export_package",
        "production_manifest_invalid": "regenerate_production_manifest",
        "publish_delivery_qc_missing": "run_publish_delivery_qc",
        "publish_delivery_qc_failed": "repair_publish_delivery",
        "production_manifest_publish_evidence_missing": "regenerate_production_manifest",
        "claim_qc_failed": "repair_or_approve_claim_qc",
    }
    for reason in blocking_reasons:
        action = reason_actions.get(reason)
        if action:
            return action
    if status == "review_ready":
        if render.get("preview_render_asset_id") and not render.get("preview_render_approved"):
            return "review_preview_render"
        if render.get("final_render_asset_id") and not render.get("final_render_approved"):
            return "review_final_render"
        return "inspect_render_for_next_review_gate"
    if status == "delivery_ready":
        return "complete_workflow_or_inspect_publish_evidence"
    if status == "media_ready":
        return "continue_workflow_to_render"
    if publish.get("publish_job_status") and publish.get("publish_job_status") != "completed":
        return "wait_for_or_repair_publish_job"
    return "inspect_workflow_handoff"


def _latest_run_until_blocked_record(episode: Episode) -> dict | None:
    control = episode.workflow_control or {}
    evidence = control.get("last_run_until_blocked")
    if isinstance(evidence, dict):
        return evidence
    run = control.get("run")
    if isinstance(run, dict) and isinstance(run.get("last_run_until_blocked"), dict):
        return run["last_run_until_blocked"]
    return None


def _compact_run_until_blocked_summary(episode: Episode) -> dict:
    evidence = _latest_run_until_blocked_record(episode)
    if not isinstance(evidence, dict):
        return {
            "schema_version": "production_workflow_run_until_blocked_summary.v1",
            "status": "missing",
            "stop_reason": None,
            "pending_approval_count": 0,
            "pending_approval_stages": [],
            "completion_failed_checks": [],
            "handoff": {
                "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
                "status": "missing",
                "blocking_reasons": [],
            },
            "orchestration_attempt_count": 0,
            "orchestration_attempt_ids": [],
        }
    attempt_ids = [str(value) for value in evidence.get("orchestration_attempt_ids", []) if value][
        :10
    ]
    return {
        "schema_version": "production_workflow_run_until_blocked_summary.v1",
        "source_schema_version": evidence.get("schema_version"),
        "recorded_at": evidence.get("recorded_at"),
        "status": evidence.get("status"),
        "stop_reason": evidence.get("stop_reason"),
        "pass_count": evidence.get("pass_count"),
        "progressed_stage_count": evidence.get("progressed_stage_count"),
        "pending_approval_count": evidence.get("pending_approval_count"),
        "pending_approval_stages": [
            str(stage) for stage in evidence.get("pending_approval_stages", []) if stage
        ][:8],
        "completion_status": evidence.get("completion_status"),
        "completion_failed_checks": [
            str(check) for check in evidence.get("completion_failed_checks", []) if check
        ][:8],
        "handoff": evidence.get("handoff")
        if isinstance(evidence.get("handoff"), dict)
        else {
            "schema_version": "workflow_run_until_blocked_handoff_summary.v1",
            "status": "missing",
            "blocking_reasons": [],
        },
        "orchestration_attempt_count": len(attempt_ids),
        "orchestration_attempt_ids": attempt_ids,
    }


@router.get("/episodes/{episode_id}/status", response_model=ProductionStatus)
async def episode_status(
    episode_id: UUID,
    repo: RepositoryDep,
    engine: DiscussionEngineDep,
) -> ProductionStatus:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return engine.status(episode)


@router.get("/episodes/{episode_id}/workflow/replay")
async def replay_episode_workflow(
    episode_id: UUID,
    repo: RepositoryDep,
    production_control: ProductionControlServiceDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return production_control.replay_workflow(episode)


@router.get("/episodes/{episode_id}/workflow/completion-readiness")
async def episode_completion_readiness(
    episode_id: UUID,
    repo: RepositoryDep,
    production_control: ProductionControlServiceDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return production_control.completion_readiness(episode)


@router.get("/episodes/{episode_id}/pilot-readiness")
async def episode_pilot_readiness(
    episode_id: UUID,
    repo: RepositoryDep,
    system_health: SystemHealthServiceDep,
    comfyui: ComfyUiServiceDep,
    refresh_comfyui_health: bool = Query(default=False),
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    refresh = await _refresh_pilot_comfyui_health(repo, comfyui) if refresh_comfyui_health else None
    readiness = system_health.episode_pilot_readiness(episode, repo)
    if refresh is not None:
        readiness["comfyui_health_refresh"] = refresh
    return readiness


async def _refresh_pilot_comfyui_health(
    repo: EpisodeRepository,
    comfyui: ComfyUiService,
) -> dict:
    endpoints = repo.list_comfyui_endpoints()
    refreshable = [
        endpoint
        for endpoint in endpoints
        if endpoint.enabled
        and (endpoint.adapter_type != "mock" or endpoint.capabilities.get("native_comfyui") is True)
    ]
    refreshed = []
    for endpoint in refreshable:
        try:
            checked = await comfyui.check_endpoint_health(endpoint)
            saved = repo.upsert_comfyui_endpoint(checked)
            refreshed.append(_comfyui_health_refresh_evidence(saved, status="pass"))
        except RuntimeError as exc:
            refreshed.append(
                _comfyui_health_refresh_error(endpoint, "fail", f"RuntimeError: {exc}")
            )
        except httpx.HTTPError as exc:
            endpoint.health_status = "unhealthy"
            saved = repo.upsert_comfyui_endpoint(endpoint)
            refreshed.append(
                _comfyui_health_refresh_error(
                    saved,
                    "fail",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    issues = [
        f"{entry.get('endpoint_id')}:{entry.get('status')}"
        for entry in refreshed
        if entry.get("status") != "pass"
    ]
    return {
        "schema_version": "pilot_comfyui_health_refresh.v1",
        "status": "pass" if not issues else "warning",
        "candidate_endpoint_count": len(refreshable),
        "refreshed": refreshed,
        "issues": issues,
    }


def _comfyui_health_refresh_evidence(endpoint: ComfyUiEndpoint, *, status: str) -> dict:
    capabilities = endpoint.capabilities if isinstance(endpoint.capabilities, dict) else {}
    return {
        "endpoint_id": endpoint.id,
        "status": status,
        "health_status": endpoint.health_status,
        "native_comfyui": capabilities.get("native_comfyui"),
        "prompt_admission_ready": capabilities.get("prompt_admission_ready"),
        "prompt_admission": _compact_prompt_admission(capabilities),
    }


def _comfyui_health_refresh_error(
    endpoint: ComfyUiEndpoint,
    status: str,
    error: str,
) -> dict:
    return {
        **_comfyui_health_refresh_evidence(endpoint, status=status),
        "error": error,
    }


def _compact_prompt_admission(capabilities: dict) -> dict | None:
    probe = capabilities.get("prompt_admission_probe")
    if not isinstance(probe, dict):
        return None
    response = probe.get("response") if isinstance(probe.get("response"), dict) else {}
    detail = response.get("detail") if isinstance(response.get("detail"), dict) else {}
    hardware = (
        detail.get("hardware_resource_policy")
        if isinstance(detail.get("hardware_resource_policy"), dict)
        else {}
    )
    return {
        "ready": capabilities.get("prompt_admission_ready") is True,
        "status_code": probe.get("status_code"),
        "code": detail.get("code"),
        "message": detail.get("message"),
        "detail": hardware.get("detail"),
    }


@router.get("/episodes/{episode_id}/production-test-report")
async def episode_production_test_report(
    episode_id: UUID,
    repo: RepositoryDep,
    production_control: ProductionControlServiceDep,
    system_health: SystemHealthServiceDep,
    settings: SettingsDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    completion = production_control.completion_readiness(episode)
    pilot = system_health.episode_pilot_readiness(episode, repo)
    object_store = create_object_store(settings)
    return _episode_production_test_report(
        episode,
        completion,
        pilot,
        object_store,
        managed_media_smoke_evidence_path=settings.b1_managed_media_smoke_evidence_path,
        live_provider_preflight=_latest_live_provider_preflight_summary(repo),
        voicebox_endpoints=repo.list_voicebox_endpoints(),
        voice_profiles=repo.list_voice_profiles(),
    )


def _episode_production_test_report(
    episode: Episode,
    completion: dict,
    pilot: dict,
    object_store,
    managed_media_smoke_evidence_path: str | None = None,
    provider_requirements_path: str | None = "/home/mordred/media-requirements.md",
    live_provider_preflight: dict | None = None,
    voicebox_endpoints: list[VoiceboxEndpoint] | None = None,
    voice_profiles: list[VoiceProfile] | None = None,
) -> dict:
    completion_passed = completion.get("status") == "pass"
    target_satisfied = completion.get("production_target_satisfied") is True
    latest_publish_job = _latest_publish_job(episode)
    publish_completed = latest_publish_job is not None and latest_publish_job.status == "completed"
    selected_mode = (
        pilot.get("selected_pilot_mode")
        if isinstance(pilot.get("selected_pilot_mode"), dict)
        else {}
    )
    native_visual_mode = next(
        (
            mode
            for mode in pilot.get("pilot_modes", [])
            if isinstance(mode, dict) and mode.get("mode") == "native_visual"
        ),
        None,
    )
    blockers = list(completion.get("failed_checks") or [])
    deliverables = {
        "final_render": _asset_evidence(
            episode,
            completion.get("final_render_asset_id"),
            AssetType.render,
            object_store,
        ),
        "thumbnail": _asset_evidence(
            episode,
            completion.get("thumbnail_asset_id"),
            AssetType.thumbnail,
            object_store,
        ),
        "export_package": _asset_evidence(
            episode,
            completion.get("export_package_asset_id"),
            AssetType.export_package,
            object_store,
        ),
        "production_manifest": _asset_evidence(
            episode,
            completion.get("production_manifest_asset_id"),
            AssetType.production_manifest,
            object_store,
        ),
    }
    for key in ("final_render", "export_package", "production_manifest"):
        evidence = deliverables.get(key)
        if not evidence:
            blockers.append(f"{key}_missing")
        elif not evidence.get("downloadable"):
            blockers.append(f"{key}_not_downloadable")
    package_asset = _asset_by_id(
        episode,
        deliverables["export_package"]["asset_id"] if deliverables.get("export_package") else None,
    )
    package_inspection = (
        _youtube_package_inspection(episode, package_asset, object_store)
        if package_asset is not None
        else None
    )
    if package_inspection is None:
        blockers.append("export_package_not_inspectable")
    elif package_inspection.get("status") != "pass":
        blockers.append("export_package_not_inspectable")
    if latest_publish_job is None:
        blockers.append("publish_job_missing")
    elif latest_publish_job.status != "completed":
        blockers.append("publish_job_not_completed")
    report_status = (
        "pass"
        if completion_passed and target_satisfied and publish_completed and not blockers
        else "warning"
    )
    required_deliverables = {
        name: deliverables.get(name)
        for name in ("final_render", "export_package", "production_manifest")
    }
    asset_download_status = (
        "pass"
        if all(
            isinstance(evidence, dict) and evidence.get("downloadable") is True
            for evidence in required_deliverables.values()
        )
        else "fail"
    )
    publish_evidence_binding = _publish_evidence_binding(
        episode,
        latest_publish_job,
        deliverables.get("export_package"),
        deliverables.get("production_manifest"),
    )
    workflow_run_until_blocked = _compact_run_until_blocked_summary(episode)
    acceptance_summary = _production_acceptance_summary(
        episode=episode,
        completion=completion,
        report_status=report_status,
        production_target=completion.get("production_target"),
        production_target_satisfied=target_satisfied,
        deliverables=required_deliverables,
        package_inspection=package_inspection,
        asset_download_status=asset_download_status,
        blockers=blockers,
        publish_evidence_binding=publish_evidence_binding,
        workflow_run_until_blocked=workflow_run_until_blocked,
    )
    media_readiness = _production_media_readiness(
        episode,
        completion,
        pilot,
        native_visual_mode,
        managed_media_smoke_evidence_path=managed_media_smoke_evidence_path,
        voicebox_endpoints=voicebox_endpoints,
        voice_profiles=voice_profiles,
    )
    live_provider_preflight = live_provider_preflight or _missing_live_provider_preflight_summary()
    real_life_test_readiness = _production_real_life_test_readiness(
        report_status=report_status,
        audio_first_test_ready=completion_passed and target_satisfied,
        native_visual_test_ready=_native_visual_test_ready(completion, native_visual_mode),
        live_provider_preflight=live_provider_preflight,
        media_readiness=media_readiness,
    )
    operator_next_action = _production_operator_next_action(
        report_status=report_status,
        blockers=blockers,
        media_readiness=media_readiness,
        package_inspection=package_inspection,
        latest_publish_job=latest_publish_job,
        asset_download_status=asset_download_status,
        live_provider_preflight=live_provider_preflight,
        workflow_run_until_blocked=workflow_run_until_blocked,
    )
    return {
        "schema_version": "production_test_report.v1",
        "episode_id": str(episode.id),
        "episode_status": episode.status,
        "checked_at": datetime.now(UTC),
        "status": report_status,
        "production_target": completion.get("production_target"),
        "production_target_satisfied": target_satisfied,
        "audio_first_test_ready": completion_passed and target_satisfied,
        "native_visual_test_ready": _native_visual_test_ready(completion, native_visual_mode),
        "real_life_test_readiness": real_life_test_readiness,
        "selected_pilot_mode": {
            "mode": selected_mode.get("mode"),
            "status": selected_mode.get("status"),
            "warnings": selected_mode.get("warnings", []),
            "blockers": selected_mode.get("blockers", []),
        },
        "completion": {
            "status": completion.get("status"),
            "failed_checks": completion.get("failed_checks", []),
            "playable_turn_count": completion.get("playable_turn_count"),
            "completed_audio_turn_count": completion.get("completed_audio_turn_count"),
            "completed_primary_visual_turn_count": (
                completion.get("completed_primary_visual_turn_count")
            ),
            "visual_source_summary": completion.get("visual_source_summary"),
        },
        "media_readiness": media_readiness,
        "live_provider_preflight": live_provider_preflight,
        "workflow_run_until_blocked": workflow_run_until_blocked,
        "provider_repair_handoff": _provider_repair_handoff(provider_requirements_path),
        "deliverables": deliverables,
        "approvals": {
            "preview_render_approved": completion.get("preview_render_approved") is True,
            "final_render_approved": completion.get("final_render_approved") is True,
            "latest_final_render_approval": _approval_evidence(
                _latest_approval(episode, "final_render_review")
            ),
        },
        "publish": _publish_job_evidence(latest_publish_job),
        "publish_evidence_binding": publish_evidence_binding,
        "package_inspection": package_inspection,
        "quality": {
            "audio_qc_status": completion.get("audio_qc_status"),
            "visual_qc_status": completion.get("visual_qc_status"),
            "subtitle_qc_status": completion.get("subtitle_qc_status"),
            "timeline_qc_status": completion.get("timeline_qc_status"),
            "final_render_qc_status": completion.get("final_render_qc_status"),
            "export_package_qc_status": completion.get("export_package_qc_status"),
            "publish_delivery_qc_status": completion.get("publish_delivery_qc_status"),
            "production_manifest_publish_evidence_valid": (
                completion.get("production_manifest_publish_evidence_valid")
            ),
        },
        "asset_counts": _asset_counts(episode),
        "blockers": blockers,
        "warnings": list(pilot.get("warnings") or []),
        "acceptance_summary": acceptance_summary,
        "operator_next_action": operator_next_action,
        "operator_next_actions": _production_operator_next_actions(
            primary_action=operator_next_action,
            report_status=report_status,
            blockers=blockers,
            media_readiness=media_readiness,
            package_inspection=package_inspection,
            latest_publish_job=latest_publish_job,
            asset_download_status=asset_download_status,
            live_provider_preflight=live_provider_preflight,
            workflow_run_until_blocked=workflow_run_until_blocked,
        ),
    }


def _provider_repair_handoff(path: str | None) -> dict:
    if not path:
        return {
            "schema_version": "provider_repair_handoff.v1",
            "configured": False,
            "status": "not_configured",
            "path": None,
            "exists": False,
        }
    requirements_path = Path(path)
    evidence = {
        "schema_version": "provider_repair_handoff.v1",
        "configured": True,
        "path": str(requirements_path),
        "exists": False,
        "status": "missing",
        "section_count": 0,
        "latest_sections": [],
        "has_voicebox_requirements": False,
        "has_managed_media_requirements": False,
    }
    try:
        stat = requirements_path.stat()
        text = requirements_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return evidence
    except OSError as exc:
        return {
            **evidence,
            "status": "unreadable",
            "error_type": exc.__class__.__name__,
        }
    headings = [
        line.strip().lstrip("#").strip() for line in text.splitlines() if line.startswith("### ")
    ]
    return {
        **evidence,
        "exists": True,
        "status": "present",
        "file_size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC),
        "section_count": len(headings),
        "latest_sections": headings[-4:],
        "has_voicebox_requirements": "Voicebox Smoke Recheck Added" in text,
        "has_managed_media_requirements": "B1 Managed Media Smoke Recheck Added" in text,
    }


def _missing_live_provider_preflight_summary() -> dict:
    return {
        "schema_version": "production_live_provider_preflight_summary.v1",
        "status": "missing",
        "ready": False,
        "action": "run_live_provider_preflight_before_real_life_test",
    }


def _latest_live_provider_preflight_summary(repo: EpisodeRepository) -> dict:
    events = repo.list_audit_events(
        limit=1,
        event_type="live_provider.cast_preflight_checked",
    )
    if not events:
        return _missing_live_provider_preflight_summary()
    event = events[0]
    details = event.details if isinstance(event.details, dict) else {}
    model_summary = (
        details.get("model_summary") if isinstance(details.get("model_summary"), dict) else {}
    )
    voicebox_summary = (
        details.get("voicebox_summary") if isinstance(details.get("voicebox_summary"), dict) else {}
    )
    status = str(details.get("status") or "unknown")
    blocking_sections = [
        str(section) for section in details.get("blocking_sections", []) if section
    ][:6]
    return {
        "schema_version": "production_live_provider_preflight_summary.v1",
        "status": status,
        "ready": status == "pass",
        "checked_at": event.created_at,
        "actor": event.actor,
        "participant_ids": [
            str(participant_id)
            for participant_id in details.get("participant_ids", [])
            if participant_id
        ][:12],
        "blocking_sections": blocking_sections,
        "model_participant_count": int(model_summary.get("participant_count") or 0),
        "model_failed_count": int(model_summary.get("failed_count") or 0),
        "failed_model_ids": [
            str(model_id) for model_id in model_summary.get("failed_model_ids", []) if model_id
        ][:8],
        "voicebox_participant_count": int(voicebox_summary.get("participant_count") or 0),
        "voicebox_failed_count": int(voicebox_summary.get("failed_count") or 0),
        "failed_voice_profile_ids": [
            str(profile_id)
            for profile_id in voicebox_summary.get("failed_voice_profile_ids", [])
            if profile_id
        ][:8],
        "action": _live_provider_preflight_operator_action(
            status=status,
            blocking_sections=blocking_sections,
        ),
    }


def _live_provider_preflight_operator_action(
    *,
    status: str,
    blocking_sections: list[str],
) -> str:
    if status == "pass":
        return "live_provider_preflight_ready"
    if "voicebox" in blocking_sections and "openrouter" in blocking_sections:
        return "fix_live_provider_failures_then_rerun_preflight"
    if "voicebox" in blocking_sections:
        return "fix_voicebox_generation_then_rerun_live_preflight"
    if "openrouter" in blocking_sections:
        return "fix_model_provider_then_rerun_live_preflight"
    if status == "missing":
        return "run_live_provider_preflight_before_real_life_test"
    return "inspect_live_provider_preflight"


def _production_real_life_test_readiness(
    *,
    report_status: str,
    audio_first_test_ready: bool,
    native_visual_test_ready: bool,
    live_provider_preflight: dict,
    media_readiness: dict,
) -> dict:
    live_preflight_ready = live_provider_preflight.get("ready") is True
    managed_smoke = (
        media_readiness.get("managed_media_smoke")
        if isinstance(media_readiness.get("managed_media_smoke"), dict)
        else {}
    )
    managed_smoke_ready = managed_smoke.get("ready") is True and managed_smoke.get("fresh") is True
    audio_blockers = _real_life_test_blockers(
        local_acceptance_ready=report_status == "pass" and audio_first_test_ready,
        live_preflight_ready=live_preflight_ready,
        include_managed_media=False,
        managed_smoke_ready=managed_smoke_ready,
        live_provider_preflight=live_provider_preflight,
        managed_smoke=managed_smoke,
    )
    native_blockers = _real_life_test_blockers(
        local_acceptance_ready=report_status == "pass" and native_visual_test_ready,
        live_preflight_ready=live_preflight_ready,
        include_managed_media=True,
        managed_smoke_ready=managed_smoke_ready,
        live_provider_preflight=live_provider_preflight,
        managed_smoke=managed_smoke,
    )
    audio_ready = not audio_blockers
    native_ready = not native_blockers
    return {
        "schema_version": "production_real_life_test_readiness.v1",
        "audio_first_ready": audio_ready,
        "native_visual_ready": native_ready,
        "ready": audio_ready or native_ready,
        "recommended_mode": (
            "native_visual" if native_ready else "audio_first" if audio_ready else None
        ),
        "audio_first_blockers": audio_blockers,
        "native_visual_blockers": native_blockers,
        "live_provider_preflight_ready": live_preflight_ready,
        "managed_media_smoke_ready": managed_smoke_ready,
        "next_action": _real_life_test_next_action(
            audio_blockers=audio_blockers,
            native_blockers=native_blockers,
        ),
    }


def _real_life_test_blockers(
    *,
    local_acceptance_ready: bool,
    live_preflight_ready: bool,
    include_managed_media: bool,
    managed_smoke_ready: bool,
    live_provider_preflight: dict,
    managed_smoke: dict,
) -> list[str]:
    blockers: list[str] = []
    if not local_acceptance_ready:
        blockers.append("local_acceptance_not_ready")
    if not live_preflight_ready:
        action = str(live_provider_preflight.get("action") or "")
        blockers.append(action or "live_provider_preflight_not_ready")
    if include_managed_media and not managed_smoke_ready:
        action = str(managed_smoke.get("action") or "")
        blockers.append(action or "b1_managed_media_smoke_not_ready")
    return blockers


def _real_life_test_next_action(
    *,
    audio_blockers: list[str],
    native_blockers: list[str],
) -> str:
    blockers = audio_blockers or native_blockers
    if not blockers:
        return "start_real_life_test"
    if any("voicebox" in blocker or "provider" in blocker for blocker in blockers):
        return "rerun_live_provider_preflight_after_provider_fix"
    if any("managed_media" in blocker or "b1_" in blocker for blocker in blockers):
        return "rerun_b1_managed_media_smoke_after_provider_fix"
    return blockers[0]


def _production_media_readiness(
    episode: Episode,
    completion: dict,
    pilot: dict,
    native_visual_mode: object,
    managed_media_smoke_evidence_path: str | None = None,
    voicebox_endpoints: list[VoiceboxEndpoint] | None = None,
    voice_profiles: list[VoiceProfile] | None = None,
) -> dict:
    visual_stage = next(
        (
            stage
            for stage in pilot.get("stages", [])
            if isinstance(stage, dict) and stage.get("category") == "visuals"
        ),
        {},
    )
    visual_details = (
        visual_stage.get("details") if isinstance(visual_stage.get("details"), dict) else {}
    )
    checks = (
        visual_details.get("readiness_checks")
        if isinstance(visual_details.get("readiness_checks"), dict)
        else {}
    )
    native_mode = native_visual_mode if isinstance(native_visual_mode, dict) else {}
    native_visual_config_ready = native_mode.get("status") == "pass"
    native_visual_ready = _native_visual_test_ready(completion, native_visual_mode)
    managed_media_execution = _managed_media_execution_evidence(
        episode,
        managed_media_required=bool(visual_details.get("managed_media_required_endpoints", [])),
    )
    managed_media_smoke = _managed_media_smoke_evidence(managed_media_smoke_evidence_path)
    audio_generation = _production_audio_generation_evidence(
        episode,
        voicebox_endpoints=voicebox_endpoints,
        voice_profiles=voice_profiles,
    )
    return {
        "schema_version": "production_media_readiness.v1",
        "production_target": completion.get("production_target"),
        "audio_first_ready": completion.get("status") == "pass"
        and completion.get("production_target_satisfied") is True,
        "audio_generation_ready": audio_generation["ready"],
        "audio_generation": audio_generation,
        "audio_operator_action": _audio_generation_operator_action(audio_generation),
        "native_visual_ready": native_visual_ready,
        "native_visual_config_ready": native_visual_config_ready,
        "visual_stage_status": visual_stage.get("status"),
        "visual_stage_blockers": visual_stage.get("blockers", []),
        "visual_stage_warnings": visual_stage.get("warnings", []),
        "visual_source_summary": completion.get("visual_source_summary"),
        "managed_media_catalog_ready": checks.get("selected_b1_managed_media_presets_available")
        is True,
        "managed_media_execution_ready": managed_media_execution["ready"],
        "managed_media_execution": managed_media_execution,
        "managed_media_smoke": managed_media_smoke,
        "managed_media_operator_action": _managed_media_operator_action(
            managed_media_execution,
            managed_media_smoke,
        ),
        "managed_media_required_endpoints": visual_details.get(
            "managed_media_required_endpoints",
            [],
        ),
        "managed_media_missing_preset_endpoints": visual_details.get(
            "managed_media_missing_preset_endpoints",
            [],
        ),
        "native_prompt_admission_ready": checks.get(
            "selected_native_comfyui_prompt_admission_ready"
        )
        is True,
    }


def _production_operator_next_action(
    *,
    report_status: str,
    blockers: list[str],
    media_readiness: dict,
    package_inspection: dict | None,
    latest_publish_job,
    asset_download_status: str,
    live_provider_preflight: dict | None = None,
    workflow_run_until_blocked: dict | None = None,
) -> str:
    if report_status == "pass":
        return "inspect_export_package_and_publish_evidence"
    audio_action = str(media_readiness.get("audio_operator_action") or "")
    if audio_action in {
        "fix_voicebox_generation_then_retry_audio_assets",
        "reset_cancelled_audio_assets_for_retry",
        "sync_voicebox_jobs",
    }:
        return audio_action
    preflight_action = _live_provider_preflight_action_entry(live_provider_preflight)
    if preflight_action is not None:
        return str(preflight_action["action"])
    managed_media_action = str(media_readiness.get("managed_media_operator_action") or "")
    if managed_media_action not in {
        "managed_media_execution_ready",
        "no_managed_media_action_required",
        "inspect_managed_media_execution",
    }:
        return managed_media_action
    workflow_action = _workflow_run_until_blocked_operator_action(workflow_run_until_blocked)
    if workflow_action is not None:
        return str(workflow_action["action"])
    if audio_action not in {"audio_generation_ready", "inspect_audio_generation"}:
        return audio_action
    if asset_download_status != "pass":
        return "restore_or_regenerate_missing_delivery_artifacts"
    if package_inspection is None or package_inspection.get("status") != "pass":
        return "inspect_or_regenerate_youtube_export_package"
    if latest_publish_job is None:
        return "run_dry_run_publish_for_real_life_test"
    if latest_publish_job.status != "completed":
        return "wait_for_or_repair_publish_job"
    if blockers:
        return "resolve_completion_readiness_blockers"
    return "resolve_blockers_before_real_life_test"


def _production_operator_next_actions(
    *,
    primary_action: str,
    report_status: str,
    blockers: list[str],
    media_readiness: dict,
    package_inspection: dict | None,
    latest_publish_job,
    asset_download_status: str,
    live_provider_preflight: dict | None = None,
    workflow_run_until_blocked: dict | None = None,
) -> list[dict]:
    preflight_action = _live_provider_preflight_action_entry(live_provider_preflight)
    if report_status == "pass":
        actions = [{"scope": "acceptance", "action": primary_action, "status": "pass"}]
        if preflight_action is not None:
            actions.append(preflight_action)
        actions.extend(_speech_followup_actions(media_readiness))
        actions.extend(_native_visual_followup_actions(media_readiness))
        return actions

    actions: list[dict] = []
    audio_action = str(media_readiness.get("audio_operator_action") or "")
    if audio_action not in {"", "audio_generation_ready", "inspect_audio_generation"}:
        audio = media_readiness.get("audio_generation")
        actions.append(
            {
                "scope": "speech",
                "action": audio_action,
                "status": audio.get("status") if isinstance(audio, dict) else None,
            }
        )
    if preflight_action is not None:
        actions.append(preflight_action)

    managed_action = str(media_readiness.get("managed_media_operator_action") or "")
    if managed_action not in {
        "",
        "managed_media_execution_ready",
        "no_managed_media_action_required",
        "inspect_managed_media_execution",
    }:
        smoke = media_readiness.get("managed_media_smoke")
        execution = media_readiness.get("managed_media_execution")
        status = (
            smoke.get("status")
            if isinstance(smoke, dict) and smoke.get("status") not in {None, "missing"}
            else execution.get("status")
            if isinstance(execution, dict)
            else None
        )
        actions.append({"scope": "managed_media", "action": managed_action, "status": status})

    workflow_action = _workflow_run_until_blocked_operator_action(workflow_run_until_blocked)
    if workflow_action is not None and not _operator_action_exists(
        actions,
        str(workflow_action["action"]),
    ):
        actions.append(workflow_action)

    if asset_download_status != "pass":
        actions.append(
            {
                "scope": "delivery_artifacts",
                "action": "restore_or_regenerate_missing_delivery_artifacts",
                "status": asset_download_status,
            }
        )
    if package_inspection is None or package_inspection.get("status") != "pass":
        actions.append(
            {
                "scope": "export_package",
                "action": "inspect_or_regenerate_youtube_export_package",
                "status": package_inspection.get("status")
                if isinstance(package_inspection, dict)
                else None,
            }
        )
    if latest_publish_job is None:
        actions.append(
            {
                "scope": "publishing",
                "action": "run_dry_run_publish_for_real_life_test",
                "status": None,
            }
        )
    elif latest_publish_job.status != "completed":
        actions.append(
            {
                "scope": "publishing",
                "action": "wait_for_or_repair_publish_job",
                "status": latest_publish_job.status,
            }
        )
    if blockers:
        actions.append(
            {
                "scope": "completion",
                "action": "resolve_completion_readiness_blockers",
                "status": "blocked",
                "failed_check_count": len(blockers),
            }
        )

    actions.extend(_native_visual_followup_actions(media_readiness, include_managed_media=False))
    return actions or [{"scope": "production", "action": primary_action, "status": report_status}]


def _live_provider_preflight_action_entry(evidence: dict | None) -> dict | None:
    if not isinstance(evidence, dict):
        return None
    action = str(evidence.get("action") or "").strip()
    if action in {"", "live_provider_preflight_ready"}:
        return None
    status = str(evidence.get("status") or "unknown")
    return {
        "scope": "live_provider_preflight",
        "action": action,
        "status": status,
        "blocking_sections": [
            str(section) for section in evidence.get("blocking_sections", []) if section
        ][:6],
        "model_failed_count": int(evidence.get("model_failed_count") or 0),
        "voicebox_failed_count": int(evidence.get("voicebox_failed_count") or 0),
    }


def _workflow_run_until_blocked_operator_action(evidence: dict | None) -> dict | None:
    if not isinstance(evidence, dict):
        return None
    handoff = evidence.get("handoff")
    if not isinstance(handoff, dict):
        return None
    action = str(handoff.get("next_handoff_action") or "").strip()
    if not action or action in {
        "inspect_workflow_handoff",
        "complete_workflow_or_inspect_publish_evidence",
    }:
        return None
    status = str(handoff.get("status") or evidence.get("status") or "blocked")
    if status in {"missing", "delivery_ready"}:
        return None
    pending_stages = [str(stage) for stage in evidence.get("pending_approval_stages", []) if stage][
        :4
    ]
    blocking_reasons = [str(reason) for reason in handoff.get("blocking_reasons", []) if reason][:6]
    return {
        "scope": "workflow",
        "action": action,
        "status": status,
        "stop_reason": evidence.get("stop_reason"),
        "pending_approval_stages": pending_stages,
        "blocking_reasons": blocking_reasons,
    }


def _operator_action_exists(actions: list[dict], action: str) -> bool:
    return any(item.get("action") == action for item in actions if isinstance(item, dict))


def _speech_followup_actions(media_readiness: dict) -> list[dict]:
    audio_action = str(media_readiness.get("audio_operator_action") or "")
    if audio_action in {"", "audio_generation_ready", "inspect_audio_generation"}:
        return []
    audio = media_readiness.get("audio_generation")
    status = audio.get("status") if isinstance(audio, dict) else None
    return [{"scope": "speech", "action": audio_action, "status": status}]


def _native_visual_followup_actions(
    media_readiness: dict,
    *,
    include_managed_media: bool = True,
) -> list[dict]:
    if media_readiness.get("native_visual_ready") is True:
        return []
    visual_source = media_readiness.get("visual_source_summary")
    fallback_count = (
        int(visual_source.get("fallback_primary_visual_turn_count") or 0)
        if isinstance(visual_source, dict)
        else 0
    )
    missing_count = (
        int(visual_source.get("missing_primary_visual_turn_count") or 0)
        if isinstance(visual_source, dict)
        else 0
    )
    actions: list[dict] = []
    managed_action = str(media_readiness.get("managed_media_operator_action") or "")
    if include_managed_media and managed_action not in {
        "",
        "managed_media_execution_ready",
        "no_managed_media_action_required",
        "inspect_managed_media_execution",
    }:
        smoke = media_readiness.get("managed_media_smoke")
        execution = media_readiness.get("managed_media_execution")
        status = (
            smoke.get("status")
            if isinstance(smoke, dict) and smoke.get("status") not in {None, "missing"}
            else execution.get("status")
            if isinstance(execution, dict)
            else None
        )
        actions.append({"scope": "managed_media", "action": managed_action, "status": status})
    if fallback_count > 0:
        actions.append(
            {
                "scope": "native_visual",
                "action": "retry_fallback_visuals_as_native_after_b1_fix",
                "status": "fallback_visuals_present",
                "asset_count": fallback_count,
            }
        )
    elif missing_count > 0:
        actions.append(
            {
                "scope": "native_visual",
                "action": "produce_native_visual_assets",
                "status": "native_visuals_missing",
                "asset_count": missing_count,
            }
        )
    return actions


def _production_audio_generation_evidence(
    episode: Episode,
    *,
    voicebox_endpoints: list[VoiceboxEndpoint] | None = None,
    voice_profiles: list[VoiceProfile] | None = None,
) -> dict:
    voicebox_assets = [
        asset
        for asset in episode.assets
        if asset.asset_type == AssetType.audio
        and isinstance(asset.generation_metadata, dict)
        and asset.generation_metadata.get("adapter") == "voicebox"
    ]
    endpoint_by_id = {endpoint.id: endpoint for endpoint in voicebox_endpoints or []}
    voice_profile_by_id = {profile.id: profile for profile in voice_profiles or []}
    required_endpoint_ids = sorted(
        {
            endpoint_id
            for asset in voicebox_assets
            if (
                endpoint_id := _audio_asset_voicebox_endpoint_id(
                    asset,
                    voice_profile_by_id,
                )
            )
        }
    )
    unhealthy_endpoints = [
        endpoint_by_id[endpoint_id]
        for endpoint_id in required_endpoint_ids
        if endpoint_id in endpoint_by_id
        and endpoint_by_id[endpoint_id].health_status == "unhealthy"
    ]
    unknown_health_endpoint_ids = [
        endpoint_id
        for endpoint_id in required_endpoint_ids
        if endpoint_id not in endpoint_by_id
        or endpoint_by_id[endpoint_id].health_status in {"unknown", "unconfigured"}
    ]
    failed_assets = [asset for asset in voicebox_assets if asset.status == "failed"]
    running_assets = [
        asset for asset in voicebox_assets if asset.status in {"submitted", "running"}
    ]
    cancelled_assets = [asset for asset in voicebox_assets if asset.status == "cancelled"]
    retryable_cancelled_assets = [
        asset
        for asset in cancelled_assets
        if asset.generation_metadata.get("ready_for_retry") is True
    ]
    blocked_cancelled_assets = [
        asset
        for asset in cancelled_assets
        if asset.generation_metadata.get("ready_for_retry") is not True
    ]
    completed_assets = [
        asset
        for asset in voicebox_assets
        if asset.status == "completed" and bool(asset.storage_uri)
    ]
    ready = None
    if voicebox_assets:
        ready = (
            len(completed_assets) == len(voicebox_assets)
            and not failed_assets
            and not running_assets
            and not unhealthy_endpoints
            and not unknown_health_endpoint_ids
        )
    return {
        "schema_version": "production_audio_generation_evidence.v1",
        "ready": ready,
        "status": _audio_generation_execution_status(
            completed_count=len(completed_assets),
            failed_count=len(failed_assets),
            running_count=len(running_assets),
            total_count=len(voicebox_assets),
        ),
        "voicebox_asset_count": len(voicebox_assets),
        "completed_artifact_count": len(completed_assets),
        "submitted_or_running_count": len(running_assets),
        "failed_count": len(failed_assets),
        "cancelled_count": len(cancelled_assets),
        "retryable_cancelled_count": len(retryable_cancelled_assets),
        "blocked_cancelled_count": len(blocked_cancelled_assets),
        "required_endpoint_ids": required_endpoint_ids,
        "unhealthy_endpoint_count": len(unhealthy_endpoints),
        "unknown_health_endpoint_count": len(unknown_health_endpoint_ids),
        "provider_ready": (
            not unhealthy_endpoints and not unknown_health_endpoint_ids
            if required_endpoint_ids
            else None
        ),
        "provider_issue_samples": [
            _voicebox_endpoint_issue_sample(endpoint) for endpoint in unhealthy_endpoints[:5]
        ]
        + [
            {
                "endpoint_id": endpoint_id,
                "health_status": (
                    endpoint_by_id[endpoint_id].health_status
                    if endpoint_id in endpoint_by_id
                    else "missing"
                ),
                "action": "inspect_voicebox_endpoint_health",
            }
            for endpoint_id in unknown_health_endpoint_ids[:5]
        ],
        "failure_samples": [_audio_generation_failure_sample(asset) for asset in failed_assets[:5]],
    }


def _audio_asset_voicebox_endpoint_id(
    asset: Asset,
    voice_profile_by_id: dict[str, VoiceProfile],
) -> str | None:
    metadata = asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
    endpoint_id = metadata.get("voicebox_endpoint_id")
    if isinstance(endpoint_id, str) and endpoint_id:
        return endpoint_id
    voice_profile_id = metadata.get("voice_profile_id")
    if not isinstance(voice_profile_id, str) or not voice_profile_id:
        return None
    profile = voice_profile_by_id.get(voice_profile_id)
    return profile.voicebox_endpoint_id if profile else None


def _voicebox_endpoint_issue_sample(endpoint: VoiceboxEndpoint) -> dict:
    canary = endpoint.capabilities.get("generation_canary")
    canary_status = canary.get("status") if isinstance(canary, dict) else None
    status_code = canary.get("status_code") if isinstance(canary, dict) else None
    riff_wave = canary.get("riff_wave") if isinstance(canary, dict) else None
    return {
        "endpoint_id": endpoint.id,
        "health_status": endpoint.health_status,
        "adapter_type": endpoint.adapter_type,
        "canary_status": canary_status,
        "canary_status_code": status_code,
        "canary_riff_wave": riff_wave,
        "action": "fix_voicebox_generation_then_rerun_health_check",
    }


def _audio_generation_execution_status(
    *,
    completed_count: int,
    failed_count: int,
    running_count: int,
    total_count: int,
) -> str:
    if total_count == 0:
        return "not_attempted"
    if failed_count:
        return "fail"
    if running_count:
        return "running"
    if completed_count and completed_count < total_count:
        return "partial"
    if completed_count:
        return "pass"
    return "planned"


def _audio_generation_operator_action(execution: dict) -> str:
    status = str(execution.get("status") or "")
    if execution.get("provider_ready") is False:
        return "fix_voicebox_generation_then_retry_audio_assets"
    if int(execution.get("blocked_cancelled_count") or 0) > 0:
        return "reset_cancelled_audio_assets_for_retry"
    if status == "not_attempted":
        return "plan_or_produce_speech_assets"
    if status == "planned":
        return "produce_speech_assets"
    if status == "running":
        return "sync_voicebox_jobs"
    if status == "partial":
        return "produce_remaining_speech_assets"
    if status == "fail":
        return "fix_voicebox_generation_then_retry_audio_assets"
    if status == "pass":
        return "audio_generation_ready"
    return "inspect_audio_generation"


def _audio_generation_failure_sample(asset: Asset) -> dict:
    metadata = asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
    return {
        "asset_id": str(asset.id),
        "status": asset.status,
        "voicebox_endpoint_id": metadata.get("voicebox_endpoint_id"),
        "voice_profile_id": metadata.get("voice_profile_id"),
        "remote_profile_id": metadata.get("remote_profile_id"),
        "adapter_type": metadata.get("adapter_type"),
        "failure_type": metadata.get("failure_type"),
        "failure": metadata.get("failure"),
    }


def _managed_media_operator_action(execution: dict, smoke: dict | None = None) -> str:
    status = str(execution.get("status") or "")
    smoke_status = str((smoke or {}).get("status") or "")
    if smoke_status in {"runner_failed", "fail", "timeout"}:
        if (smoke or {}).get("fresh") is False:
            return "run_b1_managed_media_smoke"
        return "fix_b1_managed_media_runner_then_rerun_smoke"
    if status == "not_required":
        return "no_managed_media_action_required"
    if status == "not_attempted":
        return "run_b1_managed_media_smoke_or_start_native_visual_production"
    if status == "running":
        return "sync_managed_media_jobs"
    if status == "fail":
        return "fix_b1_managed_media_runner_then_retry_visual_assets"
    if status == "fallback":
        return "retry_managed_media_visual_assets_after_provider_fix"
    if status == "pass":
        return "managed_media_execution_ready"
    return "inspect_managed_media_execution"


def _managed_media_execution_evidence(
    episode: Episode,
    *,
    managed_media_required: bool = False,
) -> dict:
    managed_assets = [asset for asset in episode.assets if _asset_uses_b1_managed_media(asset)]
    completed_artifacts = [
        asset
        for asset in managed_assets
        if asset.status == "completed"
        and bool(asset.storage_uri)
        and asset.generation_metadata.get("fallback_visual") is not True
    ]
    failed_assets = [
        asset
        for asset in managed_assets
        if asset.status == "failed"
        or _managed_media_provider_state(asset) in {"failed", "cancelled", "expired"}
    ]
    running_assets = [
        asset
        for asset in managed_assets
        if asset.status in {"submitted", "running"}
        or _managed_media_provider_state(asset) in {"queued", "running", "submitted"}
    ]
    fallback_assets = [
        asset
        for asset in managed_assets
        if asset.generation_metadata.get("fallback_visual") is True
    ]
    required = managed_media_required or len(managed_assets) > 0
    return {
        "schema_version": "managed_media_execution_evidence.v1",
        "required": required,
        "ready": (
            len(completed_artifacts) > 0
            and not failed_assets
            and not running_assets
            and not fallback_assets
        )
        if required
        else None,
        "status": _managed_media_execution_status(
            required=required,
            completed_artifact_count=len(completed_artifacts),
            failed_count=len(failed_assets),
            running_count=len(running_assets),
            fallback_count=len(fallback_assets),
        ),
        "managed_asset_count": len(managed_assets),
        "completed_artifact_count": len(completed_artifacts),
        "submitted_or_running_count": len(running_assets),
        "failed_count": len(failed_assets),
        "fallback_visual_count": len(fallback_assets),
        "models": sorted(
            {model for asset in managed_assets if (model := _managed_media_asset_model(asset))}
        ),
        "operations": sorted(
            {
                operation
                for asset in managed_assets
                if (operation := _managed_media_asset_operation(asset))
            }
        ),
        "failure_samples": [_managed_media_failure_sample(asset) for asset in failed_assets[:5]],
    }


def _asset_uses_b1_managed_media(asset: Asset) -> bool:
    metadata = asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
    return (
        metadata.get("adapter") == "b1_managed_media"
        or "managed_media_payload" in metadata
        or "managed_media_api_base" in metadata
    )


def _managed_media_execution_status(
    *,
    required: bool,
    completed_artifact_count: int,
    failed_count: int,
    running_count: int,
    fallback_count: int,
) -> str:
    if not required:
        return "not_required"
    if (
        completed_artifact_count == 0
        and failed_count == 0
        and running_count == 0
        and fallback_count == 0
    ):
        return "not_attempted"
    if failed_count:
        return "fail"
    if fallback_count:
        return "fallback"
    if running_count:
        return "running"
    if completed_artifact_count:
        return "pass"
    return "missing_artifact"


def _managed_media_provider_payload(asset: Asset) -> dict:
    metadata = asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
    payload = metadata.get("provider_response")
    return payload if isinstance(payload, dict) else {}


def _managed_media_provider_state(asset: Asset) -> str | None:
    payload = _managed_media_provider_payload(asset)
    value = payload.get("state") or payload.get("status") or payload.get("b1_status")
    return str(value) if isinstance(value, str) and value else None


def _managed_media_asset_model(asset: Asset) -> str | None:
    payload = _managed_media_provider_payload(asset)
    metadata = asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
    request_payload = metadata.get("managed_media_payload")
    if not isinstance(request_payload, dict):
        request_payload = {}
    for value in (
        payload.get("model_alias"),
        payload.get("model"),
        request_payload.get("model"),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _managed_media_asset_operation(asset: Asset) -> str | None:
    payload = _managed_media_provider_payload(asset)
    metadata = asset.generation_metadata if isinstance(asset.generation_metadata, dict) else {}
    request_payload = metadata.get("managed_media_payload")
    if not isinstance(request_payload, dict):
        request_payload = {}
    for value in (
        payload.get("operation"),
        request_payload.get("operation"),
    ):
        if isinstance(value, str) and value:
            return value
    return None


def _managed_media_failure_sample(asset: Asset) -> dict:
    payload = _managed_media_provider_payload(asset)
    return {
        "asset_id": str(asset.id),
        "asset_type": asset.asset_type.value,
        "status": asset.status,
        "remote_job_id": asset.generation_metadata.get("remote_job_id"),
        "model": _managed_media_asset_model(asset),
        "operation": _managed_media_asset_operation(asset),
        "provider_state": _managed_media_provider_state(asset),
        "stage": payload.get("stage"),
        "failure_category": payload.get("failure_category"),
        "failure_message": payload.get("failure_message"),
    }


def _native_visual_test_ready(completion: dict, native_visual_mode: object) -> bool:
    native_mode = native_visual_mode if isinstance(native_visual_mode, dict) else {}
    visual_source = completion.get("visual_source_summary")
    native_visual_complete = (
        isinstance(visual_source, dict) and visual_source.get("native_visual_complete") is True
    )
    return native_mode.get("status") == "pass" and native_visual_complete


def _production_acceptance_summary(
    *,
    episode: Episode,
    completion: dict,
    report_status: str,
    production_target: object,
    production_target_satisfied: bool,
    deliverables: dict,
    package_inspection: dict | None,
    asset_download_status: str,
    blockers: list[str],
    publish_evidence_binding: dict | None = None,
    workflow_run_until_blocked: dict | None = None,
) -> dict:
    package = package_inspection if isinstance(package_inspection, dict) else {}
    return {
        "schema_version": "production_acceptance_summary.v1",
        "status": (
            "pass"
            if episode.status == EpisodeStatus.completed
            and completion.get("status") == "pass"
            and report_status == "pass"
            and package.get("status") == "pass"
            and asset_download_status == "pass"
            else "fail"
        ),
        "episode_id": str(episode.id),
        "episode_status": episode.status,
        "production_target": production_target,
        "production_target_satisfied": production_target_satisfied,
        "completion_status": completion.get("status"),
        "production_test_status": report_status,
        "package_inspection_status": package.get("status"),
        "artifact_download_status": asset_download_status,
        "blockers": blockers,
        "failed_checks": completion.get("failed_checks", []),
        "deliverables": {
            name: _compact_deliverable_summary(deliverables.get(name))
            for name in ("final_render", "export_package", "production_manifest")
        },
        "package": {
            "package_asset_id": package.get("package_asset_id"),
            "file_count": package.get("file_count"),
            "manifest_schema_version": package.get("manifest_schema_version"),
            "chapter_count": package.get("chapter_count"),
            "subtitle_count": package.get("subtitle_count"),
            "evidence_source_count": package.get("evidence_source_count"),
            "manifest_matches_asset_metadata": package.get("manifest_matches_asset_metadata"),
            "issues": package.get("issues", []),
        },
        "publish_evidence": _compact_publish_binding_summary(publish_evidence_binding),
        "workflow_run_until_blocked": workflow_run_until_blocked
        if isinstance(workflow_run_until_blocked, dict)
        else {
            "schema_version": "production_workflow_run_until_blocked_summary.v1",
            "status": "missing",
        },
    }


def _compact_publish_binding_summary(value: object) -> dict:
    if not isinstance(value, dict):
        return {"status": "not_checked"}
    return {
        "status": value.get("status"),
        "publish_job_id": value.get("publish_job_id"),
        "job_status": value.get("job_status"),
        "dry_run": value.get("dry_run"),
        "package_asset_matches": value.get("package_asset_matches"),
        "payload_package_matches": value.get("payload_package_matches"),
        "current_manifest_embeds_publish_job": value.get("current_manifest_embeds_publish_job"),
        "current_manifest_publish_job_status_matches": value.get(
            "current_manifest_publish_job_status_matches"
        ),
        "payload_manifest_is_current": value.get("payload_manifest_is_current"),
        "payload_production_manifest_schema_version": value.get(
            "payload_production_manifest_schema_version"
        ),
    }


def _compact_deliverable_summary(value: object) -> dict:
    if not isinstance(value, dict):
        return {"status": "missing"}
    return {
        "asset_id": value.get("asset_id"),
        "status": value.get("status"),
        "checksum": value.get("checksum"),
        "mime_type": value.get("mime_type"),
        "downloadable": value.get("downloadable"),
        "file_size_bytes": value.get("file_size_bytes"),
        "download_missing_reason": value.get("download_missing_reason"),
    }


def _asset_evidence(
    episode: Episode,
    asset_id: object,
    expected_type: AssetType,
    object_store,
) -> dict | None:
    asset = _asset_by_id(episode, asset_id)
    if asset is None:
        asset = _latest_asset(episode, expected_type)
    if asset is None:
        return None
    storage_path = object_store.path_for_uri(asset.storage_uri) if asset.storage_uri else None
    downloadable = bool(storage_path and storage_path.exists() and storage_path.is_file())
    file_size_bytes = storage_path.stat().st_size if downloadable and storage_path else None
    missing_reason = None
    if not asset.storage_uri:
        missing_reason = "missing_storage_uri"
    elif storage_path is None:
        missing_reason = "unsupported_storage_uri"
    elif not storage_path.exists():
        missing_reason = "stored_object_not_found"
    elif not storage_path.is_file():
        missing_reason = "stored_object_not_file"
    return {
        "asset_id": str(asset.id),
        "asset_type": asset.asset_type,
        "status": asset.status,
        "storage_uri": asset.storage_uri,
        "mime_type": asset.mime_type,
        "checksum": asset.checksum,
        "duration_ms": asset.duration_ms,
        "width": asset.width,
        "height": asset.height,
        "created_at": asset.created_at,
        "updated_at": asset.updated_at,
        "source_entity_type": asset.source_entity_type,
        "source_entity_id": asset.source_entity_id,
        "downloadable": downloadable,
        "file_size_bytes": file_size_bytes,
        "download_missing_reason": missing_reason,
        "download_url": (
            f"/api/v1/episodes/{episode.id}/assets/{asset.id}/download" if downloadable else None
        ),
    }


def _download_filename(asset: Asset, fallback_name: str) -> str:
    suffix = ""
    if "." in fallback_name:
        suffix = "." + fallback_name.rsplit(".", 1)[-1]
    return f"{asset.asset_type}-{str(asset.id)[:8]}{suffix}"


def _visual_reference_download_filename(
    profile_id: str,
    reference_type: VisualReferenceImageType,
    fallback_name: str,
) -> str:
    suffix = ""
    if "." in fallback_name:
        suffix = "." + fallback_name.rsplit(".", 1)[-1]
    safe_profile_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in profile_id
    ).strip("-")
    return f"{safe_profile_id or 'visual-profile'}-{reference_type}{suffix}"


def _show_scene_reference_download_filename(fallback_name: str) -> str:
    suffix = ""
    if "." in fallback_name:
        suffix = "." + fallback_name.rsplit(".", 1)[-1]
    return f"show-scene-reference{suffix}"


def _youtube_package_inspection(episode: Episode, package_asset: Asset, object_store) -> dict:
    evidence = _asset_evidence(
        episode,
        str(package_asset.id),
        AssetType.export_package,
        object_store,
    )
    path = object_store.path_for_uri(package_asset.storage_uri or "")
    base = {
        "schema_version": "youtube_package_inspection.v1",
        "episode_id": str(episode.id),
        "package_asset_id": str(package_asset.id),
        "package": evidence,
        "status": "fail",
        "issues": [],
    }
    if path is None:
        base["issues"] = ["package_storage_uri_not_downloadable"]
        return base
    if not path.exists() or not path.is_file():
        base["issues"] = ["package_file_missing"]
        return base
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            manifest_payload = archive.read("youtube-package.json")
    except KeyError:
        return {
            **base,
            "file_count": 0,
            "included_files": [],
            "issues": ["youtube_package_manifest_missing"],
        }
    except (zipfile.BadZipFile, OSError):
        return {**base, "issues": ["youtube_package_zip_unreadable"]}
    try:
        manifest = json.loads(manifest_payload)
    except json.JSONDecodeError:
        return {
            **base,
            "file_count": len(names),
            "included_files": names,
            "issues": ["youtube_package_manifest_unreadable"],
        }
    issues = _youtube_package_inspection_issues(names, manifest, package_asset)
    embedded_manifest = package_asset.generation_metadata.get("youtube_package_manifest")
    return {
        **base,
        "status": "pass" if not issues else "warning",
        "issues": issues,
        "file_count": len(names),
        "included_files": names,
        "manifest_schema_version": manifest.get("schema_version"),
        "manifest_id": manifest.get("id"),
        "manifest_title": manifest.get("title"),
        "language": manifest.get("language"),
        "render_asset_id": manifest.get("render_asset_id"),
        "thumbnail_asset_id": manifest.get("thumbnail_asset_id"),
        "chapter_count": len(manifest.get("chapters") or []),
        "subtitle_count": len(manifest.get("subtitles") or []),
        "evidence_source_count": len(
            (manifest.get("evidence_lineage") or {}).get("referenced_sources") or []
        ),
        "manifest_matches_asset_metadata": embedded_manifest == manifest,
    }


def _youtube_package_inspection_issues(
    included_files: list[str],
    manifest: dict,
    package_asset: Asset,
) -> list[str]:
    issues = []
    if manifest.get("schema_version") != "youtube_package.v1":
        issues.append("youtube_package_manifest_schema_invalid")
    if "youtube-package.json" not in included_files:
        issues.append("youtube_package_manifest_missing")
    video_files = [name for name in included_files if name.startswith("video/render.")]
    if not video_files:
        issues.append("youtube_package_video_missing")
    embedded_files = package_asset.generation_metadata.get("included_files")
    if isinstance(embedded_files, list) and sorted(map(str, embedded_files)) != sorted(
        included_files
    ):
        issues.append("youtube_package_file_list_mismatch")
    if manifest.get("render_asset_id") != package_asset.source_entity_id:
        issues.append("youtube_package_render_asset_mismatch")
    if manifest.get("thumbnail_asset_id") and "thumbnail/thumbnail.jpg" not in included_files:
        issues.append("youtube_package_declared_thumbnail_missing_file")
    subtitle_entries = manifest.get("subtitles")
    if isinstance(subtitle_entries, list):
        declared_paths = [
            str(entry.get("path"))
            for entry in subtitle_entries
            if isinstance(entry, dict) and entry.get("path")
        ]
        missing_subtitles = [path for path in declared_paths if path not in included_files]
        if missing_subtitles:
            issues.append("youtube_package_declared_subtitles_missing_files")
    return issues


def _asset_by_id(episode: Episode, asset_id: object) -> Asset | None:
    if not asset_id:
        return None
    asset_id_text = str(asset_id)
    return next((asset for asset in episode.assets if str(asset.id) == asset_id_text), None)


def _latest_asset(episode: Episode, asset_type: AssetType) -> Asset | None:
    assets = [
        asset
        for asset in episode.assets
        if asset.asset_type == asset_type and asset.status != "replaced"
    ]
    if not assets:
        return None
    return max(assets, key=lambda asset: (asset.created_at, asset.updated_at, str(asset.id)))


def _latest_publish_job(episode: Episode) -> PublishJob | None:
    jobs = [job for job in episode.publish_jobs if job.status != "replaced"]
    if not jobs:
        return None
    return max(jobs, key=lambda job: (job.requested_at, job.updated_at, str(job.id)))


def _publish_evidence_binding(
    episode: Episode,
    job: PublishJob | None,
    package_evidence: dict | None,
    production_manifest_evidence: dict | None,
) -> dict:
    if job is None:
        return {
            "schema_version": "publish_evidence_binding.v1",
            "status": "missing",
            "publish_job_id": None,
        }
    payload = job.delivery_payload if isinstance(job.delivery_payload, dict) else {}
    package_asset_id = (
        str(package_evidence.get("asset_id"))
        if isinstance(package_evidence, dict) and package_evidence.get("asset_id")
        else None
    )
    package_checksum = (
        package_evidence.get("checksum") if isinstance(package_evidence, dict) else None
    )
    payload_package_asset_id = (
        str(payload.get("package_asset_id")) if payload.get("package_asset_id") else None
    )
    payload_package_checksum = payload.get("package_checksum")
    current_manifest_asset_id = (
        str(production_manifest_evidence.get("asset_id"))
        if isinstance(production_manifest_evidence, dict)
        and production_manifest_evidence.get("asset_id")
        else None
    )
    payload_manifest_asset_id = (
        str(payload.get("production_manifest_asset_id"))
        if payload.get("production_manifest_asset_id")
        else None
    )
    current_manifest = _asset_by_id(episode, current_manifest_asset_id)
    payload_manifest = _asset_by_id(episode, payload_manifest_asset_id)
    embedded_publish_job = _embedded_publish_job(current_manifest, job)
    package_asset_matches = package_asset_id is not None and package_asset_id == str(
        job.package_asset_id
    )
    payload_package_matches = (
        package_asset_matches
        and payload_package_asset_id == package_asset_id
        and payload_package_checksum == package_checksum
    )
    current_manifest_embeds_publish_job = embedded_publish_job is not None
    current_manifest_publish_job_status_matches = (
        current_manifest_embeds_publish_job
        and embedded_publish_job.get("status") == job.status
        and str(embedded_publish_job.get("package_asset_id")) == str(job.package_asset_id)
    )
    payload_manifest_is_current = (
        bool(payload_manifest_asset_id)
        and bool(current_manifest_asset_id)
        and payload_manifest_asset_id == current_manifest_asset_id
    )
    status = "pass"
    if (
        job.status != "completed"
        or not payload_package_matches
        or not current_manifest_publish_job_status_matches
    ):
        status = "fail"
    elif job.dry_run or not payload_manifest_is_current:
        status = "warning"
    return {
        "schema_version": "publish_evidence_binding.v1",
        "status": status,
        "publish_job_id": str(job.id),
        "job_status": job.status,
        "dry_run": job.dry_run,
        "publisher_target_id": job.publisher_target_id,
        "platform": job.platform,
        "publish_url": job.publish_url,
        "package_asset_id": package_asset_id,
        "payload_package_asset_id": payload_package_asset_id,
        "package_asset_matches": package_asset_matches,
        "payload_package_matches": payload_package_matches,
        "current_production_manifest_asset_id": current_manifest_asset_id,
        "payload_production_manifest_asset_id": payload_manifest_asset_id,
        "payload_manifest_exists": payload_manifest is not None,
        "payload_manifest_is_current": payload_manifest_is_current,
        "payload_production_manifest_schema_version": payload.get(
            "production_manifest_schema_version"
        ),
        "current_manifest_embeds_publish_job": current_manifest_embeds_publish_job,
        "current_manifest_publish_job_status_matches": (
            current_manifest_publish_job_status_matches
        ),
    }


def _embedded_publish_job(manifest_asset: Asset | None, job: PublishJob) -> dict | None:
    if manifest_asset is None:
        return None
    manifest = manifest_asset.generation_metadata.get("production_manifest")
    if not isinstance(manifest, dict):
        return None
    publish_jobs = manifest.get("publish_jobs")
    if not isinstance(publish_jobs, list):
        return None
    return next(
        (
            item
            for item in publish_jobs
            if isinstance(item, dict) and str(item.get("id")) == str(job.id)
        ),
        None,
    )


def _publish_job_evidence(job: PublishJob | None) -> dict | None:
    if job is None:
        return None
    return {
        "publish_job_id": str(job.id),
        "status": job.status,
        "dry_run": job.dry_run,
        "publisher_target_id": job.publisher_target_id,
        "platform": job.platform,
        "package_asset_id": str(job.package_asset_id),
        "publish_url": job.publish_url,
        "requested_at": job.requested_at,
        "completed_at": job.completed_at,
        "updated_at": job.updated_at,
    }


def _latest_approval(episode: Episode, stage: str) -> Approval | None:
    approvals = [approval for approval in episode.approvals if approval.stage == stage]
    if not approvals:
        return None
    return max(approvals, key=lambda approval: (approval.created_at, str(approval.id)))


def _approval_evidence(approval: Approval | None) -> dict | None:
    if approval is None:
        return None
    return {
        "approval_id": str(approval.id),
        "stage": approval.stage,
        "target_type": approval.target_type,
        "target_id": approval.target_id,
        "decision": approval.decision,
        "created_at": approval.created_at,
    }


def _asset_counts(episode: Episode) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for asset in episode.assets:
        by_status = counts.setdefault(str(asset.asset_type), {})
        by_status[asset.status] = by_status.get(asset.status, 0) + 1
    return counts


@router.get("/episodes/{episode_id}/discussion")
async def episode_discussion(
    episode_id: UUID,
    repo: RepositoryDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return {"discussion_session": episode.discussion_session}


@router.get("/episodes/{episode_id}/transcripts")
async def episode_transcripts(
    episode_id: UUID,
    repo: RepositoryDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return {"transcripts": episode.transcripts}


@router.post("/episodes/{episode_id}/localize", response_model=Episode)
async def localize_episode(
    episode_id: UUID,
    request: LocalizationRequest,
    repo: RepositoryDep,
    localization: LocalizationServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = localization.create_language_variants(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/assets", response_model=list[Asset])
async def episode_assets(
    episode_id: UUID,
    repo: RepositoryDep,
) -> list[Asset]:
    try:
        return repo.list_assets(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc


@router.get("/episodes/{episode_id}/media-index", response_model=list[EpisodeMediaAsset])
async def episode_media_index(
    episode_id: UUID,
    repo: RepositoryDep,
) -> list[EpisodeMediaAsset]:
    """Return completed primary character-performance clips for the episode."""
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc

    canonical_transcript_id = (
        str(episode.canonical_transcript_version_id)
        if episode.canonical_transcript_version_id is not None
        else None
    )
    media_assets: list[EpisodeMediaAsset] = []
    for asset in episode.assets:
        metadata = asset.generation_metadata
        if (
            asset.asset_type is not AssetType.video
            or asset.status != "completed"
            or asset.source_entity_type != "transcript_turn"
            or metadata.get("visual_role") != "video_primary"
            or (
                canonical_transcript_id is not None
                and metadata.get("transcript_version_id") != canonical_transcript_id
            )
        ):
            continue
        media_assets.append(
            EpisodeMediaAsset(
                id=asset.id,
                asset_type=asset.asset_type,
                source_entity_type=asset.source_entity_type,
                source_entity_id=asset.source_entity_id,
                storage_uri=asset.storage_uri,
                mime_type=asset.mime_type,
                duration_ms=asset.duration_ms,
                width=asset.width,
                height=asset.height,
                fps=asset.fps,
                status=asset.status,
                character_name=_compact_asset_metadata_string(metadata.get("character_name")),
                visual_role=_compact_asset_metadata_string(metadata.get("visual_role")),
                performance_applied=metadata.get("provider_performance_applied") is True,
            )
        )
    return media_assets


def _compact_asset_metadata_string(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


@router.get("/episodes/{episode_id}/assets/{asset_id}/download")
async def download_episode_asset(
    episode_id: UUID,
    asset_id: UUID,
    repo: RepositoryDep,
    settings: SettingsDep,
) -> FileResponse:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    asset = next((item for item in episode.assets if item.id == asset_id), None)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    if asset.status == "replaced":
        raise HTTPException(status_code=410, detail="asset has been replaced")
    if not asset.storage_uri:
        raise HTTPException(status_code=422, detail="asset has no stored object")
    object_store = create_object_store(settings)
    path = object_store.path_for_uri(asset.storage_uri)
    if path is None:
        raise HTTPException(status_code=422, detail="asset storage URI is not downloadable")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="stored object not found")
    return FileResponse(
        path,
        media_type=asset.mime_type or "application/octet-stream",
        filename=_download_filename(asset, path.name),
        content_disposition_type=(
            "inline"
            if (asset.mime_type or "").lower().startswith(("video/", "audio/"))
            else "attachment"
        ),
    )


@router.post("/episodes/{episode_id}/opening/media", response_model=Asset)
async def upload_episode_opening_media(
    episode_id: UUID,
    request: OpeningMediaUploadRequest,
    repo: RepositoryDep,
    settings: SettingsDep,
) -> Asset:
    """Store producer-supplied opening material as a private, renderable episode asset."""
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc

    content_type = request.content_type.split(";", 1)[0].strip().lower()
    if content_type not in {
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/webm",
    }:
        raise HTTPException(
            status_code=422,
            detail="opening media must be a JPEG, PNG, WebP, MP4, or WebM file",
        )
    try:
        payload = base64.b64decode(request.media_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="opening media must be valid base64") from exc
    if not payload:
        raise HTTPException(status_code=422, detail="opening media must not be empty")
    if len(payload) > 250 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="opening media exceeds the 250 MB limit")

    checksum = hashlib.sha256(payload).hexdigest()
    existing_asset = next(
        (
            asset
            for asset in episode.assets
            if asset.asset_type
            == (AssetType.video if content_type.startswith("video/") else AssetType.image)
            and asset.status == "completed"
            and asset.source_entity_type == "episode_opening"
            and asset.generation_metadata.get("opening_media") is True
            and asset.checksum == f"sha256:{checksum}"
        ),
        None,
    )
    if existing_asset is not None:
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="episode.opening_media.deduplicated",
                actor=request.user_id or "system",
                details={
                    "asset_id": str(existing_asset.id),
                    "content_type": content_type,
                    "size_bytes": len(payload),
                    "checksum": existing_asset.checksum,
                    "source_url": request.source_url.strip() if request.source_url else None,
                },
            )
        )
        repo.save(episode)
        return existing_asset
    extension = mimetypes.guess_extension(content_type) or ".bin"
    object_store = create_object_store(settings)
    stored = object_store.put_bytes(
        key=f"episodes/{episode.id}/opening-media/{checksum[:16]}{extension}",
        payload=payload,
        content_type=content_type,
    )
    asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video if content_type.startswith("video/") else AssetType.image,
        language=episode.source_language,
        source_entity_type="episode_opening",
        source_entity_id=str(uuid4()),
        storage_uri=stored.uri,
        mime_type=stored.content_type,
        checksum=stored.checksum,
        generation_metadata={
            "opening_media": True,
            "opening_media_role": "topic_visual",
            "title": request.title.strip() or request.filename,
            "source_url": request.source_url.strip() if request.source_url else None,
            "original_filename": request.filename,
            "primer_visual_suitability": {
                "schema_version": "dialecticore.primer_visual_suitability.v1",
                "status": "not_assessed",
                "people_visible": None,
            },
            "object_storage_path": str(stored.path),
            "storage_backend": stored.backend,
            "render_ready": True,
        },
        status="completed",
    )
    episode.assets.append(asset)
    episode.audit_events.append(
        AuditEvent(
            episode_id=episode.id,
            event_type="episode.opening_media.uploaded",
            actor=request.user_id or "system",
            details={
                "asset_id": str(asset.id),
                "asset_type": asset.asset_type.value,
                "content_type": content_type,
                "size_bytes": stored.size_bytes,
                "checksum": stored.checksum,
                "source_url": asset.generation_metadata["source_url"],
            },
        )
    )
    return repo.save(episode).assets[-1]


@router.post("/episodes/{episode_id}/assets/{asset_id}/replace", response_model=Episode)
async def replace_episode_asset(
    episode_id: UUID,
    asset_id: UUID,
    request: AssetReplacementRequest,
    repo: RepositoryDep,
    asset_replacement: AssetReplacementServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = asset_replacement.replace_asset(episode, asset_id, request)
        return repo.save(updated)
    except KeyError as exc:
        detail = str(exc) if str(exc) else "episode or asset not found"
        raise HTTPException(status_code=404, detail=detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/render-presets", response_model=list[RenderPreset])
async def list_render_presets() -> list[RenderPreset]:
    return default_render_presets()


@router.post("/show-media/scene-reference-image", response_model=SceneReferenceImageUploadResponse)
async def upload_scene_reference_image(
    request: SceneReferenceImageUploadRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> SceneReferenceImageUploadResponse:
    try:
        stored = comfyui.store_scene_reference_image(
            filename=request.filename,
            content_type=request.content_type,
            image_base64=request.image_base64,
        )
        repo.record_global_audit_event(
            AuditEvent(
                event_type="show_media.scene_reference_image_uploaded",
                actor=request.user_id or "system",
                details={
                    "content_type": stored["content_type"],
                    "size_bytes": stored["size_bytes"],
                    "checksum": stored["checksum"],
                    "storage_backend": comfyui.object_store.__class__.__name__,
                    "scene_reference_image_uri_present": True,
                },
            )
        )
        return SceneReferenceImageUploadResponse.model_validate(stored)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/show-media/scene-reference-image/download")
async def download_scene_reference_image(
    uri: Annotated[str, Query(min_length=1)],
    settings: SettingsDep,
) -> FileResponse:
    parsed = urlparse(uri)
    key = unquote(parsed.path.lstrip("/"))
    if parsed.scheme not in {"object", "s3"} or not key.startswith(
        "show-media/scene-reference-images/"
    ):
        raise HTTPException(
            status_code=422,
            detail="scene reference image URI is outside show media storage",
        )

    object_store = create_object_store(settings)
    path = object_store.path_for_uri(uri)
    if path is None:
        raise HTTPException(
            status_code=422,
            detail="scene reference image storage URI is not downloadable",
        )
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="stored scene reference image not found")

    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=_show_scene_reference_download_filename(path.name),
    )


@router.get("/publisher-targets", response_model=list[PublisherTarget])
async def list_publisher_targets(repo: RepositoryDep) -> list[PublisherTarget]:
    return repo.list_publisher_targets()


@router.post("/publisher-targets", response_model=PublisherTarget)
async def upsert_publisher_target(
    request: PublisherTargetCreateRequest,
    repo: RepositoryDep,
) -> PublisherTarget:
    return repo.upsert_publisher_target(request.to_target())


@router.get("/publisher-targets/{target_id}", response_model=PublisherTarget)
async def get_publisher_target(target_id: str, repo: RepositoryDep) -> PublisherTarget:
    try:
        return repo.get_publisher_target(target_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="publisher target not found") from exc


@router.put("/publisher-targets/{target_id}", response_model=PublisherTarget)
async def update_publisher_target(
    target_id: str,
    request: PublisherTargetCreateRequest,
    repo: RepositoryDep,
) -> PublisherTarget:
    if target_id != request.id:
        raise HTTPException(status_code=422, detail="target id cannot be changed")
    return repo.upsert_publisher_target(request.to_target())


@router.post("/publisher-targets/{target_id}/health", response_model=PublisherTarget)
async def check_publisher_target_health(
    target_id: str,
    repo: RepositoryDep,
    publisher_service: PublisherServiceDep,
) -> PublisherTarget:
    try:
        target = repo.get_publisher_target(target_id)
        checked = publisher_service.check_target_health(target)
        return repo.upsert_publisher_target(checked)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="publisher target not found") from exc


@router.delete("/publisher-targets/{target_id}", status_code=204)
async def delete_publisher_target(target_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_publisher_target(target_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="publisher target not found") from exc


@router.post("/episodes/{episode_id}/audio-assets/plan", response_model=Episode)
async def plan_episode_audio_assets(
    episode_id: UUID,
    request: AudioAssetPlanRequest,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = voicebox.plan_audio_assets(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/audio-assets/generate", response_model=Episode)
async def generate_episode_audio_assets(
    episode_id: UUID,
    request: AudioGenerationRequest,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = await voicebox.generate_audio_assets(
            episode,
            request,
            voicebox_endpoints=repo.list_voicebox_endpoints(),
            voice_profiles=repo.list_voice_profiles(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/speech/produce", response_model=Episode)
async def produce_episode_speech(
    episode_id: UUID,
    request: AudioGenerationRequest,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        try:
            episode = voicebox.plan_audio_assets(
                episode,
                AudioAssetPlanRequest(
                    transcript_version_id=request.transcript_version_id,
                    language=request.language,
                    user_id=request.user_id,
                    regenerate=request.regenerate,
                ),
            )
        except ValueError as exc:
            if "audio assets already planned for target transcript" not in str(exc):
                raise
        updated = await voicebox.generate_audio_assets(
            episode,
            request,
            voicebox_endpoints=repo.list_voicebox_endpoints(),
            voice_profiles=repo.list_voice_profiles(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/audio-assets/qc", response_model=Episode)
async def check_episode_audio_assets(
    episode_id: UUID,
    request: AudioQualityRequest,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = voicebox.run_audio_quality(
            episode,
            request,
            voicebox_endpoints=repo.list_voicebox_endpoints(),
            voice_profiles=repo.list_voice_profiles(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/audio-assets/sync", response_model=Episode)
async def sync_episode_audio_assets(
    episode_id: UUID,
    request: AudioResultSyncRequest,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = await voicebox.sync_audio_results(
            episode,
            request,
            voicebox_endpoints=repo.list_voicebox_endpoints(),
            voice_profiles=repo.list_voice_profiles(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/audio-assets/cancel", response_model=Episode)
async def cancel_episode_audio_jobs(
    episode_id: UUID,
    request: AudioCancellationRequest,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = await voicebox.cancel_audio_jobs(
            episode,
            request,
            voicebox_endpoints=repo.list_voicebox_endpoints(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/visual-assets/plan", response_model=Episode)
async def plan_episode_visual_assets(
    episode_id: UUID,
    request: VisualAssetPlanRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = comfyui.plan_visual_assets(
            episode,
            request,
            visual_profiles=repo.list_visual_profiles(),
            workflows=repo.list_comfyui_workflows(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/visual-assets/generate", response_model=Episode)
async def generate_episode_visual_assets(
    episode_id: UUID,
    request: VisualGenerationRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = await comfyui.generate_visual_assets(
            episode,
            request,
            endpoints=repo.list_comfyui_endpoints(),
            workflows=repo.list_comfyui_workflows(),
            visual_profiles=repo.list_visual_profiles(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/visual-assets/{asset_id}/seated-character-review",
    response_model=Episode,
)
async def review_episode_seated_character(
    episode_id: UUID,
    asset_id: UUID,
    request: SeatedCharacterReviewRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    """Proxy mandatory seated-plate approval to B1 before recording it locally."""
    try:
        episode = repo.get(episode_id)
        updated = await comfyui.review_seated_character(
            episode,
            str(asset_id),
            request,
            endpoints=repo.list_comfyui_endpoints(),
            workflows=repo.list_comfyui_workflows(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/episodes/{episode_id}/visual-assets/{asset_id}/studio-panel-review",
    response_model=Episode,
)
async def review_episode_studio_panel_keyframe(
    episode_id: UUID,
    asset_id: UUID,
    request: StudioPanelReviewRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    """Require an explicit human decision before the panel master feeds turn clips."""
    try:
        episode = repo.get(episode_id)
        updated = comfyui.review_studio_panel_keyframe(
            episode,
            str(asset_id),
            request,
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/visual-assets/repair-directed", response_model=Episode)
async def repair_episode_directed_visual_assets(
    episode_id: UUID,
    request: VisualGenerationRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    """Queue only missing/failed studio-directed coverage, preserving speaking clips."""
    try:
        episode = repo.get(episode_id)
        updated = await comfyui.repair_directed_visual_assets(
            episode,
            request,
            endpoints=repo.list_comfyui_endpoints(),
            workflows=repo.list_comfyui_workflows(),
            visual_profiles=repo.list_visual_profiles(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/visuals/produce", response_model=Episode)
async def produce_episode_visuals(
    episode_id: UUID,
    request: VisualGenerationRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        workflows = repo.list_comfyui_workflows()
        updated = comfyui.plan_visual_assets(
            episode,
            VisualAssetPlanRequest(
                transcript_version_id=request.transcript_version_id,
                language=request.language,
                user_id=request.user_id,
                regenerate=request.regenerate,
            ),
            visual_profiles=repo.list_visual_profiles(),
            workflows=workflows,
        )
        updated = await comfyui.generate_visual_assets(
            updated,
            request,
            endpoints=repo.list_comfyui_endpoints(),
            workflows=workflows,
            visual_profiles=repo.list_visual_profiles(),
        )
        updated = comfyui.run_visual_quality(
            updated,
            VisualQualityRequest(
                transcript_version_id=request.transcript_version_id,
                language=request.language,
                asset_ids=request.asset_ids,
                transcript_turn_ids=request.transcript_turn_ids,
                participant_ids=request.participant_ids,
                failed_only=request.failed_only,
                user_id=request.user_id,
            ),
            endpoints=repo.list_comfyui_endpoints(),
            workflows=workflows,
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/visual-assets/sync", response_model=Episode)
async def sync_episode_visual_assets(
    episode_id: UUID,
    request: VisualResultSyncRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = await comfyui.sync_visual_results(
            episode,
            request,
            endpoints=repo.list_comfyui_endpoints(),
            workflows=repo.list_comfyui_workflows(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/visual-assets/cancel", response_model=Episode)
async def cancel_episode_visual_jobs(
    episode_id: UUID,
    request: VisualCancellationRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = await comfyui.cancel_visual_jobs(
            episode,
            request,
            endpoints=repo.list_comfyui_endpoints(),
            workflows=repo.list_comfyui_workflows(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="comfyui cancellation failed") from None


@router.post("/episodes/{episode_id}/visual-assets/qc", response_model=Episode)
async def check_episode_visual_assets(
    episode_id: UUID,
    request: VisualQualityRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = comfyui.run_visual_quality(
            episode,
            request,
            endpoints=repo.list_comfyui_endpoints(),
            workflows=repo.list_comfyui_workflows(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/subtitles/generate", response_model=Episode)
async def generate_episode_subtitles(
    episode_id: UUID,
    request: SubtitleGenerationRequest,
    repo: RepositoryDep,
    subtitle_service: SubtitleServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = subtitle_service.generate_subtitles(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/timeline/build", response_model=Episode)
async def build_episode_timeline(
    episode_id: UUID,
    request: TimelineBuildRequest,
    repo: RepositoryDep,
    timeline_service: TimelineServiceDep,
    branding_service: BrandingServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        project = repo.get_project(episode.project_id) if episode.project_id is not None else None
        opening = episode.definition.media.opening
        introduce_participants = (
            opening.post_primer_bridge.introduce_participants
            if opening.post_primer_bridge.introduce_participants is not None
            else opening.introduce_participants
        )
        if introduce_participants:
            branding_service.ensure_identity_slate(episode, project)
        updated = timeline_service.build_timeline(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/timeline")
async def get_episode_timeline(
    episode_id: UUID,
    repo: RepositoryDep,
    timeline_service: TimelineServiceDep,
    transcript_version_id: Annotated[UUID | None, Query()] = None,
    language: Annotated[str | None, Query()] = None,
) -> dict:
    try:
        episode = repo.get(episode_id)
        return timeline_service.latest_timeline_payload(
            episode,
            transcript_version_id=transcript_version_id,
            language=language,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/episodes/{episode_id}/timeline", response_model=Episode)
async def update_episode_timeline(
    episode_id: UUID,
    request: TimelineUpdateRequest,
    repo: RepositoryDep,
    timeline_service: TimelineServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = timeline_service.update_timeline(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/renders", response_model=list[Asset])
async def list_episode_renders(
    episode_id: UUID,
    repo: RepositoryDep,
) -> list[Asset]:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return [asset for asset in episode.assets if asset.asset_type == "render"]


@router.post("/episodes/{episode_id}/renders", response_model=Episode, status_code=202)
async def render_episode(
    episode_id: UUID,
    request: RenderRequest,
    repo: RepositoryDep,
    render_service: RenderServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = render_service.enqueue_render(
            episode,
            request,
            presets=default_render_presets(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/thumbnails/generate", response_model=Episode)
async def generate_episode_thumbnail(
    episode_id: UUID,
    request: ThumbnailRequest,
    repo: RepositoryDep,
    render_service: RenderServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = render_service.generate_thumbnail(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/youtube-package/export", response_model=Episode)
async def export_episode_youtube_package(
    episode_id: UUID,
    request: YouTubeExportRequest,
    repo: RepositoryDep,
    render_service: RenderServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = render_service.export_youtube_package(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/youtube-package/inspect")
async def inspect_episode_youtube_package(
    episode_id: UUID,
    repo: RepositoryDep,
    settings: SettingsDep,
    package_asset_id: Annotated[UUID | None, Query()] = None,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    package_asset = (
        _asset_by_id(episode, package_asset_id)
        if package_asset_id is not None
        else _latest_asset(episode, AssetType.export_package)
    )
    if package_asset is None or package_asset.asset_type != AssetType.export_package:
        raise HTTPException(status_code=404, detail="export package asset not found")
    object_store = create_object_store(settings)
    inspection = _youtube_package_inspection(episode, package_asset, object_store)
    if inspection["status"] == "fail":
        raise HTTPException(status_code=422, detail=inspection)
    return inspection


@router.post("/episodes/{episode_id}/production-manifest", response_model=Episode)
async def generate_episode_production_manifest(
    episode_id: UUID,
    request: ProductionManifestRequest,
    repo: RepositoryDep,
    render_service: RenderServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = render_service.generate_production_manifest(episode, request)
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/publish-jobs", response_model=list[PublishJob])
async def list_episode_publish_jobs(
    episode_id: UUID,
    repo: RepositoryDep,
) -> list[PublishJob]:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return episode.publish_jobs


@router.post("/episodes/{episode_id}/publish", response_model=Episode)
async def publish_episode_package(
    episode_id: UUID,
    request: PublishRequest,
    repo: RepositoryDep,
    publisher_service: PublisherServiceDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = publisher_service.publish_package(
            episode,
            request,
            targets=repo.list_publisher_targets(),
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/discussion/turns/{turn_id}/regenerate", response_model=Episode)
async def regenerate_discussion_turn(
    episode_id: UUID,
    turn_id: UUID,
    request: TurnReviewActionRequest,
    repo: RepositoryDep,
    engine: DiscussionEngineDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = await engine.regenerate_turn(
            episode,
            turn_id,
            user_id=request.user_id,
            comment=request.comment,
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode or turn not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/discussion/transcript/reopen", response_model=Episode)
async def reopen_approved_discussion_transcript(
    episode_id: UUID,
    request: TurnReviewActionRequest,
    repo: RepositoryDep,
    engine: DiscussionEngineDep,
) -> Episode:
    """Create an editable successor to the currently approved transcript."""
    try:
        episode = repo.get(episode_id)
        updated = engine.reopen_approved_transcript_review(
            episode,
            user_id=request.user_id,
            comment=request.comment,
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/discussion/post-primer-bridge", response_model=Episode)
async def add_post_primer_bridge_draft(
    episode_id: UUID,
    request: TurnReviewActionRequest,
    repo: RepositoryDep,
    engine: DiscussionEngineDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = engine.add_post_primer_bridge_draft(
            episode,
            user_id=request.user_id,
            comment=request.comment,
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/episodes/{episode_id}/discussion/turns/{turn_id}/text", response_model=Episode)
async def edit_discussion_turn_text(
    episode_id: UUID,
    turn_id: UUID,
    request: TurnManualEditRequest,
    repo: RepositoryDep,
    engine: DiscussionEngineDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = engine.edit_turn_text(
            episode,
            turn_id,
            text=request.text,
            user_id=request.user_id,
            comment=request.comment,
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode or turn not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/episodes/{episode_id}/discussion/turns/{turn_id}/exclude", response_model=Episode)
async def exclude_discussion_turn(
    episode_id: UUID,
    turn_id: UUID,
    request: TurnReviewActionRequest,
    repo: RepositoryDep,
    engine: DiscussionEngineDep,
) -> Episode:
    try:
        episode = repo.get(episode_id)
        updated = engine.exclude_turn(
            episode,
            turn_id,
            user_id=request.user_id,
            comment=request.comment,
        )
        return repo.save(updated)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode or turn not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/quality")
async def episode_quality(
    episode_id: UUID,
    repo: RepositoryDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return {"quality_results": episode.quality_results}


@router.get("/episodes/{episode_id}/approvals")
async def episode_approvals(
    episode_id: UUID,
    repo: RepositoryDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return {"approvals": episode.approvals}


@router.post("/episodes/{episode_id}/approvals/{approval_id}/decision", response_model=Episode)
async def decide_approval(
    episode_id: UUID,
    approval_id: UUID,
    request: ApprovalDecisionRequest,
    repo: RepositoryDep,
) -> Episode:
    try:
        return repo.record_approval_decision(episode_id, approval_id, request)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="approval or episode not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/episodes/{episode_id}/audit")
async def episode_audit(
    episode_id: UUID,
    repo: RepositoryDep,
) -> dict:
    try:
        episode = repo.get(episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return {"audit_events": episode.audit_events}


@router.get("/audit-events", response_model=list[AuditEvent])
async def list_audit_events(
    repo: RepositoryDep,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AuditEvent]:
    return repo.list_audit_events(limit=limit)


@router.get("/model-endpoints", response_model=list[ModelEndpoint])
async def list_model_endpoints(repo: RepositoryDep) -> list[ModelEndpoint]:
    return repo.list_model_endpoints()


@router.post("/model-endpoints", response_model=ModelEndpoint)
async def create_model_endpoint(
    request: ModelEndpointCreateRequest,
    repo: RepositoryDep,
) -> ModelEndpoint:
    return repo.upsert_model_endpoint(request.to_endpoint())


@router.get("/model-endpoints/{endpoint_id}", response_model=ModelEndpoint)
async def get_model_endpoint(endpoint_id: str, repo: RepositoryDep) -> ModelEndpoint:
    try:
        return repo.get_model_endpoint(endpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="model endpoint not found") from exc


@router.put("/model-endpoints/{endpoint_id}", response_model=ModelEndpoint)
async def update_model_endpoint(
    endpoint_id: str,
    request: ModelEndpointCreateRequest,
    repo: RepositoryDep,
) -> ModelEndpoint:
    endpoint = request.to_endpoint()
    endpoint.id = endpoint_id
    return repo.upsert_model_endpoint(endpoint)


@router.post("/model-endpoints/{endpoint_id}/health", response_model=ModelEndpoint)
async def check_model_endpoint_health(
    endpoint_id: str,
    repo: RepositoryDep,
    model_endpoints: ModelEndpointServiceDep,
) -> ModelEndpoint:
    try:
        endpoint = repo.get_model_endpoint(endpoint_id)
        checked = await model_endpoints.check_endpoint_health(endpoint)
        return repo.upsert_model_endpoint(checked)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="model endpoint not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/model-endpoints/openrouter/presets/provision",
    response_model=OpenRouterPresetProvisionResponse,
)
async def provision_openrouter_presets(
    request: OpenRouterPresetProvisionRequest,
    repo: RepositoryDep,
) -> OpenRouterPresetProvisionResponse:
    result = repo.provision_openrouter_presets(
        assign_participants=request.assign_participants,
    )
    return OpenRouterPresetProvisionResponse.model_validate(result)


@router.delete("/model-endpoints/{endpoint_id}", status_code=204)
async def delete_model_endpoint(endpoint_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_model_endpoint(endpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="model endpoint not found") from exc


@router.get("/voicebox-endpoints", response_model=list[VoiceboxEndpoint])
async def list_voicebox_endpoints(repo: RepositoryDep) -> list[VoiceboxEndpoint]:
    return repo.list_voicebox_endpoints()


@router.post("/voicebox-endpoints", response_model=VoiceboxEndpoint)
async def create_voicebox_endpoint(
    request: VoiceboxEndpointCreateRequest,
    repo: RepositoryDep,
) -> VoiceboxEndpoint:
    return repo.upsert_voicebox_endpoint(request.to_endpoint())


@router.get("/voicebox-endpoints/{endpoint_id}", response_model=VoiceboxEndpoint)
async def get_voicebox_endpoint(endpoint_id: str, repo: RepositoryDep) -> VoiceboxEndpoint:
    try:
        return repo.get_voicebox_endpoint(endpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="voicebox endpoint not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/voicebox-endpoints/{endpoint_id}", response_model=VoiceboxEndpoint)
async def update_voicebox_endpoint(
    endpoint_id: str,
    request: VoiceboxEndpointCreateRequest,
    repo: RepositoryDep,
) -> VoiceboxEndpoint:
    endpoint = request.to_endpoint()
    endpoint.id = endpoint_id
    return repo.upsert_voicebox_endpoint(endpoint)


@router.post("/voicebox-endpoints/{endpoint_id}/health", response_model=VoiceboxEndpoint)
async def check_voicebox_endpoint_health(
    endpoint_id: str,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> VoiceboxEndpoint:
    try:
        endpoint = repo.get_voicebox_endpoint(endpoint_id)
        checked = await voicebox.check_endpoint_health(endpoint)
        return repo.upsert_voicebox_endpoint(checked)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="voicebox endpoint not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError:
        endpoint.health_status = "unhealthy"
        return repo.upsert_voicebox_endpoint(endpoint)


@router.post(
    "/voicebox-endpoints/{endpoint_id}/ca-certificate/bootstrap",
    response_model=VoiceboxEndpoint,
)
async def bootstrap_voicebox_endpoint_ca_certificate(
    endpoint_id: str,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> VoiceboxEndpoint:
    try:
        endpoint = repo.get_voicebox_endpoint(endpoint_id)
        bootstrapped = await voicebox.bootstrap_ca_certificate(endpoint)
        checked = await voicebox.check_endpoint_health(bootstrapped)
        saved = repo.upsert_voicebox_endpoint(checked)
        bootstrap = saved.capabilities.get("ca_cert_bootstrap")
        bootstrap_details = bootstrap if isinstance(bootstrap, dict) else {}
        repo.record_global_audit_event(
            AuditEvent(
                event_type="voicebox_endpoint.ca_certificate_bootstrapped",
                details={
                    "endpoint_id": saved.id,
                    "health_status": saved.health_status,
                    "ca_cert_stored": bootstrap_details.get("stored") is True,
                    "ca_cert_sha256_matches": (bootstrap_details.get("sha256_matches") is True),
                    "tls_ca_cert_path_configured": bool(
                        str(saved.capabilities.get("tls_ca_cert_path") or "").strip()
                    ),
                },
            )
        )
        return saved
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="voicebox endpoint not found") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/voicebox-endpoints/{endpoint_id}/b1-german-voice-presets/provision",
    response_model=B1VoicePresetProvisionResponse,
)
async def provision_b1_german_voice_presets(
    endpoint_id: str,
    request: B1VoicePresetProvisionRequest,
    repo: RepositoryDep,
) -> B1VoicePresetProvisionResponse:
    try:
        result = repo.provision_b1_german_voice_presets(
            endpoint_id=endpoint_id,
            assign_participants=request.assign_participants,
            reassign_participants=request.reassign_participants,
        )
        return B1VoicePresetProvisionResponse.model_validate(result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="voicebox endpoint not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/voicebox-endpoints/{endpoint_id}/b1-voice-inventory/sync",
    response_model=B1VoiceInventorySyncResponse,
)
async def sync_b1_voice_inventory(
    endpoint_id: str,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> B1VoiceInventorySyncResponse:
    try:
        endpoint = repo.get_voicebox_endpoint(endpoint_id)
        discovered_profiles = await voicebox.list_b1_voice_profiles(endpoint)
        result = repo.sync_b1_voicebox_profile_inventory(
            endpoint_id=endpoint_id,
            discovered_profiles=discovered_profiles,
        )
        return B1VoiceInventorySyncResponse.model_validate(result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="voicebox endpoint not found") from exc
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/voicebox-endpoints/{endpoint_id}", status_code=204)
async def delete_voicebox_endpoint(endpoint_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_voicebox_endpoint(endpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="voicebox endpoint not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/voice-profiles", response_model=list[VoiceProfile])
async def list_voice_profiles(repo: RepositoryDep) -> list[VoiceProfile]:
    return repo.list_voice_profiles()


@router.post("/voice-profiles/preview")
async def preview_voice_profile(
    request: VoicePreviewRequest,
    repo: RepositoryDep,
    voicebox: VoiceboxServiceDep,
) -> Response:
    profile = request.profile.to_profile()
    try:
        endpoint = repo.get_voicebox_endpoint(profile.voicebox_endpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="voicebox endpoint is not available") from exc
    if not endpoint.enabled:
        raise HTTPException(status_code=422, detail="voicebox endpoint is disabled")
    if not profile.enabled:
        raise HTTPException(status_code=422, detail="voice profile is disabled")
    try:
        payload, mime_type = await voicebox.render_voice_preview(endpoint, profile, request.text)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=payload,
        media_type=mime_type,
        headers={
            "cache-control": "no-store",
            "content-disposition": 'inline; filename="voice-preview.wav"',
        },
    )


@router.get("/primer-narrator-profiles", response_model=list[PrimerNarratorProfile])
async def list_primer_narrator_profiles(repo: RepositoryDep) -> list[PrimerNarratorProfile]:
    return repo.list_primer_narrator_profiles()


@router.post("/primer-narrator-profiles", response_model=PrimerNarratorProfile)
async def create_primer_narrator_profile(
    request: PrimerNarratorProfileCreateRequest, repo: RepositoryDep
) -> PrimerNarratorProfile:
    try:
        return repo.upsert_primer_narrator_profile(request.to_profile())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/primer-narrator-profiles/{profile_id}", response_model=PrimerNarratorProfile)
async def update_primer_narrator_profile(
    profile_id: str, request: PrimerNarratorProfileCreateRequest, repo: RepositoryDep
) -> PrimerNarratorProfile:
    if request.id != profile_id:
        raise HTTPException(status_code=422, detail="profile ID does not match request path")
    try:
        repo.get_primer_narrator_profile(profile_id)
        return repo.upsert_primer_narrator_profile(request.to_profile())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="narrator profile not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/primer-narrator-profiles/{profile_id}", status_code=204)
async def delete_primer_narrator_profile(profile_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_primer_narrator_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="narrator profile not found") from exc


@router.post("/voice-profiles", response_model=VoiceProfile)
async def create_voice_profile(
    request: VoiceProfileCreateRequest,
    repo: RepositoryDep,
) -> VoiceProfile:
    try:
        return repo.upsert_voice_profile(request.to_profile())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/voice-profiles/{profile_id}", response_model=VoiceProfile)
async def get_voice_profile(profile_id: str, repo: RepositoryDep) -> VoiceProfile:
    try:
        return repo.get_voice_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="voice profile not found") from exc


@router.put("/voice-profiles/{profile_id}", response_model=VoiceProfile)
async def update_voice_profile(
    profile_id: str,
    request: VoiceProfileCreateRequest,
    repo: RepositoryDep,
) -> VoiceProfile:
    profile = request.to_profile()
    profile.id = profile_id
    try:
        return repo.upsert_voice_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/voice-profiles/{profile_id}", status_code=204)
async def delete_voice_profile(profile_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_voice_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="voice profile not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/comfyui-endpoints", response_model=list[ComfyUiEndpoint])
async def list_comfyui_endpoints(repo: RepositoryDep) -> list[ComfyUiEndpoint]:
    return repo.list_comfyui_endpoints()


@router.post("/comfyui-endpoints", response_model=ComfyUiEndpoint)
async def create_comfyui_endpoint(
    request: ComfyUiEndpointCreateRequest,
    repo: RepositoryDep,
) -> ComfyUiEndpoint:
    return repo.upsert_comfyui_endpoint(request.to_endpoint())


@router.get("/comfyui-endpoints/{endpoint_id}", response_model=ComfyUiEndpoint)
async def get_comfyui_endpoint(endpoint_id: str, repo: RepositoryDep) -> ComfyUiEndpoint:
    try:
        return repo.get_comfyui_endpoint(endpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="comfyui endpoint not found") from exc


@router.put("/comfyui-endpoints/{endpoint_id}", response_model=ComfyUiEndpoint)
async def update_comfyui_endpoint(
    endpoint_id: str,
    request: ComfyUiEndpointCreateRequest,
    repo: RepositoryDep,
) -> ComfyUiEndpoint:
    endpoint = request.to_endpoint()
    endpoint.id = endpoint_id
    return repo.upsert_comfyui_endpoint(endpoint)


@router.post("/comfyui-endpoints/{endpoint_id}/health", response_model=ComfyUiEndpoint)
async def check_comfyui_endpoint_health(
    endpoint_id: str,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> ComfyUiEndpoint:
    try:
        endpoint = repo.get_comfyui_endpoint(endpoint_id)
        checked = await comfyui.check_endpoint_health(endpoint)
        return repo.upsert_comfyui_endpoint(checked)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="comfyui endpoint not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError:
        endpoint.health_status = "unhealthy"
        return repo.upsert_comfyui_endpoint(endpoint)


@router.post(
    "/comfyui-endpoints/{endpoint_id}/ca-certificate/bootstrap",
    response_model=ComfyUiEndpoint,
)
async def bootstrap_comfyui_endpoint_ca_certificate(
    endpoint_id: str,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> ComfyUiEndpoint:
    try:
        endpoint = repo.get_comfyui_endpoint(endpoint_id)
        bootstrapped = await comfyui.bootstrap_ca_certificate(endpoint)
        checked = await comfyui.check_endpoint_health(bootstrapped)
        saved = repo.upsert_comfyui_endpoint(checked)
        bootstrap = saved.capabilities.get("ca_cert_bootstrap")
        bootstrap_details = bootstrap if isinstance(bootstrap, dict) else {}
        repo.record_global_audit_event(
            AuditEvent(
                event_type="comfyui_endpoint.ca_certificate_bootstrapped",
                details={
                    "endpoint_id": saved.id,
                    "health_status": saved.health_status,
                    "ca_cert_stored": bootstrap_details.get("stored") is True,
                    "ca_cert_sha256_matches": (bootstrap_details.get("sha256_matches") is True),
                    "tls_ca_cert_path_configured": bool(
                        str(saved.capabilities.get("tls_ca_cert_path") or "").strip()
                    ),
                },
            )
        )
        return saved
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="comfyui endpoint not found") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/comfyui-endpoints/{endpoint_id}", status_code=204)
async def delete_comfyui_endpoint(endpoint_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_comfyui_endpoint(endpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="comfyui endpoint not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/comfyui-workflows", response_model=list[ComfyUiWorkflow])
async def list_comfyui_workflows(repo: RepositoryDep) -> list[ComfyUiWorkflow]:
    return repo.list_comfyui_workflows()


@router.post("/comfyui-workflows", response_model=ComfyUiWorkflow)
async def create_comfyui_workflow(
    request: ComfyUiWorkflowCreateRequest,
    repo: RepositoryDep,
) -> ComfyUiWorkflow:
    try:
        return repo.upsert_comfyui_workflow(request.to_workflow())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/comfyui-workflows/{workflow_id}", response_model=ComfyUiWorkflow)
async def get_comfyui_workflow(workflow_id: str, repo: RepositoryDep) -> ComfyUiWorkflow:
    try:
        return repo.get_comfyui_workflow(workflow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="comfyui workflow not found") from exc


@router.put("/comfyui-workflows/{workflow_id}", response_model=ComfyUiWorkflow)
async def update_comfyui_workflow(
    workflow_id: str,
    request: ComfyUiWorkflowCreateRequest,
    repo: RepositoryDep,
) -> ComfyUiWorkflow:
    workflow = request.to_workflow()
    workflow.id = workflow_id
    try:
        return repo.upsert_comfyui_workflow(workflow)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/comfyui-workflows/{workflow_id}", status_code=204)
async def delete_comfyui_workflow(workflow_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_comfyui_workflow(workflow_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="comfyui workflow not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/discussion-prompt-templates", response_model=list[DiscussionPromptTemplate])
async def list_discussion_prompt_templates(
    repo: RepositoryDep,
) -> list[DiscussionPromptTemplate]:
    return repo.list_discussion_prompt_templates()


@router.post("/discussion-prompt-templates", response_model=DiscussionPromptTemplate)
async def create_discussion_prompt_template(
    request: DiscussionPromptTemplateCreateRequest,
    repo: RepositoryDep,
) -> DiscussionPromptTemplate:
    try:
        return repo.upsert_discussion_prompt_template(request.to_template())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/discussion-prompt-templates/{template_id}",
    response_model=DiscussionPromptTemplate,
)
async def get_discussion_prompt_template(
    template_id: str,
    repo: RepositoryDep,
) -> DiscussionPromptTemplate:
    try:
        return repo.get_discussion_prompt_template(template_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="discussion prompt template not found") from exc


@router.put(
    "/discussion-prompt-templates/{template_id}",
    response_model=DiscussionPromptTemplate,
)
async def update_discussion_prompt_template(
    template_id: str,
    request: DiscussionPromptTemplateCreateRequest,
    repo: RepositoryDep,
) -> DiscussionPromptTemplate:
    template = request.to_template()
    template.id = template_id
    try:
        return repo.upsert_discussion_prompt_template(template)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/discussion-prompt-templates/{template_id}", status_code=204)
async def delete_discussion_prompt_template(template_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_discussion_prompt_template(template_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="discussion prompt template not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/visual-profiles", response_model=list[VisualProfile])
async def list_visual_profiles(repo: RepositoryDep) -> list[VisualProfile]:
    return repo.list_visual_profiles()


@router.post("/visual-profiles", response_model=VisualProfile)
async def create_visual_profile(
    request: VisualProfileCreateRequest,
    repo: RepositoryDep,
) -> VisualProfile:
    try:
        return repo.upsert_visual_profile(request.to_profile())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/visual-profiles/{profile_id}", response_model=VisualProfile)
async def get_visual_profile(profile_id: str, repo: RepositoryDep) -> VisualProfile:
    try:
        return repo.get_visual_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="visual profile not found") from exc


@router.put("/visual-profiles/{profile_id}", response_model=VisualProfile)
async def update_visual_profile(
    profile_id: str,
    request: VisualProfileCreateRequest,
    repo: RepositoryDep,
) -> VisualProfile:
    profile = request.to_profile()
    profile.id = profile_id
    try:
        return repo.upsert_visual_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/visual-profiles/{profile_id}/reference-image", response_model=VisualProfile)
async def upload_visual_profile_reference_image(
    profile_id: str,
    request: VisualProfileReferenceImageUploadRequest,
    repo: RepositoryDep,
    comfyui: ComfyUiServiceDep,
) -> VisualProfile:
    try:
        profile = repo.get_visual_profile(profile_id)
        stored = comfyui.store_visual_profile_reference_image(
            profile,
            filename=request.filename,
            content_type=request.content_type,
            image_base64=request.image_base64,
            reference_type=request.reference_type,
        )
        reference_images = list(profile.reference_images)
        if request.reference_type != "wardrobe":
            reference_images = [
                reference
                for reference in profile.reference_images
                if reference.reference_type != request.reference_type
            ]
        uploaded_reference = VisualReferenceImage(
            reference_type=request.reference_type,
            uri=stored["reference_image_uri"],
            filename=request.filename,
            content_type=stored["reference_image_content_type"],
            checksum=stored["reference_image_checksum"],
            size_bytes=stored["reference_image_size_bytes"],
            uploaded_at=datetime.now(UTC),
        )
        reference_images.append(uploaded_reference)
        legacy_reference_uri = (
            stored["reference_image_uri"]
            if request.reference_type == "portrait" or not profile.reference_image_uri
            else profile.reference_image_uri
        )
        updated = profile.model_copy(
            update={
                "reference_image_uri": legacy_reference_uri,
                "reference_images": reference_images,
            }
        )
        saved = repo.upsert_visual_profile(updated)
        repo.record_global_audit_event(
            AuditEvent(
                event_type="visual_profile.reference_image_uploaded",
                actor=request.user_id or "system",
                details={
                    "profile_id": saved.id,
                    "reference_type": request.reference_type,
                    "content_type": stored["reference_image_content_type"],
                    "size_bytes": stored["reference_image_size_bytes"],
                    "checksum": stored["reference_image_checksum"],
                    "storage_backend": comfyui.object_store.__class__.__name__,
                    "reference_image_uri_present": True,
                },
            )
        )
        return saved
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="visual profile not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/visual-profiles/{profile_id}/reference-images/{reference_type}/download")
async def download_visual_profile_reference_image(
    profile_id: str,
    reference_type: VisualReferenceImageType,
    repo: RepositoryDep,
    settings: SettingsDep,
    uri: str | None = None,
) -> FileResponse:
    try:
        profile = repo.get_visual_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="visual profile not found") from exc

    reference = next(
        (
            item
            for item in reversed(profile.reference_images)
            if item.reference_type == reference_type and (uri is None or item.uri == uri)
        ),
        None,
    )
    uri = reference.uri if reference else None
    content_type = reference.content_type if reference else None
    if uri is None and reference_type == "portrait" and not profile.reference_images:
        uri = profile.reference_image_uri
    if uri is None:
        raise HTTPException(status_code=404, detail="reference image not found")

    object_store = create_object_store(settings)
    path = object_store.path_for_uri(uri)
    if path is None:
        raise HTTPException(
            status_code=422,
            detail="reference image storage URI is not downloadable",
        )
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="stored reference image not found")

    return FileResponse(
        path,
        media_type=content_type or "application/octet-stream",
        filename=_visual_reference_download_filename(profile.id, reference_type, path.name),
    )


@router.delete(
    "/visual-profiles/{profile_id}/reference-images/{reference_type}",
    response_model=VisualProfile,
)
async def delete_visual_profile_reference_image(
    profile_id: str,
    reference_type: VisualReferenceImageType,
    repo: RepositoryDep,
    uri: str | None = None,
) -> VisualProfile:
    try:
        profile = repo.get_visual_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="visual profile not found") from exc

    removed_reference = next(
        (
            reference
            for reference in profile.reference_images
            if reference.reference_type == reference_type and (uri is None or reference.uri == uri)
        ),
        None,
    )
    if removed_reference is None:
        raise HTTPException(status_code=404, detail="reference image not found")

    reference_images = [
        reference for reference in profile.reference_images if reference is not removed_reference
    ]
    legacy_reference_uri = profile.reference_image_uri
    if reference_type == "portrait" and profile.reference_image_uri == removed_reference.uri:
        legacy_reference_uri = None

    saved = repo.upsert_visual_profile(
        profile.model_copy(
            update={
                "reference_image_uri": legacy_reference_uri,
                "reference_images": reference_images,
            }
        )
    )
    repo.record_global_audit_event(
        AuditEvent(
            event_type="visual_profile.reference_image_removed",
            actor="system",
            details={
                "profile_id": saved.id,
                "reference_type": reference_type,
                "content_type": removed_reference.content_type,
                "size_bytes": removed_reference.size_bytes,
                "checksum": removed_reference.checksum,
                "reference_image_uri_present": True,
                "stored_object_retained": True,
            },
        )
    )
    return saved


@router.delete("/visual-profiles/{profile_id}", status_code=204)
async def delete_visual_profile(profile_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_visual_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="visual profile not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/participant-profiles", response_model=list[ParticipantProfile])
async def list_participant_profiles(repo: RepositoryDep) -> list[ParticipantProfile]:
    return repo.list_participant_profiles()


@router.post("/participant-profiles", response_model=ParticipantProfile)
async def create_participant_profile(
    request: ParticipantProfileCreateRequest,
    repo: RepositoryDep,
) -> ParticipantProfile:
    try:
        return repo.upsert_participant_profile(request.to_profile())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/participant-profiles/{profile_id}", response_model=ParticipantProfile)
async def get_participant_profile(profile_id: str, repo: RepositoryDep) -> ParticipantProfile:
    try:
        return repo.get_participant_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="participant profile not found") from exc


@router.put("/participant-profiles/{profile_id}", response_model=ParticipantProfile)
async def update_participant_profile(
    profile_id: str,
    request: ParticipantProfileCreateRequest,
    repo: RepositoryDep,
) -> ParticipantProfile:
    profile = request.to_profile()
    profile.id = profile_id
    try:
        return repo.upsert_participant_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/participant-profiles/{profile_id}", status_code=204)
async def delete_participant_profile(profile_id: str, repo: RepositoryDep) -> None:
    try:
        repo.delete_participant_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="participant profile not found") from exc
