import io
import json
import math
import shutil
import struct
import subprocess
import wave
import zipfile
import zlib
from array import array
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.core.config import Settings
from app.domain.defaults import (
    default_model_endpoints,
    default_participants,
    default_render_presets,
)
from app.domain.enums import AssetType, QualitySeverity, TranscriptType
from app.domain.schemas import (
    Approval,
    Asset,
    AuditEvent,
    Claim,
    EpisodeCreateRequest,
    ProductionManifestRequest,
    PublishJob,
    QualityResult,
    RenderRequest,
    TimelineBuildRequest,
    TranscriptTurn,
    TranscriptVersion,
    YouTubeExportRequest,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.render_service import RenderService
from app.services.timeline_service import TimelineService
from tests.test_discussion_engine import definition


def test_timeline_integrity_warning_does_not_block_preview_render() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        status="completed",
        generation_metadata={
            "timeline_json": {"media": {"composition_policy": "seated_studio_panel.v1"}}
        },
    )
    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="timeline_asset",
            target_id=str(timeline_asset.id),
            check_type="timeline_integrity",
            severity=QualitySeverity.warning,
            status="warning",
            details={
                "failure_count": 0,
                "warning_count": 1,
                "issues": [{"severity": "warning", "issue": "timeline_missing_subtitles"}],
            },
        )
    )

    RenderService._ensure_timeline_integrity_passes(episode, timeline_asset)


def wav_bytes(duration_ms: int = 1000, sample_rate: int = 48_000) -> bytes:
    frame_count = max(1, int(sample_rate * (duration_ms / 1000)))
    samples = array(
        "h",
        (
            int(32767 * 0.12 * math.sin(2 * math.pi * 220 * index / sample_rate))
            for index in range(frame_count)
        ),
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(samples.tobytes())
    return buffer.getvalue()


def test_thumbnail_seek_prefers_first_rear_screen_cutaway() -> None:
    episode_id = uuid4()
    render_asset = Asset(
        episode_id=episode_id,
        asset_type=AssetType.render,
        source_entity_type="timeline_asset",
        source_entity_id=str(uuid4()),
        status="completed",
        duration_ms=120_000,
        generation_metadata={
            "render_manifest": {
                "composition": {
                    "segment_layers": [
                        {
                            "start_ms": 0,
                            "duration_ms": 60_000,
                            "layout_policy": {"name": "full_frame_primary"},
                        },
                        {
                            "start_ms": 71_500,
                            "duration_ms": 4_000,
                            "layout_policy": {"name": "seated_panel_rear_screen_cutaway"},
                        },
                    ]
                }
            }
        },
    )

    assert RenderService._thumbnail_seek_seconds(render_asset) == 73.5


def test_thumbnail_luma_detects_black_frame(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required")
    black_path = tmp_path / "black.jpg"
    white_path = tmp_path / "white.jpg"
    for color, output_path in (("black", black_path), ("white", white_path)):
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color={color}:size=64x64",
                "-frames:v",
                "1",
                str(output_path),
            ],
            check=True,
        )

    service = RenderService(Settings())

    assert (service._thumbnail_average_luma(black_path) or 0) < 8
    assert (service._thumbnail_average_luma(white_path) or 0) > 240


def png_rgb(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    rows = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_ffmpeg_text_replaces_apostrophes_for_drawtext_quotes() -> None:
    escaped = RenderService(Settings())._ffmpeg_text("ChatGPT's GPT-4")

    assert escaped == "ChatGPT\u2019s GPT-4"
    assert "'" not in escaped


def test_preview_composition_includes_the_full_timeline() -> None:
    service = RenderService(Settings())
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")

    composition = service._scene_composition_plan(
        {
            "segments": [
                {"id": "segment-1", "start_ms": 0, "end_ms": 30_000},
                {"id": "segment-2", "start_ms": 30_000, "end_ms": 65_000},
            ]
        },
        {},
        "preview",
        preset,
    )

    assert composition["source_segment_count"] == 2
    assert composition["segment_count"] == 2


def test_seated_panel_composition_places_rear_screen_media_in_safe_region(
    tmp_path: Path,
) -> None:
    service = RenderService(Settings())
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
    episode_id = uuid4()
    primary_path = tmp_path / "seated-panel.mp4"
    wall_screen_path = tmp_path / "wall-screen.png"
    primary_path.write_bytes(b"video")
    wall_screen_path.write_bytes(png_rgb(2, 2, (10, 20, 30)))
    primary = Asset(
        episode_id=episode_id,
        asset_type=AssetType.video,
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        storage_uri="object://dialecticore/test/seated-panel.mp4",
        mime_type="video/mp4",
        status="completed",
        generation_metadata={"object_storage_path": str(primary_path)},
    )
    wall_screen = Asset(
        episode_id=episode_id,
        asset_type=AssetType.image,
        source_entity_type="primer_beat",
        source_entity_id="beat-1",
        storage_uri="object://dialecticore/test/wall-screen.png",
        mime_type="image/png",
        status="completed",
        generation_metadata={"object_storage_path": str(wall_screen_path)},
    )

    composition = service._scene_composition_plan(
        {
            "segments": [
                {
                    "id": "turn-1",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "duration_ms": 1000,
                    "video_asset_id": str(primary.id),
                    "direction": {"speaker_mouth_mode": "audio_driven_seated_panel"},
                    "visual_layers": [
                        {"asset_id": str(primary.id), "role": "video_primary"},
                        {
                            "asset_id": str(wall_screen.id),
                            "role": "wall_screen_broll",
                            "purpose": "rear_studio_display_composited",
                        },
                    ],
                }
            ]
        },
        {str(primary.id): primary, str(wall_screen.id): wall_screen},
        "preview",
        preset,
    )

    segment = composition["segment_layers"][0]
    assert composition["studio_context_segment_count"] == 1
    assert segment["layout_policy"]["name"] == "seated_panel_rear_screen_cutaway"
    assert len(segment["composited_visual_overlays"]) == 1
    assert len(segment["legacy_composited_visual_overlays"]) == 1
    wall_layer = next(
        layer for layer in segment["visual_layers"] if layer["role"] == "wall_screen_broll"
    )
    assert wall_layer["embedded_in_primary"] is False
    assert wall_layer["layout_slot"]["name"] == "seated_panel_rear_screen"
    assert wall_layer["layout_slot"]["y"] < preset.height // 3


def test_render_request_records_explicit_qualification_scope() -> None:
    request = RenderRequest(review_scope="qualification_slice")

    assert request.review_scope == "qualification_slice"
    assert RenderService._render_request_payload(request)["review_scope"] == ("qualification_slice")
    assert RenderRequest().review_scope == "full_timeline"


def test_rear_screen_insert_animates_overlay_without_black_scene_fade() -> None:
    service = RenderService(Settings())
    policy = service._transition_policy_for_segment(
        {
            "segment_type": "discussion_wall_screen_insert",
            "camera_action": "broll_insert",
        },
        5,
    )

    assert policy["name"] == "broll_insert_slide"
    assert policy["whole_scene_fade"] is False
    assert "fade=t=in" not in service._append_transition_video_filter(
        "format=yuv420p",
        policy,
        3.0,
    )
    animation = service._layer_animation_policy("wall_screen_broll", policy)
    assert animation["rendered_transform"] == "overlay_x_slide"
    assert animation["rendered_opacity_keyframes"] is True


def test_seated_panel_virtual_camera_uses_normalized_speaker_region() -> None:
    service = RenderService(Settings())
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
    episode_id = uuid4()
    scene = Asset(
        episode_id=episode_id,
        asset_type=AssetType.studio_scene,
        source_entity_type="episode",
        source_entity_id="episode:panel",
        status="completed",
        generation_metadata={
            "studio_panel": {
                "seat_map": [
                    {
                        "participant_id": "host",
                        "face_region": {
                            "x": 0.41,
                            "y": 0.18,
                            "width": 0.16,
                            "height": 0.23,
                        },
                    }
                ]
            }
        },
    )
    primary = Asset(
        episode_id=episode_id,
        asset_type=AssetType.video,
        source_entity_type="transcript_turn",
        source_entity_id="turn-host",
        status="completed",
        generation_metadata={
            "shot_plan": {
                "paired_participant_ids": [],
                "seating_plan": {"host": 3},
            }
        },
    )
    segment = {
        "id": "turn-host",
        "speaker_id": "host",
        "video_asset_id": str(primary.id),
        "studio_panel_scene_asset_id": str(scene.id),
        "camera_view": "speaker_close_up",
        "camera_action": "slow_push",
        "direction": {
            "view": "speaker_close_up",
            "action": "slow_push",
            "speaker_mouth_mode": "audio_driven_seated_panel",
        },
        "visual_layers": [{"asset_id": str(primary.id), "role": "video_primary"}],
    }

    camera = service._seated_panel_virtual_camera(
        segment=segment,
        asset_by_id={str(primary.id): primary, str(scene.id): scene},
    )
    composition = service._scene_composition_plan(
        {"segments": [{**segment, "start_ms": 0, "end_ms": 1000}]},
        {str(primary.id): primary, str(scene.id): scene},
        "preview",
        preset,
    )

    assert camera == {
        "schema_version": "dialecticore.virtual_camera.v3",
        "view": "speaker_close_up",
        "scale": 1.9968,
        "focus_x": 0.49,
        "focus_y": 0.295,
        "focus_source": "active_speaker_face_region_or_seating_plan",
        "speaker_participant_id": "host",
        "context_participant_ids": [],
        "motion": "slow_push",
        "framing_policy": {
            "speaker_target_frame_x": 0.5,
            "speaker_allowed_frame_x": [0.45, 0.55],
            "speaker_must_be_primary": True,
            "retain_head_shoulders": True,
            "retain_desk_context": True,
        },
    }
    assert "crop=w='trunc(iw/1.9968/2)*2'" in service._seated_panel_virtual_camera_filter(
        preset,
        {**camera, "motion": None},
    )
    layout_policy = composition["segment_layers"][0]["layout_policy"]
    assert layout_policy["name"] == "seated_panel_virtual_camera"
    assert layout_policy["virtual_camera"] is True


def test_seated_panel_virtual_camera_centers_speaker_not_pair_midpoint() -> None:
    service = RenderService(Settings())
    episode_id = uuid4()
    scene = Asset(
        episode_id=episode_id,
        asset_type=AssetType.studio_scene,
        source_entity_type="episode",
        source_entity_id="episode:panel",
        status="completed",
        generation_metadata={
            "studio_panel": {
                "seat_map": [
                    {
                        "participant_id": "speaker",
                        "face_region": {"x": 0.18, "y": 0.2, "width": 0.12, "height": 0.2},
                    },
                    {
                        "participant_id": "neighbor",
                        "face_region": {"x": 0.58, "y": 0.2, "width": 0.12, "height": 0.2},
                    },
                ]
            }
        },
    )
    primary = Asset(
        episode_id=episode_id,
        asset_type=AssetType.video,
        source_entity_type="transcript_turn",
        source_entity_id="turn-speaker",
        status="completed",
        generation_metadata={
            "shot_plan": {
                "paired_participant_ids": ["neighbor"],
                "seating_plan": {"speaker": 1, "neighbor": 2},
            }
        },
    )

    camera = service._seated_panel_virtual_camera(
        segment={
            "speaker_id": "speaker",
            "video_asset_id": str(primary.id),
            "studio_panel_scene_asset_id": str(scene.id),
            "direction": {
                "view": "panel_two_shot",
                "action": "cut",
                "speaker_mouth_mode": "audio_driven_seated_panel",
            },
        },
        asset_by_id={str(primary.id): primary, str(scene.id): scene},
    )

    assert camera is not None
    assert camera["focus_x"] == 0.24
    assert camera["focus_x"] != 0.44
    assert camera["speaker_participant_id"] == "speaker"
    assert camera["context_participant_ids"] == ["neighbor"]


def test_establishing_wide_fly_in_uses_animated_virtual_camera() -> None:
    service = RenderService(Settings())
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
    camera = service._seated_panel_virtual_camera(
        segment={
            "speaker_id": "host",
            "direction": {
                "view": "establishing_wide",
                "action": "fly_in",
                "speaker_mouth_mode": "audio_driven_seated_panel",
            },
        },
        asset_by_id={},
    )

    assert camera is not None
    assert camera["view"] == "establishing_wide"
    assert camera["motion"] == "fly_in"
    rendered = service._seated_panel_virtual_camera_filter(
        preset,
        camera,
        duration_seconds=5.0,
    )
    assert "zoompan=" in rendered
    assert "on/" in rendered


def test_parallel_broll_render_view_preserves_dialogue_and_source_clock() -> None:
    service = RenderService(Settings())
    timeline = {
        "duration_ms": 10_000,
        "segments": [
            {
                "id": "segment-1",
                "start_ms": 0,
                "end_ms": 10_000,
                "duration_ms": 10_000,
                "speaker_id": "host",
                "audio_asset_id": "audio-1",
                "video_asset_id": "speaker-1",
                "visual_layers": [{"role": "video_primary", "asset_id": "speaker-1"}],
                "direction": {
                    "view": "speaker_medium",
                    "action": "cut",
                    "speaker_mouth_mode": "audio_driven_seated_panel",
                },
                "graphics": [{"kind": "lower_third"}],
                "citations": [],
                "citation_overlay_asset_ids": [],
            }
        ],
        "tracks": {
            "broll_content": [
                {
                    "id": "content-1",
                    "asset_id": "broll-1",
                    "start_ms": 2_000,
                    "end_ms": 9_000,
                    "source_in_ms": 500,
                }
            ],
            "broll_presentation": [
                {
                    "id": "presentation-1",
                    "content_clip_id": "content-1",
                    "linked_segment_id": "segment-1",
                    "start_ms": 2_000,
                    "end_ms": 9_000,
                    "transition_duration_ms": 1_500,
                    "keyframes": [
                        {
                            "time_ms": 2_000,
                            "state": "rear_screen",
                            "transition_duration_ms": 1_000,
                        },
                        {
                            "time_ms": 5_000,
                            "state": "fullscreen",
                            "transition_duration_ms": 2_000,
                        },
                        {
                            "time_ms": 8_000,
                            "state": "rear_screen",
                            "transition_duration_ms": 1_250,
                        },
                    ],
                }
            ],
        },
    }

    render_view = service._timeline_render_view(timeline)

    assert timeline["segments"][0]["end_ms"] == 10_000
    assert [segment["start_ms"] for segment in render_view["segments"]] == [
        0,
        2_000,
        5_000,
        8_000,
        9_000,
    ]
    assert [segment["end_ms"] for segment in render_view["segments"]] == [
        2_000,
        5_000,
        8_000,
        9_000,
        10_000,
    ]
    assert [segment["audio_source_offset_ms"] for segment in render_view["segments"]] == [
        0,
        2_000,
        5_000,
        8_000,
        9_000,
    ]
    assert render_view["segments"][1]["broll_playback"]["source_start_ms"] == 500
    assert render_view["segments"][2]["broll_playback"]["source_start_ms"] == 3_500
    assert render_view["segments"][3]["broll_playback"]["source_start_ms"] == 6_500
    assert render_view["segments"][1]["transition_duration_ms"] == 1_000
    assert render_view["segments"][2]["transition_duration_ms"] == 2_000
    assert render_view["segments"][3]["transition_duration_ms"] == 1_250
    assert render_view["segments"][1]["broll_playback"]["state"] == "rear_screen"
    assert render_view["segments"][2]["broll_playback"]["state"] == "fullscreen"
    assert render_view["segments"][3]["broll_playback"]["state"] == "rear_screen"
    assert render_view["segments"][2]["audio_asset_id"] == "audio-1"
    assert render_view["segments"][2]["direction"]["speaker_mouth_mode"] == ("off_camera_dialogue")
    assert render_view["render_materialization"]["source_clock_preserved"] is True


def test_parallel_broll_transition_duration_reaches_render_policy() -> None:
    service = RenderService(Settings())

    policy = service._transition_policy_for_segment(
        {
            "camera_transition": "dissolve",
            "transition_duration_ms": 1_750,
        },
        1,
    )

    assert policy["name"] == "soft_dissolve"
    assert policy["duration_ms"] == 1_750

    bounded = service._transition_policy_for_segment(
        {
            "camera_transition": "dissolve",
            "transition_duration_ms": 9_000,
        },
        1,
    )

    assert bounded["duration_ms"] == 5_000


def test_scene_filter_omits_speaker_lower_third_for_off_camera_segment() -> None:
    service = RenderService(Settings())
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")

    filtergraph = service._scene_video_filter(
        SimpleNamespace(assets=[]),
        {
            "segments": [
                {
                    "id": "primer-01",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "duration_ms": 1000,
                }
            ]
        },
        preset,
        1.0,
    )

    assert "Speaker" not in filtergraph
    assert "y=ih-126" not in filtergraph


def test_scene_filter_anchors_speaker_lower_third_to_input_frame_height() -> None:
    service = RenderService(Settings())
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")

    filtergraph = service._scene_video_filter(
        SimpleNamespace(assets=[]),
        {
            "segments": [
                {
                    "id": "turn-grok",
                    "speaker_id": "grok",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "duration_ms": 1000,
                }
            ]
        },
        preset,
        1.0,
    )

    assert "drawbox=x=48:y=ih-126:w=540:h=76" in filtergraph
    assert "drawtext=text='grok':x=76:y=h-100" in filtergraph
    assert "drawbox=x=48:y=h-126" not in filtergraph


def test_timeline_qc_fails_segments_without_source_discussion_turn_links(
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
    source_discussion_turn_id = uuid4()
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language="en",
        status="approved",
        turns=[
            TranscriptTurn(
                source_discussion_turn_ids=[source_discussion_turn_id],
                speaker_participant_id="host",
                text="Welcome.",
                status="accepted",
            )
        ],
    )
    episode.transcripts.append(transcript)
    turn = transcript.turns[0]
    audio_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/audio/test.wav",
        mime_type="audio/wav",
        duration_ms=1000,
        checksum="sha256:audio",
        status="completed",
        generation_metadata={"transcript_version_id": str(transcript.id)},
    )
    video_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/visual/test.mp4",
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
        },
    )
    episode.assets.extend([audio_asset, video_asset])
    timeline = {
        "id": str(uuid4()),
        "schema_version": "episode_timeline.v1",
        "language": "en",
        "duration_ms": 1000,
        "segments": [
            {
                "id": f"segment-0001-{turn.id}",
                "start_ms": 0,
                "end_ms": 1000,
                "duration_ms": 1000,
                "speaker_id": "host",
                "source_turn_id": str(turn.id),
                "audio_asset_id": str(audio_asset.id),
                "video_asset_id": str(video_asset.id),
                "fallback_video_asset_id": str(video_asset.id),
            }
        ],
        "chapters": [],
    }
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        status="completed",
        generation_metadata={"timeline_json": timeline},
    )

    qc = TimelineService(settings)._timeline_qc(episode, transcript, timeline, timeline_asset)

    assert qc.status == "fail"
    assert qc.details["missing_source_discussion_link_segment_count"] == 1
    assert any(
        issue["issue"] == "timeline_segment_missing_source_discussion_turn_links"
        for issue in qc.details["issues"]
    )


def test_render_manifest_includes_evidence_source_lineage(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = RenderService(settings)
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    evidence_pack_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.evidence_pack,
        language="en",
        source_entity_type="episode",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/research/evidence.json",
        mime_type="application/vnd.dialecticore.evidence-pack+json",
        checksum="sha256:evidence-pack",
        status="completed",
        generation_metadata={
            "evidence_pack": {
                "id": "pack-test",
                "source_index": [
                    {
                        "id": "source-a",
                        "title": "AI Governance Report",
                        "source_type": "government_report",
                        "uri": "https://example.gov/ai-governance",
                        "confidence": 0.91,
                        "content_checksum": "sha256:source-a",
                        "score_factors": {"authority_bonus": 0.18},
                    }
                ],
            },
            "retrieval_attempt_count": 1,
            "retrieval_success_count": 1,
            "retrieval_failure_count": 0,
            "retrieval_tool_log": [{"tool": "http_get", "status": "succeeded"}],
        },
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        checksum="sha256:timeline",
        status="completed",
    )
    episode.assets.extend([evidence_pack_asset, timeline_asset])
    timeline = {
        "id": "timeline-test",
        "duration_ms": 2000,
        "segments": [
            {
                "id": "segment-1",
                "source_turn_id": "turn-1",
                "citations": [
                    {
                        "claim": "AI assistants improved review throughput.",
                        "evidence_ref": "source-a",
                        "source_turn_id": "turn-1",
                    }
                ],
            }
        ],
    }
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")

    manifest = service._render_manifest(
        episode=episode,
        timeline_asset=timeline_asset,
        timeline=timeline,
        preset=preset,
        request=RenderRequest(preset_id=preset.id),
        render_id="render-lineage",
    )

    lineage = manifest["evidence_lineage"]
    assert any(
        asset["asset_id"] == str(evidence_pack_asset.id) for asset in manifest["source_assets"]
    )
    assert lineage["schema_version"] == "evidence_lineage.v1"
    assert lineage["evidence_pack_asset_id"] == str(evidence_pack_asset.id)
    assert lineage["referenced_source_ids"] == ["source-a"]
    assert lineage["referenced_sources"][0]["title"] == "AI Governance Report"
    assert lineage["citation_links"][0]["source_uri"] == "https://example.gov/ai-governance"
    assert lineage["retrieval_tool_log_summary"]["success_count"] == 1


def test_render_manifest_records_resolved_dialogue_audio_layers(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = RenderService(settings)
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    stored_audio = service.object_store.put_bytes(
        key=f"audio/{episode.id}/segment.wav",
        payload=wav_bytes(),
        content_type="audio/wav",
    )
    audio_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        storage_uri=stored_audio.uri,
        mime_type="audio/wav",
        duration_ms=1000,
        checksum=stored_audio.checksum,
        status="completed",
        generation_metadata={"object_storage_path": str(stored_audio.path)},
    )
    stored_visual = service.object_store.put_bytes(
        key=f"visuals/{episode.id}/segment.png",
        payload=png_rgb(16, 9, (32, 112, 180)),
        content_type="image/png",
    )
    visual_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.image,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        storage_uri=stored_visual.uri,
        mime_type="image/png",
        duration_ms=1000,
        width=16,
        height=9,
        checksum=stored_visual.checksum,
        status="completed",
        generation_metadata={
            "visual_role": "video_primary",
            "render_ready": True,
            "object_storage_path": str(stored_visual.path),
        },
    )
    subtitle_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        storage_uri="mock://subtitles/en/test.vtt",
        mime_type="text/vtt",
        duration_ms=1000,
        checksum="sha256:subtitle",
        status="completed",
        generation_metadata={
            "format": "vtt",
            "subtitle_text": (
                "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\nModerator: Caption text\n"
            ),
        },
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        checksum="sha256:timeline",
        status="completed",
    )
    episode.assets.extend([audio_asset, visual_asset, subtitle_asset, timeline_asset])
    timeline = {
        "id": "timeline-test",
        "duration_ms": 1000,
        "media": {"subtitle_mode": "selectable"},
        "segments": [
            {
                "id": "segment-1",
                "start_ms": 0,
                "end_ms": 1000,
                "duration_ms": 1000,
                "speaker_id": "host",
                "source_turn_id": "turn-1",
                "audio_asset_id": str(audio_asset.id),
                "video_asset_id": str(visual_asset.id),
                "subtitle_asset_id": str(subtitle_asset.id),
                "camera_transition": "cut",
                "visual_role": "video_primary",
            }
        ],
    }
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")

    manifest = service._render_manifest(
        episode=episode,
        timeline_asset=timeline_asset,
        timeline=timeline,
        preset=preset,
        request=RenderRequest(preset_id=preset.id),
        render_id="render-audio-layers",
    )

    composition = manifest["composition"]
    assert composition["audio_mix_strategy"] == (
        "timeline_ordered_dialogue_concatenation_with_silence_gaps"
    )
    assert composition["dialogue_audio_layer_count"] == 1
    assert composition["resolved_dialogue_audio_layer_count"] == 1
    assert composition["silent_dialogue_fallback_count"] == 0
    assert composition["visual_plate_layer_count"] == 1
    assert composition["resolved_visual_plate_layer_count"] == 1
    assert composition["generated_visual_fallback_count"] == 0
    assert composition["subtitle_track_count"] == 1
    assert composition["burned_in_caption_cue_count"] == 0
    assert composition["caption_composition_strategy"] == "render_timed_selectable_vtt_sidecar"
    assert composition["segment_layers"][0]["dialogue_audio"] == {
        "asset_id": str(audio_asset.id),
        "asset_type": "audio",
        "storage_uri": stored_audio.uri,
        "mime_type": "audio/wav",
        "duration_ms": 1000,
        "resolved": True,
    }
    assert composition["segment_layers"][0]["caption_cues"][0]["text"] == (
        "Moderator: Caption text"
    )
    assert composition["segment_layers"][0]["selected_visual"]["asset_id"] == str(visual_asset.id)
    assert composition["segment_layers"][0]["selected_visual"]["resolved"] is True
    assert composition["layout_policy_names"] == ["full_frame_primary"]
    assert composition["transition_policy_names"] == ["hard_cut"]
    assert composition["animated_scene_count"] == 0
    assert composition["motion_primitive_names"] == []
    assert composition["motion_primitive_count"] == 0
    assert composition["advanced_layout_policy_count"] == 0
    assert composition["split_screen_scene_count"] == 0
    assert composition["focus_shift_scene_count"] == 1
    assert composition["cross_scene_transition_count"] == 0
    assert composition["rendered_cross_scene_xfade_count"] == 0
    assert composition["cross_scene_renderer"] == "frame_scheduled_camera_cuts"
    assert composition["rendered_layer_transform_names"] == []
    assert composition["rendered_layer_transform_count"] == 0
    assert composition["rendered_layer_opacity_keyframe_count"] == 0
    assert composition["rendered_layer_scale_keyframe_count"] == 0
    assert composition["rendered_layer_easing_curve_names"] == []
    assert composition["rendered_layer_easing_curve_count"] == 0
    assert composition["rendered_layer_mask_names"] == []
    assert composition["rendered_layer_mask_count"] == 0
    assert composition["layer_mask_renderer"] == "no_layer_masks"
    assert composition["layer_motion_renderer"] == "static_overlay_geometry"
    scene_layer = composition["segment_layers"][0]
    assert scene_layer["layout_policy"]["name"] == "full_frame_primary"
    assert scene_layer["transition_policy"]["name"] == "hard_cut"
    assert scene_layer["visual_layers"][0]["layout_slot"]["name"] == "full_frame"


def test_render_manifest_records_bespoke_motion_curves_and_geometric_masks(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = RenderService(settings)
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    visual_assets: list[Asset] = []
    for role, turn_id, color in (
        ("studio_scene", "transcript-test", (20, 30, 48)),
        ("video_primary", "turn-1", (32, 112, 180)),
        ("broll", "turn-1", (220, 180, 80)),
        ("video_primary", "turn-2", (130, 90, 220)),
    ):
        stored_visual = service.object_store.put_bytes(
            key=f"visuals/{episode.id}/{role}-{turn_id}.png",
            payload=png_rgb(16, 9, color),
            content_type="image/png",
        )
        visual_assets.append(
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.image,
                language="en",
                source_entity_type=(
                    "transcript_version" if role == "studio_scene" else "transcript_turn"
                ),
                source_entity_id=turn_id,
                storage_uri=stored_visual.uri,
                mime_type="image/png",
                duration_ms=1000,
                width=16,
                height=9,
                checksum=stored_visual.checksum,
                status="completed",
                generation_metadata={
                    "visual_role": role,
                    "render_ready": True,
                    "object_storage_path": str(stored_visual.path),
                },
            )
        )
    studio_asset, primary_asset, broll_asset, spotlight_asset = visual_assets
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        checksum="sha256:timeline",
        status="completed",
    )
    episode.assets.extend([*visual_assets, timeline_asset])
    timeline = {
        "id": "timeline-bespoke-motion",
        "duration_ms": 2000,
        "segments": [
            {
                "id": "segment-1",
                "start_ms": 0,
                "end_ms": 1000,
                "duration_ms": 1000,
                "speaker_id": "host",
                "source_turn_id": "turn-1",
                "video_asset_id": str(primary_asset.id),
                "secondary_visual_asset_id": str(broll_asset.id),
                "studio_scene_asset_id": str(studio_asset.id),
                "camera_transition": "source_reveal",
                "visual_layers": [
                    {"role": "studio_scene", "asset_id": str(studio_asset.id)},
                    {"role": "video_primary", "asset_id": str(primary_asset.id)},
                    {"role": "broll", "asset_id": str(broll_asset.id)},
                ],
            },
            {
                "id": "segment-2",
                "start_ms": 1000,
                "end_ms": 2000,
                "duration_ms": 1000,
                "speaker_id": "panelist",
                "source_turn_id": "turn-2",
                "video_asset_id": str(spotlight_asset.id),
                "studio_scene_asset_id": str(studio_asset.id),
                "camera_transition": "speaker_spotlight",
                "visual_layers": [
                    {"role": "studio_scene", "asset_id": str(studio_asset.id)},
                    {"role": "video_primary", "asset_id": str(spotlight_asset.id)},
                ],
            },
        ],
    }
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")

    manifest = service._render_manifest(
        episode=episode,
        timeline_asset=timeline_asset,
        timeline=timeline,
        preset=preset,
        request=RenderRequest(preset_id=preset.id),
        render_id="render-bespoke-motion",
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/render-bespoke-motion.mp4",
        mime_type="video/mp4",
        duration_ms=2000,
        width=preset.width,
        height=preset.height,
        fps=preset.fps,
        checksum="sha256:render",
        status="completed",
        generation_metadata={
            "render_id": "render-bespoke-motion",
            "render_type": "preview",
            "timeline_asset_id": str(timeline_asset.id),
            "render_manifest": manifest,
        },
    )

    composition = manifest["composition"]
    assert composition["transition_policy_names"] == [
        "source_reveal_arc",
        "speaker_spotlight_bounce",
    ]
    assert composition["motion_primitive_names"] == [
        "broll_arc_reveal",
        "opacity_ramp",
        "speaker_spotlight_bounce",
    ]
    assert composition["rendered_layer_transform_names"] == [
        "overlay_xy_arc_reveal",
        "overlay_xy_spotlight_bounce",
    ]
    assert composition["rendered_layer_easing_curve_names"] == [
        "ease_in_out",
        "ease_out_back",
    ]
    assert composition["rendered_layer_mask_names"] == [
        "circle_alpha",
        "diamond_alpha",
        "rounded_rect_alpha",
    ]
    assert composition["rendered_non_rectangular_mask_count"] == 2
    assert composition["layer_mask_renderer"] == "ffmpeg_alpha_geometric_masks"

    qc = service._render_qc(
        episode=episode,
        render_asset=render_asset,
        timeline=timeline,
        preset=preset,
        probe={
            "duration_ms": 2000,
            "width": preset.width,
            "height": preset.height,
            "fps": preset.fps,
            "audio_sample_rate": preset.audio_sample_rate,
            "audio_channels": 2,
        },
    )

    assert qc.status == "pass"
    assert qc.details["rendered_non_rectangular_mask_count"] == 2
    assert qc.details["rendered_layer_easing_curve_names"] == [
        "ease_in_out",
        "ease_out_back",
    ]
    assert qc.details["layer_mask_renderer"] == "ffmpeg_alpha_geometric_masks"


def test_final_render_creates_targeted_pending_approval(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = RenderService(settings)
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        checksum="sha256:timeline",
        status="completed",
        generation_metadata={
            "timeline_json": {
                "id": "timeline-test",
                "language": "en",
                "duration_ms": 1000,
                "segments": [],
            }
        },
    )
    episode.assets.append(timeline_asset)
    service._render_manifest = lambda **_kwargs: {  # type: ignore[method-assign]
        "schema_version": "render_manifest.v1",
        "id": "render-test",
    }
    service._render_media_bytes = lambda *_args: b"fake mp4"  # type: ignore[method-assign]
    service._probe_render = lambda _path: {  # type: ignore[method-assign]
        "duration_ms": 1000,
        "width": 1920,
        "height": 1080,
        "fps": 30,
    }
    service._render_qc = lambda **kwargs: QualityResult(  # type: ignore[method-assign]
        episode_id=episode.id,
        target_type="render_asset",
        target_id=str(kwargs["render_asset"].id),
        check_type="render_final_integrity",
        severity=QualitySeverity.pass_,
        status="pass",
        details={"failure_count": 0, "warning_count": 0},
    )

    blocked = None
    try:
        service.render_episode(
            episode,
            RenderRequest(
                timeline_asset_id=timeline_asset.id,
                render_type="final",
                preset_id="youtube-1080p",
                user_id="tester",
            ),
            presets=default_render_presets(),
        )
    except ValueError as exc:
        blocked = str(exc)
    assert blocked == "approved preview render is required before final rendering"

    previewed = service.render_episode(
        episode,
        RenderRequest(
            timeline_asset_id=timeline_asset.id,
            render_type="preview",
            preset_id="preview-low-bitrate",
            user_id="tester",
        ),
        presets=default_render_presets(),
    )
    preview_asset = next(
        asset
        for asset in previewed.assets
        if asset.asset_type == AssetType.render
        and asset.generation_metadata["render_type"] == "preview"
    )
    preview_approval = next(
        approval for approval in previewed.approvals if approval.stage == "preview_render_review"
    )
    assert preview_approval.decision == "pending"
    assert preview_approval.target_id == str(preview_asset.id)
    assert preview_asset.generation_metadata["approval_status"] == "pending"
    assert previewed.audit_events[-1].event_type == "approval.required"

    blocked = None
    try:
        service.render_episode(
            episode,
            RenderRequest(
                timeline_asset_id=timeline_asset.id,
                render_type="final",
                preset_id="youtube-1080p",
                user_id="tester",
            ),
            presets=default_render_presets(),
        )
    except ValueError as exc:
        blocked = str(exc)
    assert blocked == "approved preview render is required before final rendering"

    preview_approval.decision = "approved"
    preview_asset.generation_metadata["approval_status"] = "approved"
    rendered = service.render_episode(
        episode,
        RenderRequest(
            timeline_asset_id=timeline_asset.id,
            render_type="final",
            preset_id="youtube-1080p",
            user_id="tester",
        ),
        presets=default_render_presets(),
    )

    render_asset = next(
        asset
        for asset in rendered.assets
        if asset.asset_type == AssetType.render
        and asset.generation_metadata["render_type"] == "final"
    )
    approval = next(
        approval for approval in rendered.approvals if approval.stage == "final_render_review"
    )
    assert approval.decision == "pending"
    assert approval.target_type == "render_asset"
    assert approval.target_id == str(render_asset.id)
    assert render_asset.generation_metadata["approval_status"] == "pending"
    assert render_asset.generation_metadata["approval_id"] == str(approval.id)
    assert rendered.audit_events[-1].event_type == "approval.required"
    assert rendered.audit_events[-1].details["render_asset_id"] == str(render_asset.id)


def test_primer_timeline_renders_do_not_create_talkshow_approvals(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = RenderService(settings)
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="primer_production",
        source_entity_id="primer-test",
        status="completed",
        generation_metadata={
            "timeline_json": {
                "id": "primer-timeline-test",
                "language": "en",
                "duration_ms": 1000,
                "segments": [],
            }
        },
    )
    episode.assets.append(timeline_asset)
    service._render_manifest = lambda **_kwargs: {  # type: ignore[method-assign]
        "schema_version": "render_manifest.v1",
        "id": "primer-render-test",
    }
    service._render_media_bytes = lambda *_args: b"fake mp4"  # type: ignore[method-assign]
    service._probe_render = lambda _path: {  # type: ignore[method-assign]
        "duration_ms": 1000,
        "width": 1920,
        "height": 1080,
        "fps": 30,
    }
    service._render_qc = lambda **kwargs: QualityResult(  # type: ignore[method-assign]
        episode_id=episode.id,
        target_type="render_asset",
        target_id=str(kwargs["render_asset"].id),
        check_type="render_integrity",
        severity=QualitySeverity.pass_,
        status="pass",
        details={"failure_count": 0, "warning_count": 0},
    )

    previewed = service.render_episode(
        episode,
        RenderRequest(
            timeline_asset_id=timeline_asset.id,
            render_type="preview",
            preset_id="preview-low-bitrate",
            user_id="tester",
        ),
        presets=default_render_presets(),
    )
    rendered = service.render_episode(
        previewed,
        RenderRequest(
            timeline_asset_id=timeline_asset.id,
            render_type="final",
            preset_id="youtube-1080p",
            user_id="tester",
        ),
        presets=default_render_presets(),
    )

    assert rendered.approvals == []
    assert all(
        "approval_status" not in asset.generation_metadata
        for asset in rendered.assets
        if asset.asset_type == AssetType.render
    )


def test_final_render_qc_fails_when_runtime_exceeds_episode_bounds() -> None:
    service = RenderService(Settings())
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.target_duration_seconds = 3
    episode.minimum_duration_seconds = 2
    episode.maximum_duration_seconds = 4
    preset = next(item for item in default_render_presets() if item.id == "youtube-1080p")
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        checksum="sha256:timeline",
        status="completed",
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        duration_ms=5000,
        width=preset.width,
        height=preset.height,
        fps=preset.fps,
        checksum="sha256:render",
        status="completed",
        generation_metadata={
            "render_id": "render-runtime",
            "render_type": "final",
            "timeline_asset_id": str(timeline_asset.id),
            "render_manifest": {"schema_version": "render_manifest.v1"},
        },
    )

    qc = service._render_qc(
        episode=episode,
        render_asset=render_asset,
        timeline={"id": "timeline-test", "duration_ms": 5000, "segments": []},
        preset=preset,
        probe={
            "duration_ms": 5000,
            "width": preset.width,
            "height": preset.height,
            "fps": preset.fps,
            "audio_sample_rate": preset.audio_sample_rate,
            "audio_channels": 2,
        },
    )

    assert qc.status == "fail"
    assert qc.details["target_duration_ms"] == 3000
    assert qc.details["minimum_duration_ms"] == 2000
    assert qc.details["maximum_duration_ms"] == 4000
    assert qc.details["final_runtime_within_episode_bounds"] is False
    assert "final_render_runtime_above_maximum" in {
        issue["issue"] for issue in qc.details["issues"]
    }


def test_final_render_qc_allows_configured_primer_outside_discussion_maximum() -> None:
    service = RenderService(Settings())
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.target_duration_seconds = 3
    episode.minimum_duration_seconds = 2
    episode.maximum_duration_seconds = 4
    preset = next(item for item in default_render_presets() if item.id == "youtube-1080p")
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        checksum="sha256:timeline",
        status="completed",
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/final-with-primer.mp4",
        mime_type="video/mp4",
        duration_ms=6_000,
        width=preset.width,
        height=preset.height,
        fps=preset.fps,
        checksum="sha256:render-with-primer",
        status="completed",
        generation_metadata={
            "render_id": "render-runtime-with-primer",
            "render_type": "final",
            "timeline_asset_id": str(timeline_asset.id),
            "render_manifest": {"schema_version": "render_manifest.v1"},
        },
    )

    qc = service._render_qc(
        episode=episode,
        render_asset=render_asset,
        timeline={
            "id": "timeline-test",
            "duration_ms": 6_000,
            "segments": [
                {"segment_type": "topic_primer", "duration_ms": 3_000},
                {"segment_type": "discussion", "duration_ms": 3_000},
            ],
        },
        preset=preset,
        probe={
            "duration_ms": 6_000,
            "width": preset.width,
            "height": preset.height,
            "fps": preset.fps,
            "audio_sample_rate": preset.audio_sample_rate,
            "audio_channels": 2,
        },
    )

    assert "final_render_runtime_above_maximum" not in {
        issue["issue"] for issue in qc.details["issues"]
    }
    assert qc.details["maximum_duration_ms"] == 7_000
    assert qc.details["discussion_maximum_duration_ms"] == 4_000
    assert qc.details["topic_primer_duration_ms"] == 3_000
    assert qc.details["final_runtime_within_episode_bounds"] is True


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_render_service_creates_preview_render_with_manifest_and_qc(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type=TranscriptType.broadcast,
        language="en",
        status="approved",
        turns=[
            TranscriptTurn(
                source_discussion_turn_ids=[],
                speaker_participant_id="host",
                text="Welcome to the preview render.",
                claims=[
                    Claim(
                        text="AI assistants improved review throughput.",
                        claim_type="supported",
                        confidence=0.8,
                        evidence_refs=["source-a"],
                    )
                ],
            ),
            TranscriptTurn(
                source_discussion_turn_ids=[],
                speaker_participant_id="optimist",
                text="This segment proves the render pipeline.",
            ),
        ],
    )
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    evidence_pack_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.evidence_pack,
        language="en",
        source_entity_type="episode",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/research/evidence.json",
        mime_type="application/vnd.dialecticore.evidence-pack+json",
        checksum="sha256:evidence-pack",
        status="completed",
        generation_metadata={
            "evidence_pack": {
                "id": "pack-render-test",
                "source_index": [
                    {
                        "id": "source-a",
                        "title": "AI Governance Report",
                        "source_type": "government_report",
                        "uri": "https://example.gov/ai-governance",
                        "confidence": 0.91,
                    }
                ],
            },
        },
    )
    subtitle_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        storage_uri="mock://subtitles/en/test.vtt",
        mime_type="text/vtt",
        duration_ms=2000,
        checksum="sha256:subtitle",
        status="completed",
        generation_metadata={
            "format": "vtt",
            "transcript_version_id": str(transcript.id),
            "subtitle_text": (
                "WEBVTT\n\n"
                "1\n"
                "00:00:00.000 --> 00:00:01.000\n"
                "Moderator: Welcome to the preview render.\n\n"
                "2\n"
                "00:00:01.000 --> 00:00:02.000\n"
                "The Optimist: This segment proves the render pipeline.\n"
            ),
        },
    )
    timeline_service = TimelineService(settings)
    stored_citation_card = timeline_service.object_store.put_bytes(
        key=f"visuals/{episode.id}/{transcript.turns[0].id}.citation.svg",
        payload=(
            b'<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080">'
            b'<rect width="1920" height="1080" fill="#082f49"/>'
            b'<text x="120" y="160" fill="#ffffff">Evidence</text>'
            b"</svg>"
        ),
        content_type="image/svg+xml",
    )
    citation_card_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.citation_card,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(transcript.turns[0].id),
        storage_uri=stored_citation_card.uri,
        mime_type=stored_citation_card.content_type,
        width=1920,
        height=1080,
        checksum=stored_citation_card.checksum,
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "citation_overlay",
            "render_ready": True,
            "object_storage_path": str(stored_citation_card.path),
        },
    )
    stored_studio_scene = timeline_service.object_store.put_bytes(
        key=f"visuals/{episode.id}/studio.png",
        payload=png_rgb(32, 18, (18, 32, 64)),
        content_type="image/png",
    )
    studio_scene_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        language="en",
        source_entity_type="episode",
        source_entity_id=str(episode.id),
        storage_uri=stored_studio_scene.uri,
        mime_type="image/png",
        duration_ms=2000,
        width=32,
        height=18,
        checksum=stored_studio_scene.checksum,
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "studio_scene",
            "render_ready": True,
            "object_storage_path": str(stored_studio_scene.path),
        },
    )
    episode.assets.extend(
        [evidence_pack_asset, subtitle_asset, citation_card_asset, studio_scene_asset]
    )
    for index, turn in enumerate(transcript.turns, start=1):
        stored_audio = timeline_service.object_store.put_bytes(
            key=f"audio/{episode.id}/{turn.id}.wav",
            payload=wav_bytes(),
            content_type="audio/wav",
        )
        stored_visual = timeline_service.object_store.put_bytes(
            key=f"visuals/{episode.id}/{turn.id}.png",
            payload=png_rgb(
                32,
                18,
                (32 + index * 40, 96 + index * 20, 176 - index * 16),
            ),
            content_type="image/png",
        )
        stored_reaction = timeline_service.object_store.put_bytes(
            key=f"visuals/{episode.id}/{turn.speaker_participant_id}.reaction.png",
            payload=png_rgb(24, 18, (140, 48 + index * 36, 78)),
            content_type="image/png",
        )
        extra_visual_assets = [
            Asset(
                episode_id=episode.id,
                asset_type=AssetType.reaction_loop,
                language="en",
                source_entity_type="participant_profile",
                source_entity_id=turn.speaker_participant_id,
                storage_uri=stored_reaction.uri,
                mime_type="image/png",
                duration_ms=1000,
                width=24,
                height=18,
                checksum=stored_reaction.checksum,
                status="completed",
                generation_metadata={
                    "transcript_version_id": str(transcript.id),
                    "visual_role": "reaction_loop",
                    "render_ready": True,
                    "object_storage_path": str(stored_reaction.path),
                },
            )
        ]
        if index == 1:
            stored_broll = timeline_service.object_store.put_bytes(
                key=f"visuals/{episode.id}/{turn.id}.broll.png",
                payload=png_rgb(32, 18, (180, 128, 32)),
                content_type="image/png",
            )
            extra_visual_assets.append(
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.broll,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id=str(turn.id),
                    storage_uri=stored_broll.uri,
                    mime_type="image/png",
                    duration_ms=1000,
                    width=32,
                    height=18,
                    checksum=stored_broll.checksum,
                    status="completed",
                    generation_metadata={
                        "transcript_version_id": str(transcript.id),
                        "visual_role": "broll",
                        "render_ready": True,
                        "object_storage_path": str(stored_broll.path),
                    },
                )
            )
        episode.assets.extend(
            [
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.audio,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id=str(turn.id),
                    storage_uri=stored_audio.uri,
                    mime_type="audio/wav",
                    duration_ms=1000,
                    checksum=stored_audio.checksum,
                    status="completed",
                    generation_metadata={
                        "transcript_version_id": str(transcript.id),
                        "object_storage_path": str(stored_audio.path),
                    },
                ),
                Asset(
                    episode_id=episode.id,
                    asset_type=AssetType.image,
                    language="en",
                    source_entity_type="transcript_turn",
                    source_entity_id=str(turn.id),
                    storage_uri=stored_visual.uri,
                    mime_type="image/png",
                    duration_ms=1000,
                    width=32,
                    height=18,
                    checksum=stored_visual.checksum,
                    status="completed",
                    generation_metadata={
                        "transcript_version_id": str(transcript.id),
                        "visual_role": "video_primary",
                        "render_ready": True,
                        "object_storage_path": str(stored_visual.path),
                        "shot_plan": {
                            "camera_transition": (
                                "broll_insert" if index == 1 else "reaction_cutaway"
                            )
                        },
                    },
                ),
                *extra_visual_assets,
            ]
        )
    timeline_episode = timeline_service.build_timeline(
        episode,
        TimelineBuildRequest(transcript_version_id=transcript.id, user_id="tester"),
    )
    timeline_asset = next(
        asset for asset in timeline_episode.assets if asset.asset_type == AssetType.timeline
    )

    rendered = RenderService(settings).render_episode(
        timeline_episode,
        RenderRequest(
            timeline_asset_id=timeline_asset.id,
            preset_id="preview-low-bitrate",
            user_id="tester",
        ),
        presets=default_render_presets(),
    )

    render_asset = next(asset for asset in rendered.assets if asset.asset_type == AssetType.render)
    render_qc = [
        result
        for result in rendered.quality_results
        if result.check_type == "render_preview_integrity"
    ][-1]
    assert render_asset.status == "completed"
    assert render_asset.mime_type == "video/mp4"
    assert render_asset.checksum and render_asset.checksum.startswith("sha256:")
    assert render_asset.width == 1280
    assert render_asset.height == 720
    assert render_asset.duration_ms is not None
    assert Path(render_asset.generation_metadata["object_storage_path"]).exists()
    assert Path(render_asset.generation_metadata["render_manifest_path"]).exists()
    assert (
        render_asset.generation_metadata["render_manifest"]["normalization"]["audio"]["sample_rate"]
        == 48_000
    )
    assert render_asset.generation_metadata["render_manifest"]["source_assets"]
    source_asset = render_asset.generation_metadata["render_manifest"]["source_assets"][0]
    assert {"source_entity_type", "source_entity_id", "status", "render_ready"} <= set(source_asset)
    composition = render_asset.generation_metadata["render_manifest"]["composition"]
    assert composition["mode"] == "timeline_scene_composite_preview"
    assert composition["segment_count"] == 2
    assert composition["audio_mix_strategy"] == (
        "timeline_ordered_dialogue_concatenation_with_silence_gaps"
    )
    assert composition["dialogue_audio_layer_count"] == 2
    assert composition["resolved_dialogue_audio_layer_count"] == 2
    assert composition["silent_dialogue_fallback_count"] == 0
    assert composition["visual_plate_layer_count"] == 2
    assert composition["resolved_visual_plate_layer_count"] == 2
    assert composition["generated_visual_fallback_count"] == 0
    # The direction policy uses reaction cutaways selectively rather than
    # compositing every available reaction asset into each scene.
    assert composition["composited_visual_overlay_layer_count"] == 3
    assert composition["layout_policy_names"] == ["studio_speaker_medium"]
    assert composition["transition_policy_names"] == [
        "broll_insert_slide",
        "reaction_cutaway_snap",
    ]
    assert composition["animated_scene_count"] == 2
    assert composition["motion_primitive_names"] == [
        "broll_slide_in",
        "opacity_ramp",
        "reaction_focus_pop",
    ]
    assert composition["motion_primitive_count"] == 9
    assert composition["advanced_layout_policy_count"] == 2
    assert composition["split_screen_scene_count"] == 0
    assert composition["focus_shift_scene_count"] == 2
    assert composition["cross_scene_transition_count"] == 2
    assert composition["rendered_cross_scene_xfade_count"] == 0
    assert composition["cross_scene_renderer"] == "frame_scheduled_camera_cuts"
    assert "overlay_x_slide" in composition["rendered_layer_transform_names"]
    assert composition["rendered_layer_transform_count"] >= 1
    assert composition["rendered_layer_opacity_keyframe_count"] >= 1
    assert composition["rendered_layer_easing_curve_names"] == ["ease_out"]
    assert composition["rendered_layer_easing_curve_count"] >= 1
    assert composition["rendered_layer_mask_names"] == ["rounded_rect_alpha"]
    assert composition["rendered_layer_mask_count"] >= 1
    assert composition["layer_mask_renderer"] == "ffmpeg_alpha_rounded_rect_masks"
    assert composition["layer_motion_renderer"] == (
        "ffmpeg_overlay_position_scale_opacity_eased_keyframes"
    )
    first_scene = composition["segment_layers"][0]
    assert first_scene["layout_policy"]["name"] == "studio_speaker_medium"
    assert first_scene["layout_policy"]["screen_mode"] == "speaker_with_studio_context"
    assert first_scene["layout_policy"]["focus_role"] == "video_primary"
    assert first_scene["transition_policy"]["name"] == "broll_insert_slide"
    assert first_scene["transition_policy"]["cross_scene"] is True
    assert [layer["layout_slot"]["name"] for layer in first_scene["visual_layers"]] == [
        "full_frame",
        "speaker_medium_focus",
        "broll_picture_in_picture",
    ]
    broll_layer = next(layer for layer in first_scene["visual_layers"] if layer["role"] == "broll")
    assert broll_layer["animation"]["name"] == "slide_in_from_right"
    assert broll_layer["animation"]["motion_primitive"] == "broll_slide_in"
    assert not any(
        primitive.get("name") == "split_screen_focus_layout"
        for primitive in first_scene["motion_primitives"]
    )
    assert composition["subtitle_track_count"] == 2
    assert composition["burned_in_caption_cue_count"] == 0
    assert composition["citation_overlay_asset_count"] == 0
    assert composition["resolved_citation_overlay_asset_count"] == 0
    assert composition["composited_citation_overlay_count"] == 0
    assert composition["segment_layers"][0]["dialogue_audio"]["resolved"] is True
    assert render_qc.status == "pass"
    assert render_qc.details["composition_policy"] == "studio_camera_cuts.v1"
    assert render_qc.details["timing_tolerance_ms"] == 42
    assert abs(
        int(render_qc.details["duration_ms"] or 0)
        - int(render_qc.details["expected_duration_ms"] or 0)
    ) <= int(render_qc.details["timing_tolerance_ms"] or 0)
    assert int(render_qc.details["av_offset_ms"] or 0) <= int(
        render_qc.details["timing_tolerance_ms"] or 0
    )
    assert render_qc.details["source_asset_count"] >= 6
    assert render_qc.details["stale_source_asset_count"] == 0
    assert render_qc.details["missing_source_asset_count"] == 0
    assert render_qc.details["composition_mode"] == "timeline_scene_composite_preview"
    assert render_qc.details["composition_segment_count"] == 2
    assert render_qc.details["caption_track_mode"] == "selectable"
    assert render_qc.details["caption_track_asset_id"]
    assert render_qc.details["resolved_dialogue_audio_layer_count"] == 2
    assert render_qc.details["silent_dialogue_fallback_count"] == 0
    assert render_qc.details["resolved_visual_plate_layer_count"] == 2
    assert render_qc.details["generated_visual_fallback_count"] == 0
    assert render_qc.details["composited_visual_overlay_layer_count"] == 3
    assert render_qc.details["layout_policy_names"] == ["studio_speaker_medium"]
    assert render_qc.details["transition_policy_names"] == [
        "broll_insert_slide",
        "reaction_cutaway_snap",
    ]
    assert render_qc.details["animated_scene_count"] == 2
    assert render_qc.details["motion_primitive_names"] == [
        "broll_slide_in",
        "opacity_ramp",
        "reaction_focus_pop",
    ]
    assert render_qc.details["motion_primitive_count"] == 9
    assert render_qc.details["advanced_layout_policy_count"] == 2
    assert render_qc.details["split_screen_scene_count"] == 0
    assert render_qc.details["focus_shift_scene_count"] == 2
    assert render_qc.details["cross_scene_transition_count"] == 2
    assert render_qc.details["rendered_cross_scene_xfade_count"] == 0
    assert render_qc.details["cross_scene_renderer"] == "frame_scheduled_camera_cuts"
    assert "overlay_x_slide" in render_qc.details["rendered_layer_transform_names"]
    assert render_qc.details["rendered_layer_transform_count"] >= 1
    assert render_qc.details["rendered_layer_opacity_keyframe_count"] >= 1
    assert render_qc.details["rendered_layer_easing_curve_names"] == ["ease_out"]
    assert render_qc.details["rendered_layer_easing_curve_count"] >= 1
    assert render_qc.details["rendered_layer_mask_names"] == ["rounded_rect_alpha"]
    assert render_qc.details["rendered_layer_mask_count"] >= 1
    assert render_qc.details["layer_mask_renderer"] == "ffmpeg_alpha_rounded_rect_masks"
    assert render_qc.details["layer_motion_renderer"] == (
        "ffmpeg_overlay_position_scale_opacity_eased_keyframes"
    )
    assert render_qc.details["subtitle_track_count"] == 2
    assert render_qc.details["burned_in_caption_cue_count"] == 0
    assert render_qc.details["citation_overlay_asset_count"] == 0
    assert render_qc.details["composited_citation_overlay_count"] == 0
    assert "render.preview.completed" in [
        audit_event.event_type for audit_event in rendered.audit_events
    ]


def test_render_qc_fails_when_manifest_source_asset_is_stale(tmp_path: Path) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "objects")))
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        storage_uri="object://dialecticore/timelines/timeline.json",
        mime_type="application/vnd.dialecticore.timeline+json",
        checksum="sha256:timeline-original",
        status="completed",
    )
    episode.assets.append(timeline_asset)
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/preview.mp4",
        mime_type="video/mp4",
        duration_ms=1000,
        width=1280,
        height=720,
        fps=24,
        checksum="sha256:render",
        status="completed",
        generation_metadata={
            "render_id": "render-stale-source",
            "render_type": "preview",
            "preset_id": "preview-low-bitrate",
            "timeline_asset_id": str(timeline_asset.id),
            "render_manifest": {
                "schema_version": "render_manifest.v1",
                "source_assets": [
                    {
                        "asset_id": str(timeline_asset.id),
                        "asset_type": "timeline",
                        "source_entity_type": "transcript_version",
                        "source_entity_id": "transcript-test",
                        "status": "completed",
                        "storage_uri": timeline_asset.storage_uri,
                        "mime_type": timeline_asset.mime_type,
                        "duration_ms": timeline_asset.duration_ms,
                        "width": timeline_asset.width,
                        "height": timeline_asset.height,
                        "fps": timeline_asset.fps,
                        "checksum": "sha256:timeline-original",
                    }
                ],
                "composition": {"mode": "timeline_scene_composite_preview"},
            },
        },
    )
    episode.assets.append(render_asset)
    timeline_asset.checksum = "sha256:timeline-mutated"
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")

    qc = service._render_qc(
        episode=episode,
        render_asset=render_asset,
        timeline={"id": "timeline-test", "duration_ms": 1000, "segments": []},
        preset=preset,
        probe={
            "duration_ms": 1000,
            "width": preset.width,
            "height": preset.height,
            "fps": preset.fps,
            "audio_sample_rate": preset.audio_sample_rate,
            "audio_channels": 2,
        },
    )

    issues = [
        issue for issue in qc.details["issues"] if issue["issue"] == "render_source_asset_stale"
    ]
    assert qc.status == "fail"
    assert qc.details["stale_source_asset_count"] == 1
    assert qc.details["missing_source_asset_count"] == 0
    assert issues[0]["asset_id"] == str(timeline_asset.id)
    assert "checksum_changed" in issues[0]["mismatch_reasons"]


def test_render_qc_fails_when_manifest_source_asset_is_missing(tmp_path: Path) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "objects")))
    episode = EpisodeRepository().create(EpisodeCreateRequest(definition=definition()))
    missing_asset_id = str(uuid4())
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=missing_asset_id,
        storage_uri="object://dialecticore/renders/preview.mp4",
        mime_type="video/mp4",
        duration_ms=1000,
        width=1280,
        height=720,
        fps=24,
        checksum="sha256:render",
        status="completed",
        generation_metadata={
            "render_id": "render-missing-source",
            "render_type": "preview",
            "preset_id": "preview-low-bitrate",
            "timeline_asset_id": missing_asset_id,
            "render_manifest": {
                "schema_version": "render_manifest.v1",
                "source_assets": [
                    {
                        "asset_id": missing_asset_id,
                        "asset_type": "timeline",
                        "source_entity_type": "transcript_version",
                        "source_entity_id": "transcript-test",
                        "status": "completed",
                        "storage_uri": "object://dialecticore/timelines/missing.json",
                        "mime_type": "application/vnd.dialecticore.timeline+json",
                        "checksum": "sha256:timeline-missing",
                    }
                ],
                "composition": {"mode": "timeline_scene_composite_preview"},
            },
        },
    )
    episode.assets.append(render_asset)
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")

    qc = service._render_qc(
        episode=episode,
        render_asset=render_asset,
        timeline={"id": "timeline-test", "duration_ms": 1000, "segments": []},
        preset=preset,
        probe={
            "duration_ms": 1000,
            "width": preset.width,
            "height": preset.height,
            "fps": preset.fps,
            "audio_sample_rate": preset.audio_sample_rate,
            "audio_channels": 2,
        },
    )

    issues = [
        issue for issue in qc.details["issues"] if issue["issue"] == "render_source_asset_missing"
    ]
    assert qc.status == "fail"
    assert qc.details["stale_source_asset_count"] == 0
    assert qc.details["missing_source_asset_count"] == 1
    assert issues[0]["asset_id"] == missing_asset_id


def test_render_service_reencodes_frame_scheduled_camera_cut_pieces(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "objects")))
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
    first = tmp_path / "first.mp4"
    second = tmp_path / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        commands.append(command)
        Path(command[-1]).write_bytes(b"camera-cut-video")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    output = service._concat_video_pieces(
        pieces=[
            {
                "path": first,
                "duration_seconds": 1.0,
                "transition_policy": None,
            },
            {
                "path": second,
                "duration_seconds": 1.18,
                "transition_policy": service._transition_policy_for_segment(
                    {"camera_transition": "reaction_cutaway"},
                    2,
                ),
            },
        ],
        directory=tmp_path,
        ffmpeg="/usr/bin/ffmpeg",
        preset=preset,
    )

    filter_complex = commands[0][commands[0].index("-filter_complex") + 1]
    assert output == tmp_path / "visual-plate.mp4"
    assert output.read_bytes() == b"camera-cut-video"
    assert "[0:v]setpts=PTS-STARTPTS,fps=24,format=yuv420p[v0]" in filter_complex
    assert "[1:v]setpts=PTS-STARTPTS,fps=24,format=yuv420p[v1]" in filter_complex
    assert "concat=n=2:v=1:a=0" in filter_complex
    assert "xfade=" not in filter_complex


def test_render_service_uses_ffmpeg_overlay_expression_for_layer_motion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "objects")))
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
    base = tmp_path / "base.png"
    overlay = tmp_path / "overlay.png"
    base.write_bytes(b"base")
    overlay.write_bytes(b"overlay")
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        commands.append(command)
        Path(command[-1]).write_bytes(b"layered-video")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    transition = service._transition_policy_for_segment(
        {"camera_transition": "broll_insert"},
        1,
    )

    output = service._layered_visual_piece_path(
        layers=[
            {
                "role": "studio_scene",
                "path": base,
                "asset": SimpleNamespace(mime_type="image/png"),
            },
            {
                "role": "broll",
                "path": overlay,
                "asset": SimpleNamespace(mime_type="image/png"),
                "layout_slot": {
                    "x": 816,
                    "y": 132,
                    "width": 416,
                    "height": 201,
                    "mask": {
                        "name": "rounded_rect_alpha",
                        "radius_px": 24,
                        "rendered_mask": "ffmpeg_alpha_mask",
                    },
                },
                "animation": service._layer_animation_policy("broll", transition),
            },
        ],
        directory=tmp_path,
        filename="layered.mp4",
        duration_seconds=1.0,
        preset=preset,
        ffmpeg="/usr/bin/ffmpeg",
        transition_policy=transition,
    )

    filter_complex = commands[0][commands[0].index("-filter_complex") + 1]
    assert output == tmp_path / "layered.mp4"
    assert output.read_bytes() == b"layered-video"
    assert (
        "overlay=x='if(lt(t,0.240),1280+(-464)*(1-pow(1-(t/0.240),3)),816)':"
        "y=132:eval=frame:format=auto"
    ) in filter_complex
    assert (
        "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        "a='if(lt(X,24)*lt(Y,24)*gt(pow(24-X,2)+pow(24-Y,2),576),0,"
    ) in filter_complex
    assert "fade=t=in:st=0:d=0.240:alpha=1" in filter_complex


def test_render_service_uses_ffmpeg_scale_and_opacity_keyframes_for_focus_motion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "objects")))
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
    base = tmp_path / "base.png"
    overlay = tmp_path / "overlay.png"
    base.write_bytes(b"base")
    overlay.write_bytes(b"overlay")
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        commands.append(command)
        Path(command[-1]).write_bytes(b"layered-video")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    transition = service._transition_policy_for_segment(
        {"camera_transition": "reaction_cutaway"},
        1,
    )

    service._layered_visual_piece_path(
        layers=[
            {
                "role": "studio_scene",
                "path": base,
                "asset": SimpleNamespace(mime_type="image/png"),
            },
            {
                "role": "reaction_loop",
                "path": overlay,
                "asset": SimpleNamespace(mime_type="image/png"),
                "layout_slot": {
                    "x": 691,
                    "y": 115,
                    "width": 512,
                    "height": 374,
                },
                "animation": service._layer_animation_policy("reaction_loop", transition),
            },
        ],
        directory=tmp_path,
        filename="layered.mp4",
        duration_seconds=1.0,
        preset=preset,
        ffmpeg="/usr/bin/ffmpeg",
        transition_policy=transition,
    )

    filter_complex = commands[0][commands[0].index("-filter_complex") + 1]
    assert (
        "zoompan=z='if(lt(on,5),1.040+(-0.040)*(1-pow(1-(on/5),3)),1.000)':d=1:s=512x374:fps=24"
    ) in filter_complex
    assert (
        "overlay=x='if(lt(t,0.180),701+(-10)*(1-pow(1-(t/0.180),3)),691)':"
        "y='if(lt(t,0.180),122+(-7)*(1-pow(1-(t/0.180),3)),115)':"
        "eval=frame:format=auto"
    ) in filter_complex
    assert "fade=t=in:st=0:d=0.180:alpha=1" in filter_complex


def test_render_service_uses_arc_curve_and_diamond_mask_for_source_reveal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "objects")))
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
    base = tmp_path / "base.png"
    overlay = tmp_path / "overlay.png"
    base.write_bytes(b"base")
    overlay.write_bytes(b"overlay")
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        commands.append(command)
        Path(command[-1]).write_bytes(b"layered-video")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    transition = service._transition_policy_for_segment(
        {"camera_transition": "source_reveal"},
        1,
    )
    animation = service._layer_animation_policy("broll", transition)
    layout_slot = service._layout_slot_with_animation_mask(
        {
            "x": 760,
            "y": 180,
            "width": 420,
            "height": 220,
            "mask": {
                "name": "rounded_rect_alpha",
                "radius_px": 24,
                "rendered_mask": "ffmpeg_alpha_mask",
            },
        },
        animation,
    )

    service._layered_visual_piece_path(
        layers=[
            {
                "role": "studio_scene",
                "path": base,
                "asset": SimpleNamespace(mime_type="image/png"),
            },
            {
                "role": "broll",
                "path": overlay,
                "asset": SimpleNamespace(mime_type="image/png"),
                "layout_slot": layout_slot,
                "animation": animation,
            },
        ],
        directory=tmp_path,
        filename="layered.mp4",
        duration_seconds=1.0,
        preset=preset,
        ffmpeg="/usr/bin/ffmpeg",
        transition_policy=transition,
    )

    filter_complex = commands[0][commands[0].index("-filter_complex") + 1]
    assert transition["name"] == "source_reveal_arc"
    assert animation["rendered_transform"] == "overlay_xy_arc_reveal"
    assert layout_slot["mask"]["name"] == "diamond_alpha"
    assert "zoompan=z='if(lt(on,11),1.080+(-0.080)*(if(lt(on/11,0.5)" in filter_complex
    assert "overlay=x='if(lt(t,0.420),852+(-92)*(if(lt(t/0.420,0.5)" in filter_complex
    assert "sin(3.14159*(if(lt(t/0.420,0.5)" in filter_complex
    assert "abs(X-W/2)/(W/2)+abs(Y-H/2)/(H/2)" in filter_complex
    assert "fade=t=in:st=0:d=0.420:alpha=1" in filter_complex


def test_render_service_uses_back_curve_and_circle_mask_for_speaker_spotlight(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "objects")))
    preset = next(item for item in default_render_presets() if item.id == "preview-low-bitrate")
    base = tmp_path / "base.png"
    overlay = tmp_path / "overlay.png"
    base.write_bytes(b"base")
    overlay.write_bytes(b"overlay")
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        commands.append(command)
        Path(command[-1]).write_bytes(b"layered-video")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    transition = service._transition_policy_for_segment(
        {"camera_transition": "speaker_spotlight"},
        1,
    )
    animation = service._layer_animation_policy("video_primary", transition)
    layout_slot = service._layout_slot_with_animation_mask(
        {
            "x": 691,
            "y": 115,
            "width": 512,
            "height": 374,
            "mask": {
                "name": "rounded_rect_alpha",
                "radius_px": 24,
                "rendered_mask": "ffmpeg_alpha_mask",
            },
        },
        animation,
    )

    service._layered_visual_piece_path(
        layers=[
            {
                "role": "studio_scene",
                "path": base,
                "asset": SimpleNamespace(mime_type="image/png"),
            },
            {
                "role": "video_primary",
                "path": overlay,
                "asset": SimpleNamespace(mime_type="image/png"),
                "layout_slot": layout_slot,
                "animation": animation,
            },
        ],
        directory=tmp_path,
        filename="layered.mp4",
        duration_seconds=1.0,
        preset=preset,
        ffmpeg="/usr/bin/ffmpeg",
        transition_policy=transition,
    )

    filter_complex = commands[0][commands[0].index("-filter_complex") + 1]
    assert transition["name"] == "speaker_spotlight_bounce"
    assert animation["easing"] == "ease_out_back"
    assert layout_slot["mask"]["name"] == "circle_alpha"
    assert "zoompan=z='if(lt(on,9),1.120+(-0.120)*(1+2.70158*pow((on/9)-1,3)" in filter_complex
    assert "overlay=x='if(lt(t,0.360),711+(-20)*(1+2.70158*pow((t/0.360)-1,3)" in filter_complex
    assert "pow(X-256.000,2)+pow(Y-187.000,2)" in filter_complex
    assert "fade=t=in:st=0:d=0.360:alpha=1" in filter_complex


def test_render_service_exports_youtube_package_from_stored_assets(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = RenderService(settings)
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    episode.model_endpoints[0].capabilities.update(
        {
            "safe_context_window": 128000,
            "apiKey": "leaked-endpoint-api-key",
            "nested": {"clientSecret": "leaked-endpoint-secret"},
        }
    )
    episode.transcripts.append(
        TranscriptVersion(
            episode_id=episode.id,
            type=TranscriptType.broadcast,
            language="en",
            status="approved",
            turns=[
                TranscriptTurn(
                    source_discussion_turn_ids=[],
                    speaker_participant_id="host",
                    text="Opening",
                    status="accepted",
                )
            ],
        )
    )
    primary_visual_asset_id = "00000000-0000-0000-0000-00000000a001"
    reaction_loop_asset_id = "00000000-0000-0000-0000-00000000a002"
    studio_scene_asset_id = "00000000-0000-0000-0000-00000000a003"
    timeline_payload = {
        "id": "timeline-test",
        "schema_version": "episode_timeline.v1",
        "language": "en",
        "duration_ms": 2000,
        "segments": [
            {
                "id": "segment-1",
                "start_ms": 0,
                "end_ms": 2000,
                "speaker_id": "host",
                "source_turn_id": "turn-1",
                "video_asset_id": primary_visual_asset_id,
                "reaction_visual_asset_id": reaction_loop_asset_id,
                "studio_scene_asset_id": studio_scene_asset_id,
                "evidence_refs": ["source-a"],
                "visual_layers": [
                    {"role": "video_primary", "asset_id": primary_visual_asset_id},
                    {"role": "reaction_loop", "asset_id": reaction_loop_asset_id},
                    {"role": "studio_scene", "asset_id": studio_scene_asset_id},
                ],
            }
        ],
        "chapters": [{"title": "Opening", "start_ms": 0, "source_turn_id": "turn-1"}],
    }
    stored_timeline = service.object_store.put_bytes(
        key=f"timelines/{episode.id}/timeline-test.json",
        payload=json.dumps(timeline_payload).encode("utf-8"),
        content_type="application/vnd.dialecticore.timeline+json",
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        storage_uri=stored_timeline.uri,
        mime_type=stored_timeline.content_type,
        duration_ms=2000,
        checksum=stored_timeline.checksum,
        status="completed",
        generation_metadata={
            "timeline_json": timeline_payload,
            "object_storage_path": str(stored_timeline.path),
        },
    )
    primary_visual_asset = Asset(
        id=primary_visual_asset_id,
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id="turn-1",
        storage_uri="mock://video/primary.mp4",
        mime_type="video/mp4",
        duration_ms=2000,
        checksum="sha256:primary",
        status="completed",
        generation_metadata={
            "transcript_version_id": "transcript-test",
            "visual_role": "video_primary",
            "shot_plan": {
                "reusable_reaction_asset_id": reaction_loop_asset_id,
                "studio_scene_asset_id": studio_scene_asset_id,
            },
        },
    )
    reaction_loop_asset = Asset(
        id=reaction_loop_asset_id,
        episode_id=episode.id,
        asset_type=AssetType.reaction_loop,
        language="en",
        source_entity_type="participant_profile",
        source_entity_id="host",
        storage_uri="mock://video/reaction.mp4",
        mime_type="video/mp4",
        duration_ms=2000,
        checksum="sha256:reaction",
        status="completed",
        generation_metadata={
            "transcript_version_id": "transcript-test",
            "visual_role": "reaction_loop",
            "render_ready": True,
        },
    )
    studio_scene_asset = Asset(
        id=studio_scene_asset_id,
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        language="en",
        source_entity_type="episode",
        source_entity_id=str(episode.id),
        storage_uri="mock://video/studio.mp4",
        mime_type="video/mp4",
        duration_ms=2000,
        checksum="sha256:studio",
        status="completed",
        generation_metadata={
            "transcript_version_id": "transcript-test",
            "visual_role": "studio_scene",
            "render_ready": True,
        },
    )
    subtitle_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        storage_uri="mock://subtitles/en/test.vtt",
        mime_type="text/vtt",
        duration_ms=2000,
        checksum="sha256:subtitle",
        status="completed",
        generation_metadata={
            "format": "vtt",
            "subtitle_text": "WEBVTT\n\n00:00.000 --> 00:02.000\nOpening\n",
            "manual_replacement": True,
            "replacement_of_asset_id": "subtitle-original",
            "user_id": "editor-1",
            "comment": "Corrected subtitle timing.",
        },
    )
    stored_render = service.object_store.put_bytes(
        key=f"renders/{episode.id}/final-test.mp4",
        payload=b"fake mp4 package payload",
        content_type="video/mp4",
    )
    render_manifest = {
        "id": "render-test",
        "schema_version": "render_manifest.v1",
        "source_assets": [
            {"asset_id": str(timeline_asset.id), "asset_type": AssetType.timeline.value},
            {"asset_id": str(subtitle_asset.id), "asset_type": AssetType.subtitle.value},
        ],
        "evidence_lineage": {
            "schema_version": "evidence_lineage.v1",
            "evidence_pack_asset_id": "evidence-pack-test",
            "evidence_pack_id": "pack-test",
            "evidence_pack_checksum": "sha256:evidence-pack",
            "citation_count": 1,
            "referenced_source_ids": ["source-a"],
            "referenced_sources": [
                {
                    "source_id": "source-a",
                    "title": "AI Governance Report",
                    "source_type": "government_report",
                    "uri": "https://example.gov/ai-governance",
                    "confidence": 0.91,
                }
            ],
            "unresolved_source_ids": [],
            "citation_links": [
                {
                    "segment_id": "segment-1",
                    "source_turn_id": "turn-1",
                    "claim": "AI assistants improved review throughput.",
                    "evidence_ref": "source-a",
                    "source_title": "AI Governance Report",
                    "source_uri": "https://example.gov/ai-governance",
                }
            ],
            "retrieval_tool_log_summary": {
                "attempt_count": 1,
                "success_count": 1,
                "failure_count": 0,
                "entries": [],
            },
        },
    }
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri=stored_render.uri,
        mime_type=stored_render.content_type,
        duration_ms=2000,
        width=1920,
        height=1080,
        fps=30,
        checksum=stored_render.checksum,
        status="completed",
        generation_metadata={
            "render_id": "render-test",
            "render_type": "final",
            "preset_id": "youtube-1080p",
            "timeline_asset_id": str(timeline_asset.id),
            "object_storage_path": str(stored_render.path),
            "render_manifest": render_manifest,
            "provider_response": {
                "accessToken": "leaked-asset-token",
                "nested": {"clientSecret": "leaked-asset-secret"},
            },
        },
    )
    stored_thumbnail = service.object_store.put_bytes(
        key=f"thumbnails/{episode.id}/thumbnail-test.jpg",
        payload=b"fake jpg package payload",
        content_type="image/jpeg",
    )
    thumbnail_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.thumbnail,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri=stored_thumbnail.uri,
        mime_type=stored_thumbnail.content_type,
        width=1920,
        height=1080,
        checksum=stored_thumbnail.checksum,
        status="completed",
        generation_metadata={
            "thumbnail_id": "thumbnail-test",
            "render_asset_id": str(render_asset.id),
            "object_storage_path": str(stored_thumbnail.path),
        },
    )
    episode.assets.extend(
        [
            timeline_asset,
            primary_visual_asset,
            reaction_loop_asset,
            studio_scene_asset,
            subtitle_asset,
            render_asset,
            thumbnail_asset,
        ]
    )

    with pytest.raises(ValueError, match="final render must be approved"):
        service.export_youtube_package(
            episode,
            YouTubeExportRequest(user_id="tester"),
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

    exported = service.export_youtube_package(
        episode,
        YouTubeExportRequest(user_id="tester"),
    )

    package_asset = next(
        asset for asset in exported.assets if asset.asset_type == AssetType.export_package
    )
    package_qc = [
        result
        for result in exported.quality_results
        if result.check_type == "youtube_package_integrity"
    ][-1]
    package_path = Path(package_asset.generation_metadata["object_storage_path"])
    assert package_asset.status == "completed"
    assert package_qc.status == "pass"
    assert package_qc.details["included_file_count"] == 4
    assert package_qc.details["subtitle_file_count"] == 1
    assert package_qc.details["chapter_count"] == 1
    assert package_qc.details["evidence_citation_count"] == 1
    assert package_qc.details["evidence_source_count"] == 1
    assert exported.audit_events[-2].event_type == "youtube.package.exported"
    with zipfile.ZipFile(package_path) as archive:
        assert set(archive.namelist()) == {
            "youtube-package.json",
            "video/render.mp4",
            "thumbnail/thumbnail.jpg",
            "subtitles/en.vtt",
        }
        manifest = json.loads(archive.read("youtube-package.json"))
    assert manifest["render_asset_id"] == str(render_asset.id)
    assert manifest["thumbnail_asset_id"] == str(thumbnail_asset.id)
    assert manifest["chapters"][0]["start_time"] == "00:00"
    assert manifest["evidence_lineage"]["referenced_source_ids"] == ["source-a"]
    exported.audit_events.append(
        AuditEvent(
            episode_id=exported.id,
            event_type="asset.replaced",
            actor="editor-1",
            details={
                "asset_id": str(subtitle_asset.id),
                "reason": "Manual subtitle correction.",
                "api_key": "leaked-manual-edit-api-key",
                "nested": {"clientSecret": "leaked-manual-edit-client-secret"},
            },
        )
    )
    exported.publish_jobs.append(
        PublishJob(
            episode_id=exported.id,
            publisher_target_id="http-live",
            platform="youtube",
            package_asset_id=package_asset.id,
            status="completed",
            dry_run=False,
            remote_job_id="remote-1",
            publish_url="https://publisher.test/watch/remote-1",
            delivery_payload={
                "schema_version": "publish_delivery_payload.v1",
                "title": "Packaged Episode",
                "authorization": "Bearer leaked-delivery-token",
                "nested": {
                    "api_key": "leaked-delivery-api-key",
                    "apiKey": "leaked-delivery-camel-api-key",
                },
            },
            result_metadata={
                "status_code": 202,
                "response": {
                    "access_token": "leaked-result-token",
                    "accessToken": "leaked-result-camel-token",
                    "nested": {
                        "client_secret": "leaked-result-secret",
                        "clientSecret": "leaked-result-camel-secret",
                    },
                },
            },
        )
    )
    exported.quality_results.append(
        QualityResult(
            episode_id=exported.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="operator_review_evidence",
            severity=QualitySeverity.pass_,
            status="pass",
            score=1.0,
            details={
                "reviewed": True,
                "api_key": "leaked-qc-api-key",
                "nested": {"accessToken": "leaked-qc-access-token"},
            },
        )
    )

    manifested = service.generate_production_manifest(
        exported,
        ProductionManifestRequest(user_id="tester"),
    )

    manifest_asset = [
        asset for asset in manifested.assets if asset.asset_type == AssetType.production_manifest
    ][-1]
    production_manifest = manifest_asset.generation_metadata["production_manifest"]
    manifest_assets_by_id = {asset["asset_id"]: asset for asset in production_manifest["assets"]}
    render_manifest_entry = manifest_assets_by_id[str(render_asset.id)]
    subtitle_manifest_entry = manifest_assets_by_id[str(subtitle_asset.id)]
    endpoint_manifest_entry = production_manifest["model_endpoints"][0]
    quality_results_by_check = {
        result["check_type"]: result for result in production_manifest["quality_results"]
    }
    assert manifest_asset.status == "completed"
    assert manifest_asset.mime_type == "application/vnd.dialecticore.production-manifest+json"
    assert manifest_asset.source_entity_type == "export_package"
    assert manifest_asset.source_entity_id == str(package_asset.id)
    assert manifest_asset.generation_metadata["asset_count"] == len(production_manifest["assets"])
    assert manifest_asset.generation_metadata["timeline_segment_count"] == 1
    assert production_manifest["schema_version"] == "production_manifest.v1"
    assert production_manifest["timeline"]["asset_id"] == str(timeline_asset.id)
    assert production_manifest["timeline"]["chapter_count"] == 1
    manifest_transcript = production_manifest["transcripts"][0]
    assert manifest_transcript["localization_metadata"] == {}
    assert manifest_transcript["turn_lineage"][0]["source_discussion_turn_ids"] == []
    assert production_manifest["timeline"]["chapters"][0] == {
        "title": "Opening",
        "start_ms": 0,
        "start_time": "00:00",
        "source_turn_id": "turn-1",
        "segment_id": None,
    }
    assert production_manifest["timeline_segments"][0]["source_turn_id"] == "turn-1"
    assert (
        production_manifest["timeline_segments"][0]["reaction_visual_asset_id"]
        == reaction_loop_asset_id
    )
    assert (
        production_manifest["timeline_segments"][0]["studio_scene_asset_id"]
        == studio_scene_asset_id
    )
    assert production_manifest["talkshow_visuals"] == {
        "schema_version": "talkshow_visual_handoff.v1",
        "reaction_loop": {
            "expected_segment_count": 1,
            "linked_segment_count": 1,
            "missing_segment_ids": [],
            "asset_ids": [reaction_loop_asset_id],
            "ready": True,
        },
        "studio_scene": {
            "expected_segment_count": 1,
            "linked_segment_count": 1,
            "missing_segment_ids": [],
            "asset_ids": [studio_scene_asset_id],
            "ready": True,
        },
        "ready": True,
    }
    assert production_manifest["render"]["manifest"]["schema_version"] == "render_manifest.v1"
    assert production_manifest["delivery_package"]["manifest"]["schema_version"] == (
        "youtube_package.v1"
    )
    assert endpoint_manifest_entry["capabilities"]["safe_context_window"] == 128000
    assert endpoint_manifest_entry["capabilities"]["apiKey"] == "[redacted]"
    assert endpoint_manifest_entry["capabilities"]["nested"]["clientSecret"] == "[redacted]"
    assert (
        quality_results_by_check["operator_review_evidence"]["details"]["api_key"] == "[redacted]"
    )
    assert (
        quality_results_by_check["operator_review_evidence"]["details"]["nested"]["accessToken"]
        == "[redacted]"
    )
    assert production_manifest["publish_jobs"][0]["delivery_payload"]["authorization"] == (
        "[redacted]"
    )
    assert (
        production_manifest["publish_jobs"][0]["delivery_payload"]["nested"]["api_key"]
        == "[redacted]"
    )
    assert (
        production_manifest["publish_jobs"][0]["delivery_payload"]["nested"]["apiKey"]
        == "[redacted]"
    )
    assert (
        production_manifest["publish_jobs"][0]["result_metadata"]["response"]["access_token"]
        == "[redacted]"
    )
    assert (
        production_manifest["publish_jobs"][0]["result_metadata"]["response"]["accessToken"]
        == "[redacted]"
    )
    assert (
        production_manifest["publish_jobs"][0]["result_metadata"]["response"]["nested"][
            "client_secret"
        ]
        == "[redacted]"
    )
    assert (
        production_manifest["publish_jobs"][0]["result_metadata"]["response"]["nested"][
            "clientSecret"
        ]
        == "[redacted]"
    )
    assert "leaked-" not in json.dumps(
        production_manifest["publish_jobs"],
        sort_keys=True,
    )
    assert "leaked-endpoint-" not in json.dumps(
        production_manifest["model_endpoints"],
        sort_keys=True,
    )
    assert "leaked-qc-" not in json.dumps(
        production_manifest["quality_results"],
        sort_keys=True,
    )
    assert production_manifest["evidence_lineage"]["referenced_source_ids"] == ["source-a"]
    assert render_manifest_entry["created_at"] == render_asset.created_at.isoformat()
    assert render_manifest_entry["updated_at"] == render_asset.updated_at.isoformat()
    assert render_manifest_entry["source_turn_id"] is None
    assert render_manifest_entry["source_evidence_refs"] == ["source-a"]
    assert render_manifest_entry["generation_metadata"]["provider_response"]["accessToken"] == (
        "[redacted]"
    )
    assert (
        render_manifest_entry["generation_metadata"]["provider_response"]["nested"]["clientSecret"]
        == "[redacted]"
    )
    assert "leaked-asset-token" not in json.dumps(render_manifest_entry, sort_keys=True)
    assert "leaked-asset-secret" not in json.dumps(render_manifest_entry, sort_keys=True)
    assert render_manifest_entry["approval_state"]["decision"] == "approved"
    assert render_manifest_entry["approval_state"]["user_id"] == "tester"
    assert render_manifest_entry["retry_history"] == {
        "generation_attempt_count": 0,
        "sync_attempt_count": 0,
        "cancellation_attempt_count": 0,
        "failure": None,
        "failed_at": None,
        "last_sync_error": None,
        "last_synced_at": None,
        "ready_for_retry": False,
    }
    assert render_manifest_entry["reproducibility"]["workflow_version"] == "render_manifest.v1"
    assert subtitle_manifest_entry["manual_edits"][0] == {
        "edit_type": "manual_replacement",
        "asset_id": str(subtitle_asset.id),
        "replacement_of_asset_id": "subtitle-original",
        "replaced_by_asset_id": None,
        "user_id": "editor-1",
        "comment": "Corrected subtitle timing.",
    }
    audit_manual_edit = subtitle_manifest_entry["manual_edits"][1]
    assert audit_manual_edit["edit_type"] == "asset.replaced"
    assert audit_manual_edit["actor"] == "editor-1"
    assert audit_manual_edit["details"]["asset_id"] == str(subtitle_asset.id)
    assert audit_manual_edit["details"]["reason"] == "Manual subtitle correction."
    assert audit_manual_edit["details"]["api_key"] == "[redacted]"
    assert audit_manual_edit["details"]["nested"]["clientSecret"] == "[redacted]"
    assert "leaked-manual-edit" not in json.dumps(audit_manual_edit, sort_keys=True)
    assert {
        AssetType.render.value,
        AssetType.export_package.value,
        AssetType.timeline.value,
        AssetType.reaction_loop.value,
        AssetType.studio_scene.value,
    } <= {asset["asset_type"] for asset in production_manifest["assets"]}
    manifest_path = Path(manifest_asset.generation_metadata["object_storage_path"])
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == production_manifest
    assert manifested.audit_events[-1].event_type == "production.manifest.created"
    assert manifested.audit_events[-1].details["timeline_segment_count"] == 1

    with pytest.raises(ValueError, match="production manifest already exists"):
        service.generate_production_manifest(
            manifested,
            ProductionManifestRequest(user_id="tester"),
        )

    regenerated = service.generate_production_manifest(
        manifested,
        ProductionManifestRequest(user_id="tester", regenerate=True),
    )
    manifest_assets = [
        asset for asset in regenerated.assets if asset.asset_type == AssetType.production_manifest
    ]
    assert [asset.status for asset in manifest_assets] == ["replaced", "completed"]
    assert "production_manifest" not in manifest_assets[0].generation_metadata
    assert manifest_assets[0].generation_metadata["production_manifest_summary"] == {
        "id": production_manifest["id"],
        "schema_version": "production_manifest.v1",
        "created_at": production_manifest["created_at"],
        "package_asset_id": str(package_asset.id),
        "stored_immutably": True,
    }
    regenerated_manifest = manifest_assets[-1].generation_metadata["production_manifest"]
    assert AssetType.production_manifest.value not in {
        asset["asset_type"] for asset in regenerated_manifest["assets"]
    }
    assert regenerated_manifest["production_manifest_history"] == [
        {
            "asset_id": str(manifest_assets[0].id),
            "status": "replaced",
            "package_asset_id": str(package_asset.id),
            "storage_uri": manifest_assets[0].storage_uri,
            "checksum": manifest_assets[0].checksum,
            "created_at": manifest_assets[0].created_at.isoformat(),
        }
    ]
    assert str(manifest_assets[0].id) not in json.dumps(regenerated_manifest["assets"])


def test_youtube_package_qc_fails_when_final_package_omits_required_delivery_media(
    tmp_path: Path,
) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "object-store")))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    subtitle_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        storage_uri="object://dialecticore/subtitles/en.vtt",
        mime_type="text/vtt",
        checksum="sha256:subtitle",
        status="completed",
        generation_metadata={"format": "vtt"},
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id="timeline-test",
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:render",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "render_id": "render-final",
            "timeline_asset_id": "timeline-test",
            "render_manifest": {
                "schema_version": "render_manifest.v1",
                "source_assets": [
                    {
                        "asset_id": str(subtitle_asset.id),
                        "asset_type": AssetType.subtitle.value,
                    }
                ],
            },
        },
    )
    thumbnail_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.thumbnail,
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/thumbnails/final.jpg",
        mime_type="image/jpeg",
        checksum="sha256:thumbnail",
        status="completed",
        generation_metadata={"render_asset_id": str(render_asset.id)},
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/final.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
        generation_metadata={
            "package_id": "pkg-final",
            "included_files": ["youtube-package.json", "video/render.mp4"],
            "youtube_package_manifest": {"schema_version": "youtube_package.v1"},
        },
    )
    episode.assets.extend([subtitle_asset, render_asset, thumbnail_asset, package_asset])

    qc = service._youtube_package_qc(
        episode=episode,
        package_asset=package_asset,
        render_asset=render_asset,
        thumbnail_asset=thumbnail_asset,
        included_files=["youtube-package.json", "video/render.mp4"],
    )

    issues = {issue["issue"]: issue for issue in qc.details["issues"]}
    assert qc.status == "fail"
    assert qc.details["failure_count"] == 2
    assert qc.details["warning_count"] == 0
    assert qc.details["thumbnail_required"] is True
    assert qc.details["required_subtitle_asset_count"] == 1
    assert issues["youtube_package_missing_required_thumbnail"]["severity"] == "fail"
    assert issues["youtube_package_missing_required_subtitles"]["severity"] == "fail"
    assert issues["youtube_package_missing_required_subtitles"]["required_subtitle_asset_ids"] == [
        str(subtitle_asset.id)
    ]


def test_youtube_package_follows_explicit_selectable_caption_track() -> None:
    service = RenderService(Settings())
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    subtitle_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.subtitle,
        language="de",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        storage_uri="object://dialecticore/subtitles/de.vtt",
        mime_type="text/vtt",
        checksum="sha256:subtitle",
        status="completed",
        generation_metadata={"format": "vtt"},
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="de",
        source_entity_type="timeline_asset",
        source_entity_id="timeline-test",
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:render",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "caption_track_asset_id": str(subtitle_asset.id),
            "caption_track_mode": "selectable",
        },
    )
    episode.assets.extend([subtitle_asset, render_asset])

    assert service._subtitle_assets_for_package(episode, render_asset) == [
        subtitle_asset
    ]
    assert service._subtitle_entries(episode, render_asset) == [
        {
            "asset_id": str(subtitle_asset.id),
            "language": "de",
            "format": "vtt",
            "storage_uri": subtitle_asset.storage_uri,
            "checksum": subtitle_asset.checksum,
        }
    ]


def test_youtube_package_qc_fails_when_package_omits_timeline_chapters(
    tmp_path: Path,
) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "object-store")))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        storage_uri="object://dialecticore/timelines/chaptered.json",
        mime_type="application/vnd.dialecticore.timeline+json",
        checksum="sha256:timeline",
        status="completed",
        generation_metadata={
            "timeline_json": {
                "schema_version": "episode_timeline.v1",
                "chapters": [
                    {
                        "title": "Opening",
                        "start_ms": 0,
                        "source_turn_id": "turn-1",
                    }
                ],
            }
        },
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:render",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "render_id": "render-final",
            "timeline_asset_id": str(timeline_asset.id),
            "render_manifest": {"schema_version": "render_manifest.v1", "source_assets": []},
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/final.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
        generation_metadata={
            "package_id": "pkg-final",
            "included_files": ["youtube-package.json", "video/render.mp4"],
            "youtube_package_manifest": {
                "schema_version": "youtube_package.v1",
                "chapters": [],
            },
        },
    )
    episode.assets.extend([timeline_asset, render_asset, package_asset])

    qc = service._youtube_package_qc(
        episode=episode,
        package_asset=package_asset,
        render_asset=render_asset,
        thumbnail_asset=None,
        included_files=["youtube-package.json", "video/render.mp4"],
    )

    issues = {issue["issue"]: issue for issue in qc.details["issues"]}
    assert qc.status == "fail"
    assert qc.details["chapter_count"] == 0
    assert issues["youtube_package_missing_required_chapters"] == {
        "severity": "fail",
        "issue": "youtube_package_missing_required_chapters",
        "required_chapter_count": 1,
        "package_chapter_count": 0,
    }


def test_youtube_package_qc_warns_when_no_thumbnail_or_subtitles_exist(
    tmp_path: Path,
) -> None:
    service = RenderService(Settings(object_storage_local_path=str(tmp_path / "object-store")))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id="timeline-test",
        storage_uri="object://dialecticore/renders/final.mp4",
        mime_type="video/mp4",
        checksum="sha256:render",
        status="completed",
        generation_metadata={
            "render_type": "final",
            "render_id": "render-final",
            "timeline_asset_id": "timeline-test",
            "render_manifest": {"schema_version": "render_manifest.v1", "source_assets": []},
        },
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri="object://dialecticore/exports/final.zip",
        mime_type="application/zip",
        checksum="sha256:package",
        status="completed",
        generation_metadata={
            "package_id": "pkg-final",
            "included_files": ["youtube-package.json", "video/render.mp4"],
            "youtube_package_manifest": {"schema_version": "youtube_package.v1"},
        },
    )
    episode.assets.extend([render_asset, package_asset])

    qc = service._youtube_package_qc(
        episode=episode,
        package_asset=package_asset,
        render_asset=render_asset,
        thumbnail_asset=None,
        included_files=["youtube-package.json", "video/render.mp4"],
    )

    issues = {issue["issue"]: issue for issue in qc.details["issues"]}
    assert qc.status == "warning"
    assert qc.details["failure_count"] == 0
    assert qc.details["warning_count"] == 2
    assert qc.details["thumbnail_required"] is False
    assert qc.details["required_subtitle_asset_count"] == 0
    assert issues["youtube_package_missing_thumbnail_asset"]["severity"] == "warning"
    assert issues["youtube_package_missing_subtitles"]["severity"] == "warning"


def test_production_manifest_requires_non_failing_package_qc(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    service = RenderService(settings)
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    timeline_payload = {
        "id": "timeline-qc-gate",
        "schema_version": "episode_timeline.v1",
        "language": "en",
        "duration_ms": 1000,
        "segments": [
            {
                "id": "segment-1",
                "start_ms": 0,
                "end_ms": 1000,
                "speaker_id": "host",
                "source_turn_id": "turn-1",
                "evidence_refs": [],
            }
        ],
        "chapters": [],
    }
    stored_timeline = service.object_store.put_bytes(
        key=f"timelines/{episode.id}/timeline-qc-gate.json",
        payload=json.dumps(timeline_payload).encode("utf-8"),
        content_type="application/vnd.dialecticore.timeline+json",
    )
    timeline_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.timeline,
        language="en",
        source_entity_type="transcript_version",
        source_entity_id="transcript-test",
        storage_uri=stored_timeline.uri,
        mime_type=stored_timeline.content_type,
        duration_ms=1000,
        checksum=stored_timeline.checksum,
        status="completed",
        generation_metadata={
            "timeline_json": timeline_payload,
            "object_storage_path": str(stored_timeline.path),
        },
    )
    stored_render = service.object_store.put_bytes(
        key=f"renders/{episode.id}/final-qc-gate.mp4",
        payload=b"fake mp4 package payload",
        content_type="video/mp4",
    )
    render_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="timeline_asset",
        source_entity_id=str(timeline_asset.id),
        storage_uri=stored_render.uri,
        mime_type=stored_render.content_type,
        duration_ms=1000,
        checksum=stored_render.checksum,
        status="completed",
        generation_metadata={
            "render_type": "final",
            "timeline_asset_id": str(timeline_asset.id),
            "object_storage_path": str(stored_render.path),
            "render_manifest": {"schema_version": "render_manifest.v1"},
        },
    )
    stored_package = service.object_store.put_bytes(
        key=f"exports/{episode.id}/youtube-qc-gate.zip",
        payload=b"fake zip package payload",
        content_type="application/zip",
    )
    package_asset = Asset(
        episode_id=episode.id,
        asset_type=AssetType.export_package,
        language="en",
        source_entity_type="render_asset",
        source_entity_id=str(render_asset.id),
        storage_uri=stored_package.uri,
        mime_type=stored_package.content_type,
        checksum=stored_package.checksum,
        status="completed",
        generation_metadata={
            "package_id": "youtube-qc-gate",
            "object_storage_path": str(stored_package.path),
            "included_files": ["youtube-package.json", "video/render.mp4"],
            "youtube_package_manifest": {"schema_version": "youtube_package.v1"},
        },
    )
    episode.assets.extend([timeline_asset, render_asset, package_asset])

    with pytest.raises(ValueError, match="package QC is required"):
        service.generate_production_manifest(
            episode,
            ProductionManifestRequest(user_id="tester"),
        )

    episode.quality_results.append(
        QualityResult(
            episode_id=episode.id,
            target_type="export_package_asset",
            target_id=str(package_asset.id),
            check_type="youtube_package_integrity",
            severity=QualitySeverity.fail,
            status="fail",
            score=0.0,
            details={"issue": "youtube_package_missing_video_file"},
        )
    )
    with pytest.raises(ValueError, match="failing YouTube package QC blocks"):
        service.generate_production_manifest(
            episode,
            ProductionManifestRequest(user_id="tester"),
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
            details={"issue_count": 0},
        )
    )

    manifested = service.generate_production_manifest(
        episode,
        ProductionManifestRequest(user_id="tester"),
    )

    manifest_asset = [
        asset for asset in manifested.assets if asset.asset_type == AssetType.production_manifest
    ][-1]
    assert manifest_asset.status == "completed"
    assert manifest_asset.source_entity_id == str(package_asset.id)
