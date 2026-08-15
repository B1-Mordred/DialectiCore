from __future__ import annotations

import io
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.core.config import Settings
from app.domain.enums import AssetType
from app.domain.schemas import (
    Asset,
    AuditEvent,
    Episode,
    StudioCameraPlateCalibration,
    StudioCameraPlateGenerateRequest,
    StudioCameraPlateReviewRequest,
    StudioCameraPlateUploadMetadata,
)
from app.services.object_storage import ObjectStore, create_object_store
from PIL import Image, UnidentifiedImageError

MAX_CAMERA_PLATE_BYTES = 25 * 1024 * 1024
MAX_CAMERA_PLATE_PIXELS = 50_000_000


class StudioCameraPlateService:
    def __init__(
        self,
        settings: Settings,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.settings = settings
        self.object_store = object_store or create_object_store(settings)

    def upload_plate(
        self,
        episode: Episode,
        payload: bytes,
        metadata: StudioCameraPlateUploadMetadata,
    ) -> Asset:
        normalized, width, height = self._normalize_image(payload)
        plate_id = uuid4()
        stored = self.object_store.put_bytes(
            key=f"studio-camera-plates/{episode.id}/{metadata.angle_id}/{plate_id}.png",
            payload=normalized,
            content_type="image/png",
        )
        asset = Asset(
            id=plate_id,
            episode_id=episode.id,
            asset_type=AssetType.studio_scene,
            language=episode.source_language,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            storage_uri=stored.uri,
            mime_type="image/png",
            width=width,
            height=height,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                "schema_version": "studio_camera_plate_bundle.v1",
                "visual_role": "studio_camera_plate",
                "angle_id": metadata.angle_id,
                "calibration": metadata.calibration.model_dump(mode="json"),
                "provenance": metadata.provenance,
                "review": {"decision": "pending", "reviewed_at": None},
                "render_ready": False,
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
            },
        )
        episode.assets.append(asset)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="studio_camera_plate.uploaded",
                actor=metadata.user_id or "system",
                details={
                    "asset_id": str(asset.id),
                    "angle_id": metadata.angle_id,
                    "checksum": asset.checksum,
                },
            )
        )
        return asset

    def plan_managed_generation(
        self,
        episode: Episode,
        request: StudioCameraPlateGenerateRequest,
    ) -> Asset:
        transcript_id = episode.canonical_transcript_version_id
        if transcript_id is None:
            raise ValueError("camera-plate generation requires a canonical transcript")
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.studio_scene,
            language=episode.source_language,
            source_entity_type="episode",
            source_entity_id=str(episode.id),
            status="planned",
            generation_metadata={
                "schema_version": "studio_camera_plate_bundle.v1",
                "visual_role": "studio_camera_plate",
                "angle_id": request.angle_id,
                "transcript_version_id": str(transcript_id),
                "comfyui_workflow_id": "workflow-studio-panel-shot-v1",
                "prompt_inputs": {
                    "camera_view": request.angle_id,
                    "transcript_text": request.prompt,
                    "style_prompt": request.prompt,
                    "width": episode.definition.media.width,
                    "height": episode.definition.media.height,
                },
                "generation_transport": "managed_b1_scheduler_api",
                "calibration": None,
                "review": {"decision": "pending", "reviewed_at": None},
                "render_ready": False,
            },
        )
        episode.assets.append(asset)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="studio_camera_plate.generation_planned",
                actor=request.user_id or "system",
                details={
                    "asset_id": str(asset.id),
                    "angle_id": request.angle_id,
                    "transport": "managed_b1_scheduler_api",
                },
            )
        )
        return asset

    def review_plate(
        self,
        episode: Episode,
        asset_id: UUID,
        request: StudioCameraPlateReviewRequest,
    ) -> Asset:
        asset = next((item for item in episode.assets if item.id == asset_id), None)
        if (
            asset is None
            or asset.generation_metadata.get("visual_role") != "studio_camera_plate"
        ):
            raise ValueError("studio camera plate not found")
        calibration_payload = (
            request.calibration.model_dump(mode="json")
            if request.calibration is not None
            else asset.generation_metadata.get("calibration")
        )
        if request.decision == "approved":
            if not isinstance(calibration_payload, dict):
                raise ValueError("camera plate approval requires complete calibration")
            calibration = StudioCameraPlateCalibration.model_validate(calibration_payload)
            required_participants = {participant.id for participant in episode.participants}
            missing = sorted(required_participants - set(calibration.seat_anchors))
            if missing:
                raise ValueError(
                    "camera plate approval requires seat anchors for: " + ", ".join(missing)
                )
            if not asset.storage_uri or not asset.checksum:
                raise ValueError("camera plate approval requires a completed image artifact")
        now = datetime.now(UTC)
        asset.generation_metadata = {
            **asset.generation_metadata,
            "calibration": calibration_payload,
            "review": {
                "decision": request.decision,
                "comment": request.comment,
                "reviewed_by": request.user_id or "system",
                "reviewed_at": now.isoformat(),
            },
            "render_ready": request.decision == "approved",
        }
        asset.updated_at = now
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type=f"studio_camera_plate.{request.decision}",
                actor=request.user_id or "system",
                details={
                    "asset_id": str(asset.id),
                    "angle_id": asset.generation_metadata.get("angle_id"),
                    "checksum": asset.checksum,
                },
            )
        )
        return asset

    @staticmethod
    def approved_plate_for_angle(episode: Episode, angle_id: str) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.status == "completed"
                and asset.generation_metadata.get("visual_role") == "studio_camera_plate"
                and asset.generation_metadata.get("angle_id") == angle_id
                and asset.generation_metadata.get("render_ready") is True
                and isinstance(asset.generation_metadata.get("calibration"), dict)
                and asset.storage_uri
                and asset.checksum
            ),
            None,
        )

    @staticmethod
    def _normalize_image(payload: bytes) -> tuple[bytes, int, int]:
        if not payload:
            raise ValueError("camera plate upload is empty")
        if len(payload) > MAX_CAMERA_PLATE_BYTES:
            raise ValueError("camera plate upload exceeds the 25 MiB limit")
        try:
            with Image.open(io.BytesIO(payload)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("camera plate must be PNG, JPEG, or WebP")
                width, height = source.size
                if width < 1 or height < 1 or width * height > MAX_CAMERA_PLATE_PIXELS:
                    raise ValueError("camera plate pixel dimensions are outside the safe limit")
                image = source.convert("RGBA")
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("camera plate is not a valid PNG, JPEG, or WebP image") from exc
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True, compress_level=9)
        return output.getvalue(), width, height
