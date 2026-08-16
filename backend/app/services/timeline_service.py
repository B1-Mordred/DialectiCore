from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.core.config import Settings
from app.domain.enums import AssetType, EpisodeStatus, QualitySeverity, TranscriptType
from app.domain.schemas import (
    Asset,
    AuditEvent,
    Episode,
    EpisodeTimeline,
    QualityResult,
    TimelineBuildRequest,
    TimelineUpdateRequest,
    TranscriptTurn,
    TranscriptVersion,
)
from app.services.object_storage import ObjectStore, create_object_store


class TimelineService:
    def __init__(
        self,
        settings: Settings,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.settings = settings
        self.object_store = object_store or create_object_store(settings)

    def build_timeline(
        self,
        episode: Episode,
        request: TimelineBuildRequest,
    ) -> Episode:
        transcript = self._target_transcript(episode, request)
        playable_turns = [turn for turn in transcript.turns if turn.status != "excluded"]
        if not playable_turns:
            raise ValueError("target transcript has no playable turns")

        existing = self._latest_timeline_asset(episode, transcript)
        if existing is not None and not request.regenerate:
            raise ValueError("timeline already built for target transcript")
        if existing is not None:
            existing.status = "replaced"
            existing.updated_at = datetime.now(UTC)

        episode.status = EpisodeStatus.building_timeline
        episode.audit_events.append(
            AuditEvent(
                episode_id=episode.id,
                event_type="workflow.stage.changed",
                actor=request.user_id or "system",
                details={"stage": EpisodeStatus.building_timeline.value},
            )
        )

        timeline = self._compose_timeline(episode, transcript, playable_turns)
        asset = self._store_timeline_asset(
            episode=episode,
            transcript=transcript,
            timeline=timeline,
            operation="build",
        )
        episode.assets.append(asset)
        qc = self._timeline_qc(episode, transcript, timeline, asset)
        episode.quality_results.append(qc)
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type="timeline.asset.built",
                    actor=request.user_id or "system",
                    details={
                        "transcript_version_id": str(transcript.id),
                        "language": transcript.language,
                        "asset_id": str(asset.id),
                        "segment_count": len(timeline["segments"]),
                        "duration_ms": timeline["duration_ms"],
                        "checksum": asset.checksum,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="timeline.qc.completed",
                    actor=request.user_id or "system",
                    details={
                        "transcript_version_id": str(transcript.id),
                        "asset_id": str(asset.id),
                        "status": qc.status,
                        "failure_count": qc.details["failure_count"],
                        "warning_count": qc.details["warning_count"],
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="workflow.stage.changed",
                    actor=request.user_id or "system",
                    details={"stage": EpisodeStatus.ready.value},
                ),
            ]
        )
        episode.status = EpisodeStatus.ready
        episode.updated_at = datetime.now(UTC)
        return episode

    def update_timeline(
        self,
        episode: Episode,
        request: TimelineUpdateRequest,
    ) -> Episode:
        transcript_id = request.timeline.get("transcript_version_id")
        if not isinstance(transcript_id, str):
            raise ValueError("timeline must include transcript_version_id")
        transcript = self._transcript_by_id(episode, UUID(transcript_id))
        request_timeline = self._relink_seated_panel_camera_media(
            episode,
            transcript,
            request.timeline,
        )
        raw_segments = request_timeline.get("segments")
        if not isinstance(raw_segments, list):
            raise ValueError("timeline must include a segments array")
        if not isinstance(request_timeline.get("tracks"), dict):
            raise ValueError("timeline must include tracks")
        segments = self._normalize_timeline_segments(raw_segments)
        tracks = self._normalize_timeline_tracks(request_timeline["tracks"])
        self._validate_timeline_track_resources(episode, segments, tracks)

        latest = self._latest_timeline_asset(episode, transcript)
        if latest is not None:
            latest.status = "replaced"
            latest.updated_at = datetime.now(UTC)

        timeline = {
            **request_timeline,
            "id": str(uuid4()),
            "schema_version": request.timeline.get("schema_version", "episode_timeline.v1"),
            "episode_id": str(episode.id),
            "transcript_version_id": str(transcript.id),
            "language": transcript.language,
            "editable": True,
            "edit_version": int(request.timeline.get("edit_version", 1)) + 1,
            "tracks": tracks,
            "segments": segments,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        timeline["duration_ms"] = self._timeline_duration_ms(timeline)
        asset = self._store_timeline_asset(
            episode=episode,
            transcript=transcript,
            timeline=timeline,
            operation="edit",
        )
        episode.assets.append(asset)
        qc = self._timeline_qc(episode, transcript, timeline, asset)
        episode.quality_results.append(qc)
        episode.audit_events.extend(
            [
                AuditEvent(
                    episode_id=episode.id,
                    event_type="timeline.asset.edited",
                    actor=request.user_id or "system",
                    details={
                        "transcript_version_id": str(transcript.id),
                        "asset_id": str(asset.id),
                        "previous_asset_id": str(latest.id) if latest is not None else None,
                        "comment": request.comment,
                        "segment_count": len(timeline["segments"]),
                        "duration_ms": timeline["duration_ms"],
                        "checksum": asset.checksum,
                    },
                ),
                AuditEvent(
                    episode_id=episode.id,
                    event_type="timeline.qc.completed",
                    actor=request.user_id or "system",
                    details={
                        "transcript_version_id": str(transcript.id),
                        "asset_id": str(asset.id),
                        "status": qc.status,
                        "failure_count": qc.details["failure_count"],
                        "warning_count": qc.details["warning_count"],
                    },
                ),
            ]
        )
        episode.updated_at = datetime.now(UTC)
        return episode

    def _relink_seated_panel_camera_media(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        timeline: dict,
    ) -> dict:
        """Bind edited panel directions to the current matching per-turn source.

        The native B1 clips are camera-specific.  An immutable timeline edit may
        therefore keep all editorial timing and parallel tracks, but it must not
        keep an older source whose requested camera differs from the UI choice.
        Planned/submitted sources are intentionally linkable; render preflight
        blocks until their provider result is complete and verified.
        """
        media = timeline.get("media") if isinstance(timeline, dict) else None
        directing = episode.definition.media.directing
        seated_panel = (
            isinstance(media, dict)
            and media.get("composition_policy") == "seated_studio_panel.v1"
        ) or (
            directing.mode == "studio_directed"
            and directing.studio_layout == "seated_panel"
        )
        if not seated_panel:
            return timeline
        relinked = copy.deepcopy(timeline)
        relinked_media = relinked.get("media")
        if not isinstance(relinked_media, dict):
            relinked_media = {}
            relinked["media"] = relinked_media
        relinked_media["composition_policy"] = "seated_studio_panel.v1"
        assets = list(reversed(episode.assets))
        asset_by_id = {str(asset.id): asset for asset in episode.assets}
        tracks = relinked.get("tracks")
        performance_asset_by_segment_id: dict[str, Asset] = {}
        if isinstance(tracks, dict):
            for clip in tracks.get("character_performance", []):
                if not isinstance(clip, dict):
                    continue
                segment_id = str(clip.get("linked_segment_id") or clip.get("id") or "")
                performance_id = str(
                    clip.get("speaking_asset_id") or clip.get("asset_id") or ""
                )
                performance = asset_by_id.get(performance_id)
                if (
                    segment_id
                    and performance is not None
                    and performance.status != "replaced"
                    and performance.generation_metadata.get("visual_role")
                    == "production_v2_speaking_character"
                ):
                    performance_asset_by_segment_id[segment_id] = performance
        camera_asset_by_segment_id: dict[str, str] = {}
        direction_by_segment_id: dict[str, tuple[str, str]] = {}
        for segment in relinked.get("segments", []):
            if not isinstance(segment, dict) or segment.get("segment_type") == "topic_primer":
                continue
            turn_id = str(segment.get("source_turn_id") or "")
            direction = segment.get("direction")
            desired_view = self._canonical_seated_panel_camera_view(
                (direction.get("view") if isinstance(direction, dict) else None)
                or segment.get("camera_view")
                or "speaker_medium"
            )
            camera_source = next(
                (
                    asset
                    for asset in assets
                    if asset.status != "replaced"
                    and asset.source_entity_type == "transcript_turn"
                    and asset.source_entity_id == turn_id
                    and asset.language == transcript.language
                    and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
                    and asset.generation_metadata.get("visual_role") == "video_primary"
                    and asset.generation_metadata.get("prompt_inputs", {}).get("camera_view")
                    == desired_view
                ),
                None,
            )
            if camera_source is None:
                continue
            segment_id = str(segment.get("id") or "")
            performance = performance_asset_by_segment_id.get(segment_id)
            if performance is None:
                legacy_performance = asset_by_id.get(
                    str(segment.get("studio_scene_asset_id") or "")
                )
                if (
                    legacy_performance is not None
                    and legacy_performance.status != "replaced"
                    and legacy_performance.source_entity_id == turn_id
                    and legacy_performance.generation_metadata.get("visual_role")
                    == "production_v2_speaking_character"
                ):
                    performance = legacy_performance
            old_primary_id = str(segment.get("video_asset_id") or "")
            camera_source_id = str(camera_source.id)
            primary = performance or camera_source
            primary_id = str(primary.id)
            shot_plan = self._shot_plan_for_asset(camera_source)
            segment["video_asset_id"] = primary_id
            segment["camera_source_asset_id"] = camera_source_id
            segment["studio_panel_scene_asset_id"] = (
                shot_plan.get("studio_panel_scene_asset_id")
                or segment.get("studio_panel_scene_asset_id")
            )
            segment["camera_view"] = desired_view
            segment["camera_action"] = str(
                (direction.get("action") if isinstance(direction, dict) else None)
                or segment.get("camera_action")
                or "cut"
            )
            if not isinstance(direction, dict):
                direction = {}
                segment["direction"] = direction
            direction.update(
                {
                    "view": desired_view,
                    "action": segment["camera_action"],
                    "speaker_mouth_mode": "audio_driven_seated_panel",
                }
            )
            preserved_layers = [
                layer
                for layer in segment.get("visual_layers", [])
                if isinstance(layer, dict)
                and layer.get("role") not in {"video_primary", "studio_scene"}
                and str(layer.get("asset_id") or "") != old_primary_id
            ]
            segment["visual_layers"] = [
                *self._seated_panel_visual_layers(primary),
                *preserved_layers,
            ]
            fingerprints = segment.get("media_fingerprints")
            if not isinstance(fingerprints, dict):
                fingerprints = {}
                segment["media_fingerprints"] = fingerprints
            fingerprints["video_primary"] = self._asset_fingerprint(primary)
            fingerprints["camera_source"] = self._asset_fingerprint(camera_source)
            camera_asset_by_segment_id[segment_id] = camera_source_id
            direction_by_segment_id[str(segment.get("id") or "")] = (
                desired_view,
                segment["camera_action"],
            )
        if isinstance(tracks, dict):
            for clip in tracks.get("camera_direction", []):
                if not isinstance(clip, dict):
                    continue
                clip_id = str(clip.get("id") or "")
                linked_segment_id = str(clip.get("linked_segment_id") or "")
                if not linked_segment_id:
                    if clip_id in camera_asset_by_segment_id:
                        linked_segment_id = clip_id
                    elif (
                        clip_id.startswith("camera-")
                        and clip_id[7:] in camera_asset_by_segment_id
                    ):
                        linked_segment_id = clip_id[7:]
                if linked_segment_id in camera_asset_by_segment_id:
                    clip["linked_segment_id"] = linked_segment_id
                    clip["camera_source_asset_id"] = camera_asset_by_segment_id[
                        linked_segment_id
                    ]
                    clip.pop("camera_plate_asset_id", None)
                if linked_segment_id in direction_by_segment_id:
                    clip["view"], clip["action"] = direction_by_segment_id[
                        linked_segment_id
                    ]
        return relinked

    @staticmethod
    def _canonical_seated_panel_camera_view(value: object) -> str:
        view = str(value or "speaker_medium")
        return {
            "speaker_centered": "speaker_medium",
            "speaker_close": "speaker_close_up",
        }.get(view, view)

    def _normalize_timeline_segments(self, segments: list) -> list[dict]:
        normalized: list[dict] = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise ValueError("timeline segments must be objects")
            start_ms = int(segment.get("start_ms") or 0)
            end_ms = int(segment.get("end_ms") or 0)
            if end_ms <= start_ms:
                raise ValueError("timeline segment end_ms must be greater than start_ms")
            normalized.append(
                {
                    **segment,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "duration_ms": end_ms - start_ms,
                }
            )
        return normalized

    def _normalize_timeline_tracks(self, tracks: dict) -> dict:
        normalized: dict = {}
        for track_name, raw_clips in tracks.items():
            if not isinstance(track_name, str) or not track_name:
                raise ValueError("timeline track names must be non-empty strings")
            if not isinstance(raw_clips, list):
                raise ValueError(f"timeline track {track_name} must be an array")
            if track_name not in {
                "dialogue",
                "character_performance",
                "camera_direction",
                "broll_content",
                "broll_presentation",
                "screen_graphics",
                "caption_clips",
            }:
                normalized[track_name] = raw_clips
                continue
            clips: list[dict] = []
            seen_ids: set[str] = set()
            for raw_clip in raw_clips:
                if not isinstance(raw_clip, dict):
                    raise ValueError(f"timeline track {track_name} clips must be objects")
                clip_id = str(raw_clip.get("id") or "").strip()
                if not clip_id or clip_id in seen_ids:
                    raise ValueError(f"timeline track {track_name} clip IDs must be unique")
                start_ms = int(raw_clip.get("start_ms") or 0)
                end_ms = int(raw_clip.get("end_ms") or 0)
                if start_ms < 0 or end_ms <= start_ms:
                    raise ValueError(
                        f"timeline track {track_name} clip end_ms must exceed start_ms"
                    )
                source_in_ms = int(raw_clip.get("source_in_ms") or 0)
                source_out_ms = int(
                    raw_clip.get("source_out_ms") or source_in_ms + end_ms - start_ms
                )
                if source_in_ms < 0 or source_out_ms <= source_in_ms:
                    raise ValueError(f"timeline track {track_name} source range must be positive")
                transition_duration_ms = raw_clip.get("transition_duration_ms")
                if transition_duration_ms is not None:
                    transition_duration_ms = int(transition_duration_ms)
                    if not 0 <= transition_duration_ms <= 5_000:
                        raise ValueError(
                            f"timeline track {track_name} transition duration must be 0-5000ms"
                        )
                if track_name == "camera_direction":
                    action = str(raw_clip.get("action") or "cut")
                    if action not in {
                        "cut",
                        "dissolve",
                        "slow_push",
                        "slow_pull",
                        "fly_in",
                        "pan_left",
                        "pan_right",
                        "pan_to_participant",
                    }:
                        raise ValueError(f"unsupported camera action: {action}")
                    if action == "pan_to_participant" and not raw_clip.get(
                        "target_participant_id"
                    ):
                        raise ValueError(
                            "pan_to_participant camera clips require target_participant_id"
                        )
                    easing = str(raw_clip.get("easing") or "ease_in_out")
                    if easing not in {"linear", "ease_in", "ease_out", "ease_in_out"}:
                        raise ValueError(f"unsupported camera easing: {easing}")
                if track_name == "broll_presentation":
                    mode = raw_clip.get("mode")
                    if mode is not None and mode not in {
                        "rear_screen",
                        "fullscreen",
                        "enter",
                        "exit",
                        "roundtrip",
                    }:
                        raise ValueError(f"unsupported B-roll presentation mode: {mode}")
                    fit = str(raw_clip.get("fit") or "contain")
                    if fit not in {"contain", "cover"}:
                        raise ValueError(f"unsupported B-roll fit: {fit}")
                    focal_y = float(raw_clip.get("focal_y", 0.5))
                    if not 0.0 <= focal_y <= 1.0:
                        raise ValueError("B-roll focal_y must be between 0 and 1")
                if track_name == "screen_graphics":
                    if raw_clip.get("kind") != "show_identity":
                        raise ValueError("unsupported screen graphic kind")
                    if not raw_clip.get("asset_id"):
                        raise ValueError("show identity clips require asset_id")
                clips.append(
                    {
                        **raw_clip,
                        "id": clip_id,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "duration_ms": end_ms - start_ms,
                        "source_in_ms": source_in_ms,
                        "source_out_ms": source_out_ms,
                        **(
                            {"transition_duration_ms": transition_duration_ms}
                            if transition_duration_ms is not None
                            else {}
                        ),
                        **(
                            {
                                "fit": str(raw_clip.get("fit") or "contain"),
                                "focal_y": float(raw_clip.get("focal_y", 0.5)),
                            }
                            if track_name == "broll_presentation"
                            else {}
                        ),
                    }
                )
                seen_ids.add(clip_id)
            normalized[track_name] = sorted(
                clips, key=lambda clip: (clip["start_ms"], clip["end_ms"], clip["id"])
            )
        self._validate_parallel_track_links(normalized)
        return normalized

    @staticmethod
    def _validate_parallel_track_links(tracks: dict) -> None:
        content_ids = {
            str(clip.get("id"))
            for clip in tracks.get("broll_content", [])
            if isinstance(clip, dict)
        }
        for clip in tracks.get("broll_presentation", []):
            content_clip_id = str(clip.get("content_clip_id") or "")
            if content_clip_id and content_clip_id not in content_ids:
                raise ValueError(
                    f"B-roll presentation clip {clip.get('id')} has an orphaned content link"
                )
        for graphic in tracks.get("screen_graphics", []):
            if graphic.get("kind") != "show_identity":
                continue
            for presentation in tracks.get("broll_presentation", []):
                if max(int(graphic["start_ms"]), int(presentation["start_ms"])) < min(
                    int(graphic["end_ms"]), int(presentation["end_ms"])
                ):
                    raise ValueError(
                        "B-roll presentation cannot overlap the branded participant introduction"
                    )

    @staticmethod
    def _validate_timeline_track_resources(
        episode: Episode,
        segments: list[dict],
        tracks: dict,
    ) -> None:
        duration_ms = max((int(segment["end_ms"]) for segment in segments), default=0)
        assets_by_id = {str(asset.id): asset for asset in episode.assets}
        participant_ids = {participant.id for participant in episode.participants}
        for track_name, clips in tracks.items():
            if not isinstance(clips, list):
                continue
            for clip in clips:
                if isinstance(clip, dict) and int(clip.get("end_ms") or 0) > duration_ms:
                    raise ValueError(
                        f"timeline track {track_name} clip {clip.get('id')} "
                        "exceeds programme duration"
                    )
        for clip in tracks.get("broll_content", []):
            asset = assets_by_id.get(str(clip.get("asset_id") or ""))
            if (
                asset is not None
                and asset.duration_ms is not None
                and int(clip.get("source_out_ms") or 0) > asset.duration_ms
                and not clip.get("loop")
            ):
                raise ValueError(f"B-roll clip {clip.get('id')} exceeds its source duration")
        thumbnail_markers = 0
        for clip in tracks.get("screen_graphics", []):
            if clip.get("kind") != "show_identity":
                continue
            asset = assets_by_id.get(str(clip.get("asset_id") or ""))
            if (
                asset is None
                or asset.status != "completed"
                or asset.generation_metadata.get("visual_role") != "show_identity_slate"
                or asset.generation_metadata.get("episode_title") != episode.title
            ):
                raise ValueError(
                    f"screen graphic {clip.get('id')} requires this episode's immutable "
                    "show-identity slate"
                )
            if clip.get("thumbnail_candidate") is True:
                thumbnail_markers += 1
        if thumbnail_markers > 1:
            raise ValueError("timeline may contain only one branded thumbnail marker")
        for clip in tracks.get("camera_direction", []):
            target_id = clip.get("target_participant_id")
            if target_id is not None and target_id not in participant_ids:
                raise ValueError(
                    f"camera clip {clip.get('id')} targets an unknown participant"
                )
            angle_id = str(clip.get("angle_id") or "frontal")
            if angle_id == "frontal":
                continue
            if str(clip.get("view") or "").startswith("speaker_"):
                raise ValueError("alternate camera plates cannot be used for speaking closeups")
            plate_asset_id = str(clip.get("camera_plate_asset_id") or "")
            plate = assets_by_id.get(plate_asset_id)
            if (
                plate is None
                or plate.generation_metadata.get("visual_role") != "studio_camera_plate"
                or plate.generation_metadata.get("angle_id") != angle_id
                or plate.generation_metadata.get("render_ready") is not True
                or not isinstance(plate.generation_metadata.get("calibration"), dict)
            ):
                raise ValueError(
                    f"camera angle {angle_id} requires its exact approved calibrated plate"
                )
            for graphic in tracks.get("screen_graphics", []):
                if max(int(clip["start_ms"]), int(graphic["start_ms"])) < min(
                    int(clip["end_ms"]), int(graphic["end_ms"])
                ):
                    raise ValueError(
                        "alternate camera plates cannot overlap screen graphics until the "
                        "approved rear-screen calibration has been materialized"
                    )

    def latest_timeline_payload(
        self,
        episode: Episode,
        transcript_version_id: UUID | None = None,
        language: str | None = None,
    ) -> dict:
        transcript = self._target_transcript(
            episode,
            TimelineBuildRequest(
                transcript_version_id=transcript_version_id,
                language=language,
            ),
        )
        asset = self._latest_timeline_asset(episode, transcript)
        if asset is None:
            raise ValueError("timeline not found")
        timeline = asset.generation_metadata.get("timeline_json")
        if not isinstance(timeline, dict):
            timeline = self._load_timeline_json(asset)
        return {
            "asset": asset,
            "timeline": timeline,
            "timeline_entity": self._timeline_entity_from_asset(asset, timeline),
        }

    def _compose_timeline(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        playable_turns: list[TranscriptTurn],
    ) -> dict:
        seated_panel = (
            episode.definition.media.directing.mode == "studio_directed"
            and episode.definition.media.directing.studio_layout == "seated_panel"
        )
        audio_by_turn = self._completed_audio_assets_by_turn(episode, transcript)
        primary_video_by_turn = self._completed_visual_assets_by_turn(
            episode,
            transcript,
            visual_role="video_primary",
        )
        broll_by_turn = self._completed_visual_assets_by_turn(
            episode,
            transcript,
            visual_role="wall_screen_broll" if seated_panel else "broll",
        )
        reaction_by_participant = self._completed_reaction_assets_by_participant(
            episode,
            transcript,
        )
        studio_scene = self._completed_studio_scene_asset(episode, transcript)
        studio_group_cutaway = self._completed_studio_group_cutaway_asset(episode, transcript)
        subtitle_asset = self._completed_subtitle_asset(episode, transcript)
        citation_cards_by_turn = (
            self._completed_citation_card_assets_by_turn(episode, transcript)
            if episode.definition.media.evidence_presentation == "burned_overlays"
            else {}
        )
        primer_render = self._latest_completed_primer_render(episode, transcript.language)
        # Once the primer is rendered, it is the opening. Its source clips must not
        # be reused as arbitrary visuals for the first discussion turn.
        opening_visuals = (
            [] if primer_render is not None else self._completed_opening_visual_assets(episode)
        )
        if seated_panel:
            # Discussion always begins on the physical studio set. The primer owns
            # full-screen topic media; a generic opening image must not displace
            # the moderator's post-primer hand-off.
            opening_visuals = []

        cursor_ms = 0
        segments: list[dict] = []
        tracks = {
            "video_primary": [],
            "video_secondary": [],
            "audio_dialogue": [],
            "audio_music": [],
            "audio_effects": [],
            "captions": [],
            "graphics": [],
            "citations": [],
            "chapters": [],
            "dialogue": [],
            "character_performance": [],
            "camera_direction": [],
            "broll_content": [],
            "broll_presentation": [],
            "screen_graphics": [],
            "caption_clips": [],
        }
        chapters: list[dict] = []
        previous_turn_type: str | None = None

        if primer_render is not None:
            primer_duration_ms = int(primer_render.duration_ms or 0)
            primer_segment = {
                "id": f"segment-primer-{primer_render.id}",
                "start_ms": cursor_ms,
                "end_ms": cursor_ms + primer_duration_ms,
                "duration_ms": primer_duration_ms,
                "segment_type": "topic_primer",
                "source_primer_render_id": str(primer_render.id),
                "audio_asset_id": str(primer_render.id),
                "audio_source_offset_ms": 0,
                "video_asset_id": str(primer_render.id),
                "secondary_visual_asset_id": None,
                "reaction_visual_asset_id": None,
                "studio_scene_asset_id": None,
                "fallback_video_asset_id": None,
                "subtitle_asset_id": None,
                "media_fingerprints": {
                    "primer_render": self._asset_fingerprint(primer_render),
                },
                "character_reference_image_uri": None,
                "character_reference_images": {},
                "camera_transition": "source_reveal",
                "visual_role": "topic_primer",
                "visual_layers": [
                    {
                        "role": "video_primary",
                        "asset_id": str(primer_render.id),
                        "purpose": "completed_topic_primer",
                    }
                ],
                "graphics": [],
                "citations": [],
                "citation_overlay_asset_ids": [],
            }
            segments.append(primer_segment)
            for track in ("video_primary", "audio_dialogue"):
                tracks[track].append(primer_segment["id"])
            primer_chapter = {
                "id": "chapter-primer",
                "start_ms": cursor_ms,
                "title": "Topic primer",
                "segment_id": primer_segment["id"],
            }
            chapters.append(primer_chapter)
            tracks["chapters"].append(primer_chapter["id"])
            cursor_ms += primer_duration_ms

        discussion_start_ms = cursor_ms
        for index, turn in enumerate(playable_turns, start=1):
            audio_asset = audio_by_turn.get(str(turn.id))
            generated_primary_video_asset = primary_video_by_turn.get(str(turn.id))
            opening_visual_asset = (
                opening_visuals[0]
                if index == 1 and episode.definition.media.opening.enabled and opening_visuals
                else None
            )
            primary_video_asset = opening_visual_asset or generated_primary_video_asset
            broll_asset = broll_by_turn.get(str(turn.id))
            shot_plan = self._shot_plan_for_asset(primary_video_asset)
            reaction_asset = (
                reaction_by_participant.get(turn.speaker_participant_id)
                if shot_plan.get("reusable_reaction_asset_id")
                else None
            )
            group_cutaway_asset = (
                studio_group_cutaway if shot_plan.get("studio_group_cutaway_asset_id") else None
            )
            fallback_asset = None if seated_panel else broll_asset or reaction_asset or studio_scene
            duration_ms = (
                audio_asset.duration_ms
                if audio_asset is not None and audio_asset.duration_ms
                else self._estimate_duration_ms(turn.text)
            )
            segment_id = f"segment-{index:04d}-{turn.id}"
            citation_card_assets = citation_cards_by_turn.get(str(turn.id), [])
            segment = {
                "id": segment_id,
                "start_ms": cursor_ms,
                "end_ms": cursor_ms + duration_ms,
                "duration_ms": duration_ms,
                "speaker_id": turn.speaker_participant_id,
                "turn_type": turn.turn_type.value if turn.turn_type is not None else None,
                "source_turn_id": str(turn.id),
                "source_discussion_turn_ids": [
                    str(turn_id) for turn_id in turn.source_discussion_turn_ids
                ],
                "audio_asset_id": str(audio_asset.id) if audio_asset is not None else None,
                "video_asset_id": (
                    str(primary_video_asset.id) if primary_video_asset is not None else None
                ),
                "opening_visual_asset_ids": (
                    [str(asset.id) for asset in opening_visuals]
                    if index == 1 and episode.definition.media.opening.enabled
                    else []
                ),
                "segment_type": (
                    "episode_opening"
                    if opening_visual_asset
                    else "post_primer_host_bridge"
                    if turn.turn_type is not None and turn.turn_type.value == "post_primer_bridge"
                    else "discussion"
                ),
                "secondary_visual_asset_id": (
                    str(broll_asset.id) if broll_asset is not None else None
                ),
                "reaction_visual_asset_id": (
                    str(reaction_asset.id) if reaction_asset is not None else None
                ),
                "studio_scene_asset_id": str(studio_scene.id) if studio_scene else None,
                "studio_group_cutaway_asset_id": (
                    str(group_cutaway_asset.id) if group_cutaway_asset is not None else None
                ),
                "studio_panel_scene_asset_id": shot_plan.get("studio_panel_scene_asset_id"),
                "wall_screen_visual_asset_id": (
                    str(broll_asset.id) if seated_panel and broll_asset is not None else None
                ),
                "studio_reference_image_uri": episode.definition.media.scene_reference_image_uri,
                "fallback_video_asset_id": str(fallback_asset.id) if fallback_asset else None,
                "subtitle_asset_id": str(subtitle_asset.id) if subtitle_asset is not None else None,
                "media_fingerprints": self._segment_media_fingerprints(
                    audio_asset=audio_asset,
                    primary_video_asset=primary_video_asset,
                    broll_asset=broll_asset,
                    reaction_asset=reaction_asset,
                    studio_scene=studio_scene,
                    studio_group_cutaway=group_cutaway_asset,
                    fallback_asset=fallback_asset,
                    subtitle_asset=subtitle_asset,
                    citation_card_assets=citation_card_assets,
                ),
                "character_reference_image_uri": self._character_reference_image_uri(
                    primary_video_asset,
                    reaction_asset,
                    broll_asset,
                ),
                "character_reference_images": self._character_reference_images(
                    primary_video_asset,
                    reaction_asset,
                    broll_asset,
                ),
                "camera_transition": shot_plan.get(
                    "camera_transition", "source_reveal" if opening_visual_asset else "cut"
                ),
                "camera_view": shot_plan.get("camera_view", "speaker_medium"),
                "camera_action": shot_plan.get("camera_action", "cut"),
                "direction": {
                    "schema_version": "dialecticore.scene_direction.v1",
                    "view": shot_plan.get("camera_view", "speaker_medium"),
                    "action": shot_plan.get("camera_action", "cut"),
                    "requirements": shot_plan.get("requires", {}),
                    "speaker_mouth_mode": shot_plan.get(
                        "speaker_mouth_mode", "audio_driven_single_portrait"
                    ),
                    "group_cutaway_audio_mode": shot_plan.get(
                        "group_cutaway_audio_mode", "not_used"
                    ),
                },
                "visual_role": "broll" if opening_visual_asset else "video_primary",
                "visual_layers": (
                    self._seated_panel_visual_layers(primary_video_asset)
                    if seated_panel
                    else self._visual_layers(
                        primary_video_asset=primary_video_asset,
                        broll_asset=broll_asset,
                        reaction_asset=reaction_asset,
                        studio_scene=studio_scene,
                        studio_group_cutaway=group_cutaway_asset,
                        studio_reference_image_uri=episode.definition.media.scene_reference_image_uri,
                        fallback_asset=fallback_asset,
                    )
                ),
                "graphics": self._graphics_for_turn(
                    turn,
                    opening=opening_visual_asset is not None,
                ),
                "citations": self._citations_for_turn(turn),
                "citation_overlay_asset_ids": [str(asset.id) for asset in citation_card_assets],
            }
            turn_segments = [segment]
            if (
                not seated_panel
                and index == 1
                and episode.definition.media.opening.enabled
                and len(opening_visuals) > 1
            ):
                durations = self._split_duration_ms(duration_ms, len(opening_visuals))
                turn_segments = []
                offset_ms = 0
                for opening_index, (opening_asset, piece_duration_ms) in enumerate(
                    zip(opening_visuals, durations, strict=True), start=1
                ):
                    piece_start_ms = cursor_ms + offset_ms
                    piece = {
                        **segment,
                        "id": f"{segment_id}-opening-{opening_index:02d}",
                        "start_ms": piece_start_ms,
                        "end_ms": piece_start_ms + piece_duration_ms,
                        "duration_ms": piece_duration_ms,
                        "audio_source_offset_ms": offset_ms,
                        "video_asset_id": str(opening_asset.id),
                        "media_fingerprints": self._segment_media_fingerprints(
                            audio_asset=audio_asset,
                            primary_video_asset=opening_asset,
                            broll_asset=broll_asset,
                            reaction_asset=reaction_asset,
                            studio_scene=studio_scene,
                            studio_group_cutaway=group_cutaway_asset,
                            fallback_asset=fallback_asset,
                            subtitle_asset=subtitle_asset,
                            citation_card_assets=citation_card_assets,
                        ),
                        "visual_layers": self._visual_layers(
                            primary_video_asset=opening_asset,
                            broll_asset=broll_asset,
                            reaction_asset=reaction_asset,
                            studio_scene=studio_scene,
                            studio_group_cutaway=group_cutaway_asset,
                            studio_reference_image_uri=episode.definition.media.scene_reference_image_uri,
                            fallback_asset=fallback_asset,
                        ),
                        "camera_transition": "source_reveal" if opening_index == 1 else "dissolve",
                        "graphics": self._graphics_for_turn(turn, opening=opening_index == 1),
                        "citations": self._citations_for_turn(turn) if opening_index == 1 else [],
                        "citation_overlay_asset_ids": (
                            [str(asset.id) for asset in citation_card_assets]
                            if opening_index == 1
                            else []
                        ),
                    }
                    turn_segments.append(piece)
                    offset_ms += piece_duration_ms
            for timeline_segment in turn_segments:
                segments.append(timeline_segment)
                timeline_segment_id = timeline_segment["id"]
                tracks["video_primary"].append(timeline_segment_id)
                if broll_asset is not None:
                    tracks["video_secondary"].append(timeline_segment_id)
                if audio_asset is not None:
                    tracks["audio_dialogue"].append(timeline_segment_id)
                if subtitle_asset is not None:
                    tracks["captions"].append(timeline_segment_id)
                if timeline_segment["graphics"] or timeline_segment["citation_overlay_asset_ids"]:
                    tracks["graphics"].append(timeline_segment_id)
                if timeline_segment["citations"]:
                    tracks["citations"].append(timeline_segment_id)
                self._append_parallel_directing_clips(
                    tracks=tracks,
                    segment=timeline_segment,
                    broll_asset=broll_asset,
                    seated_panel=seated_panel,
                )
            current_turn_type = self._chapter_key(index)
            if current_turn_type != previous_turn_type:
                chapter = {
                    "id": f"chapter-{len(chapters) + 1:02d}",
                    "start_ms": cursor_ms,
                    "title": current_turn_type,
                    "source_turn_id": str(turn.id),
                }
                chapters.append(chapter)
                tracks["chapters"].append(chapter["id"])
                previous_turn_type = current_turn_type
            cursor_ms += duration_ms

        self._apply_branded_participant_introduction(episode, segments, tracks)

        return {
            "id": str(uuid4()),
            "schema_version": "episode_timeline.v3",
            "episode_id": str(episode.id),
            "transcript_version_id": str(transcript.id),
            "language": transcript.language,
            "editable": True,
            "edit_version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "duration_ms": cursor_ms,
            "media": {
                "width": episode.definition.media.width,
                "height": episode.definition.media.height,
                "fps": episode.definition.media.fps,
                "aspect_ratio": episode.definition.media.aspect_ratio,
                "subtitle_mode": episode.definition.media.subtitle_mode,
                "evidence_presentation": episode.definition.media.evidence_presentation,
                "composition_policy": (
                    "seated_studio_panel.v1" if seated_panel else "studio_camera_cuts.v1"
                ),
                "directing": episode.definition.media.directing.model_dump(mode="json"),
            },
            "tracks": tracks,
            "track_schema_version": "dialecticore.parallel_directing_tracks.v2",
            "segments": segments,
            "chapters": chapters,
            "program_structure": {
                "primer": {
                    "included": primer_render is not None,
                    "render_asset_id": str(primer_render.id) if primer_render else None,
                    "duration_ms": int(primer_render.duration_ms or 0) if primer_render else 0,
                },
                "discussion": {
                    "start_ms": discussion_start_ms,
                    "duration_ms": cursor_ms - discussion_start_ms,
                },
                "post_primer_host_bridge": {
                    "included": any(
                        segment.get("segment_type") == "post_primer_host_bridge"
                        for segment in segments
                    ),
                    "segment_id": next(
                        (
                            segment.get("id")
                            for segment in segments
                            if segment.get("segment_type") == "post_primer_host_bridge"
                        ),
                        None,
                    ),
                },
            },
        }

    def _apply_branded_participant_introduction(
        self,
        episode: Episode,
        segments: list[dict],
        tracks: dict,
    ) -> None:
        opening = episode.definition.media.opening
        introduce_participants = (
            opening.post_primer_bridge.introduce_participants
            if opening.post_primer_bridge.introduce_participants is not None
            else opening.introduce_participants
        )
        if not introduce_participants:
            return
        intro = next(
            (
                segment
                for segment in segments
                if segment.get("segment_type") == "post_primer_host_bridge"
            ),
            None,
        )
        if intro is None:
            return
        slate = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.image
                and asset.status == "completed"
                and asset.generation_metadata.get("visual_role") == "show_identity_slate"
            ),
            None,
        )
        if slate is None:
            raise ValueError(
                "participant introduction requires a generated show identity slate"
            )
        start_ms = int(intro["start_ms"])
        end_ms = int(intro["end_ms"])
        duration_ms = end_ms - start_ms
        intro["show_identity_slate_asset_id"] = str(slate.id)
        intro["thumbnail_candidate"] = True
        intro["camera_view"] = "establishing_wide"
        intro["camera_action"] = "fly_in"
        intro["direction"] = {
            **(intro.get("direction") if isinstance(intro.get("direction"), dict) else {}),
            "view": "establishing_wide",
            "action": "fly_in",
            "framing_policy": "branded_participant_introduction.v1",
        }
        camera = next(
            (
                clip
                for clip in tracks.get("camera_direction", [])
                if clip.get("linked_segment_id") == intro.get("id")
            ),
            None,
        )
        if camera is not None:
            camera.update(
                {
                    "view": "establishing_wide",
                    "action": "fly_in",
                    "angle_id": "frontal",
                    "easing": "ease_in_out",
                    "framing_policy": "branded_participant_introduction.v1",
                }
            )
        tracks["screen_graphics"].append(
            {
                "id": f"show-identity-{intro['id']}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": duration_ms,
                "source_in_ms": 0,
                "source_out_ms": duration_ms,
                "kind": "show_identity",
                "asset_id": str(slate.id),
                "linked_segment_id": intro["id"],
                "thumbnail_candidate": True,
                "rear_screen_priority": 100,
            }
        )
        tracks["broll_presentation"] = [
            clip
            for clip in tracks.get("broll_presentation", [])
            if not max(start_ms, int(clip["start_ms"])) < min(end_ms, int(clip["end_ms"]))
        ]

    def _visual_layers(
        self,
        primary_video_asset: Asset | None,
        broll_asset: Asset | None,
        reaction_asset: Asset | None,
        studio_scene: Asset | None,
        studio_group_cutaway: Asset | None,
        studio_reference_image_uri: str | None,
        fallback_asset: Asset | None,
    ) -> list[dict]:
        layers = []
        if studio_scene is None and studio_reference_image_uri:
            layers.append(
                {
                    "role": "studio_scene",
                    "asset_id": None,
                    "storage_uri": studio_reference_image_uri,
                    "asset_type": AssetType.image.value,
                    "purpose": "configured_studio_reference",
                    "reference_only": True,
                }
            )
        for role, asset, purpose in (
            ("studio_scene", studio_scene, "base"),
            ("studio_group_cutaway", studio_group_cutaway, "silent_panel_cutaway"),
            ("video_primary", primary_video_asset, "talking_head"),
            ("broll", broll_asset, "picture_in_picture"),
            ("reaction_loop", reaction_asset, "reaction_picture_in_picture"),
            ("fallback", fallback_asset, "fallback"),
        ):
            if asset is None:
                continue
            asset_id = str(asset.id)
            if any(layer.get("asset_id") == asset_id for layer in layers):
                continue
            layers.append(
                {
                    "role": role,
                    "asset_id": asset_id,
                    "asset_type": asset.asset_type.value,
                    "purpose": purpose,
                    "character_reference_image_uri": self._asset_reference_image_uri(asset),
                    "character_reference_images": self._asset_reference_images(asset),
                }
            )
        return layers

    def _seated_panel_visual_layers(
        self,
        primary_video_asset: Asset | None,
    ) -> list[dict]:
        """Use native B1 speaker coverage without floating overlays."""
        layers: list[dict] = []
        if primary_video_asset is not None:
            layers.append(
                {
                    "role": "video_primary",
                    "asset_id": str(primary_video_asset.id),
                    "asset_type": primary_video_asset.asset_type.value,
                    "purpose": "audio_driven_seated_panel_full_frame",
                    "character_reference_image_uri": self._asset_reference_image_uri(
                        primary_video_asset
                    ),
                    "character_reference_images": self._asset_reference_images(primary_video_asset),
                }
            )
        return layers

    def _append_parallel_directing_clips(
        self,
        *,
        tracks: dict,
        segment: dict,
        broll_asset: Asset | None,
        seated_panel: bool,
    ) -> None:
        """Index independently editable clips on the episode master clock.

        Sequential segments remain the compatibility and dialogue-rendering
        layer.  These clips add overlap without cutting a dialogue turn into
        artificial before/insert/after segments.
        """
        segment_id = str(segment["id"])
        start_ms = int(segment["start_ms"])
        end_ms = int(segment["end_ms"])
        duration_ms = end_ms - start_ms
        common = {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": duration_ms,
            "source_in_ms": int(segment.get("audio_source_offset_ms") or 0),
            "source_out_ms": int(segment.get("audio_source_offset_ms") or 0) + duration_ms,
            "linked_segment_id": segment_id,
            "source_turn_id": segment.get("source_turn_id"),
        }
        if segment.get("audio_asset_id"):
            tracks["dialogue"].append(
                {
                    "id": f"dialogue-{segment_id}",
                    **common,
                    "asset_id": segment["audio_asset_id"],
                    "audio_mode": "primary_dialogue",
                }
            )
        if segment.get("video_asset_id"):
            tracks["character_performance"].append(
                {
                    "id": f"character-{segment_id}",
                    **common,
                    "asset_id": segment["video_asset_id"],
                    "participant_id": segment.get("speaker_id"),
                    "performance_policy": "normalized_seated_character.v2",
                }
            )
        direction = segment.get("direction") if isinstance(segment.get("direction"), dict) else {}
        tracks["camera_direction"].append(
            {
                "id": f"camera-{segment_id}",
                **common,
                "speaker_participant_id": segment.get("speaker_id"),
                "view": direction.get("view") or segment.get("camera_view") or "speaker_medium",
                "action": direction.get("action") or segment.get("camera_action") or "cut",
                "framing_policy": "active_speaker_centered.v2",
            }
        )
        if segment.get("subtitle_asset_id"):
            tracks["caption_clips"].append(
                {
                    "id": f"caption-{segment_id}",
                    **common,
                    "asset_id": segment["subtitle_asset_id"],
                }
            )
        if broll_asset is None:
            return

        # Start after the shot establishes and finish before the next dialogue
        # cut.  Content and presentation share a playback link, so changing the
        # presentation envelope never restarts the source video.
        inset_ms = min(1_000, max(0, duration_ms // 8))
        broll_start_ms = start_ms + inset_ms
        broll_end_ms = max(broll_start_ms + 1, end_ms - inset_ms)
        broll_duration_ms = broll_end_ms - broll_start_ms
        content_id = f"broll-content-{segment_id}"
        tracks["broll_content"].append(
            {
                "id": content_id,
                "start_ms": broll_start_ms,
                "end_ms": broll_end_ms,
                "duration_ms": broll_duration_ms,
                "source_in_ms": 0,
                "source_out_ms": broll_duration_ms,
                "asset_id": str(broll_asset.id),
                "linked_segment_id": segment_id,
                "source_turn_id": segment.get("source_turn_id"),
                "loop": broll_asset.asset_type != AssetType.video,
                "audio_mode": "muted",
                "provenance": broll_asset.generation_metadata.get("provenance"),
            }
        )
        tracks["broll_presentation"].append(
            {
                "id": f"broll-presentation-{segment_id}",
                "start_ms": broll_start_ms,
                "end_ms": broll_end_ms,
                "duration_ms": broll_duration_ms,
                "source_in_ms": 0,
                "source_out_ms": broll_duration_ms,
                "content_clip_id": content_id,
                "linked_segment_id": segment_id,
                "crop_mode": "cover",
                "overscan": 1.02,
                "transition_duration_ms": 1_500,
                "keyframes": [
                    {
                        "time_ms": broll_start_ms,
                        "state": "rear_screen" if seated_panel else "fullscreen",
                        "easing": "ease_in_out",
                        "transition_duration_ms": 1_500,
                    },
                    {
                        "time_ms": broll_end_ms,
                        "state": "rear_screen" if seated_panel else "fullscreen",
                        "easing": "ease_in_out",
                        "transition_duration_ms": 1_500,
                    },
                ],
            }
        )

    def _seated_panel_broll_insert_segments(
        self,
        *,
        segment: dict,
        primary_video_asset: Asset | None,
        studio_scene_asset: Asset,
        wall_screen_asset: Asset,
    ) -> list[dict]:
        """Cut briefly to the physical rear screen, then return to the speaker."""
        duration_ms = int(segment["duration_ms"])
        insert_duration_ms = min(4_000, max(2_500, duration_ms // 3))
        opening_duration_ms = (duration_ms - insert_duration_ms) // 2
        closing_duration_ms = duration_ms - insert_duration_ms - opening_duration_ms
        durations = [opening_duration_ms, insert_duration_ms, closing_duration_ms]
        offsets = [0, opening_duration_ms, opening_duration_ms + insert_duration_ms]
        pieces: list[dict] = []
        for piece_index, (piece_duration_ms, offset_ms) in enumerate(
            zip(durations, offsets, strict=True),
            start=1,
        ):
            piece_start_ms = int(segment["start_ms"]) + offset_ms
            is_screen_insert = piece_index == 2
            piece_suffix = "wall-screen" if is_screen_insert else f"speaker-{piece_index}"
            piece = {
                **segment,
                "id": f"{segment['id']}-{piece_suffix}",
                "start_ms": piece_start_ms,
                "end_ms": piece_start_ms + piece_duration_ms,
                "duration_ms": piece_duration_ms,
                "audio_source_offset_ms": offset_ms,
                "source_start_ms": offset_ms,
                "source_end_ms": offset_ms + piece_duration_ms,
                "graphics": segment["graphics"] if piece_index == 1 else [],
                "citations": segment["citations"] if piece_index == 1 else [],
                "citation_overlay_asset_ids": (
                    segment["citation_overlay_asset_ids"] if piece_index == 1 else []
                ),
            }
            if is_screen_insert:
                piece.update(
                    {
                        "segment_type": "discussion_wall_screen_insert",
                        "camera_transition": "broll_insert",
                        "camera_view": "establishing_wide",
                        "camera_action": "broll_insert",
                        "visual_role": "wall_screen_broll",
                        "visual_layers": [
                            {
                                "role": "studio_scene",
                                "asset_id": str(studio_scene_asset.id),
                                "asset_type": studio_scene_asset.asset_type.value,
                                "purpose": "seated_panel_rear_screen_base",
                            },
                            {
                                "role": "wall_screen_broll",
                                "asset_id": str(wall_screen_asset.id),
                                "asset_type": wall_screen_asset.asset_type.value,
                                "purpose": "rear_studio_display_composited",
                            },
                        ],
                        "direction": {
                            **segment["direction"],
                            "view": "establishing_wide",
                            "action": "broll_insert",
                        },
                    }
                )
            else:
                piece["visual_layers"] = self._seated_panel_visual_layers(primary_video_asset)
                piece["wall_screen_visual_asset_id"] = None
                piece["secondary_visual_asset_id"] = None
                piece["camera_transition"] = (
                    segment["camera_transition"] if piece_index == 1 else "dissolve"
                )
            pieces.append(piece)
        return pieces

    def _character_reference_images(self, *assets: Asset | None) -> dict:
        references: dict = {}
        for asset in assets:
            for reference_type, reference in self._asset_reference_images(asset).items():
                references.setdefault(reference_type, reference)
        return references

    def _character_reference_image_uri(self, *assets: Asset | None) -> str | None:
        for asset in assets:
            reference = self._asset_reference_image_uri(asset)
            if reference:
                return reference
        return None

    def _asset_reference_images(self, asset: Asset | None) -> dict:
        if asset is None:
            return {}
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        if not isinstance(prompt_inputs, dict):
            return {}
        references = prompt_inputs.get("reference_images")
        return references if isinstance(references, dict) else {}

    def _asset_reference_image_uri(self, asset: Asset | None) -> str | None:
        if asset is None:
            return None
        prompt_inputs = asset.generation_metadata.get("prompt_inputs")
        if not isinstance(prompt_inputs, dict):
            return None
        reference = prompt_inputs.get("reference_image_uri")
        return reference if isinstance(reference, str) and reference else None

    def _store_timeline_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        timeline: dict,
        operation: str,
    ) -> Asset:
        payload = json.dumps(timeline, indent=2, sort_keys=True).encode("utf-8")
        stored = self.object_store.put_bytes(
            key=f"timelines/{episode.id}/{timeline['id']}.json",
            payload=payload,
            content_type="application/vnd.dialecticore.timeline+json",
        )
        asset = Asset(
            episode_id=episode.id,
            asset_type=AssetType.timeline,
            language=transcript.language,
            source_entity_type="transcript_version",
            source_entity_id=str(transcript.id),
            storage_uri=stored.uri,
            mime_type=stored.content_type,
            duration_ms=timeline.get("duration_ms"),
            checksum=stored.checksum,
            generation_metadata={
                "adapter": "timeline_builder",
                "operation": operation,
                "schema_version": timeline.get("schema_version"),
                "transcript_version_id": str(transcript.id),
                "timeline_id": timeline["id"],
                "edit_version": timeline.get("edit_version", 1),
                "segment_count": len(timeline.get("segments", [])),
                "chapter_count": len(timeline.get("chapters", [])),
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
                "timeline_json": timeline,
            },
            status="completed",
        )
        asset.generation_metadata["timeline_entity"] = self._timeline_entity_from_asset(
            asset,
            timeline,
        ).model_dump(mode="json")
        return asset

    def _segment_media_fingerprints(
        self,
        audio_asset: Asset | None,
        primary_video_asset: Asset | None,
        broll_asset: Asset | None,
        reaction_asset: Asset | None,
        studio_scene: Asset | None,
        studio_group_cutaway: Asset | None,
        fallback_asset: Asset | None,
        subtitle_asset: Asset | None,
        citation_card_assets: list[Asset],
    ) -> dict:
        citation_fingerprints = [
            fingerprint
            for fingerprint in (self._asset_fingerprint(asset) for asset in citation_card_assets)
            if fingerprint is not None
        ]
        return {
            role: fingerprint
            for role, fingerprint in (
                ("audio", self._asset_fingerprint(audio_asset)),
                ("video_primary", self._asset_fingerprint(primary_video_asset)),
                ("broll", self._asset_fingerprint(broll_asset)),
                ("reaction_loop", self._asset_fingerprint(reaction_asset)),
                ("studio_scene", self._asset_fingerprint(studio_scene)),
                ("studio_group_cutaway", self._asset_fingerprint(studio_group_cutaway)),
                ("fallback", self._asset_fingerprint(fallback_asset)),
                ("subtitle", self._asset_fingerprint(subtitle_asset)),
                ("citation_overlays", citation_fingerprints),
            )
            if fingerprint not in (None, [])
        }

    def _asset_fingerprint(self, asset: Asset | None) -> dict | None:
        if asset is None:
            return None
        return {
            "schema_version": "timeline_media_asset_fingerprint.v1",
            "asset_id": str(asset.id),
            "asset_type": asset.asset_type.value,
            "source_entity_type": asset.source_entity_type,
            "source_entity_id": asset.source_entity_id,
            "status": asset.status,
            "checksum": asset.checksum,
            "storage_uri": asset.storage_uri,
            "duration_ms": asset.duration_ms,
            "render_ready": asset.generation_metadata.get("render_ready"),
        }

    def _timeline_entity_from_asset(self, asset: Asset, timeline: dict) -> EpisodeTimeline:
        return EpisodeTimeline(
            id=UUID(str(timeline["id"])),
            episode_id=asset.episode_id,
            language=asset.language or str(timeline.get("language") or ""),
            version=int(timeline.get("edit_version") or timeline.get("version") or 1),
            status=asset.status,
            duration_ms=int(timeline.get("duration_ms") or asset.duration_ms or 0),
            timeline_json=timeline,
            created_at=self._coerce_datetime(timeline.get("created_at"), asset.created_at),
        )

    def _coerce_datetime(self, value: object, default: datetime) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return default
        return default

    def _timeline_qc(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        timeline: dict,
        timeline_asset: Asset,
    ) -> QualityResult:
        seated_panel = (
            timeline.get("media", {}).get("composition_policy") == "seated_studio_panel.v1"
        )
        render_contract = timeline.get("render_contract")
        high_quality_studio = (
            render_contract.get("high_quality_studio")
            if isinstance(render_contract, dict)
            else None
        )
        high_quality_studio_enabled = (
            isinstance(high_quality_studio, dict)
            and high_quality_studio.get("policy")
            == "high_quality_seated_performance.v1"
        )
        ordered_playable_turn_ids = [
            str(turn.id) for turn in transcript.turns if turn.status != "excluded"
        ]
        playable_turn_ids = set(ordered_playable_turn_ids)
        source_discussion_turn_ids_by_turn = {
            str(turn.id): [str(turn_id) for turn_id in turn.source_discussion_turn_ids]
            for turn in transcript.turns
            if turn.status != "excluded"
        }
        segments = timeline.get("segments", [])
        issues: list[dict] = []
        preview_config = timeline.get("program_structure", {}).get("preview")
        qualification_preview = (
            isinstance(preview_config, dict) and preview_config.get("included") is True
        )
        if qualification_preview:
            turn_range = preview_config.get("turn_range")
            segment_turn_ids = [
                str(segment.get("source_turn_id") or "")
                for segment in segments
                if isinstance(segment, dict)
            ]
            valid_range = (
                isinstance(turn_range, list)
                and len(turn_range) == 2
                and all(isinstance(value, int) for value in turn_range)
                and 1 <= turn_range[0] <= turn_range[1] <= len(ordered_playable_turn_ids)
            )
            expected_turn_ids = (
                ordered_playable_turn_ids[turn_range[0] - 1 : turn_range[1]] if valid_range else []
            )
            duration_ms = int(timeline.get("duration_ms") or 0)
            if (
                not valid_range
                or segment_turn_ids != expected_turn_ids
                or not 30_000 <= duration_ms <= 60_000
            ):
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "timeline_qualification_preview_slice_invalid",
                        "turn_range": turn_range,
                        "segment_turn_ids": segment_turn_ids,
                        "expected_turn_ids": expected_turn_ids,
                        "duration_ms": duration_ms,
                    }
                )
            else:
                playable_turn_ids = set(expected_turn_ids)
        opening_segment_count = sum(
            1
            for segment in segments
            if isinstance(segment, dict) and segment.get("segment_type") == "episode_opening"
        )
        primer_segment_count = sum(
            1
            for segment in segments
            if isinstance(segment, dict) and segment.get("segment_type") == "topic_primer"
        )
        if len(segments) != len(playable_turn_ids) and not (
            len(segments) > len(playable_turn_ids)
            and (opening_segment_count > 1 or primer_segment_count == 1)
        ):
            issues.append(
                {
                    "severity": "fail",
                    "issue": "timeline_turn_segment_count_mismatch",
                    "segment_count": len(segments),
                    "playable_turn_count": len(playable_turn_ids),
                }
            )
        seen_turn_ids = set()
        previous_end_ms = 0
        missing_audio_count = 0
        missing_video_count = 0
        missing_fallback_count = 0
        estimated_duration_count = 0
        subtitle_linked_count = 0
        citation_segment_count = 0
        citation_overlay_linked_count = 0
        timing_gap_count = 0
        missing_shot_planned_reaction_loop_count = 0
        missing_shot_planned_studio_scene_count = 0
        missing_shot_planned_studio_group_cutaway_count = 0
        missing_seated_panel_scene_count = 0
        missing_seated_lipsync_mode_count = 0
        seated_wall_screen_count = 0
        stale_media_link_count = 0
        source_discussion_linked_segment_count = 0
        missing_source_discussion_link_count = 0
        mismatched_source_discussion_link_count = 0
        asset_by_id = {
            str(asset.id): asset for asset in episode.assets if asset.status != "replaced"
        }
        for segment in segments:
            turn_id = segment.get("source_turn_id")
            if isinstance(turn_id, str):
                seen_turn_ids.add(turn_id)
            segment_id = segment.get("id")
            expected_source_discussion_turn_ids = (
                source_discussion_turn_ids_by_turn.get(turn_id)
                if isinstance(turn_id, str)
                else None
            )
            actual_source_discussion_turn_ids = segment.get("source_discussion_turn_ids")
            expected_source_discussion_turn_ids = expected_source_discussion_turn_ids or []
            if expected_source_discussion_turn_ids and (
                not isinstance(actual_source_discussion_turn_ids, list)
                or not all(
                    isinstance(item, str) and item for item in actual_source_discussion_turn_ids
                )
            ):
                missing_source_discussion_link_count += 1
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "timeline_segment_missing_source_discussion_turn_links",
                        "segment_id": segment_id,
                        "source_turn_id": turn_id,
                    }
                )
            elif expected_source_discussion_turn_ids and (
                actual_source_discussion_turn_ids != expected_source_discussion_turn_ids
            ):
                mismatched_source_discussion_link_count += 1
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "timeline_segment_source_discussion_turn_link_mismatch",
                        "segment_id": segment_id,
                        "source_turn_id": turn_id,
                        "expected_source_discussion_turn_ids": expected_source_discussion_turn_ids,
                        "actual_source_discussion_turn_ids": actual_source_discussion_turn_ids,
                    }
                )
            elif isinstance(actual_source_discussion_turn_ids, list) and all(
                isinstance(item, str) and item for item in actual_source_discussion_turn_ids
            ):
                source_discussion_linked_segment_count += 1
            if segment.get("audio_asset_id") is None:
                missing_audio_count += 1
                estimated_duration_count += 1
                issues.append(
                    {
                        "severity": "fail",
                        "issue": "timeline_segment_missing_audio",
                        "segment_id": segment_id,
                        "source_turn_id": turn_id,
                    }
                )
            if segment.get("video_asset_id") is None:
                missing_video_count += 1
                severity = "warning" if segment.get("fallback_video_asset_id") else "fail"
                issues.append(
                    {
                        "severity": severity,
                        "issue": "timeline_segment_missing_primary_video",
                        "segment_id": segment_id,
                        "source_turn_id": turn_id,
                        "fallback_video_asset_id": segment.get("fallback_video_asset_id"),
                    }
                )
            if segment.get("fallback_video_asset_id") is None:
                missing_fallback_count += 1
            if segment.get("subtitle_asset_id") is not None:
                subtitle_linked_count += 1
            if segment.get("citations"):
                citation_segment_count += 1
                overlay_ids = segment.get("citation_overlay_asset_ids")
                if isinstance(overlay_ids, list) and overlay_ids:
                    citation_overlay_linked_count += 1
            if segment.get("start_ms") != previous_end_ms:
                timing_gap_count += 1
                issues.append(
                    {
                        "severity": "warning",
                        "issue": "timeline_segment_timing_gap",
                        "segment_id": segment_id,
                        "expected_start_ms": previous_end_ms,
                        "actual_start_ms": segment.get("start_ms"),
                    }
                )
            media_fingerprints = segment.get("media_fingerprints")
            if isinstance(media_fingerprints, dict):
                stale_media_link_count += self._append_stale_media_fingerprint_issues(
                    issues,
                    segment_id=segment_id,
                    source_turn_id=turn_id,
                    media_fingerprints=media_fingerprints,
                    asset_by_id=asset_by_id,
                )
            primary_video_id = segment.get("video_asset_id")
            primary_asset = (
                asset_by_id.get(primary_video_id) if isinstance(primary_video_id, str) else None
            )
            shot_plan = self._shot_plan_for_asset(primary_asset)
            if seated_panel and segment.get("segment_type") != "topic_primer":
                if high_quality_studio_enabled:
                    performance_ready = (
                        primary_asset is not None
                        and primary_asset.status == "completed"
                        and primary_asset.generation_metadata.get("visual_role")
                        == "production_v2_speaking_character"
                    )
                    if not performance_ready:
                        missing_seated_panel_scene_count += 1
                        issues.append(
                            {
                                "severity": "fail",
                                "issue": "timeline_missing_high_quality_seated_performance",
                                "segment_id": segment_id,
                                "source_turn_id": turn_id,
                                "actual_asset_id": primary_video_id,
                            }
                        )
                else:
                    planned_scene_id = self._non_empty_string(
                        shot_plan.get("studio_panel_scene_asset_id")
                    )
                    actual_scene_id = segment.get("studio_panel_scene_asset_id")
                    planned_scene = (
                        asset_by_id.get(planned_scene_id) if planned_scene_id else None
                    )
                    scene_ready = (
                        planned_scene_id is not None
                        and actual_scene_id == planned_scene_id
                        and self._is_completed_render_ready_asset(
                            planned_scene, AssetType.studio_scene
                        )
                    )
                    if not scene_ready:
                        missing_seated_panel_scene_count += 1
                        issues.append(
                            {
                                "severity": "fail",
                                "issue": "timeline_missing_seated_panel_scene",
                                "segment_id": segment_id,
                                "source_turn_id": turn_id,
                                "expected_asset_id": planned_scene_id,
                                "actual_asset_id": actual_scene_id,
                            }
                        )
                direction = segment.get("direction")
                if (
                    not isinstance(direction, dict)
                    or direction.get("speaker_mouth_mode") != "audio_driven_seated_panel"
                ):
                    missing_seated_lipsync_mode_count += 1
                    issues.append(
                        {
                            "severity": "fail",
                            "issue": "timeline_missing_audio_driven_seated_panel_mode",
                            "segment_id": segment_id,
                            "source_turn_id": turn_id,
                        }
                    )
                if segment.get("wall_screen_visual_asset_id"):
                    seated_wall_screen_count += 1
            planned_reaction_id = self._non_empty_string(
                shot_plan.get("reusable_reaction_asset_id")
            )
            if planned_reaction_id is not None:
                actual_reaction_id = segment.get("reaction_visual_asset_id")
                planned_reaction = asset_by_id.get(planned_reaction_id)
                if (
                    actual_reaction_id != planned_reaction_id
                    or not self._is_completed_render_ready_asset(
                        planned_reaction,
                        AssetType.reaction_loop,
                    )
                ):
                    missing_shot_planned_reaction_loop_count += 1
                    issues.append(
                        {
                            "severity": "fail",
                            "issue": "timeline_missing_shot_planned_reaction_loop",
                            "segment_id": segment_id,
                            "source_turn_id": turn_id,
                            "speaker_id": segment.get("speaker_id"),
                            "expected_asset_id": planned_reaction_id,
                            "actual_asset_id": actual_reaction_id,
                        }
                    )
            planned_studio_id = self._non_empty_string(shot_plan.get("studio_scene_asset_id"))
            if planned_studio_id is not None:
                actual_studio_id = segment.get("studio_scene_asset_id")
                planned_studio = asset_by_id.get(planned_studio_id)
                if (
                    actual_studio_id != planned_studio_id
                    or not self._is_completed_render_ready_asset(
                        planned_studio,
                        AssetType.studio_scene,
                    )
                ):
                    missing_shot_planned_studio_scene_count += 1
                    issues.append(
                        {
                            "severity": "fail",
                            "issue": "timeline_missing_shot_planned_studio_scene",
                            "segment_id": segment_id,
                            "source_turn_id": turn_id,
                            "expected_asset_id": planned_studio_id,
                            "actual_asset_id": actual_studio_id,
                        }
                    )
            planned_group_id = self._non_empty_string(
                shot_plan.get("studio_group_cutaway_asset_id")
            )
            if planned_group_id is not None:
                actual_group_id = segment.get("studio_group_cutaway_asset_id")
                planned_group = asset_by_id.get(planned_group_id)
                if actual_group_id != planned_group_id or not self._is_completed_render_ready_asset(
                    planned_group,
                    AssetType.studio_scene,
                ):
                    missing_shot_planned_studio_group_cutaway_count += 1
                    issues.append(
                        {
                            "severity": "fail",
                            "issue": "timeline_missing_shot_planned_studio_group_cutaway",
                            "segment_id": segment_id,
                            "source_turn_id": turn_id,
                            "expected_asset_id": planned_group_id,
                            "actual_asset_id": actual_group_id,
                        }
                    )
            previous_end_ms = int(segment.get("end_ms") or previous_end_ms)
        missing_turn_ids = sorted(playable_turn_ids - seen_turn_ids)
        if missing_turn_ids:
            issues.append(
                {
                    "severity": "fail",
                    "issue": "timeline_missing_turn_segments",
                    "missing_turn_ids": missing_turn_ids,
                }
            )
        if subtitle_linked_count == 0:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "timeline_missing_subtitles",
                    "transcript_version_id": str(transcript.id),
                }
            )
        duration_ms = int(timeline.get("duration_ms") or 0)
        topic_primer_duration_ms = sum(
            int(segment.get("duration_ms") or 0)
            for segment in segments
            if isinstance(segment, dict) and segment.get("segment_type") == "topic_primer"
        )
        discussion_maximum_duration_ms = episode.maximum_duration_seconds * 1000
        maximum_duration_ms = discussion_maximum_duration_ms + topic_primer_duration_ms
        if duration_ms <= 0:
            issues.append({"severity": "fail", "issue": "timeline_duration_missing"})
        elif duration_ms > maximum_duration_ms:
            issues.append(
                {
                    "severity": "warning",
                    "issue": "timeline_duration_exceeds_episode_maximum",
                    "duration_ms": duration_ms,
                    "maximum_duration_ms": maximum_duration_ms,
                    "discussion_maximum_duration_ms": discussion_maximum_duration_ms,
                    "topic_primer_duration_ms": topic_primer_duration_ms,
                }
            )

        failure_count = sum(1 for issue in issues if issue["severity"] == "fail")
        warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
        if failure_count:
            severity = QualitySeverity.fail
        elif warning_count:
            severity = QualitySeverity.warning
        else:
            severity = QualitySeverity.pass_
        return QualityResult(
            episode_id=episode.id,
            target_type="timeline_asset",
            target_id=str(timeline_asset.id),
            check_type="timeline_integrity",
            severity=severity,
            status=severity.value,
            score=0.0 if failure_count else 1.0,
            details={
                "transcript_version_id": str(transcript.id),
                "language": transcript.language,
                "timeline_asset_id": str(timeline_asset.id),
                "timeline_id": timeline.get("id"),
                "segment_count": len(segments),
                "playable_turn_count": len(playable_turn_ids),
                "qualification_preview": qualification_preview,
                "opening_segment_count": opening_segment_count,
                "primer_segment_count": primer_segment_count,
                "topic_primer_duration_ms": topic_primer_duration_ms,
                "chapter_count": len(timeline.get("chapters", [])),
                "duration_ms": duration_ms,
                "maximum_duration_ms": maximum_duration_ms,
                "missing_audio_segment_count": missing_audio_count,
                "missing_primary_video_segment_count": missing_video_count,
                "missing_fallback_video_segment_count": missing_fallback_count,
                "estimated_duration_segment_count": estimated_duration_count,
                "subtitle_linked_segment_count": subtitle_linked_count,
                "citation_segment_count": citation_segment_count,
                "citation_overlay_linked_segment_count": citation_overlay_linked_count,
                "timing_gap_count": timing_gap_count,
                "source_discussion_linked_segment_count": (source_discussion_linked_segment_count),
                "missing_source_discussion_link_segment_count": (
                    missing_source_discussion_link_count
                ),
                "mismatched_source_discussion_link_segment_count": (
                    mismatched_source_discussion_link_count
                ),
                "missing_shot_planned_reaction_loop_segment_count": (
                    missing_shot_planned_reaction_loop_count
                ),
                "missing_shot_planned_studio_scene_segment_count": (
                    missing_shot_planned_studio_scene_count
                ),
                "missing_shot_planned_studio_group_cutaway_segment_count": (
                    missing_shot_planned_studio_group_cutaway_count
                ),
                "missing_seated_panel_scene_segment_count": missing_seated_panel_scene_count,
                "missing_audio_driven_seated_lipsync_segment_count": (
                    missing_seated_lipsync_mode_count
                ),
                "seated_wall_screen_segment_count": seated_wall_screen_count,
                "stale_media_link_count": stale_media_link_count,
                "issue_count": len(issues),
                "failure_count": failure_count,
                "warning_count": warning_count,
                "issues": issues,
            },
        )

    def _append_stale_media_fingerprint_issues(
        self,
        issues: list[dict],
        segment_id: object,
        source_turn_id: object,
        media_fingerprints: dict,
        asset_by_id: dict[str, Asset],
    ) -> int:
        stale_count = 0
        for role, fingerprint in media_fingerprints.items():
            if role == "citation_overlays" and isinstance(fingerprint, list):
                for item in fingerprint:
                    if isinstance(item, dict) and self._append_stale_media_fingerprint_issue(
                        issues,
                        segment_id,
                        source_turn_id,
                        "citation_overlay",
                        item,
                        asset_by_id,
                    ):
                        stale_count += 1
                continue
            if isinstance(fingerprint, dict) and self._append_stale_media_fingerprint_issue(
                issues,
                segment_id,
                source_turn_id,
                role,
                fingerprint,
                asset_by_id,
            ):
                stale_count += 1
        return stale_count

    def _append_stale_media_fingerprint_issue(
        self,
        issues: list[dict],
        segment_id: object,
        source_turn_id: object,
        role: str,
        fingerprint: dict,
        asset_by_id: dict[str, Asset],
    ) -> bool:
        asset_id = fingerprint.get("asset_id")
        current = asset_by_id.get(asset_id) if isinstance(asset_id, str) else None
        mismatch_reasons = self._media_fingerprint_mismatch_reasons(fingerprint, current)
        if not mismatch_reasons:
            return False
        issues.append(
            {
                "severity": "fail",
                "issue": "timeline_stale_media_fingerprint",
                "segment_id": segment_id,
                "source_turn_id": source_turn_id,
                "role": role,
                "asset_id": asset_id,
                "mismatch_reasons": mismatch_reasons,
            }
        )
        return True

    def _media_fingerprint_mismatch_reasons(
        self,
        fingerprint: dict,
        current: Asset | None,
    ) -> list[str]:
        if current is None:
            return ["asset_missing_or_replaced"]
        checks = {
            "asset_type": current.asset_type.value,
            "source_entity_type": current.source_entity_type,
            "source_entity_id": current.source_entity_id,
            "status": current.status,
            "checksum": current.checksum,
            "storage_uri": current.storage_uri,
            "duration_ms": current.duration_ms,
            "render_ready": current.generation_metadata.get("render_ready"),
        }
        return [f"{key}_changed" for key, value in checks.items() if fingerprint.get(key) != value]

    def _completed_audio_assets_by_turn(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> dict[str, Asset]:
        return {
            asset.source_entity_id: asset
            for asset in episode.assets
            if asset.asset_type == AssetType.audio
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
            and asset.status == "completed"
        }

    def _completed_opening_visual_assets(self, episode: Episode) -> list[Asset]:
        """Producer media is ordered by upload time and is only used for the opening."""
        return [
            asset
            for asset in episode.assets
            if asset.status == "completed"
            and asset.source_entity_type == "episode_opening"
            and asset.asset_type in {AssetType.image, AssetType.video, AssetType.broll}
            and asset.generation_metadata.get("opening_media") is True
            and asset.generation_metadata.get("render_ready") is not False
        ]

    def _latest_completed_primer_render(
        self,
        episode: Episode,
        language: str,
    ) -> Asset | None:
        state = episode.workflow_control.get("primer_production")
        current_render_id = state.get("render_asset_id") if isinstance(state, dict) else None
        if isinstance(current_render_id, str):
            current = next(
                (asset for asset in episode.assets if str(asset.id) == current_render_id),
                None,
            )
            if self._is_completed_primer_render(current, language):
                return current
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if self._is_completed_primer_render(asset, language)
            ),
            None,
        )

    @staticmethod
    def _is_completed_primer_render(asset: Asset | None, language: str) -> bool:
        return bool(
            asset is not None
            and asset.asset_type == AssetType.render
            and asset.status == "completed"
            and asset.language == language
            and asset.source_entity_type == "primer_timeline"
            and asset.generation_metadata.get("primer") is True
            and asset.generation_metadata.get("render_scope") == "primer"
            and asset.generation_metadata.get("render_ready") is not False
            and int(asset.duration_ms or 0) > 0
        )

    def _completed_visual_assets_by_turn(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
        visual_role: str,
    ) -> dict[str, Asset]:
        return {
            asset.source_entity_id: asset
            for asset in episode.assets
            if asset.asset_type in {AssetType.image, AssetType.video, AssetType.broll}
            and asset.language == transcript.language
            and asset.source_entity_type == "transcript_turn"
            and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
            and asset.generation_metadata.get("visual_role") == visual_role
            and asset.status == "completed"
            and asset.generation_metadata.get("render_ready") is not False
        }

    def _completed_citation_card_assets_by_turn(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> dict[str, list[Asset]]:
        cards_by_turn: dict[str, list[Asset]] = {}
        for asset in episode.assets:
            if (
                asset.asset_type == AssetType.citation_card
                and asset.language == transcript.language
                and asset.source_entity_type == "transcript_turn"
                and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
                and asset.generation_metadata.get("visual_role") == "citation_overlay"
                and asset.status == "completed"
                and asset.generation_metadata.get("render_ready") is not False
            ):
                cards_by_turn.setdefault(asset.source_entity_id, []).append(asset)
        return cards_by_turn

    def _completed_reaction_assets_by_participant(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> dict[str, Asset]:
        return {
            asset.source_entity_id: asset
            for asset in episode.assets
            if asset.asset_type == AssetType.reaction_loop
            and asset.language == transcript.language
            and asset.source_entity_type == "participant_profile"
            and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
            and asset.generation_metadata.get("visual_role") == "reaction_loop"
            and asset.status == "completed"
            and asset.generation_metadata.get("render_ready") is not False
        }

    def _completed_studio_scene_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.studio_scene
                and asset.language == transcript.language
                and asset.source_entity_type == "episode"
                and asset.source_entity_id == str(episode.id)
                and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
                and asset.generation_metadata.get("visual_role") == "studio_scene"
                and asset.status == "completed"
                and asset.generation_metadata.get("render_ready") is not False
            ),
            None,
        )

    def _completed_studio_group_cutaway_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in episode.assets
                if asset.asset_type == AssetType.studio_scene
                and asset.language == transcript.language
                and asset.source_entity_type == "episode"
                and asset.source_entity_id == f"{episode.id}:group"
                and asset.generation_metadata.get("transcript_version_id") == str(transcript.id)
                and asset.generation_metadata.get("visual_role") == "studio_group_cutaway"
                and asset.status == "completed"
                and asset.generation_metadata.get("render_ready") is not False
            ),
            None,
        )

    def _completed_subtitle_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.subtitle
                and asset.language == transcript.language
                and asset.source_entity_type == "transcript_version"
                and asset.source_entity_id == str(transcript.id)
                and asset.status == "completed"
            ),
            None,
        )

    def _latest_timeline_asset(
        self,
        episode: Episode,
        transcript: TranscriptVersion,
    ) -> Asset | None:
        return next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.timeline
                and asset.source_entity_type == "transcript_version"
                and asset.source_entity_id == str(transcript.id)
                and asset.status == "completed"
            ),
            None,
        )

    def _target_transcript(
        self,
        episode: Episode,
        request: TimelineBuildRequest,
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
            raise ValueError("transcript must be approved before timeline building")
        return transcript

    def _load_timeline_json(self, asset: Asset) -> dict:
        if asset.storage_uri is None:
            raise ValueError("timeline asset has no storage URI")
        path = self.object_store.path_for_uri(asset.storage_uri)
        if path is None or not path.exists():
            raise ValueError("timeline object not found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("timeline object is not a JSON object")
        return payload

    def _timeline_duration_ms(self, timeline: dict) -> int:
        return max(
            (
                int(segment.get("end_ms") or 0)
                for segment in timeline.get("segments", [])
                if isinstance(segment, dict)
            ),
            default=0,
        )

    def _estimate_duration_ms(self, text: str) -> int:
        word_count = max(1, len(text.split()))
        return max(1000, int((word_count / self.settings.words_per_second) * 1000))

    def _split_duration_ms(self, duration_ms: int, part_count: int) -> list[int]:
        safe_part_count = max(1, part_count)
        base_duration, remainder = divmod(max(safe_part_count, duration_ms), safe_part_count)
        return [base_duration + (1 if index < remainder else 0) for index in range(safe_part_count)]

    def _shot_plan_for_asset(self, asset: Asset | None) -> dict:
        if asset is None:
            return {}
        shot_plan = asset.generation_metadata.get("shot_plan")
        if isinstance(shot_plan, dict):
            return shot_plan
        return {}

    def _non_empty_string(self, value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    def _is_completed_render_ready_asset(
        self,
        asset: Asset | None,
        expected_type: AssetType,
    ) -> bool:
        return (
            asset is not None
            and asset.asset_type == expected_type
            and asset.status == "completed"
            and asset.generation_metadata.get("render_ready") is not False
        )

    def _chapter_key(self, turn_index: int) -> str:
        if turn_index == 1:
            return "Opening"
        chapter_number = ((turn_index - 2) // 4) + 1
        return f"Discussion {chapter_number}"

    def _graphics_for_turn(self, turn: TranscriptTurn, opening: bool = False) -> list[dict]:
        graphics = [
            {
                "type": "lower_third",
                "speaker_id": turn.speaker_participant_id,
                "source_turn_id": str(turn.id),
            }
        ]
        if opening:
            graphics.insert(
                0,
                {
                    "type": "episode_opening_title",
                    "speaker_id": turn.speaker_participant_id,
                    "source_turn_id": str(turn.id),
                },
            )
        return graphics

    def _citations_for_turn(self, turn: TranscriptTurn) -> list[dict]:
        citations = []
        for claim in turn.claims:
            for evidence_ref in claim.evidence_refs:
                citations.append(
                    {
                        "claim": claim.text,
                        "evidence_ref": evidence_ref,
                        "source_turn_id": str(turn.id),
                    }
                )
        return citations
