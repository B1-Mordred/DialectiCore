from pathlib import Path

import pytest
from app.core.config import Settings
from app.domain.defaults import default_model_endpoints, default_participants
from app.domain.enums import AssetType, TurnType
from app.domain.schemas import (
    Asset,
    EpisodeCreateRequest,
    TimelineBuildRequest,
    TimelineUpdateRequest,
    TranscriptTurn,
    TranscriptVersion,
)
from app.infrastructure.repository import EpisodeRepository
from app.services.discussion_engine import DiscussionEngine
from app.services.model_gateway import ModelGateway
from app.services.timeline_service import TimelineService
from tests.test_discussion_engine import definition


def approve_canonical_transcript(produced) -> None:
    transcript = next(
        transcript
        for transcript in produced.transcripts
        if transcript.id == produced.canonical_transcript_version_id
    )
    transcript.status = "approved"
    for turn in transcript.turns:
        if turn.status != "excluded":
            turn.status = "accepted"


def test_timeline_uses_completed_primer_render_before_discussion(
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
    episode.definition.media.scene_reference_image_uri = (
        "object://dialecticore/show-media/scene-reference-images/studio.png"
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=[],
        speaker_participant_id="chatgpt",
        turn_type=TurnType.post_primer_bridge,
        text="The discussion begins after the topic primer.",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id

    discussion_audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/audio/discussion.wav",
        mime_type="audio/wav",
        duration_ms=3_000,
        checksum="sha256:discussion-audio",
        status="completed",
        generation_metadata={"transcript_version_id": str(transcript.id)},
    )
    discussion_video = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/video/discussion.mp4",
        mime_type="video/mp4",
        duration_ms=3_000,
        checksum="sha256:discussion-video",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "video_primary",
            "render_ready": True,
        },
    )
    raw_primer_source = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="episode_opening",
        source_entity_id=str(episode.id),
        storage_uri="object://dialecticore/primer/raw-source.mp4",
        mime_type="video/mp4",
        duration_ms=10_000,
        checksum="sha256:raw-primer-source",
        status="completed",
        generation_metadata={"opening_media": True, "opening_media_role": "topic_visual"},
    )
    primer_render = Asset(
        episode_id=episode.id,
        asset_type=AssetType.render,
        language="en",
        source_entity_type="primer_timeline",
        source_entity_id="primer-timeline-1",
        storage_uri="object://dialecticore/renders/primer.mp4",
        mime_type="video/mp4",
        duration_ms=60_000,
        checksum="sha256:primer-render",
        status="completed",
        generation_metadata={
            "primer": True,
            "render_scope": "primer",
            "render_ready": True,
        },
    )
    episode.assets.extend([discussion_audio, discussion_video, raw_primer_source, primer_render])
    episode.workflow_control["primer_production"] = {
        "status": "completed",
        "render_asset_id": str(primer_render.id),
    }

    timeline = TimelineService(settings)._compose_timeline(episode, transcript, [turn])

    assert timeline["duration_ms"] == 63_000
    assert timeline["program_structure"]["primer"] == {
        "included": True,
        "render_asset_id": str(primer_render.id),
        "duration_ms": 60_000,
    }
    assert timeline["segments"][0]["segment_type"] == "topic_primer"
    assert timeline["segments"][0]["video_asset_id"] == str(primer_render.id)
    assert timeline["segments"][0]["audio_asset_id"] == str(primer_render.id)
    assert timeline["segments"][1]["segment_type"] == "post_primer_host_bridge"
    assert timeline["segments"][1]["video_asset_id"] == str(discussion_video.id)
    assert timeline["segments"][1]["video_asset_id"] != str(raw_primer_source.id)
    assert timeline["segments"][1]["studio_reference_image_uri"] == (
        "object://dialecticore/show-media/scene-reference-images/studio.png"
    )
    assert timeline["segments"][1]["visual_layers"][0] == {
        "role": "studio_scene",
        "asset_id": None,
        "storage_uri": "object://dialecticore/show-media/scene-reference-images/studio.png",
        "asset_type": "image",
        "purpose": "configured_studio_reference",
        "reference_only": True,
    }
    assert timeline["tracks"]["video_primary"][:2] == [
        timeline["segments"][0]["id"],
        timeline["segments"][1]["id"],
    ]


def test_seated_panel_timeline_cuts_to_composited_rear_screen_media(
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
    episode.definition.media.directing.studio_layout = "seated_panel"
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=[],
        speaker_participant_id="chatgpt",
        turn_type=TurnType.opening_position,
        text="A seated speaker introduces the discussion.",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode.transcripts.append(transcript)
    episode.canonical_transcript_version_id = transcript.id
    audio = Asset(
        episode_id=episode.id,
        asset_type=AssetType.audio,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/audio/turn.wav",
        mime_type="audio/wav",
        duration_ms=9_000,
        checksum="sha256:audio",
        status="completed",
        generation_metadata={"transcript_version_id": str(transcript.id)},
    )
    panel = Asset(
        episode_id=episode.id,
        asset_type=AssetType.studio_scene,
        language="en",
        source_entity_type="episode",
        source_entity_id=f"{episode.id}:panel:speaker_medium:chatgpt:solo",
        storage_uri="object://dialecticore/visuals/panel.png",
        mime_type="image/png",
        width=512,
        height=288,
        checksum="sha256:panel",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "studio_panel_keyframe",
            "render_ready": True,
        },
    )
    wall_screen = Asset(
        episode_id=episode.id,
        asset_type=AssetType.broll,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/visuals/wall-screen.png",
        mime_type="image/png",
        width=512,
        height=288,
        checksum="sha256:wall",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "wall_screen_broll",
            "render_ready": True,
        },
    )
    video = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri="object://dialecticore/visuals/seated.mp4",
        mime_type="video/mp4",
        duration_ms=9_000,
        width=512,
        height=288,
        fps=12,
        checksum="sha256:video",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "video_primary",
            "render_ready": True,
            "shot_plan": {
                "studio_panel_scene_asset_id": str(panel.id),
                "camera_view": "speaker_medium",
                "camera_action": "cut",
                "speaker_mouth_mode": "audio_driven_seated_panel",
                "requires": {"studio_panel_scene": True},
            },
        },
    )
    episode.assets.extend([audio, panel, wall_screen, video])

    timeline = TimelineService(settings)._compose_timeline(episode, transcript, [turn])

    assert len(timeline["segments"]) == 1
    segment = timeline["segments"][0]
    assert timeline["media"]["composition_policy"] == "seated_studio_panel.v1"
    assert timeline["schema_version"] == "episode_timeline.v3"
    assert timeline["track_schema_version"] == ("dialecticore.parallel_directing_tracks.v2")
    assert segment["video_asset_id"] == str(video.id)
    assert segment["studio_panel_scene_asset_id"] == str(panel.id)
    assert segment["direction"]["speaker_mouth_mode"] == "audio_driven_seated_panel"
    assert segment["visual_layers"][0]["purpose"] == "audio_driven_seated_panel_full_frame"
    assert segment["wall_screen_visual_asset_id"] == str(wall_screen.id)
    assert timeline["tracks"]["dialogue"][0]["asset_id"] == str(audio.id)
    assert timeline["tracks"]["character_performance"][0]["asset_id"] == str(video.id)
    assert timeline["tracks"]["camera_direction"][0]["framing_policy"] == (
        "active_speaker_centered.v2"
    )
    content = timeline["tracks"]["broll_content"][0]
    presentation = timeline["tracks"]["broll_presentation"][0]
    assert content["asset_id"] == str(wall_screen.id)
    assert content["audio_mode"] == "muted"
    assert presentation["content_clip_id"] == content["id"]
    assert presentation["transition_duration_ms"] == 1_500
    assert all(
        keyframe["transition_duration_ms"] == 1_500 for keyframe in presentation["keyframes"]
    )
    assert [keyframe["state"] for keyframe in presentation["keyframes"]] == [
        "rear_screen",
        "rear_screen",
    ]
    assert sum(item["duration_ms"] for item in timeline["segments"]) == 9_000


def test_parallel_directing_tracks_are_normalized_and_legacy_tracks_survive() -> None:
    service = TimelineService(Settings())

    normalized = service._normalize_timeline_tracks(
        {
            "video_primary": ["segment-1"],
            "broll_content": [
                {
                    "id": "clip-2",
                    "start_ms": 3_000,
                    "end_ms": 5_000,
                    "source_in_ms": 400,
                },
                {
                    "id": "clip-1",
                    "start_ms": 1_000,
                    "end_ms": 2_500,
                    "source_in_ms": 0,
                    "source_out_ms": 1_500,
                },
            ],
        }
    )

    assert normalized["video_primary"] == ["segment-1"]
    assert [clip["id"] for clip in normalized["broll_content"]] == ["clip-1", "clip-2"]
    assert normalized["broll_content"][1]["duration_ms"] == 2_000
    assert normalized["broll_content"][1]["source_out_ms"] == 2_400


def test_parallel_track_transition_duration_is_normalized_and_bounded() -> None:
    service = TimelineService(Settings())

    normalized = service._normalize_timeline_tracks(
        {
            "broll_presentation": [
                {
                    "id": "presentation-1",
                    "start_ms": 1_000,
                    "end_ms": 4_000,
                    "transition_duration_ms": "1750",
                }
            ]
        }
    )

    assert normalized["broll_presentation"][0]["transition_duration_ms"] == 1_750

    contained = service._normalize_timeline_tracks(
        {
            "broll_presentation": [
                {
                    "id": "presentation-contained",
                    "start_ms": 1_000,
                    "end_ms": 4_000,
                    "fit": "contain",
                    "focal_y": 0.25,
                }
            ]
        }
    )["broll_presentation"][0]
    assert contained["fit"] == "contain"
    assert contained["focal_y"] == 0.25

    with pytest.raises(ValueError, match="unsupported B-roll fit"):
        service._normalize_timeline_tracks(
            {
                "broll_presentation": [
                    {
                        "id": "presentation-stretched",
                        "start_ms": 1_000,
                        "end_ms": 4_000,
                        "fit": "stretch",
                    }
                ]
            }
        )

    with pytest.raises(ValueError, match="transition duration must be 0-5000ms"):
        service._normalize_timeline_tracks(
            {
                "broll_presentation": [
                    {
                        "id": "presentation-1",
                        "start_ms": 1_000,
                        "end_ms": 4_000,
                        "transition_duration_ms": 5_001,
                    }
                ]
            }
        )

    with pytest.raises(ValueError, match="clip IDs must be unique"):
        service._normalize_timeline_tracks(
            {
                "broll_content": [
                    {"id": "duplicate", "start_ms": 0, "end_ms": 1_000},
                    {"id": "duplicate", "start_ms": 2_000, "end_ms": 3_000},
                ]
            }
        )


def test_virtual_camera_actions_and_broll_modes_are_validated() -> None:
    service = TimelineService(Settings())

    normalized = service._normalize_timeline_tracks(
        {
            "camera_direction": [
                {"id": "camera-1", "start_ms": 0, "end_ms": 2_000, "action": "fly_in"}
            ],
            "broll_presentation": [
                {
                    "id": "presentation-1",
                    "start_ms": 0,
                    "end_ms": 2_000,
                    "mode": "rear_screen",
                }
            ],
        }
    )

    assert normalized["camera_direction"][0]["action"] == "fly_in"
    assert normalized["broll_presentation"][0]["mode"] == "rear_screen"

    with pytest.raises(ValueError, match="unsupported camera action"):
        service._normalize_timeline_tracks(
            {
                "camera_direction": [
                    {"id": "camera-1", "start_ms": 0, "end_ms": 2_000, "action": "orbit"}
                ]
            }
        )


def test_panel_camera_relink_preserves_parallel_editorial_tracks() -> None:
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    transcript = TranscriptVersion(
        episode_id=episode.id,
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=[],
        speaker_participant_id="host",
        turn_type=TurnType.post_primer_bridge,
        text="Welcome to the studio.",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode.transcripts.append(transcript)
    old_primary = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        status="replaced",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "video_primary",
            "prompt_inputs": {"camera_view": "speaker_medium"},
        },
    )
    wide_primary = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        status="planned",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "video_primary",
            "prompt_inputs": {"camera_view": "establishing_wide"},
            "shot_plan": {"studio_panel_scene_asset_id": "scene-wide"},
        },
    )
    performance = Asset(
        episode_id=episode.id,
        asset_type=AssetType.video,
        language="en",
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        status="completed",
        storage_uri="object://dialecticore/production-v2/speaking/host.mp4",
        width=1024,
        height=1024,
        fps=12,
        checksum="sha256:performance",
        generation_metadata={
            "visual_role": "production_v2_speaking_character",
            "participant_id": "host",
            "animation_input_policy": "normalized_seated_master",
        },
    )
    episode.assets.extend([old_primary, wide_primary, performance])
    timeline = {
        "media": {"composition_policy": "seated_studio_panel.v1"},
        "segments": [
            {
                "id": "segment-host",
                "source_turn_id": str(turn.id),
                "segment_type": "discussion",
                "video_asset_id": str(old_primary.id),
                "camera_view": "establishing_wide",
                "camera_action": "fly_in",
                "direction": {
                    "view": "establishing_wide",
                    "action": "fly_in",
                    "speaker_mouth_mode": "audio_driven_seated_panel",
                },
                "visual_layers": [
                    {"role": "studio_scene", "asset_id": str(old_primary.id)},
                    {"role": "wall_screen_broll", "asset_id": "broll-1"},
                ],
                "media_fingerprints": {},
            }
        ],
        "tracks": {
            "character_performance": [
                {
                    "id": "segment-host",
                    "asset_id": str(performance.id),
                    "speaking_asset_id": str(performance.id),
                    "start_ms": 0,
                    "end_ms": 2_000,
                }
            ],
            "camera_direction": [
                {
                    "id": "segment-host",
                    "camera_plate_asset_id": str(old_primary.id),
                    "view": "speaker_centered",
                    "action": "cut",
                }
            ],
            "broll_content": [
                {
                    "id": "broll-content-1",
                    "asset_id": "broll-1",
                    "source_in_ms": 1375,
                    "source_out_ms": 9375,
                }
            ],
        },
    }

    relinked = TimelineService(Settings())._relink_seated_panel_camera_media(
        episode,
        transcript,
        timeline,
    )

    segment = relinked["segments"][0]
    assert segment["video_asset_id"] == str(performance.id)
    assert segment["camera_source_asset_id"] == str(wide_primary.id)
    assert segment["studio_panel_scene_asset_id"] == "scene-wide"
    assert segment["direction"]["view"] == "establishing_wide"
    assert segment["direction"]["action"] == "fly_in"
    assert segment["visual_layers"][0]["role"] == "video_primary"
    assert segment["visual_layers"][0]["asset_id"] == str(performance.id)
    assert segment["visual_layers"][1] == {
        "role": "wall_screen_broll",
        "asset_id": "broll-1",
    }
    assert relinked["tracks"]["camera_direction"][0]["camera_source_asset_id"] == str(
        wide_primary.id
    )
    assert "camera_plate_asset_id" not in relinked["tracks"]["camera_direction"][0]
    assert relinked["tracks"]["camera_direction"][0]["linked_segment_id"] == (
        "segment-host"
    )
    assert relinked["tracks"]["camera_direction"][0]["view"] == "establishing_wide"
    assert relinked["tracks"]["camera_direction"][0]["action"] == "fly_in"
    assert relinked["tracks"]["broll_content"] == timeline["tracks"]["broll_content"]
    assert timeline["segments"][0]["video_asset_id"] == str(old_primary.id)


def test_timeline_build_rejects_pending_review_transcript(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    episode = EpisodeRepository().create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    transcript = episode.transcripts = []
    pending = TranscriptVersion(
        episode_id=episode.id,
        type="broadcast",
        language="en",
        status="pending_review",
    )
    pending.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=[],
            speaker_participant_id="chatgpt",
            text="Pending transcript timelines should not be generated.",
            status="pending_review",
        )
    )
    transcript.append(pending)
    episode.canonical_transcript_version_id = pending.id

    with pytest.raises(
        ValueError,
        match="transcript must be approved before timeline building",
    ):
        TimelineService(settings).build_timeline(
            episode,
            TimelineBuildRequest(transcript_version_id=pending.id),
        )


@pytest.mark.asyncio
async def test_timeline_builder_persists_editable_timeline_with_qc(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), settings).run(episode)
    approve_canonical_transcript(produced)
    transcript = next(
        transcript
        for transcript in produced.transcripts
        if transcript.id == produced.canonical_transcript_version_id
    )
    playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
    subtitle_asset = Asset(
        episode_id=produced.id,
        asset_type=AssetType.subtitle,
        language=transcript.language,
        source_entity_type="transcript_version",
        source_entity_id=str(transcript.id),
        storage_uri="mock://subtitles/en/test.vtt",
        mime_type="text/vtt",
        duration_ms=24_000,
        checksum="sha256:subtitle",
        status="completed",
        generation_metadata={"transcript_version_id": str(transcript.id), "format": "vtt"},
    )
    produced.assets.append(subtitle_asset)
    studio_scene_asset = Asset(
        episode_id=produced.id,
        asset_type=AssetType.studio_scene,
        language=transcript.language,
        source_entity_type="episode",
        source_entity_id=str(produced.id),
        storage_uri="object://dialecticore/visuals/studio.mp4",
        mime_type="video/mp4",
        duration_ms=24_000,
        width=1920,
        height=1080,
        fps=30,
        checksum="sha256:studio-scene",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "studio_scene",
            "render_ready": True,
        },
    )
    produced.assets.append(studio_scene_asset)
    for index, turn in enumerate(playable_turns, start=1):
        produced.assets.extend(
            [
                Asset(
                    episode_id=produced.id,
                    asset_type=AssetType.audio,
                    language=transcript.language,
                    source_entity_type="transcript_turn",
                    source_entity_id=str(turn.id),
                    storage_uri=f"object://dialecticore/audio/{turn.id}.wav",
                    mime_type="audio/wav",
                    duration_ms=1200 + index,
                    checksum=f"sha256:audio-{index}",
                    status="completed",
                    generation_metadata={"transcript_version_id": str(transcript.id)},
                ),
                Asset(
                    episode_id=produced.id,
                    asset_type=AssetType.video,
                    language=transcript.language,
                    source_entity_type="transcript_turn",
                    source_entity_id=str(turn.id),
                    storage_uri=f"object://dialecticore/video/{turn.id}.mp4",
                    mime_type="video/mp4",
                    duration_ms=1200 + index,
                    width=1920,
                    height=1080,
                    fps=30,
                    checksum=f"sha256:video-{index}",
                    status="completed",
                    generation_metadata={
                        "transcript_version_id": str(transcript.id),
                        "visual_role": "video_primary",
                        "render_ready": True,
                        "prompt_inputs": {
                            "reference_image_uri": (
                                "object://dialecticore/visual-profiles/"
                                f"{turn.speaker_participant_id}/reference.png"
                            )
                        },
                        "shot_plan": {
                            "camera_transition": "studio_establishing" if index == 1 else "cut",
                        },
                    },
                ),
            ]
        )
        if index == 1:
            produced.assets.extend(
                [
                    Asset(
                        episode_id=produced.id,
                        asset_type=AssetType.broll,
                        language=transcript.language,
                        source_entity_type="transcript_turn",
                        source_entity_id=str(turn.id),
                        storage_uri=f"object://dialecticore/visuals/{turn.id}.broll.png",
                        mime_type="image/png",
                        duration_ms=1200 + index,
                        width=1920,
                        height=1080,
                        checksum="sha256:broll-1",
                        status="completed",
                        generation_metadata={
                            "transcript_version_id": str(transcript.id),
                            "visual_role": "broll",
                            "render_ready": True,
                        },
                    ),
                    Asset(
                        episode_id=produced.id,
                        asset_type=AssetType.reaction_loop,
                        language=transcript.language,
                        source_entity_type="participant_profile",
                        source_entity_id=turn.speaker_participant_id,
                        storage_uri=(
                            "object://dialecticore/visuals/"
                            f"{turn.speaker_participant_id}.reaction.mp4"
                        ),
                        mime_type="video/mp4",
                        duration_ms=1200 + index,
                        width=1920,
                        height=1080,
                        fps=30,
                        checksum="sha256:reaction-1",
                        status="completed",
                        generation_metadata={
                            "transcript_version_id": str(transcript.id),
                            "visual_role": "reaction_loop",
                            "render_ready": True,
                            "prompt_inputs": {
                                "reference_image_uri": (
                                    "object://dialecticore/visual-profiles/"
                                    f"{turn.speaker_participant_id}/reaction.png"
                                )
                            },
                        },
                    ),
                ]
            )

    service = TimelineService(settings)
    built = service.build_timeline(
        produced,
        TimelineBuildRequest(transcript_version_id=transcript.id, user_id="tester"),
    )

    timeline_asset = next(asset for asset in built.assets if asset.asset_type == AssetType.timeline)
    timeline = timeline_asset.generation_metadata["timeline_json"]
    timeline_qc = [
        result for result in built.quality_results if result.check_type == "timeline_integrity"
    ][-1]
    assert timeline_asset.status == "completed"
    assert timeline_asset.mime_type == "application/vnd.dialecticore.timeline+json"
    assert timeline_asset.checksum and timeline_asset.checksum.startswith("sha256:")
    assert Path(timeline_asset.generation_metadata["object_storage_path"]).exists()
    timeline_entity = timeline_asset.generation_metadata["timeline_entity"]
    assert timeline_entity["id"] == timeline["id"]
    assert timeline_entity["episode_id"] == str(built.id)
    assert timeline_entity["language"] == transcript.language
    assert timeline_entity["version"] == 1
    assert timeline_entity["status"] == "completed"
    assert timeline_entity["duration_ms"] == timeline["duration_ms"]
    assert timeline_entity["timeline_json"]["id"] == timeline["id"]
    assert timeline["editable"] is True
    assert timeline["segments"][0]["camera_transition"] == "studio_establishing"
    assert timeline["segments"][0]["secondary_visual_asset_id"]
    assert timeline["segments"][0]["reaction_visual_asset_id"] is None
    assert timeline["segments"][0]["studio_scene_asset_id"] == str(studio_scene_asset.id)
    assert timeline["segments"][0]["media_fingerprints"]["audio"]["schema_version"] == (
        "timeline_media_asset_fingerprint.v1"
    )
    assert (
        timeline["segments"][0]["media_fingerprints"]["video_primary"]["asset_id"]
        == (timeline["segments"][0]["video_asset_id"])
    )
    assert timeline["segments"][0]["character_reference_image_uri"].endswith(
        f"{playable_turns[0].speaker_participant_id}/reference.png"
    )
    primary_layer = next(
        layer
        for layer in timeline["segments"][0]["visual_layers"]
        if layer["role"] == "video_primary"
    )
    assert (
        primary_layer["character_reference_image_uri"]
        == (timeline["segments"][0]["character_reference_image_uri"])
    )
    assert {layer["role"] for layer in timeline["segments"][0]["visual_layers"]}.issuperset(
        {"studio_scene", "video_primary", "broll"}
    )
    assert timeline["segments"][0]["camera_view"] == "speaker_medium"
    assert timeline["segments"][0]["direction"]["speaker_mouth_mode"] == (
        "audio_driven_single_portrait"
    )
    assert len(timeline["segments"]) == len(playable_turns)
    assert timeline["tracks"]["audio_dialogue"] == [
        segment["id"] for segment in timeline["segments"]
    ]
    assert timeline_qc.status == "pass"
    assert timeline_qc.details["missing_audio_segment_count"] == 0
    assert timeline_qc.details["missing_primary_video_segment_count"] == 0
    assert timeline_qc.details["subtitle_linked_segment_count"] == len(playable_turns)
    assert built.audit_events[-3].event_type == "timeline.asset.built"

    edited_timeline = {
        **timeline,
        "segments": [
            {
                **timeline["segments"][0],
                "camera_transition": "dissolve",
                "end_ms": timeline["segments"][0]["start_ms"] + 1800,
            },
            *timeline["segments"][1:],
        ],
    }
    edited = service.update_timeline(
        built,
        TimelineUpdateRequest(
            timeline=edited_timeline,
            user_id="tester",
            comment="Use a softer opening transition.",
        ),
    )

    timeline_assets = [asset for asset in edited.assets if asset.asset_type == AssetType.timeline]
    assert len(timeline_assets) == 2
    assert timeline_assets[0].status == "replaced"
    assert timeline_assets[-1].generation_metadata["edit_version"] == 2
    assert timeline_assets[-1].generation_metadata["timeline_entity"]["version"] == 2
    assert timeline_assets[-1].generation_metadata["timeline_entity"]["status"] == "completed"
    assert (
        timeline_assets[-1].generation_metadata["timeline_json"]["segments"][0]["camera_transition"]
        == "dissolve"
    )
    assert (
        timeline_assets[-1].generation_metadata["timeline_json"]["segments"][0]["duration_ms"]
        == 1800
    )
    assert edited.audit_events[-2].event_type == "timeline.asset.edited"

    preview_segments = []
    for index, segment in enumerate(timeline["segments"][:2]):
        preview_segments.append(
            {
                **segment,
                "start_ms": index * 15_000,
                "end_ms": (index + 1) * 15_000,
                "duration_ms": 15_000,
            }
        )
    preview_ids = {segment["id"] for segment in preview_segments}
    qualification_preview = service.update_timeline(
        edited,
        TimelineUpdateRequest(
            timeline={
                **timeline,
                "duration_ms": 30_000,
                "segments": preview_segments,
                "tracks": {
                    name: [
                        item
                        for item in items
                        if (
                            item in preview_ids
                            if isinstance(item, str)
                            else item.get("linked_segment_id") in preview_ids
                        )
                    ]
                    for name, items in timeline["tracks"].items()
                },
                "chapters": [],
                "program_structure": {
                    "preview": {
                        "included": True,
                        "turn_range": [1, 2],
                        "start_ms": 0,
                        "duration_ms": 30_000,
                    }
                },
            },
            user_id="tester",
            comment="Contiguous qualification preview.",
        ),
    )
    preview_asset = next(
        asset
        for asset in reversed(qualification_preview.assets)
        if asset.asset_type == AssetType.timeline and asset.status == "completed"
    )
    preview_qc = next(
        result
        for result in reversed(qualification_preview.quality_results)
        if result.check_type == "timeline_integrity" and result.target_id == str(preview_asset.id)
    )
    assert preview_qc.status == "pass"
    assert preview_qc.details["qualification_preview"] is True
    assert preview_qc.details["playable_turn_count"] == 2


@pytest.mark.asyncio
async def test_timeline_qc_fails_when_linked_media_fingerprint_is_stale(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), settings).run(episode)
    approve_canonical_transcript(produced)
    transcript = next(
        transcript
        for transcript in produced.transcripts
        if transcript.id == produced.canonical_transcript_version_id
    )
    turn = next(turn for turn in transcript.turns if turn.status != "excluded")
    audio_asset = Asset(
        episode_id=produced.id,
        asset_type=AssetType.audio,
        language=transcript.language,
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri=f"object://dialecticore/audio/{turn.id}.wav",
        mime_type="audio/wav",
        duration_ms=1400,
        checksum="sha256:audio-original",
        status="completed",
        generation_metadata={"transcript_version_id": str(transcript.id)},
    )
    video_asset = Asset(
        episode_id=produced.id,
        asset_type=AssetType.video,
        language=transcript.language,
        source_entity_type="transcript_turn",
        source_entity_id=str(turn.id),
        storage_uri=f"object://dialecticore/video/{turn.id}.mp4",
        mime_type="video/mp4",
        duration_ms=1400,
        width=1920,
        height=1080,
        fps=30,
        checksum="sha256:video-original",
        status="completed",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "video_primary",
            "render_ready": True,
        },
    )
    produced.assets.extend([audio_asset, video_asset])

    service = TimelineService(settings)
    built = service.build_timeline(
        produced,
        TimelineBuildRequest(transcript_version_id=transcript.id, user_id="tester"),
    )
    timeline_asset = next(asset for asset in built.assets if asset.asset_type == AssetType.timeline)
    timeline = timeline_asset.generation_metadata["timeline_json"]

    audio_asset.checksum = "sha256:audio-mutated"
    qc = service._timeline_qc(built, transcript, timeline, timeline_asset)

    issues = [
        issue
        for issue in qc.details["issues"]
        if issue["issue"] == "timeline_stale_media_fingerprint"
    ]
    assert qc.status == "fail"
    assert qc.details["stale_media_link_count"] == 1
    assert issues[0]["role"] == "audio"
    assert issues[0]["asset_id"] == str(audio_asset.id)
    assert "checksum_changed" in issues[0]["mismatch_reasons"]


@pytest.mark.asyncio
async def test_timeline_qc_fails_when_shot_planned_reusable_media_is_not_render_ready(
    tmp_path: Path,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    repo = EpisodeRepository()
    episode = repo.create(
        EpisodeCreateRequest(
            definition=definition(),
            participants=default_participants(),
            model_endpoints=default_model_endpoints(),
        )
    )
    produced = await DiscussionEngine(ModelGateway(), settings).run(episode)
    approve_canonical_transcript(produced)
    transcript = next(
        transcript
        for transcript in produced.transcripts
        if transcript.id == produced.canonical_transcript_version_id
    )
    playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
    first_turn = playable_turns[0]
    planned_studio_scene = Asset(
        episode_id=produced.id,
        asset_type=AssetType.studio_scene,
        language=transcript.language,
        source_entity_type="episode",
        source_entity_id=str(produced.id),
        storage_uri="object://dialecticore/visuals/studio.pending.mp4",
        mime_type="video/mp4",
        checksum="sha256:studio-pending",
        status="planned",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "studio_scene",
            "render_ready": False,
        },
    )
    planned_reaction_loop = Asset(
        episode_id=produced.id,
        asset_type=AssetType.reaction_loop,
        language=transcript.language,
        source_entity_type="participant_profile",
        source_entity_id=first_turn.speaker_participant_id,
        storage_uri="object://dialecticore/visuals/reaction.pending.mp4",
        mime_type="video/mp4",
        checksum="sha256:reaction-pending",
        status="planned",
        generation_metadata={
            "transcript_version_id": str(transcript.id),
            "visual_role": "reaction_loop",
            "render_ready": False,
        },
    )
    produced.assets.extend([planned_studio_scene, planned_reaction_loop])

    for index, turn in enumerate(playable_turns, start=1):
        shot_plan = {
            "camera_transition": "studio_establishing" if index == 1 else "cut",
        }
        if turn.id == first_turn.id:
            shot_plan.update(
                {
                    "studio_scene_asset_id": str(planned_studio_scene.id),
                    "reusable_reaction_asset_id": str(planned_reaction_loop.id),
                }
            )
        produced.assets.extend(
            [
                Asset(
                    episode_id=produced.id,
                    asset_type=AssetType.audio,
                    language=transcript.language,
                    source_entity_type="transcript_turn",
                    source_entity_id=str(turn.id),
                    storage_uri=f"object://dialecticore/audio/{turn.id}.wav",
                    mime_type="audio/wav",
                    duration_ms=1200 + index,
                    checksum=f"sha256:audio-{index}",
                    status="completed",
                    generation_metadata={"transcript_version_id": str(transcript.id)},
                ),
                Asset(
                    episode_id=produced.id,
                    asset_type=AssetType.video,
                    language=transcript.language,
                    source_entity_type="transcript_turn",
                    source_entity_id=str(turn.id),
                    storage_uri=f"object://dialecticore/video/{turn.id}.mp4",
                    mime_type="video/mp4",
                    duration_ms=1200 + index,
                    width=1920,
                    height=1080,
                    fps=30,
                    checksum=f"sha256:video-{index}",
                    status="completed",
                    generation_metadata={
                        "transcript_version_id": str(transcript.id),
                        "visual_role": "video_primary",
                        "render_ready": True,
                        "shot_plan": shot_plan,
                    },
                ),
            ]
        )

    built = TimelineService(settings).build_timeline(
        produced,
        TimelineBuildRequest(transcript_version_id=transcript.id, user_id="tester"),
    )

    timeline_qc = [
        result for result in built.quality_results if result.check_type == "timeline_integrity"
    ][-1]
    issues = {issue["issue"]: issue for issue in timeline_qc.details["issues"]}
    assert timeline_qc.status == "fail"
    assert timeline_qc.details["missing_shot_planned_reaction_loop_segment_count"] == 1
    assert timeline_qc.details["missing_shot_planned_studio_scene_segment_count"] == 1
    assert "timeline_missing_shot_planned_reaction_loop" in issues
    assert "timeline_missing_shot_planned_studio_scene" in issues
    assert issues["timeline_missing_shot_planned_reaction_loop"]["expected_asset_id"] == str(
        planned_reaction_loop.id
    )
    assert issues["timeline_missing_shot_planned_studio_scene"]["expected_asset_id"] == str(
        planned_studio_scene.id
    )
