from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import Settings
from app.domain.enums import AssetType, QualitySeverity, TranscriptType
from app.domain.schemas import (
    Asset,
    AuditEvent,
    Episode,
    ParticipantProfile,
    QualityResult,
    SubtitleGenerationRequest,
    TranscriptTurn,
    TranscriptVersion,
)
from app.services.object_storage import ObjectStore, create_object_store


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    speaker: str
    text: str
    transcript_turn_id: str
    audio_asset_id: str | None
    timing_source: str
    source_word_count: int = 0


class SubtitleService:
    def __init__(self, settings: Settings, object_store: ObjectStore | None = None) -> None:
        self.settings = settings
        self.object_store = object_store or create_object_store(settings)

    def generate_subtitles(
        self,
        episode: Episode,
        request: SubtitleGenerationRequest,
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        if not playable_turns:
            raise ValueError("target transcript has no playable turns")

        existing = self._existing_subtitle_asset(episode, transcript, request.format)
        if existing is not None and not request.regenerate:
            raise ValueError("subtitles already generated for target transcript")
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = datetime.now(UTC)

        cues = self._build_cues(episode, transcript, playable_turns)
        subtitle_text = self._render_subtitle(cues, request.format)
        subtitle_payload = subtitle_text.encode("utf-8")
        stored = self.object_store.put_bytes(
            key=f"subtitles/{episode.id}/{transcript.language}/{transcript.id}.{request.format}",
            payload=subtitle_payload,
            content_type=self._mime_type(request.format),
        )
        checksum = stored.checksum
        duration_ms = max((cue.end_ms for cue in cues), default=0)
        missing_audio_count = sum(1 for cue in cues if cue.audio_asset_id is None)
        estimated_timing_count = sum(1 for cue in cues if cue.timing_source == "estimated")
        word_timed_cue_count = sum(1 for cue in cues if cue.timing_source == "word_timestamps")

        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.subtitle,
            language=transcript.language,
            source_entity_type="transcript_version",
            source_entity_id=str(transcript.id),
            storage_uri=stored.uri,
            mime_type=self._mime_type(request.format),
            duration_ms=duration_ms,
            checksum=checksum,
            generation_metadata={
                "adapter": "subtitle_composer",
                "format": request.format,
                "subtitle_text": subtitle_text,
                "transcript_version_id": str(transcript.id),
                "transcript_type": transcript.type.value,
                "localization": transcript.localization_metadata,
                "cue_count": len(cues),
                "missing_audio_count": missing_audio_count,
                "estimated_timing_count": estimated_timing_count,
                "word_timed_cue_count": word_timed_cue_count,
                "storage_backend": stored.backend,
                "object_storage_bucket": self.object_store.bucket,
                "object_storage_key": stored.key,
                "object_storage_path": str(stored.path),
                "object_size_bytes": stored.size_bytes,
                "cues": [
                    {
                        "index": cue.index,
                        "start_ms": cue.start_ms,
                        "end_ms": cue.end_ms,
                        "speaker": cue.speaker,
                        "transcript_turn_id": cue.transcript_turn_id,
                        "audio_asset_id": cue.audio_asset_id,
                        "timing_source": cue.timing_source,
                        "source_word_count": cue.source_word_count,
                    }
                    for cue in cues
                ],
            },
            status="completed",
        )
        episode.assets.append(asset)
        qc = self._subtitle_generation_qc(
            episode=episode,
            transcript=transcript,
            cues=cues,
            subtitle_asset=asset,
        )
        episode.quality_results.append(qc)
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="subtitle.asset.generated",
                actor=request.user_id or "system",
                details={
                    "transcript_version_id": str(transcript.id),
                    "language": transcript.language,
                    "format": request.format,
                    "asset_id": str(asset.id),
                    "cue_count": len(cues),
                    "missing_audio_count": missing_audio_count,
                    "estimated_timing_count": estimated_timing_count,
                    "word_timed_cue_count": word_timed_cue_count,
                    "checksum": checksum,
                    "storage_backend": stored.backend,
                },
            )
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def _target_transcript(
        self,
        episode: Episode,
        request: SubtitleGenerationRequest,
    ) -> TranscriptVersion:
        if request.transcript_version_id is not None:
            return self._transcript_by_id(episode, request.transcript_version_id)
        if request.language is not None:
            matches = [
                transcript
                for transcript in episode.transcripts
                if transcript.language == request.language
                and transcript.type in {TranscriptType.localized, TranscriptType.broadcast}
                and transcript.status == "approved"
            ]
            if matches:
                return matches[-1]
            raise ValueError(f"no transcript found for language {request.language}")
        localized = [
            transcript
            for transcript in episode.transcripts
            if transcript.type == TranscriptType.localized and transcript.status == "approved"
        ]
        if localized:
            return localized[-1]
        canonical_id = episode.canonical_transcript_version_id
        if canonical_id is None:
            raise ValueError("episode has no canonical transcript")
        return self._transcript_by_id(episode, canonical_id)

    def _transcript_by_id(
        self,
        episode: Episode,
        transcript_id: UUID,
    ) -> TranscriptVersion:
        transcript = next(
            (item for item in episode.transcripts if item.id == transcript_id),
            None,
        )
        if transcript is None:
            raise ValueError("transcript not found")
        if transcript.status != "approved":
            raise ValueError("transcript must be approved before subtitle generation")
        return transcript

    def _existing_subtitle_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        subtitle_format: str,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.subtitle
                and asset.language == transcript.language
                and asset.source_entity_type == "transcript_version"
                and asset.source_entity_id == str(transcript.id)
                and asset.generation_metadata.get("format") == subtitle_format
                and asset.status != "replaced"
            ),
            None,
        )

    def _build_cues(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        playable_turns: list[TranscriptTurn],
    ) -> list[SubtitleCue]:
        participant_by_id = {participant.id: participant for participant in episode.participants}
        audio_by_turn_id = self._completed_audio_assets_by_turn(episode, transcript)
        cursor_ms = 0
        cues: list[SubtitleCue] = []
        cue_index = 1
        for turn in playable_turns:
            audio_asset = audio_by_turn_id.get(str(turn.id))
            duration_ms = (
                audio_asset.duration_ms
                if audio_asset is not None and audio_asset.duration_ms
                else self._estimate_duration_ms(turn.text)
            )
            speaker = self._speaker_label(participant_by_id, turn.speaker_participant_id)
            if audio_asset is not None:
                word_cues = self._word_timestamp_cues(
                    audio_asset=audio_asset,
                    turn=turn,
                    speaker=speaker,
                    cursor_ms=cursor_ms,
                    first_index=cue_index,
                )
                if word_cues:
                    cues.extend(word_cues)
                    cue_index += len(word_cues)
                    cursor_ms += duration_ms
                    continue

            timing_source = "audio_asset" if audio_asset is not None else "estimated"
            cues.append(
                SubtitleCue(
                    index=cue_index,
                    start_ms=cursor_ms,
                    end_ms=cursor_ms + duration_ms,
                    speaker=speaker,
                    text=self._clean_text(turn.text),
                    transcript_turn_id=str(turn.id),
                    audio_asset_id=str(audio_asset.id) if audio_asset is not None else None,
                    timing_source=timing_source,
                    source_word_count=len(turn.text.split()),
                )
            )
            cue_index += 1
            cursor_ms += duration_ms
        return cues

    def _word_timestamp_cues(
        self,
        audio_asset: Asset,
        turn: TranscriptTurn,
        speaker: str,
        cursor_ms: int,
        first_index: int,
    ) -> list[SubtitleCue]:
        words = self._valid_word_timestamps(audio_asset)
        if not words:
            return []

        cues: list[SubtitleCue] = []
        chunk: list[dict] = []
        index = first_index
        for word in words:
            next_chunk = [*chunk, word]
            if chunk and self._subtitle_chunk_too_large(next_chunk):
                cues.append(
                    self._cue_from_word_chunk(
                        index=index,
                        chunk=chunk,
                        speaker=speaker,
                        cursor_ms=cursor_ms,
                        turn=turn,
                        audio_asset=audio_asset,
                    )
                )
                index += 1
                chunk = [word]
            else:
                chunk = next_chunk
        if chunk:
            cues.append(
                self._cue_from_word_chunk(
                    index=index,
                    chunk=chunk,
                    speaker=speaker,
                    cursor_ms=cursor_ms,
                    turn=turn,
                    audio_asset=audio_asset,
                )
            )
        return cues

    def _valid_word_timestamps(self, audio_asset: Asset) -> list[dict]:
        raw_words = audio_asset.generation_metadata.get("word_timestamps")
        if not isinstance(raw_words, list):
            return []
        valid_words: list[dict] = []
        for item in raw_words:
            if not isinstance(item, dict):
                continue
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
            word = item.get("word")
            if word is None or start_ms is None or end_ms is None:
                continue
            start = int(start_ms)
            end = int(end_ms)
            if start < 0 or end <= start:
                continue
            valid_words.append({"word": str(word), "start_ms": start, "end_ms": end})
        return valid_words

    def _subtitle_chunk_too_large(self, words: list[dict]) -> bool:
        text = " ".join(str(word["word"]) for word in words)
        duration_ms = int(words[-1]["end_ms"]) - int(words[0]["start_ms"])
        return len(words) > 7 or len(text) > 48 or duration_ms > 6000

    def _cue_from_word_chunk(
        self,
        index: int,
        chunk: list[dict],
        speaker: str,
        cursor_ms: int,
        turn: TranscriptTurn,
        audio_asset: Asset,
    ) -> SubtitleCue:
        start_ms = cursor_ms + int(chunk[0]["start_ms"])
        end_ms = cursor_ms + int(chunk[-1]["end_ms"])
        if end_ms <= start_ms:
            end_ms = start_ms + 250
        return SubtitleCue(
            index=index,
            start_ms=start_ms,
            end_ms=end_ms,
            speaker=speaker,
            text=self._clean_text(" ".join(str(word["word"]) for word in chunk)),
            transcript_turn_id=str(turn.id),
            audio_asset_id=str(audio_asset.id),
            timing_source="word_timestamps",
            source_word_count=len(chunk),
        )

    def _completed_audio_assets_by_turn(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> dict[str, Asset]:
        turn_ids = {str(turn.id) for turn in transcript.turns if turn.status != "excluded"}
        return {
            asset.source_entity_id: asset
            for asset in episode.assets
            if asset.asset_type == AssetType.audio
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.source_entity_id in turn_ids
            and asset.status == "completed"
        }

    def _subtitle_generation_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        cues: list[SubtitleCue],
        subtitle_asset: Asset,
    ) -> QualityResult:
        required_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        cue_turn_ids = {cue.transcript_turn_id for cue in cues}
        required_turn_ids = {str(turn.id) for turn in required_turns}
        missing_cue_turn_ids = sorted(required_turn_ids - cue_turn_ids)
        blank_cue_turn_ids = sorted(cue.transcript_turn_id for cue in cues if not cue.text)
        missing_source_turn_ids = sorted(
            str(turn.id) for turn in required_turns if not turn.source_discussion_turn_ids
        )
        missing_audio_count = sum(1 for cue in cues if cue.audio_asset_id is None)
        estimated_timing_count = sum(1 for cue in cues if cue.timing_source == "estimated")
        word_timed_cue_count = sum(1 for cue in cues if cue.timing_source == "word_timestamps")
        timing_overlaps = self._timing_overlaps(cues)
        excessive_line_cues = [
            {
                "cue_index": cue.index,
                "transcript_turn_id": cue.transcript_turn_id,
                "line_length": len(f"{cue.speaker}: {cue.text}"),
            }
            for cue in cues
            if len(f"{cue.speaker}: {cue.text}") > 72
        ]
        audio_sync_errors = self._audio_sync_errors(cues, episode, transcript)
        max_sync_error_ms = max(
            (item["sync_error_ms"] for item in audio_sync_errors),
            default=0,
        )

        if (
            missing_cue_turn_ids
            or blank_cue_turn_ids
            or missing_source_turn_ids
            or timing_overlaps
            or excessive_line_cues
            or max_sync_error_ms > episode.definition.quality.block_on_sync_error_ms
        ):
            severity = QualitySeverity.fail
        elif estimated_timing_count:
            severity = QualitySeverity.warning
        else:
            severity = QualitySeverity.pass_

        return QualityResult(
            episode_id=episode.id,
            target_type="asset",
            target_id=str(subtitle_asset.id),
            check_type="subtitle_generation_completeness",
            severity=severity,
            status=severity.value,
            score=1.0 if severity == QualitySeverity.pass_ else 0.5
            if severity == QualitySeverity.warning
            else 0.0,
            details={
                "language": transcript.language,
                "format": subtitle_asset.generation_metadata["format"],
                "transcript_version_id": str(transcript.id),
                "required_cue_count": len(required_turn_ids),
                "required_turn_count": len(required_turn_ids),
                "covered_turn_count": len(cue_turn_ids),
                "cue_count": len(cues),
                "missing_cue_turn_ids": missing_cue_turn_ids,
                "blank_cue_turn_ids": blank_cue_turn_ids,
                "missing_source_turn_ids": missing_source_turn_ids,
                "missing_audio_count": missing_audio_count,
                "estimated_timing_count": estimated_timing_count,
                "word_timed_cue_count": word_timed_cue_count,
                "timing_overlap_count": len(timing_overlaps),
                "timing_overlaps": timing_overlaps,
                "excessive_line_count": len(excessive_line_cues),
                "excessive_line_cues": excessive_line_cues,
                "audio_sync_error_count": len(audio_sync_errors),
                "max_sync_error_ms": max_sync_error_ms,
                "sync_error_threshold_ms": episode.definition.quality.block_on_sync_error_ms,
                "audio_sync_errors": audio_sync_errors,
                "storage_uri": subtitle_asset.storage_uri,
                "checksum": subtitle_asset.checksum,
            },
        )

    def _timing_overlaps(self, cues: list[SubtitleCue]) -> list[dict]:
        overlaps: list[dict] = []
        previous: SubtitleCue | None = None
        for cue in sorted(cues, key=lambda item: (item.start_ms, item.end_ms)):
            if previous is not None and cue.start_ms < previous.end_ms:
                overlaps.append(
                    {
                        "previous_cue_index": previous.index,
                        "cue_index": cue.index,
                        "overlap_ms": previous.end_ms - cue.start_ms,
                    }
                )
            previous = cue
        return overlaps

    def _audio_sync_errors(
        self,
        cues: list[SubtitleCue],
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> list[dict]:
        completed_audio = self._completed_audio_assets_by_turn(episode, transcript)
        cues_by_asset_id: dict[str, list[SubtitleCue]] = {}
        for cue in cues:
            if cue.audio_asset_id is None:
                continue
            cues_by_asset_id.setdefault(cue.audio_asset_id, []).append(cue)

        errors: list[dict] = []
        cursor_ms = 0
        for turn in [turn for turn in transcript.turns if turn.status != "excluded"]:
            asset = completed_audio.get(str(turn.id))
            duration_ms = (
                asset.duration_ms
                if asset is not None and asset.duration_ms
                else self._estimate_duration_ms(turn.text)
            )
            if asset is not None:
                asset_cues = cues_by_asset_id.get(str(asset.id), [])
                if asset_cues:
                    actual_end_ms = max(cue.end_ms for cue in asset_cues)
                    expected_end_ms = cursor_ms + duration_ms
                    # A word-timed caption should end with the final spoken word.
                    # Normal trailing silence in the audio asset is not subtitle
                    # drift. Only timestamps that run beyond the audio boundary
                    # can claim dialogue after playback has ended.
                    sync_error_ms = max(0, actual_end_ms - expected_end_ms)
                    if sync_error_ms:
                        errors.append(
                            {
                                "audio_asset_id": str(asset.id),
                                "transcript_turn_id": str(turn.id),
                                "expected_end_ms": expected_end_ms,
                                "actual_end_ms": actual_end_ms,
                                "sync_error_ms": sync_error_ms,
                            }
                        )
            cursor_ms += duration_ms
        return errors

    def _render_subtitle(self, cues: list[SubtitleCue], subtitle_format: str) -> str:
        if subtitle_format == "srt":
            return self._render_srt(cues)
        return self._render_vtt(cues)

    def _render_vtt(self, cues: list[SubtitleCue]) -> str:
        blocks = ["WEBVTT"]
        for cue in cues:
            timestamp = (
                f"{self._vtt_timestamp(cue.start_ms)} --> "
                f"{self._vtt_timestamp(cue.end_ms)}"
            )
            blocks.append(
                "\n".join(
                    [
                        str(cue.index),
                        timestamp,
                        f"{cue.speaker}: {cue.text}",
                    ]
                )
            )
        return "\n\n".join(blocks) + "\n"

    def _render_srt(self, cues: list[SubtitleCue]) -> str:
        blocks = []
        for cue in cues:
            timestamp = (
                f"{self._srt_timestamp(cue.start_ms)} --> "
                f"{self._srt_timestamp(cue.end_ms)}"
            )
            blocks.append(
                "\n".join(
                    [
                        str(cue.index),
                        timestamp,
                        f"{cue.speaker}: {cue.text}",
                    ]
                )
            )
        return "\n\n".join(blocks) + "\n"

    def _estimate_duration_ms(self, text: str) -> int:
        seconds = len(text.split()) / self.settings.words_per_second
        return max(1, int(seconds * 1000))

    def _speaker_label(
        self,
        participants: dict[str, ParticipantProfile],
        participant_id: str,
    ) -> str:
        participant = participants.get(participant_id)
        if participant is None:
            return participant_id
        return participant.display_name or participant.name

    def _clean_text(self, text: str) -> str:
        return " ".join(text.replace("-->", "->").split())

    def _mime_type(self, subtitle_format: str) -> str:
        if subtitle_format == "srt":
            return "application/x-subrip"
        return "text/vtt"

    def _vtt_timestamp(self, value_ms: int) -> str:
        hours, remainder = divmod(value_ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1_000)
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"

    def _srt_timestamp(self, value_ms: int) -> str:
        return self._vtt_timestamp(value_ms).replace(".", ",")
