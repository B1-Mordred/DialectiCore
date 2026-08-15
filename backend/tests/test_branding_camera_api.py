from __future__ import annotations

import io
import json
from pathlib import Path

from app.api.routes import (
    get_branding_service,
    get_repository,
    get_studio_camera_plate_service,
)
from app.core.config import Settings
from app.domain.defaults import default_model_endpoints, default_participants
from app.domain.schemas import EpisodeCreateRequest, ProjectCreateRequest
from app.infrastructure.repository import EpisodeRepository
from app.main import app
from app.services.branding_service import BrandingService
from app.services.studio_camera_plate_service import StudioCameraPlateService
from fastapi.testclient import TestClient
from PIL import Image
from tests.test_discussion_engine import definition


def _png(size: tuple[int, int] = (640, 360)) -> bytes:
    image = Image.new("RGB", size, (30, 50, 80))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_branding_and_camera_plate_uploads_persist_through_http_api(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "objects"))
    repository = EpisodeRepository()
    branding = BrandingService(settings)
    camera_plates = StudioCameraPlateService(settings)
    project = repository.upsert_project(
        ProjectCreateRequest(name="The Systems Show").to_project()
    )
    episode = repository.create(
        EpisodeCreateRequest(
            project_id=project.id,
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    participant_ids = [participant.id for participant in episode.participants]
    calibration = {
        "rear_screen_quadrilateral": [
            {"x": 0.3, "y": 0.15},
            {"x": 0.7, "y": 0.15},
            {"x": 0.7, "y": 0.48},
            {"x": 0.3, "y": 0.48},
        ],
        "desk_occlusion_polygon": [
            {"x": 0.1, "y": 0.7},
            {"x": 0.9, "y": 0.7},
            {"x": 0.95, "y": 1.0},
        ],
        "seat_anchors": {
            participant_id: {"x": (index + 1) / (len(participant_ids) + 1), "y": 0.6}
            for index, participant_id in enumerate(participant_ids)
        },
    }
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_branding_service] = lambda: branding
    app.dependency_overrides[get_studio_camera_plate_service] = lambda: camera_plates
    client = TestClient(app)
    try:
        project_upload = client.post(
            f"/api/v1/projects/{project.id}/branding/logo",
            files={"logo": ("logo.png", _png(), "image/png")},
        )
        assert project_upload.status_code == 200
        assert project_upload.json()["branding"]["logo"]["mime_type"] == "image/png"

        update_without_branding = client.put(
            f"/api/v1/projects/{project.id}",
            json={
                "name": project.name,
                "description": "Updated without replacing the brand.",
                "default_language": project.default_language,
                "default_show_format_id": project.default_show_format_id,
            },
        )
        assert update_without_branding.status_code == 200
        assert update_without_branding.json()["branding"]["logo"]["checksum"] == (
            project_upload.json()["branding"]["logo"]["checksum"]
        )

        episode_upload = client.post(
            f"/api/v1/episodes/{episode.id}/branding/logo",
            files={"logo": ("episode.webp", _png(), "image/webp")},
        )
        assert episode_upload.status_code == 200
        assert episode_upload.json()["definition"]["media"]["branding"][
            "logo_override"
        ]["source"] == "episode_upload"

        first_slate = client.post(
            f"/api/v1/episodes/{episode.id}/branding/identity-slate"
        )
        second_slate = client.post(
            f"/api/v1/episodes/{episode.id}/branding/identity-slate"
        )
        assert first_slate.status_code == 200
        assert second_slate.status_code == 200
        first_slate_assets = [
            asset
            for asset in first_slate.json()["assets"]
            if asset["generation_metadata"].get("visual_role") == "show_identity_slate"
        ]
        second_slate_assets = [
            asset
            for asset in second_slate.json()["assets"]
            if asset["generation_metadata"].get("visual_role") == "show_identity_slate"
        ]
        assert len(first_slate_assets) == 1
        assert [asset["id"] for asset in second_slate_assets] == [
            first_slate_assets[0]["id"]
        ]

        plate_upload = client.post(
            f"/api/v1/episodes/{episode.id}/studio-camera-plates/upload",
            files={
                "image": ("left-wide.png", _png((1280, 720)), "image/png"),
                "metadata": (
                    None,
                    json.dumps(
                        {
                            "angle_id": "left-wide",
                            "calibration": calibration,
                            "provenance": {"kind": "reviewed_upload"},
                            "user_id": "director",
                        }
                    ),
                ),
            },
        )
        assert plate_upload.status_code == 200
        plate = next(
            asset
            for asset in plate_upload.json()["assets"]
            if asset["generation_metadata"].get("visual_role") == "studio_camera_plate"
        )
        assert plate["generation_metadata"]["render_ready"] is False

        reviewed = client.post(
            f"/api/v1/episodes/{episode.id}/studio-camera-plates/{plate['id']}/review",
            json={"decision": "approved", "user_id": "director"},
        )
        assert reviewed.status_code == 200
        reviewed_plate = next(
            asset for asset in reviewed.json()["assets"] if asset["id"] == plate["id"]
        )
        assert reviewed_plate["generation_metadata"]["render_ready"] is True
    finally:
        app.dependency_overrides.clear()
