from __future__ import annotations

import io
from pathlib import Path

import pytest
from app.core.config import Settings
from app.domain.defaults import default_model_endpoints, default_participants
from app.domain.schemas import (
    EpisodeCreateRequest,
    NormalizedImagePoint,
    StudioCameraPlateCalibration,
    StudioCameraPlateGenerateRequest,
    StudioCameraPlateReviewRequest,
    StudioCameraPlateUploadMetadata,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.studio_camera_plate_service import StudioCameraPlateService
from app.services.timeline_service import TimelineService
from PIL import Image
from tests.test_discussion_engine import definition


def _episode():
    return EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )


def _png() -> bytes:
    image = Image.new("RGB", (1280, 720), (38, 45, 64))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _calibration(episode, *, all_participants: bool = True):
    participant_ids = [participant.id for participant in episode.participants]
    if not all_participants:
        participant_ids = participant_ids[:1]
    return StudioCameraPlateCalibration(
        rear_screen_quadrilateral=[
            NormalizedImagePoint(x=0.30, y=0.15),
            NormalizedImagePoint(x=0.70, y=0.15),
            NormalizedImagePoint(x=0.70, y=0.48),
            NormalizedImagePoint(x=0.30, y=0.48),
        ],
        desk_occlusion_polygon=[
            NormalizedImagePoint(x=0.12, y=0.72),
            NormalizedImagePoint(x=0.88, y=0.72),
            NormalizedImagePoint(x=0.94, y=1.0),
            NormalizedImagePoint(x=0.06, y=1.0),
        ],
        seat_anchors={
            participant_id: NormalizedImagePoint(
                x=0.18 + 0.64 * index / max(1, len(participant_ids) - 1),
                y=0.62,
            )
            for index, participant_id in enumerate(participant_ids)
        },
    )


def test_plate_upload_requires_complete_human_review_before_render_ready(
    tmp_path: Path,
) -> None:
    episode = _episode()
    service = StudioCameraPlateService(
        Settings(object_storage_local_path=str(tmp_path / "objects"))
    )
    asset = service.upload_plate(
        episode,
        _png(),
        StudioCameraPlateUploadMetadata(
            angle_id="left-wide",
            calibration=_calibration(episode, all_participants=False),
            provenance={"kind": "reviewed_upload"},
            user_id="director",
        ),
    )

    assert asset.generation_metadata["render_ready"] is False
    assert asset.generation_metadata["review"]["decision"] == "pending"
    with pytest.raises(ValueError, match="seat anchors for"):
        service.review_plate(
            episode,
            asset.id,
            StudioCameraPlateReviewRequest(decision="approved", user_id="director"),
        )

    reviewed = service.review_plate(
        episode,
        asset.id,
        StudioCameraPlateReviewRequest(
            decision="approved",
            calibration=_calibration(episode),
            comment="All geometry checked.",
            user_id="director",
        ),
    )

    assert reviewed.generation_metadata["render_ready"] is True
    assert reviewed.generation_metadata["review"]["decision"] == "approved"
    assert service.approved_plate_for_angle(episode, "left-wide") == reviewed


def test_generated_plate_is_bound_to_managed_b1_workflow_and_starts_unapproved() -> None:
    episode = _episode()
    episode.canonical_transcript_version_id = episode.id
    service = StudioCameraPlateService(Settings())

    asset = service.plan_managed_generation(
        episode,
        StudioCameraPlateGenerateRequest(
            angle_id="right-wide",
            prompt="A complete right-side total studio camera plate with every participant.",
        ),
    )

    assert asset.status == "planned"
    assert asset.generation_metadata["comfyui_workflow_id"] == (
        "workflow-studio-panel-shot-v1"
    )
    assert asset.generation_metadata["generation_transport"] == "managed_b1_scheduler_api"
    assert asset.generation_metadata["render_ready"] is False


def test_alternate_angle_timeline_requires_exact_approved_plate(tmp_path: Path) -> None:
    episode = _episode()
    service = StudioCameraPlateService(
        Settings(object_storage_local_path=str(tmp_path / "objects"))
    )
    asset = service.upload_plate(
        episode,
        _png(),
        StudioCameraPlateUploadMetadata(
            angle_id="left-wide",
            calibration=_calibration(episode),
        ),
    )
    segments = [{"id": "segment-1", "start_ms": 0, "end_ms": 5_000}]
    tracks = {
        "camera_direction": [
            {
                "id": "camera-1",
                "start_ms": 0,
                "end_ms": 5_000,
                "view": "establishing_wide",
                "angle_id": "left-wide",
                "camera_plate_asset_id": str(asset.id),
            }
        ]
    }

    with pytest.raises(ValueError, match="exact approved calibrated plate"):
        TimelineService._validate_timeline_track_resources(episode, segments, tracks)

    service.review_plate(
        episode,
        asset.id,
        StudioCameraPlateReviewRequest(decision="approved"),
    )
    TimelineService._validate_timeline_track_resources(episode, segments, tracks)

    tracks["camera_direction"][0]["view"] = "speaker_close_up"
    with pytest.raises(ValueError, match="speaking closeups"):
        TimelineService._validate_timeline_track_resources(episode, segments, tracks)
