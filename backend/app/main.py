import logging
from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import get_settings
from app.infrastructure.database import (
    create_database_engine,
    create_session_factory,
    initialize_database,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.asset_replacement_service import AssetReplacementService
from app.services.auth_service import AuthService
from app.services.b1_managed_media_smoke_service import B1ManagedMediaSmokeService
from app.services.backup_service import BackupService
from app.services.branding_service import BrandingService
from app.services.comfyui_service import ComfyUiService
from app.services.discussion_engine import DiscussionEngine
from app.services.live_provider_preflight_service import LiveProviderPreflightService
from app.services.localization_service import LocalizationService
from app.services.model_endpoint_service import ModelEndpointService
from app.services.model_gateway import ModelGateway
from app.services.primer_media_service import PrimerMediaService
from app.services.primer_production_service import PrimerProductionService
from app.services.production_control_service import ProductionControlService
from app.services.publisher_service import PublisherService
from app.services.redis_bus_service import RedisBusService
from app.services.render_service import RenderService
from app.services.research_service import ResearchService
from app.services.structured_logging import (
    CORRELATION_RESPONSE_HEADER,
    correlation_id_context,
    correlation_id_from_headers,
    monotonic_ms,
    request_log_payload,
    setup_structured_logging,
)
from app.services.studio_camera_plate_service import StudioCameraPlateService
from app.services.subtitle_service import SubtitleService
from app.services.system_health_service import SystemHealthService
from app.services.system_metrics_service import SystemMetricsService
from app.services.timeline_service import TimelineService
from app.services.voicebox_service import VoiceboxService
from app.services.worker_lease_service import WorkerLeaseService
from app.services.worker_status_service import WorkerStatusService

settings = get_settings()
setup_structured_logging(settings)
request_logger = logging.getLogger("dialecticore.api")
database_engine = create_database_engine(settings)
initialize_database(database_engine)
session_factory = create_session_factory(database_engine)
repository = EpisodeRepository(session_factory)
auth_service = AuthService(settings)
model_endpoint_service = ModelEndpointService()
model_gateway = ModelGateway(prompt_template_provider=repository.list_discussion_prompt_templates)
discussion_engine = DiscussionEngine(model_gateway, settings)
research_service = ResearchService(settings)
primer_media_service = PrimerMediaService(settings)
localization_service = LocalizationService()
voicebox_service = VoiceboxService(settings)
live_provider_preflight_service = LiveProviderPreflightService()
comfyui_service = ComfyUiService(settings)
subtitle_service = SubtitleService(settings)
timeline_service = TimelineService(settings)
render_service = RenderService(settings)
primer_production_service = PrimerProductionService(settings, voicebox_service, render_service)
production_control_service = ProductionControlService(settings)
publisher_service = PublisherService()
system_health_service = SystemHealthService(settings)
worker_status_service = WorkerStatusService(settings)
worker_lease_service = WorkerLeaseService(settings)
system_metrics_service = SystemMetricsService()
backup_service = BackupService(settings)
branding_service = BrandingService(settings)
studio_camera_plate_service = StudioCameraPlateService(settings)
redis_bus_service = RedisBusService(settings)
asset_replacement_service = AssetReplacementService(settings)
b1_managed_media_smoke_service = B1ManagedMediaSmokeService(settings)

app = FastAPI(
    title="DialectiCore Production API",
    version="0.1.0",
    description="Increment 1 scaffold for multi-AI talk show production.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.resolved_cors_allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _safe_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe_errors = []
    for error in errors:
        safe_error = {key: value for key, value in error.items() if key != "input"}
        ctx = safe_error.get("ctx")
        if isinstance(ctx, dict):
            safe_error["ctx"] = {
                key: str(value) if key == "error" else value for key, value in ctx.items()
            }
        safe_errors.append(safe_error)
    return safe_errors


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request, exc):
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(_safe_validation_errors(exc.errors()))},
    )


@app.middleware("http")
async def enforce_rbac(request, call_next):
    if request.url.path.startswith("/api/v1") and request.method.upper() != "OPTIONS":
        try:
            auth_service.authorize_request(
                method=request.method,
                path=request.url.path,
                headers=request.headers,
            )
        except PermissionError as exc:
            return JSONResponse(status_code=403, content={"detail": str(exc)})
        except RuntimeError as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})
    return await call_next(request)


@app.middleware("http")
async def correlate_and_log_request(request, call_next):
    correlation_id = correlation_id_from_headers(request.headers)
    token = correlation_id_context.set(correlation_id)
    start_ms = monotonic_ms()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers[CORRELATION_RESPONSE_HEADER] = correlation_id
        return response
    finally:
        duration_ms = monotonic_ms() - start_ms
        client_host = request.client.host if request.client else None
        request_logger.info(
            "api.request",
            extra={
                "correlation_id": correlation_id,
                "structured": request_log_payload(
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    client_host=client_host,
                ),
            },
        )
        correlation_id_context.reset(token)


app.include_router(router)
