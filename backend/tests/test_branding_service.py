from __future__ import annotations

import io
from pathlib import Path

import pytest
from app.core.config import Settings
from app.domain.defaults import default_model_endpoints, default_participants
from app.domain.enums import TurnType
from app.domain.schemas import (
    EpisodeCreateRequest,
    Project,
    ProjectBranding,
    TimelineBuildRequest,
    TimelineUpdateRequest,
    TranscriptTurn,
    TranscriptVersion,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.branding_service import MAX_LOGO_BYTES, BrandingService
from app.services.timeline_service import TimelineService
from PIL import Image
from tests.test_discussion_engine import definition


def _image_bytes(
    image_format: str = "JPEG",
    *,
    size: tuple[int, int] = (480, 240),
) -> bytes:
    image = Image.new("RGB", size, (22, 68, 120))
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _episode():
    request = EpisodeCreateRequest(
        definition=definition(),
        participants=default_participants(),
        model_endpoints=default_model_endpoints(),
    )
    request.definition.title = (
        "A deliberately long but complete episode title about reliable artificial "
        "intelligence infrastructure"
    )
    return EpisodeRepository().create(request)


def test_logo_upload_is_normalized_to_immutable_rgba_png(tmp_path: Path) -> None:
    service = BrandingService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )

    metadata = service.store_logo(
        _image_bytes("JPEG"),
        source="project_upload",
        owner_id="project-one",
    )

    assert metadata.mime_type == "image/png"
    assert metadata.source == "project_upload"
    assert metadata.width == 480
    assert metadata.height == 240
    assert metadata.storage_uri.endswith(".png")
    stored_path = service.object_store.path_for_uri(metadata.storage_uri)
    assert stored_path is not None
    with Image.open(stored_path) as normalized:
        assert normalized.format == "PNG"
        assert normalized.mode == "RGBA"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-an-image", "valid PNG, JPEG, or WebP"),
        (b"x" * (MAX_LOGO_BYTES + 1), "10 MiB"),
    ],
)
def test_logo_upload_rejects_invalid_or_oversize_payloads(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    service = BrandingService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )

    with pytest.raises(ValueError, match=message):
        service.store_logo(payload, source="project_upload", owner_id="project-one")


def test_identity_slate_uses_effective_logo_and_exact_untruncated_title(
    tmp_path: Path,
) -> None:
    service = BrandingService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    episode = _episode()
    project = Project(name="Infrastructure Forum", branding=ProjectBranding())
    project.branding.logo = service.store_logo(
        _image_bytes("WEBP"),
        source="project_upload",
        owner_id=str(project.id),
    )

    slate = service.ensure_identity_slate(episode, project)
    repeated = service.ensure_identity_slate(episode, project)

    assert repeated.id == slate.id
    assert slate.generation_metadata["visual_role"] == "show_identity_slate"
    assert slate.generation_metadata["episode_title"] == episode.title
    assert slate.generation_metadata["logo"]["checksum"] == project.branding.logo.checksum
    layout = slate.generation_metadata["layout"]
    assert layout["title_exact"] == episode.title
    assert layout["title_truncated"] is False
    assert " ".join(line["text"] for line in layout["title_lines"]) == episode.title
    assert 1 <= layout["title_line_count"] <= 4


def test_old_project_and_episode_receive_bundled_dialecticore_brand(
    tmp_path: Path,
) -> None:
    service = BrandingService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    )
    episode = _episode()
    project = Project(name="Existing Project")

    show_name, logo = service.effective_branding(episode, project)

    assert show_name == "Existing Project"
    assert logo.source == "bundled"
    assert logo.revision_id == "dialecticore-mark-v1"


def test_semantic_participant_intro_gets_identity_graphic_and_forced_wide_fly_in(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    branding = BrandingService(settings)
    timeline_service = TimelineService(settings)
    episode = _episode()
    episode.definition.media.opening.post_primer_bridge.introduce_participants = True
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type="broadcast",
        language="en",
        status="approved",
        turns=[
            TranscriptTurn(
                source_discussion_turn_ids=[],
                speaker_participant_id="chatgpt",
                turn_type=TurnType.post_primer_bridge,
                text="Welcome to the panel and meet today's participants.",
                status="accepted",
            )
        ],
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    slate = branding.ensure_identity_slate(episode, Project(name="The Show"))

    updated = timeline_service.build_timeline(
        episode,
        TimelineBuildRequest(transcript_version_id=transcript.id),
    )
    payload = timeline_service.latest_timeline_payload(
        updated, transcript_version_id=transcript.id
    )["timeline"]

    assert payload["track_schema_version"] == "dialecticore.parallel_directing_tracks.v2"
    assert payload["tracks"]["screen_graphics"] == [
        {
            "id": payload["tracks"]["screen_graphics"][0]["id"],
            "start_ms": 0,
            "end_ms": payload["duration_ms"],
            "duration_ms": payload["duration_ms"],
            "source_in_ms": 0,
            "source_out_ms": payload["duration_ms"],
            "kind": "show_identity",
            "asset_id": str(slate.id),
            "linked_segment_id": payload["segments"][0]["id"],
            "thumbnail_candidate": True,
            "rear_screen_priority": 100,
        }
    ]
    camera = payload["tracks"]["camera_direction"][0]
    assert camera["view"] == "establishing_wide"
    assert camera["action"] == "fly_in"
    assert payload["segments"][0]["thumbnail_candidate"] is True


def test_timeline_save_blocks_broll_over_branded_intro(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    branding = BrandingService(settings)
    timeline_service = TimelineService(settings)
    episode = _episode()
    episode.definition.media.opening.post_primer_bridge.introduce_participants = True
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type="broadcast",
        language="en",
        status="approved",
        turns=[
            TranscriptTurn(
                source_discussion_turn_ids=[],
                speaker_participant_id="chatgpt",
                turn_type=TurnType.post_primer_bridge,
                text="Meet the panel.",
                status="accepted",
            )
        ],
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    branding.ensure_identity_slate(episode, None)
    timeline_service.build_timeline(
        episode, TimelineBuildRequest(transcript_version_id=transcript.id)
    )
    timeline = timeline_service.latest_timeline_payload(
        episode, transcript_version_id=transcript.id
    )["timeline"]
    timeline["tracks"]["broll_content"] = [
        {
            "id": "content-1",
            "start_ms": 0,
            "end_ms": 1000,
            "source_in_ms": 0,
            "source_out_ms": 1000,
            "asset_id": "asset-1",
        }
    ]
    timeline["tracks"]["broll_presentation"] = [
        {
            "id": "presentation-1",
            "start_ms": 0,
            "end_ms": 1000,
            "source_in_ms": 0,
            "source_out_ms": 1000,
            "content_clip_id": "content-1",
            "mode": "rear_screen",
        }
    ]

    with pytest.raises(ValueError, match="cannot overlap"):
        timeline_service.update_timeline(
            episode,
            TimelineUpdateRequest(timeline=timeline),
        )
