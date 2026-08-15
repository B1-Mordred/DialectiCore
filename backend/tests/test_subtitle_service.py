from pathlib import Path

import pytest
from app.core.config import Settings
from app.domain.schemas import (
    Asset,
    Episode,
    ParticipantProfile,
    SubtitleGenerationRequest,
    TranscriptTurn,
    TranscriptVersion,
)
from app.services.subtitle_service import SubtitleService
from tests.test_discussion_engine import definition


def test_subtitle_generation_rejects_pending_review_transcript() -> None:
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000011",
        type="broadcast",
        language="en",
        status="pending_review",
    )
    transcript.turns.append(
        TranscriptTurn(
            source_discussion_turn_ids=["00000000-0000-0000-0000-000000000012"],
            speaker_participant_id="host",
            text="Pending transcript subtitles should not be generated.",
            status="pending_review",
        )
    )
    episode = Episode(
        id=transcript.episode_id,
        title="Pending Subtitle",
        slug="pending-subtitle",
        subject="Pending Subtitle",
        central_question="Should pending transcripts produce subtitles?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
    )

    with pytest.raises(
        ValueError,
        match="transcript must be approved before subtitle generation",
    ):
        SubtitleService(Settings()).generate_subtitles(
            episode,
            SubtitleGenerationRequest(transcript_version_id=transcript.id),
        )


def test_subtitle_qc_fails_overlapping_word_timed_cues(tmp_path: Path) -> None:
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000001",
        type="broadcast",
        language="en",
        status="approved",
    )
    turns = [
        TranscriptTurn(
            source_discussion_turn_ids=["00000000-0000-0000-0000-000000000002"],
            speaker_participant_id="host",
            text="one two three four five six seven eight",
            status="accepted",
        ),
        TranscriptTurn(
            source_discussion_turn_ids=["00000000-0000-0000-0000-000000000003"],
            speaker_participant_id="host",
            text="nine ten eleven twelve",
            status="accepted",
        ),
    ]
    transcript.turns.extend(turns)
    episode = Episode(
        id=transcript.episode_id,
        title="Subtitle QC",
        slug="subtitle-qc",
        subject="Subtitle QC",
        central_question="How should subtitle timing issues be represented?",
        target_duration_seconds=60,
        minimum_duration_seconds=54,
        maximum_duration_seconds=66,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turns[0].id),
                storage_uri="mock://voicebox/en/one.wav",
                mime_type="audio/wav",
                duration_ms=5000,
                checksum="one",
                status="completed",
                generation_metadata={
                    "word_timestamps": [
                        {"word": "one", "start_ms": 0, "end_ms": 500},
                        {"word": "two", "start_ms": 400, "end_ms": 900},
                        {"word": "three", "start_ms": 900, "end_ms": 1400},
                        {"word": "four", "start_ms": 1400, "end_ms": 1900},
                        {"word": "five", "start_ms": 1900, "end_ms": 2400},
                        {"word": "six", "start_ms": 2400, "end_ms": 2900},
                        {"word": "seven", "start_ms": 2900, "end_ms": 3400},
                        {"word": "eight", "start_ms": 3200, "end_ms": 5000},
                    ]
                },
            ),
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turns[1].id),
                storage_uri="mock://voicebox/en/two.wav",
                mime_type="audio/wav",
                duration_ms=2500,
                checksum="two",
                status="completed",
                generation_metadata={
                    "word_timestamps": [
                        {"word": "nine", "start_ms": 0, "end_ms": 500},
                        {"word": "ten", "start_ms": 500, "end_ms": 1000},
                        {"word": "eleven", "start_ms": 1000, "end_ms": 1500},
                        {"word": "twelve", "start_ms": 1500, "end_ms": 2500},
                    ]
                },
            ),
        ],
    )

    settings = Settings(object_storage_local_path=str(tmp_path / "object-store"))
    updated = SubtitleService(settings).generate_subtitles(
        episode,
        request=SubtitleGenerationRequest(language="en"),
    )

    qc = updated.quality_results[-1]
    subtitle = updated.assets[-1]
    assert subtitle.asset_type == "subtitle"
    assert subtitle.storage_uri and subtitle.storage_uri.startswith("object://dialecticore/")
    assert subtitle.checksum and subtitle.checksum.startswith("sha256:")
    assert Path(subtitle.generation_metadata["object_storage_path"]).exists()
    assert subtitle.generation_metadata["storage_backend"] == "local_object_store"
    assert subtitle.generation_metadata["cue_count"] > len(turns)
    assert qc.check_type == "subtitle_generation_completeness"
    assert qc.status == "fail"
    assert qc.details["timing_overlap_count"] == 1
    assert qc.details["covered_turn_count"] == len(turns)


def test_subtitle_qc_allows_trailing_audio_silence(tmp_path: Path) -> None:
    transcript = TranscriptVersion(
        episode_id="00000000-0000-0000-0000-000000000021",
        type="broadcast",
        language="en",
        status="approved",
    )
    turn = TranscriptTurn(
        source_discussion_turn_ids=["00000000-0000-0000-0000-000000000022"],
        speaker_participant_id="host",
        text="A short spoken line",
        status="accepted",
    )
    transcript.turns.append(turn)
    episode = Episode(
        id=transcript.episode_id,
        title="Trailing Silence",
        slug="trailing-silence",
        subject="Trailing Silence",
        central_question="Should silence extend a caption?",
        target_duration_seconds=10,
        minimum_duration_seconds=5,
        maximum_duration_seconds=15,
        canonical_transcript_version_id=transcript.id,
        definition=definition(),
        participants=[
            ParticipantProfile(
                id="host",
                name="host",
                display_name="Host",
                participant_type="host",
                model_endpoint_id="mock",
                model_id="mock-host-v1",
                system_prompt_template="host",
                perspective="moderate",
                expertise="discussion",
                speaking_style="clear",
                voice_profile_id="voice-host",
            )
        ],
        model_endpoints=[],
        transcripts=[transcript],
        assets=[
            Asset(
                episode_id=transcript.episode_id,
                asset_type="audio",
                language="en",
                source_entity_type="transcript_turn",
                source_entity_id=str(turn.id),
                storage_uri="mock://voicebox/en/trailing.wav",
                mime_type="audio/wav",
                duration_ms=2_000,
                checksum="trailing",
                status="completed",
                generation_metadata={
                    "word_timestamps": [
                        {"word": "A", "start_ms": 0, "end_ms": 200},
                        {"word": "short", "start_ms": 200, "end_ms": 500},
                        {"word": "spoken", "start_ms": 500, "end_ms": 800},
                        {"word": "line", "start_ms": 800, "end_ms": 1_200},
                    ]
                },
            )
        ],
    )

    updated = SubtitleService(
        Settings(object_storage_local_path=str(tmp_path / "object-store"))
    ).generate_subtitles(episode, SubtitleGenerationRequest(language="en"))

    qc = updated.quality_results[-1]
    assert qc.status == "pass"
    assert qc.details["audio_sync_error_count"] == 0
    assert updated.assets[-1].generation_metadata["cues"][-1]["timing_source"] == (
        "word_timestamps"
    )
