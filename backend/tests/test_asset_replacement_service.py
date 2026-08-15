from uuid import uuid4

from app.core.config import Settings
from app.domain.defaults import default_model_endpoints, default_participants
from app.domain.enums import AssetType
from app.domain.schemas import Asset, AssetReplacementRequest, EpisodeCreateRequest
from app.infrastructure.repository import EpisodeRepository
from app.services.asset_replacement_service import AssetReplacementService
from tests.test_discussion_engine import definition


def test_manual_asset_replacement_preserves_lineage_and_updates_timeline_refs(tmp_path) -> None:
    repository = EpisodeRepository()
    episode = repository.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    turn_id = str(uuid4())
    failed_audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=turn_id,
        storage_uri="object://dialecticore/audio/failed.wav",
        mime_type="audio/wav",
        duration_ms=1400,
        checksum="sha256:failed",
        status="failed",
    )
    timeline_json = {
        "id": str(uuid4()),
        "schema_version": "episode_timeline.v1",
        "episode_id": str(episode.id),
        "transcript_version_id": str(uuid4()),
        "language": "en",
        "duration_ms": 1400,
        "segments": [
            {
                "id": "segment-1",
                "start_ms": 0,
                "end_ms": 1400,
                "duration_ms": 1400,
                "audio_asset_id": str(failed_audio.id),
                "visual_layers": [],
                "citation_overlay_asset_ids": [],
            }
        ],
        "tracks": {"audio_dialogue": ["segment-1"]},
    }
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id=timeline_json["transcript_version_id"],
        storage_uri="object://dialecticore/timelines/original.json",
        mime_type="application/vnd.dialecticore.timeline+json",
        duration_ms=1400,
        checksum="sha256:timeline-original",
        generation_metadata={"timeline_json": timeline_json},
        status="completed",
    )
    episode.assets.extend([failed_audio, timeline_asset])

    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = AssetReplacementService(settings)
    updated = service.replace_asset(
        episode,
        failed_audio.id,
        AssetReplacementRequest(
            storage_uri="object://dialecticore/audio/manual-fixed.wav",
            mime_type="audio/wav",
            checksum="sha256:manual-fixed",
            duration_ms=1450,
            user_id="editor-1",
            comment="Uploaded corrected narration.",
        ),
    )

    replacement = updated.assets[-1]
    assert failed_audio.status == "replaced"
    assert failed_audio.generation_metadata["replaced_by_asset_id"] == str(replacement.id)
    assert replacement.asset_type == AssetType.audio
    assert replacement.source_entity_type == "transcript_turn"
    assert replacement.source_entity_id == turn_id
    assert replacement.storage_uri == "object://dialecticore/audio/manual-fixed.wav"
    assert replacement.status == "completed"
    assert replacement.generation_metadata["manual_replacement"] is True
    assert replacement.generation_metadata["replacement_of_asset_id"] == str(failed_audio.id)
    assert timeline_asset.generation_metadata["timeline_json"]["segments"][0][
        "audio_asset_id"
    ] == str(replacement.id)
    assert timeline_asset.generation_metadata["last_asset_replacement"][
        "replacement_count"
    ] == 1
    assert timeline_asset.checksum and timeline_asset.checksum.startswith("sha256:")
    assert updated.audit_events[-1].event_type == "asset.replaced"
    assert updated.audit_events[-1].details["timeline_asset_updates"][0][
        "timeline_asset_id"
    ] == str(timeline_asset.id)
